"""Shared helpers for the verification suite."""
from __future__ import annotations

import datetime as dt
import re
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__import__("os").path.abspath(__file__))))

from apex_omega import params as P
from apex_omega.alerts import Notifier
from apex_omega.providers import Bar, Snapshot
from apex_omega.runner import Config, Runner
from apex_omega.state import State
from apex_omega.timeutil import ET, UTC

KEY = ["date", "entry_m", "pillar"]
NUM = ["exit_m", "dir", "risk_pts", "net_pts", "net_r"]


def compare_ledgers(bot: pd.DataFrame, orc: pd.DataFrame) -> dict:
    b = bot.copy(); o = orc.copy()
    b["_k"] = list(zip(b.date, b.entry_m, b.pillar)); o["_k"] = list(zip(o.date, o.entry_m, o.pillar))
    assert b["_k"].is_unique and o["_k"].is_unique, "duplicate trade keys"
    only_b = sorted(set(b._k) - set(o._k)); only_o = sorted(set(o._k) - set(b._k))
    m = b.merge(o, on="_k", suffixes=("_b", "_o"))
    mism = {}
    for c in NUM:
        x = m[c + "_b"].to_numpy(dtype=np.float32); y = m[c + "_o"].to_numpy(dtype=np.float32)
        mism[c] = int((x != y).sum())
    return dict(n_bot=len(b), n_oracle=len(o), n_common=len(m), only_bot=only_b[:5], n_only_bot=len(only_b),
                only_oracle=only_o[:5], n_only_oracle=len(only_o), field_mismatches=mism,
                identical=(not only_b and not only_o and all(v == 0 for v in mism.values())))


def start0_ts(date_iso: str) -> int:
    d = dt.date.fromisoformat(date_iso)
    return int(dt.datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET).astimezone(UTC).timestamp())


class SimProvider:
    """Serves one session as a live feed would: complete bars + a forming bar whose only true field is
    its OPEN (high/low/close/volume are poisoned so any leakage is caught)."""
    name = "sim"

    def __init__(self, date_iso, bars):
        self.start0 = start0_ts(date_iso); self.bars = bars; self.cursor = self.start0 - 3600

    def fetch(self):
        k = int((self.cursor - self.start0) // 60)
        out = []
        for m in range(0, min(k, P.M_BARS)):
            b = self.bars[m]
            if b is not None:
                out.append(Bar(self.start0 + 60 * m, float(b[0]), float(b[1]), float(b[2]), float(b[3]), float(b[4])))
        if 0 <= k < P.M_BARS and self.bars[k] is not None:
            o = float(self.bars[k][0])
            out.append(Bar(self.start0 + 60 * k, o, o + 1000.0, o - 1000.0, o + 777.0, 999999.0))
        return Snapshot(out, float(self.cursor), float(self.cursor), "sim", last_bar_forming=True)


def history_rows(prior):
    """prior: list of (date, bars) of earlier valid sessions -> daily_history rows."""
    rows = []
    for date, bars in prior:
        full = []
        last = None
        for m in range(P.M_BARS):
            b = bars[m] if bars[m] is not None else last
            full.append(b); last = b
        rows.append(dict(date=date, open=float(full[0][0]), high=float(max(x[1] for x in full)),
                         low=float(min(x[2] for x in full)), close=float(full[-1][3])))
    return rows


_NUM = r"([-+]?[\d,]+\.\d+)"


def parse_payload(payload):
    """Pull the numbers a trader would read out of a Discord embed."""
    e = payload["embeds"][0]; title = e["title"]; f = {x["name"]: x["value"] for x in e.get("fields", [])}
    num = lambda s: float(re.search(_NUM, s).group(1).replace(",", ""))          # noqa: E731
    d = dict(title=title)
    if "Entry (market, at bar open)" in f:
        d["kind"] = "ENTRY"
        d["entry"] = num(f["Entry (market, at bar open)"]); d["stop"] = num(f["Stop"])
        tk = [k for k in f if k.startswith("Target")][0]
        d["target"] = num(f[tk]); d["long"] = "LONG" in title
    elif title.split()[1:3] and "MOVE" in title:
        d["kind"] = "MOVE_STOP"; d["new_stop"] = num(e["description"])
    elif "Exit price (model)" in f:
        d["kind"] = "EXIT"; d["exit"] = num(f["Exit price (model)"])
        m = re.search(r"([-+]\d+\.\d+) pts \| ([-+]\d+\.\d+)R", f["Result (after 1.0 pt friction)"])
        d["pts"], d["r"] = float(m.group(1)), float(m.group(2))
    else:
        d["kind"] = "INFO"
    return d


def simulate_live_day(date, bars, prior, offsets=(2, 33), hp=True, restart_every=None):
    """Drive the real Runner through one session with a simulated clock. Returns (runner, sent list with times)."""
    tmp = tempfile.mkdtemp(prefix="live_")
    state = State(tmp)
    state.save_history(history_rows(prior))
    prov = SimProvider(date, bars)
    notifier = Notifier(None, dry=True, out=open("/dev/null", "w"))
    cfg = Config(high_precision=hp, strict_history=False)
    runner = Runner(prov, notifier, state, cfg, clock=lambda: prov.cursor)
    log = []
    start0 = prov.start0
    steps = 0
    for k in range(-1, P.M_BARS + 2):
        for off in offsets:
            prov.cursor = start0 + 60 * k + off
            before = len(notifier.sent_payloads)
            if restart_every and steps % restart_every == 0 and steps:
                runner = Runner(prov, notifier, state, cfg, clock=lambda: prov.cursor)   # simulated crash/restart
            res = runner.step(prov.cursor)
            steps += 1
            for p in notifier.sent_payloads[before:]:
                log.append((prov.cursor - start0, p))
    return runner, notifier, log
