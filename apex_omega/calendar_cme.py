"""Which weekdays are *full* CME equity-futures sessions (09:30-16:00 ET RTH, no early close).

The backtest only contains sessions with >= 350 RTH bars, so holidays and 13:00 early
closes are never traded.  Rules are NYSE-style; they were checked against the
bar counts of every weekday in the verification dataset (see verification report).
"""
from __future__ import annotations

import datetime as dt
import json
import os

AD_HOC_CLOSURES = {"2025-01-09"}          # National Day of Mourning (Carter): no full session


def _nth_weekday(year, month, weekday, n):
    d = dt.date(year, month, 1)
    d += dt.timedelta(days=(weekday - d.weekday()) % 7)
    return d + dt.timedelta(weeks=n - 1)


def _last_weekday(year, month, weekday):
    d = dt.date(year + (month == 12), (month % 12) + 1, 1) - dt.timedelta(days=1)
    return d - dt.timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year):
    a = year % 19; b, c = divmod(year, 100); d, e = divmod(b, 4)
    f = (b + 8) // 25; g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30; i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7; m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return dt.date(year, month, day + 1)


def _observed(d):
    """Saturday holiday -> Friday, Sunday -> Monday (New Year's Day on Saturday is NOT observed)."""
    if d.weekday() == 5:
        return d - dt.timedelta(days=1)
    if d.weekday() == 6:
        return d + dt.timedelta(days=1)
    return d


def holidays(year):
    h = set()
    ny = dt.date(year, 1, 1)
    if ny.weekday() != 5:
        h.add(_observed(ny))
    h.add(_nth_weekday(year, 1, 0, 3))                       # MLK
    h.add(_nth_weekday(year, 2, 0, 3))                       # Presidents
    h.add(_easter(year) - dt.timedelta(days=2))              # Good Friday
    h.add(_last_weekday(year, 5, 0))                         # Memorial
    if year >= 2022:
        h.add(_observed(dt.date(year, 6, 19)))               # Juneteenth
    h.add(_observed(dt.date(year, 7, 4)))
    h.add(_nth_weekday(year, 9, 0, 1))                       # Labor
    h.add(_nth_weekday(year, 11, 3, 4))                      # Thanksgiving
    h.add(_observed(dt.date(year, 12, 25)))
    return h


def early_closes(year):
    e = set()
    e.add(_nth_weekday(year, 11, 3, 4) + dt.timedelta(days=1))     # day after Thanksgiving
    d = dt.date(year, 12, 24)
    if d.weekday() < 5:
        e.add(d)
    j3 = dt.date(year, 7, 3)
    if j3.weekday() < 5 and dt.date(year, 7, 4).weekday() in (1, 2, 3, 4):
        e.add(j3)
    return e


def _overrides():
    p = os.environ.get("APEX_CALENDAR_OVERRIDES", "state/calendar_overrides.json")
    try:
        with open(p) as f:
            return set(json.load(f).get("closed", []))
    except Exception:
        return set()


def is_full_session(d) -> bool:
    if isinstance(d, str):
        d = dt.date.fromisoformat(d)
    if d.weekday() >= 5:
        return False
    iso = d.isoformat()
    if iso in AD_HOC_CLOSURES or iso in _overrides():
        return False
    return d not in holidays(d.year) and d not in early_closes(d.year)
