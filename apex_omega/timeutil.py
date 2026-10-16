from __future__ import annotations
import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc


def now_et() -> dt.datetime:
    return dt.datetime.now(tz=UTC).astimezone(ET)


def et_from_epoch(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, tz=UTC).astimezone(ET)


def m_index(ts_et: dt.datetime) -> int:
    """Minutes since 09:30 ET for a timestamp (may be <0 or >=390 outside RTH)."""
    return ts_et.hour * 60 + ts_et.minute - 570


def bar_start_et(date: dt.date, m: int) -> dt.datetime:
    base = dt.datetime(date.year, date.month, date.day, 9, 30, tzinfo=ET)
    return (base.astimezone(UTC) + dt.timedelta(minutes=m)).astimezone(ET)


def fmt_hm(ts_et: dt.datetime) -> str:
    return ts_et.strftime("%H:%M:%S ET")
