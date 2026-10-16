import datetime as dt

import numpy as np
import pytest

from apex_omega.history import ensure_history, prev_full_session, rows_from_bars
from apex_omega.providers import Bar
from apex_omega.state import State
from apex_omega.timeutil import ET, UTC


def _five_min_day(date, base=100.0):
    out = []
    t0 = dt.datetime(date.year, date.month, date.day, 9, 30, tzinfo=ET)
    for i in range(78):
        ts = int((t0 + dt.timedelta(minutes=5 * i)).astimezone(UTC).timestamp())
        out.append(Bar(ts, base + i, base + i + 2, base + i - 2, base + i + 1, 10))
    return out


def test_rows_from_5m_bars_gives_rth_ohlc():
    d = dt.date(2024, 3, 5)
    rows = rows_from_bars(_five_min_day(d), min_bars=70, step_min=5)
    r = rows["2024-03-05"]
    assert (r["open"], r["high"], r["low"], r["close"]) == (100.0, 100 + 77 + 2, 98.0, 100 + 77 + 1)


def test_half_day_and_holiday_rejected():
    assert rows_from_bars(_five_min_day(dt.date(2024, 7, 3)), 70, 5) == {}     # early close day
    short = _five_min_day(dt.date(2024, 3, 5))[:60]
    assert rows_from_bars(short, 70, 5) == {}


def test_prev_full_session_skips_weekend_and_holiday():
    assert prev_full_session(dt.date(2024, 7, 5)) == dt.date(2024, 7, 2)
    assert prev_full_session(dt.date(2026, 10, 19)) == dt.date(2026, 10, 16)


def test_stale_history_fails_closed(tmp_path):
    s = State(str(tmp_path))
    s.save_history([dict(date=f"2024-02-{i:02d}", open=1, high=2, low=0, close=1) for i in range(1, 22)])
    with pytest.raises(RuntimeError):
        ensure_history(s, dt.date(2024, 3, 20), seeder=None)
