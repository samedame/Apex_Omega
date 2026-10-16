"""Historical 1-minute bar loading (parquet/CSV) into per-session RTH bars.

Mirrors ``prepare_fast_matrix.py`` + ``build_daily_tensors.py`` selection rules:
weekdays only, New-York clock, RTH = 09:30..15:59, duplicate (date, minute) rows
dropped, a session is valid only with >= 350 RTH bars.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import params as P


def load_parquet_frames(paths) -> pd.DataFrame:
    frames = []
    for pth in paths:
        df = pd.read_parquet(pth)
        frames.append(_normalise(df))
    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["date", "hm"]).sort_values(["date", "hm"]).reset_index(drop=True)
    return out


def _normalise(df: pd.DataFrame) -> pd.DataFrame:
    if "timestamp" in df.columns:
        ts = pd.to_datetime(df["timestamp"], utc=True)
    else:
        ts = pd.Series(pd.to_datetime(df.index, utc=True), index=df.index)
    ny = ts.dt.tz_convert("America/New_York")
    keep = ny.dt.dayofweek < 5
    df = df.loc[keep].copy(); ny = ny.loc[keep]
    hm = (ny.dt.hour * 60 + ny.dt.minute).astype(np.int16)
    out = pd.DataFrame({
        "date": ny.dt.strftime("%Y-%m-%d").to_numpy(),
        "hm": hm.to_numpy(),
        "open": df["open"].to_numpy(dtype=np.float32), "high": df["high"].to_numpy(dtype=np.float32),
        "low": df["low"].to_numpy(dtype=np.float32), "close": df["close"].to_numpy(dtype=np.float32),
        "volume": df["volume"].to_numpy(dtype=np.float32),
    })
    return out[(out.hm >= P.RTH_START_HM) & (out.hm < P.RTH_END_HM)].reset_index(drop=True)


def iter_valid_sessions(rth: pd.DataFrame, min_bars: int = P.MIN_BARS_VALID_DAY):
    """Yield (date, bars) where bars is a list of 390 entries: None or (o, h, l, c, v) float32."""
    counts = rth.groupby("date")["hm"].count()
    valid = set(counts[counts >= min_bars].index)
    for date, g in rth[rth.date.isin(valid)].groupby("date", sort=True):
        arr = [None] * P.M_BARS
        m = (g["hm"].to_numpy() - P.RTH_START_HM)
        o, h, l, c, v = (g[k].to_numpy(dtype=np.float32) for k in ("open", "high", "low", "close", "volume"))
        for i in range(len(g)):
            arr[int(m[i])] = (o[i], h[i], l[i], c[i], v[i])
        yield date, arr
