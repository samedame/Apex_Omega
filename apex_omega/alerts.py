"""Discord message formatting + webhook delivery (alerts only -- no orders are ever placed)."""
from __future__ import annotations

import json
import sys
import time

import requests

from . import __version__
from . import params as P
from .timeutil import bar_start_et

GREEN, RED, BLUE, GOLD, GREY = 0x2ECC71, 0xE74C3C, 0x3498DB, 0xF1C40F, 0x95A5A6
PILLAR_SHORT = {k: f"{k.split('_')[0]} {v['label']}" for k, v in P.PILLARS.items()}


class AlertError(RuntimeError):
    pass


def _px(x):
    return f"{x:,.2f}"


def _clock(date, m_close_index):
    """Wall clock (ET) at which bar `m` has just closed (= start of bar m+1)."""
    return bar_start_et(date, m_close_index + 1).strftime("%H:%M")


def _footer(feed):
    txt = f"APEX-OMEGA v{__version__} | alerts only, no orders | feed: {feed.get('source', '?')}"
    lag = feed.get("lag_s")
    if lag is not None:
        txt += f" | lag {lag/60:.1f} min"
    if feed.get("delayed"):
        txt += " | DELAYED FEED: paper/forward-test only"
    return txt[:2000]


def _banner(feed):
    if feed.get("delayed"):
        return f"**DELAYED FEED ({feed.get('lag_s', 0)/60:.0f} min old): paper tracking only, do not trade this.**\n"
    return ""


def entry_message(date, ev, feed, size=None, open_others=0):
    d = ev.data
    side = "LONG" if ev.direction == 1 else "SHORT"
    sd = d["stop_dist"]
    tgt_pts = sd * d["target_r"]
    lines = []
    if d.get("be_trig_px") is not None:
        lines.append(f"Price reaches **{_px(d['be_trig_px'])}** (+{d['be_trig_r']}R)  ->  move stop to **{_px(d['be_lock_px'])}** (+{d['be_lock_r']}R)")
    else:
        lines.append("No breakeven ratchet on this pillar")
    fields = [
        dict(name="Entry (market, at bar open)", value=f"**{_px(d['entry_px'])}**", inline=True),
        dict(name="Stop", value=f"**{_px(d['sl'])}**  ({sd:.2f} pts = 1R)", inline=True),
        dict(name=f"Target (+{d['target_r']}R)", value=f"**{_px(d['tp'])}**  (+{tgt_pts:.1f} pts)", inline=True),
        dict(name="Breakeven ratchet", value="\n".join(lines), inline=False),
        dict(name="Time exit", value=f"Flat at **{_clock(date, d['end_m'])} ET** (bar close) if still open", inline=True),
        dict(name="1R in dollars", value=f"${sd * P.POINT_VALUE_MNQ:,.0f} per MNQ | ${sd * P.POINT_VALUE_NQ:,.0f} per NQ", inline=True),
    ]
    if size is not None:
        fields.append(dict(name="Suggested size (two-gear)", value=(f"**{size} MNQ**" if size > 0 else "**SKIP** (Gear-1 shield: stop too wide for the account)"), inline=True))
    if open_others:
        fields.append(dict(name="Other open pillar positions", value=str(open_others), inline=True))
    if d.get("gap"):
        fields.append(dict(name="DATA GAP", value="No print for this minute: the model entry price is a stale forward-fill. Treat entry/stop/target as approximate and use the live market.", inline=False))
    fields.append(dict(name="If your fill differs", value="Keep the same stop/target distances from your fill (the backtest measures everything from the entry price).", inline=False))
    emb = dict(title=f"{'🟢' if ev.direction == 1 else '🔴'} {side} NQ/MNQ | {PILLAR_SHORT[ev.pillar]}",
               description=f"{_banner(feed)}Entry bar opened {bar_start_et(date, d['entry_m']).strftime('%H:%M')} ET. Setup: {d.get('why', '')}",
               color=GREEN if ev.direction == 1 else RED, fields=fields, footer=dict(text=_footer(feed)))
    return dict(content=None, embeds=[emb])


def move_stop_message(date, ev, feed):
    d = ev.data
    emb = dict(title=f"🔒 MOVE STOP | {PILLAR_SHORT[ev.pillar]} ({'LONG' if ev.direction == 1 else 'SHORT'})",
               description=f"{_banner(feed)}Move the stop to **{_px(d['new_sl'])}** (locks +{d['lock_r']}R). Entry was {_px(d['entry_px'])}.",
               color=GOLD, footer=dict(text=_footer(feed)))
    return dict(content=None, embeds=[emb])


_REASON = {"STOP": ("🛑", "STOP HIT", RED), "BE_STOP": ("🔒", "BREAKEVEN-LOCK STOP HIT", GREY),
           "TARGET": ("🎯", "TARGET HIT", GREEN), "TIME": ("⏱️", "TIME EXIT (flat at the bar close)", BLUE)}


def exit_message(date, ev, feed, contracts=None, equity=None):
    d = ev.data
    icon, label, color = _REASON[d["reason"]]
    if d["reason"] in ("TIME", "BE_STOP") and d["net_pts"] > 0:
        color = GREEN
    fields = [
        dict(name="Exit price (model)", value=f"**{_px(d['exit_px'])}**", inline=True),
        dict(name="Result (after 1.0 pt friction)", value=f"**{d['net_pts']:+.2f} pts | {d['net_r']:+.2f}R**", inline=True),
        dict(name="Per 1 MNQ", value=f"${d['net_pts'] * P.POINT_VALUE_MNQ:+,.2f}", inline=True),
    ]
    if contracts:
        fields.append(dict(name=f"Paper account ({contracts} MNQ)", value=f"${d['net_pts'] * P.POINT_VALUE_MNQ * contracts:+,.2f}" + (f" -> equity ${equity:,.2f}" if equity is not None else ""), inline=False))
    emb = dict(title=f"{icon} {label} | {PILLAR_SHORT[ev.pillar]} ({'LONG' if ev.direction == 1 else 'SHORT'})",
               description=f"{_banner(feed)}Entered {_px(d['entry_px'])} at {bar_start_et(date, d['entry_m']).strftime('%H:%M')} ET. Close the position now if it is still open.",
               color=color, fields=fields, footer=dict(text=_footer(feed)))
    return dict(content=None, embeds=[emb])


def info_message(title, text, feed, color=BLUE, fields=None):
    emb = dict(title=title[:256], description=text[:4000], color=color, footer=dict(text=_footer(feed)))
    if fields:
        emb["fields"] = fields
    return dict(content=None, embeds=[emb])


class Notifier:
    """Posts payloads to a Discord webhook; with no webhook it just prints them (dry run)."""

    def __init__(self, webhook_url=None, dry=False, out=sys.stdout, session=None):
        self.url, self.dry, self.out = webhook_url, dry or not webhook_url, out
        self.http = session or requests.Session()
        self.sent_payloads = []

    def send(self, payload):
        self.sent_payloads.append(payload)
        if self.dry:
            title = (payload.get("embeds") or [{}])[0].get("title", payload.get("content"))
            print(f"[DRY-RUN discord] {title}", file=self.out)
            return True
        for attempt in range(5):
            try:
                r = self.http.post(self.url, params={"wait": "true"}, json=payload, timeout=15)
            except requests.RequestException as e:
                time.sleep(2 ** attempt)
                err = str(e)
                continue
            if r.status_code == 429:
                try:
                    wait = float(r.json().get("retry_after", 1))
                except Exception:                                         # noqa: BLE001
                    wait = 1.0
                time.sleep(min(wait, 30))
                err = "rate limited"
                continue
            if r.status_code < 300:
                return True
            err = f"HTTP {r.status_code}"
            if 400 <= r.status_code < 500:
                break
            time.sleep(2 ** attempt)
        raise AlertError(f"discord delivery failed ({err})")
