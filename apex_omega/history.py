"""Prior-session context (RTH open/high/low/close of recent full sessions).

SMA20 of RTH closes and the mean 14-session RTH range need only the last 20 sessions.
They are seeded from Yahoo 5-minute bars (the 15:55 5m close == the 15:59 1m close, and 5m
highs/lows are the max/min of the 1m ones) and thereafter appended every evening by the
`eod` command from the final 1-minute bars.
"""
from __future__ import annotations

import datetime as dt
from collections import defaultdict

import requests

from . import params as P
from .calendar_cme import is_full_session
from .timeutil import ET, UTC


def prev_full_session(date: dt.date) -> dt.date:
    d = date - dt.timedelta(days=1)
    while not is_full_session(d):
        d -= dt.timedelta(days=1)
    return d


def rows_from_bars(bars, min_bars, step_min):
    """bars: iterable of objects with ts,o,h,l,c.  Returns {date: row} for complete RTH sessions."""
    per = defaultdict(list)
    for b in bars:
        t = dt.datetime.fromtimestamp(b.ts, tz=UTC).astimezone(ET)
        hm = t.hour * 60 + t.minute
        if P.RTH_START_HM <= hm < P.RTH_END_HM and t.weekday() < 5:
            per[t.date()].append((hm, b))
    rows = {}
    for d, lst in per.items():
        if not is_full_session(d) or len(lst) < min_bars:
            continue
        lst.sort(key=lambda x: x[0])
        if lst[0][0] != P.RTH_START_HM or lst[-1][0] != P.RTH_END_HM - step_min:
            continue
        rows[d.isoformat()] = dict(date=d.isoformat(), open=lst[0][1].o, high=max(b.h for _, b in lst),
                                   low=min(b.l for _, b in lst), close=lst[-1][1].c)
    return rows


def seed_from_yahoo_5m(symbol="NQ=F", session=None):
    from .providers import Bar
    http = session or requests.Session()
    http.headers.update({"User-Agent": "Mozilla/5.0 (apex-omega-bot)"})
    r = http.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
                 params={"range": "60d", "interval": "5m", "includePrePost": "true"}, timeout=20)
    r.raise_for_status()
    res = r.json()["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(res["timestamp"]):
        o, h, l, c = (q[k][i] for k in ("open", "high", "low", "close"))
        if None in (o, h, l, c):
            continue
        bars.append(Bar(int(t) // 300 * 300, float(o), float(h), float(l), float(c), float(q["volume"][i] or 0)))
    return list(rows_from_bars(bars, min_bars=70, step_min=5).values())


def ensure_history(state, date: dt.date, seeder=None, strict: bool = True):
    """Return history rows strictly before `date`, seeding/refreshing if the latest full session is missing."""
    need = prev_full_session(date).isoformat()
    rows = [r for r in state.history() if r["date"] < date.isoformat()]
    if (not rows or rows[-1]["date"] < need) and seeder is not None:
        merged = {r["date"]: r for r in state.history()}
        for r in seeder():
            merged.setdefault(r["date"], r)
        state.save_history(list(merged.values()))
        rows = [r for r in state.history() if r["date"] < date.isoformat()]
    if not strict:
        if not rows:
            raise RuntimeError("no prior-session history")
        return rows
    if not rows or rows[-1]["date"] < need:
        raise RuntimeError(f"prior-session history is stale (have {rows[-1]['date'] if rows else 'none'}, need {need}); "
                           f"run `python -m apex_omega seed-history`")
    if len(rows) < 20:
        raise RuntimeError(f"only {len(rows)} prior sessions in history; need 20 for the SMA20 (run seed-history)")
    return rows
