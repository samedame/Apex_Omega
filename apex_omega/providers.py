"""1-minute bar providers.

A provider returns a :class:`Snapshot`: all known 1-minute bars of the current
sessions (minute-aligned, UTC epoch seconds of the bar START), whether the last bar
is still forming, and the feed's own notion of "latest tick time" so the bot can
measure feed lag.
"""
from __future__ import annotations

import csv
import dataclasses
import time
from typing import Optional

import requests


@dataclasses.dataclass
class Bar:
    ts: int            # epoch seconds, minute aligned, bar START
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclasses.dataclass
class Snapshot:
    bars: list                  # list[Bar], ascending, unique ts
    last_tick_ts: float         # feed time of newest datum (epoch s)
    fetched_at: float
    source: str
    last_bar_forming: bool = True


class ProviderError(RuntimeError):
    pass


class YahooProvider:
    """Yahoo Finance chart API (free).  CME futures there are normally DELAYED ~10 min:
    useful for paper/forward testing, NOT for acting on the alerts in real time."""
    name = "yahoo"
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"

    def __init__(self, symbol="NQ=F", session: Optional[requests.Session] = None, range_="5d"):
        self.symbol, self.range_ = symbol, range_
        self.http = session or requests.Session()
        self.http.headers.update({"User-Agent": "Mozilla/5.0 (apex-omega-bot)"})

    def fetch(self) -> Snapshot:
        try:
            r = self.http.get(self.URL.format(sym=self.symbol),
                              params={"range": self.range_, "interval": "1m", "includePrePost": "true"}, timeout=15)
            r.raise_for_status()
            res = r.json()["chart"]["result"][0]
        except Exception as e:                                   # noqa: BLE001
            raise ProviderError(f"yahoo fetch failed: {e}") from e
        ts = res.get("timestamp") or []
        q = res["indicators"]["quote"][0]
        by_min = {}
        last_tick = 0.0
        for i, t in enumerate(ts):
            o, h, l, c, v = (q[k][i] for k in ("open", "high", "low", "close", "volume"))
            if None in (o, h, l, c):
                continue
            last_tick = max(last_tick, float(t))
            by_min[int(t) // 60 * 60] = Bar(int(t) // 60 * 60, float(o), float(h), float(l), float(c), float(v or 0))
        bars = [by_min[k] for k in sorted(by_min)]
        return Snapshot(bars, last_tick, time.time(), self.name, last_bar_forming=True)


class CsvTailProvider:
    """Reads a CSV that any platform can append to: timestamp(UTC ISO or epoch),open,high,low,close,volume.
    This is the hook for a real-time feed (NinjaTrader/Sierra/TradingView export, etc.)."""
    name = "csv"

    def __init__(self, path):
        self.path = path

    def fetch(self) -> Snapshot:
        import datetime as dt
        bars = []
        with open(self.path, newline="") as f:
            for row in csv.DictReader(f):
                t = row["timestamp"]
                try:
                    ts = int(float(t))
                except ValueError:
                    ts = int(dt.datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp())
                bars.append(Bar(ts // 60 * 60, float(row["open"]), float(row["high"]), float(row["low"]),
                                float(row["close"]), float(row.get("volume") or 0)))
        bars.sort(key=lambda b: b.ts)
        last = bars[-1].ts + 59 if bars else 0.0
        return Snapshot(bars, last, time.time(), self.name, last_bar_forming=True)


def make_provider(spec: str):
    if spec in ("yahoo", "yahoo:NQ=F"):
        return YahooProvider("NQ=F")
    if spec.startswith("yahoo:"):
        return YahooProvider(spec.split(":", 1)[1])
    if spec.startswith("csv:"):
        return CsvTailProvider(spec.split(":", 1)[1])
    if spec == "tradovate" or spec.startswith("tradovate:"):
        from .tradovate import TradovateProvider              # lazy: needs websocket-client only when used
        return TradovateProvider.from_env(spec)
    raise ValueError(f"unknown provider {spec!r}")
