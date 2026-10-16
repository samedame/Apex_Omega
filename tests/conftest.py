import os
import sys

import pandas as pd
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "verification"))

from apex_omega.loaders import iter_valid_sessions, load_parquet_frames   # noqa: E402

GOLD = os.path.join(ROOT, "tests", "golden")


@pytest.fixture(scope="session")
def sessions():
    return list(iter_valid_sessions(load_parquet_frames([os.path.join(GOLD, "rth_block.parquet")])))


@pytest.fixture(scope="session")
def oracle():
    return pd.read_csv(os.path.join(GOLD, "oracle_trades.csv"))
