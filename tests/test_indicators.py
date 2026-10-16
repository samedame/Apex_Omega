"""Streaming indicators == the vectorised tensors the backtest ran on (float32 bit-identical)."""
import os
import tempfile

import numpy as np

import oracle as OR
from apex_omega.session import SessionBars
from conftest import GOLD

FIELDS = ["VWAP", "VWAP_STD", "EMA9", "EMA21", "ATR14", "RSI5", "RVOL", "CLV", "CUM_DELTA"]


def test_indicators_bit_identical_to_reference_tensors(sessions):
    wd = tempfile.mkdtemp()
    OR.run_oracle({2099: os.path.join(GOLD, "rth_block.parquet")}, workdir=wd)
    T = np.load(os.path.join(wd, "fd", "nq_rth_tensors.npz"), allow_pickle=True)
    assert list(T["dates"]) == [d for d, _ in sessions]
    for di, (date, bars) in enumerate(sessions):
        S = SessionBars()
        for m in range(390):
            b = bars[m] if bars[m] is not None else (S.O[m-1], S.H[m-1], S.L[m-1], S.C[m-1], np.float32(0))
            S.add(m, *b)
        for f in FIELDS:
            assert np.array_equal(getattr(S, f), T[f][di]), (date, f)
