"""Per-session bar store with causal indicators computed one bar at a time.

The arithmetic deliberately mirrors ``build_daily_tensors.py`` operation by
operation in float32 (numpy >= 2 semantics), so the indicator values are
bit-identical to the tensors the backtest ran on.
"""
from __future__ import annotations

import numpy as np

from .params import M_BARS

F32 = np.float32
_A9 = F32(2.0 / 10.0);  _B9 = F32(1 - 2.0 / 10.0)
_A21 = F32(2.0 / 22.0); _B21 = F32(1 - 2.0 / 22.0)
_AATR = F32(1.0 / 14.0); _BATR = F32(1 - 1.0 / 14.0)
_ARSI = F32(1.0 / 5.0); _BRSI = F32(1 - 1.0 / 5.0)


class DailyContext:
    """Prior-session features: SMA20 of RTH closes and mean of the last 14 RTH ranges."""

    def __init__(self, sma20, atr14):
        self.sma20 = F32(sma20)
        self.atr14 = F32(atr14)

    @staticmethod
    def from_history(closes, ranges, first_open=None):
        """closes/ranges: float32-able sequences of PRIOR valid sessions, oldest first."""
        if len(closes) == 0:
            # reference convention for the very first session of a dataset
            return DailyContext(first_open if first_open is not None else 0.0, 150.0)
        c = np.asarray(closes[-20:], dtype=np.float32)
        r = np.asarray(ranges[-14:], dtype=np.float32)
        return DailyContext(np.mean(c), np.mean(r))


class SessionBars:
    def __init__(self):
        n = M_BARS
        z = lambda: np.zeros(n, dtype=np.float32)
        self.O, self.H, self.L, self.C, self.V = z(), z(), z(), z(), z()
        self.VWAP, self.VWAP_STD = z(), z()
        self.EMA9, self.EMA21 = z(), z()
        self.ATR14, self.RSI5, self.RVOL = z(), np.full(n, 50.0, dtype=np.float32), z()
        self.CLV, self.CUM_DELTA = z(), z()
        self.n = 0                      # bars 0..n-1 are filled
        self._cum_pv = F32(0.0); self._cum_p2v = F32(0.0)
        self._cum_v = F32(0.0);  self._cum_delta = F32(0.0)
        self._avg_gain = F32(0.0); self._avg_loss = F32(0.001)

    def add(self, m, o, h, l, c, v):
        """Append bar index m (must equal self.n)."""
        if m != self.n:
            raise ValueError(f"bars must be appended in order: expected {self.n}, got {m}")
        O, H, L, C, V = self.O, self.H, self.L, self.C, self.V
        O[m], H[m], L[m], C[m], V[m] = o, h, l, c, v
        o, h, l, c, v = O[m], H[m], L[m], C[m], V[m]          # float32 scalars

        # --- session VWAP / sigma (cumsum in float32, sequential) ---
        typ = ((h + l) + c) / F32(3.0)
        self._cum_pv = F32(self._cum_pv + typ * v)
        self._cum_v = F32(self._cum_v + v)
        cum_v = np.maximum(self._cum_v, F32(1.0))
        vwap = F32(self._cum_pv / cum_v)
        self._cum_p2v = F32(self._cum_p2v + (typ * typ) * v)      # array '** 2' is an exact square, not powf
        var = np.maximum((self._cum_p2v / cum_v) - (vwap * vwap), F32(0.25))
        self.VWAP[m] = vwap
        self.VWAP_STD[m] = np.sqrt(var)

        # --- CLV / cumulative delta proxy ---
        rng = h - l
        clv = ((F32(2.0) * c - h) - l) / rng if rng > 0 else F32(0.0)
        clv = F32(clv)
        self.CLV[m] = clv
        self._cum_delta = F32(self._cum_delta + clv * v)
        self.CUM_DELTA[m] = self._cum_delta

        # --- EMAs, ATR, RSI ---
        if m == 0:
            tr = h - l
            self.EMA9[0] = c; self.EMA21[0] = c; self.ATR14[0] = tr
        else:
            cp = C[m - 1]
            tr = np.maximum(h - l, np.maximum(np.abs(h - cp), np.abs(l - cp)))
            self.EMA9[m] = _A9 * c + _B9 * self.EMA9[m - 1]
            self.EMA21[m] = _A21 * c + _B21 * self.EMA21[m - 1]
            self.ATR14[m] = _AATR * tr + _BATR * self.ATR14[m - 1]
            diff = c - cp
            gain = np.maximum(diff, F32(0.0)); loss = np.maximum(-diff, F32(0.0))
            self._avg_gain = F32(_ARSI * gain + _BRSI * self._avg_gain)
            self._avg_loss = F32(_ARSI * loss + _BRSI * self._avg_loss)
            rs = self._avg_gain / np.maximum(self._avg_loss, F32(1e-6))
            self.RSI5[m] = F32(100.0) - (F32(100.0) / (F32(1.0) + rs))

        # --- relative volume (20-bar mean including the current bar) ---
        if m == 0:
            vol_ma = np.maximum(V[0], F32(1.0))
        else:
            w0 = max(0, m - 19)
            vol_ma = np.mean(V[w0:m + 1])
        self.RVOL[m] = V[m] / np.maximum(vol_ma, F32(1.0))
        self.n = m + 1

    def add_gap(self, m):
        """Forward-fill a missing minute exactly like the reference tensor builder
        (each of O/H/L/C copies its own previous value, volume 0)."""
        if m == 0:
            raise ValueError("cannot gap-fill the first RTH bar")
        self.add(m, self.O[m - 1], self.H[m - 1], self.L[m - 1], self.C[m - 1], 0.0)

    def day_close_and_range(self):
        if self.n != M_BARS:
            raise ValueError("session incomplete")
        return self.C[M_BARS - 1], F32(np.max(self.H) - np.min(self.L))
