"""Replay full sessions through :class:`SessionEngine` (used by verification and EOD reconcile)."""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import params as P
from .engine import SessionEngine
from .session import DailyContext

F32 = np.float32


def run_session(date, bars, daily: DailyContext, high_precision=True, on_event=None):
    """bars: 390 entries (None = missing minute).  Returns the finished engine."""
    eng = SessionEngine(date, daily, high_precision=high_precision)
    for m in range(P.M_BARS):
        b = bars[m]
        if b is None:
            if m == 0:
                raise ValueError(f"{date}: first RTH bar missing; the bot fails closed on such sessions")
            S = eng.S
            b = (S.O[m - 1], S.H[m - 1], S.L[m - 1], S.C[m - 1], F32(0.0))      # reference forward-fill
        evs = eng.on_open(m, b[0])
        evs += eng.on_close(m, *b)
        if on_event:
            for e in evs:
                on_event(e)
    return eng


class History:
    """Rolling store of prior valid sessions' RTH close / range (float32)."""
    def __init__(self, closes=None, ranges=None):
        self.closes = list(closes or []); self.ranges = list(ranges or [])

    def context(self, first_open=None) -> DailyContext:
        return DailyContext.from_history(self.closes, self.ranges, first_open=first_open)

    def push(self, close, rng):
        self.closes.append(F32(close)); self.ranges.append(F32(rng))
        self.closes = self.closes[-20:]; self.ranges = self.ranges[-14:]


def replay_sessions(sessions, high_precision=True, history: History | None = None, collect_events=False):
    """sessions: iterable of (date, bars).  Returns (ledger DataFrame, events list)."""
    hist = history or History()
    rows, evs = [], []
    for di, (date, bars) in enumerate(sessions):
        first_open = bars[0][0] if bars[0] is not None else None
        eng = run_session(date, bars, hist.context(first_open), high_precision,
                          on_event=(evs.append if collect_events else None) if collect_events else None)
        for t in eng.trades:
            r = dict(t); r["d"] = di; r["year"] = int(date[:4]); rows.append(r)
        close, rng = eng.S.day_close_and_range()
        hist.push(close, rng)
    cols = ["d", "date", "year", "entry_m", "exit_m", "dir", "risk_pts", "net_pts", "net_r", "pillar", "exit_reason", "emit", "entry_px", "exit_px"]
    return pd.DataFrame(rows, columns=cols), evs
