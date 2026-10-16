"""Live loop.  Every poll rebuilds the session from the bars seen so far with the SAME
engine used for the replay, then sends whatever events are new.  Because the engine is a
pure function of (prior-session context, bars so far), a restart or a late poll cannot make
the live bot diverge from the backtest logic.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import time
from typing import Optional

import numpy as np

from . import alerts as A
from . import params as P
from .calendar_cme import is_full_session
from .engine import SessionEngine
from .history import ensure_history
from .replay import History
from .session import DailyContext
from .sizing import two_gear_contracts
from .state import State
from .timeutil import ET, UTC, bar_start_et, et_from_epoch, now_et

F32 = np.float32


@dataclasses.dataclass
class Config:
    high_precision: bool = True
    account_equity: Optional[float] = None       # enables suggested sizing + a paper account
    delayed_threshold_s: float = 150.0           # feed older than this = DELAYED
    gap_fill_after_s: float = 150.0              # fill a missing minute after this long (live feeds only)
    catchup_age_s: float = 600.0                 # older unsent events are folded into one digest
    strict_history: bool = True                  # require the previous full session (+20 sessions) in history


@dataclasses.dataclass
class StepResult:
    date: str
    sent: list
    lag_s: Optional[float]
    delayed: bool
    note: str = ""
    engine: Optional[SessionEngine] = None


def history_from_rows(rows) -> History:
    h = History()
    for r in rows:
        h.push(F32(r["close"]), F32(r["high"]) - F32(r["low"]))
    return h


def session_bars_from_snapshot(snap, date: dt.date):
    """minute index -> Bar for RTH of `date`."""
    start0 = int(dt.datetime(date.year, date.month, date.day, 9, 30, tzinfo=ET).astimezone(UTC).timestamp())
    out = {}
    for b in snap.bars:
        m = (b.ts - start0) // 60
        if 0 <= m < P.M_BARS:
            out[int(m)] = b
    return out, start0


def build_engine(date_iso, daily: DailyContext, bars: dict, complete_through: int, forming: Optional[int],
                 high_precision=True, fill_to: int = -1):
    """Feed bars 0..complete_through (gap-filling holes like the reference tensor builder), then
    open the forming bar (so a pending signal is filled at its open)."""
    eng = SessionEngine(date_iso, daily, high_precision=high_precision)
    eng.gap_bars = set()
    last_needed = max(complete_through, fill_to)
    for m in range(0, last_needed + 1):
        b = bars.get(m)
        if b is None:
            if m == 0:
                raise RuntimeError("09:30 bar missing: standing down for the session (fail closed)")
            S = eng.S
            vals = (S.O[m - 1], S.H[m - 1], S.L[m - 1], S.C[m - 1], F32(0.0))
            eng.gap_bars.add(m)
        else:
            vals = (F32(b.o), F32(b.h), F32(b.l), F32(b.c), F32(b.v))
        for ev in eng.on_open(m, vals[0]):
            if m in eng.gap_bars:
                ev.data["gap"] = True
        eng.on_close(m, *vals)
    if forming is not None and forming == last_needed + 1 and forming in bars:
        eng.on_open(forming, F32(bars[forming].o))
    return eng


def event_ts(date, ev):
    st = bar_start_et(date, ev.m)
    return st.timestamp() + (0 if ev.kind == "ENTRY" else 60)


class Runner:
    def __init__(self, provider, notifier, state: State, cfg: Config = None, clock=time.time, seeder=None):
        self.provider, self.notifier, self.state, self.seeder = provider, notifier, state, seeder
        self.cfg = cfg or Config()
        self.clock = clock

    # ------------------------------------------------------------------ helpers
    def _feed(self, snap, now_ts):
        lag = max(0.0, now_ts - snap.last_tick_ts) if snap.last_tick_ts else None
        return dict(source=snap.source, lag_s=lag, delayed=(lag is not None and lag > self.cfg.delayed_threshold_s))

    def _send(self, date, key, payload):
        self.notifier.send(payload)
        self.state.mark_sent(date, key)

    def _paper(self):
        if self.cfg.account_equity is None:
            return None
        p = self.state.paper()
        if p is None:
            p = dict(equity=float(self.cfg.account_equity), peak=float(self.cfg.account_equity), open={})
        return p

    # --------------------------------------------------------------------- step
    def step(self, now_ts: Optional[float] = None) -> StepResult:
        now_ts = self.clock() if now_ts is None else now_ts
        now = dt.datetime.fromtimestamp(now_ts, tz=UTC).astimezone(ET)
        date = now.date(); iso = date.isoformat()
        if not is_full_session(date):
            return StepResult(iso, [], None, False, "not a full session")
        snap = self.provider.fetch()
        feed = self._feed(snap, now_ts)
        bars, start0 = session_bars_from_snapshot(snap, date)
        hist = history_from_rows(ensure_history(self.state, date, self.seeder, self.cfg.strict_history))
        first_open = bars[min(bars)].o if bars else None
        daily = hist.context(first_open=first_open)
        sent_keys = self.state.sent(iso)
        sent_now = []
        if not bars:
            if "INFO:armed" not in sent_keys:
                self._send(iso, "INFO:armed", self._armed_msg(daily, feed))
                sent_now.append("INFO:armed")
            return StepResult(iso, sent_now, feed["lag_s"], feed["delayed"], "waiting for 09:30 bar")

        m_last = max(bars)
        start_last = start0 + m_last * 60
        live_ok = (snap.last_tick_ts - now_ts) > -self.cfg.delayed_threshold_s
        forming_last = snap.last_bar_forming and snap.last_tick_ts < start_last + 60 and \
            not (live_ok and now_ts >= start_last + 63)
        if forming_last:
            complete_through, forming = m_last - 1, m_last
        else:
            complete_through, forming = m_last, None
        fill_to = -1
        if live_ok and not forming_last:                              # trailing gap on a live feed
            lim = int((now_ts - start0 - 60 - self.cfg.gap_fill_after_s) // 60)
            fill_to = min(lim, P.M_BARS - 1)
        if complete_through < 0 and forming is None:
            return StepResult(iso, sent_now, feed["lag_s"], feed["delayed"], "no complete bars yet")
        eng = build_engine(iso, daily, bars, complete_through, forming, self.cfg.high_precision, fill_to)

        if "INFO:armed" not in sent_keys:
            self._send(iso, "INFO:armed", self._armed_msg(daily, feed)); sent_now.append("INFO:armed")
        if eng.S.n > 10 and "INFO:regime" not in sent_keys:
            self._send(iso, "INFO:regime", self._regime_msg(eng, daily, feed)); sent_now.append("INFO:regime")

        ref_ts = snap.last_tick_ts if snap.last_tick_ts else now_ts
        todo = [e for e in eng.events if e.emit and f"{iso}|{e.key}" not in sent_keys]
        stale = [e for e in todo if ref_ts - event_ts(date, e) > self.cfg.catchup_age_s]
        fresh = [e for e in todo if e not in stale]
        if stale:
            self._send_digest(iso, date, eng, stale, feed, sent_now)
        paper = self._paper()
        for e in fresh:
            key = f"{iso}|{e.key}"
            if e.kind == "ENTRY":
                size = None
                if paper is not None:
                    size = two_gear_contracts(paper["equity"], paper["peak"], e.data["stop_dist"], e.pillar)
                payload = A.entry_message(date, e, feed, size=size, open_others=self._open_others(eng, e))
                self._send(iso, key, payload)
                if paper is not None:
                    paper["open"][e.key] = size or 0
                    self.state.save_paper(paper)
            elif e.kind == "MOVE_STOP":
                self._send(iso, key, A.move_stop_message(date, e, feed))
            else:
                contracts = None
                if paper is not None:
                    ek = f"ENTRY|{e.pillar}|{e.data['entry_m']}|{e.data['entry_m']}"
                    contracts = paper["open"].pop(ek, 0)
                    paper["equity"] += contracts * e.data["net_pts"] * P.POINT_VALUE_MNQ
                    paper["peak"] = max(paper["peak"], paper["equity"])
                    self.state.save_paper(paper)
                self._send(iso, key, A.exit_message(date, e, feed, contracts=contracts,
                                                    equity=(paper["equity"] if paper else None)))
            sent_now.append(key)
        return StepResult(iso, sent_now, feed["lag_s"], feed["delayed"], "ok", engine=eng)

    # ---------------------------------------------------------------- messages
    def _armed_msg(self, daily, feed):
        txt = (f"Watching NQ/MNQ 1-minute bars for today's session.\n"
               f"Prior-session context: SMA20 of RTH closes **{float(daily.sma20):,.2f}**, mean 14-day RTH range **{float(daily.atr14):,.1f} pts**.\n"
               f"High-Precision Core: {'ON' if self.cfg.high_precision else 'OFF'} (P2 stops <= {P.HP_P2_MAX_RISK:.0f} pts, all stops <= {P.HP_MAX_RISK:.0f} pts).")
        if feed.get("delayed"):
            txt += "\n**Feed is delayed: alerts are for paper/forward testing, not for acting on.**"
        return A.info_message("APEX-OMEGA armed", txt, feed, color=A.BLUE)

    def _regime_msg(self, eng, daily, feed):
        S = eng.S
        move = float(S.C[10] - S.O[0])
        if eng.has_drive10:
            txt = f"09:40 opening drive **{'UP' if eng.drive10_dir == 1 else 'DOWN'}** {abs(move):.1f} pts (>= 1.8 x ATR): Pillar 1 pullback entries are armed."
        else:
            txt = f"09:40 move {move:+.1f} pts is below 1.8 x ATR: Pillar 1 is off today."
        rel = "above" if S.O[0] > daily.sma20 else "below"
        txt += f"\nRTH open {float(S.O[0]):,.2f} is {rel} the 20-day SMA ({float(daily.sma20):,.2f}): " + \
               ("longs favoured for P4/P5-long" if rel == "above" else "shorts favoured for P3/P4 shorts") + "."
        return A.info_message("Opening-drive regime", txt, feed, color=A.BLUE)

    def _open_others(self, eng, ev):
        return sum(1 for p in eng.positions if not p.closed and p.emit and p.entry_m == ev.m and p.pillar != ev.pillar) + \
            sum(1 for p in eng.positions if not p.closed and p.emit and p.entry_m < ev.m)

    def _send_digest(self, iso, date, eng, stale, feed, sent_now):
        lines = []
        for e in stale:
            t = bar_start_et(date, e.m).strftime("%H:%M")
            if e.kind == "ENTRY":
                lines.append(f"{t} {'LONG' if e.direction == 1 else 'SHORT'} {A.PILLAR_SHORT[e.pillar]} entry {e.data['entry_px']:,.2f} stop {e.data['sl']:,.2f} target {e.data['tp']:,.2f}")
            elif e.kind == "MOVE_STOP":
                lines.append(f"{t} {A.PILLAR_SHORT[e.pillar]} stop moved to {e.data['new_sl']:,.2f}")
            else:
                lines.append(f"{t} {A.PILLAR_SHORT[e.pillar]} exit {e.data['reason']} {e.data['net_r']:+.2f}R")
        open_now = [p for p in eng.positions if not p.closed and p.emit]
        tail = "\n**Still open:** " + "; ".join(f"{A.PILLAR_SHORT[p.pillar]} stop {float(p.sl):,.2f} target {float(p.tp):,.2f}" for p in open_now) if open_now else ""
        self._send_many(iso, [f"{iso}|{e.key}" for e in stale],
                        A.info_message("Catch-up digest (events missed while the bot was offline/delayed)", "\n".join(lines) + tail, feed, color=A.GOLD))
        sent_now.extend(f"{iso}|{e.key}" for e in stale)

    def _send_many(self, iso, keys, payload):
        self.notifier.send(payload)
        for k in keys:
            self.state.mark_sent(iso, k)

    # -------------------------------------------------------------------- loop
    def run(self, poll_s=5.0, max_minutes=None, log=print):
        t_start = self.clock()
        while True:
            now = now_et()
            if now.hour * 60 + now.minute >= 16 * 60 + 2:
                log("session over"); return 0
            if now.hour * 60 + now.minute < 9 * 60 + 15:
                time.sleep(min(30, poll_s * 6)); continue
            try:
                res = self.step()
                if res.sent:
                    log(f"{now:%H:%M:%S} sent {len(res.sent)} ({res.note}); lag={res.lag_s}")
            except A.AlertError as e:
                log(f"ALERT DELIVERY FAILED: {e}")
            except Exception as e:                                           # noqa: BLE001
                log(f"STEP ERROR: {type(e).__name__}: {e}")
            if max_minutes is not None and (self.clock() - t_start) / 60 >= max_minutes:
                log("time budget reached: hand-off required"); return 75
            time.sleep(poll_s)
