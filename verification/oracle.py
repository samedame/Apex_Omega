"""Run the ORIGINAL backtest scripts (unmodified, hash-checked) on a set of parquet files.

The only change made is to the file *paths* in a throw-away copy (the scripts hard-code
/tmp/futures_data and /home/user/...).  Logic is untouched.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
REF = os.path.join(HERE, "reference")
SCRIPTS = ["prepare_fast_matrix.py", "build_daily_tensors.py", "stage10_apex_omega_v2.py"]


def verify_reference_hashes():
    bad = []
    for line in open(os.path.join(REF, "SHA256SUMS")):
        h, name = line.split()
        if hashlib.sha256(open(os.path.join(REF, name), "rb").read()).hexdigest() != h:
            bad.append(name)
    if bad:
        raise RuntimeError(f"reference scripts modified: {bad}")


def run_oracle(parquet_by_year: dict, workdir: str | None = None, quiet=True) -> pd.DataFrame:
    """parquet_by_year: {2020: path, ...}.  Returns the original engine's ledger (all trades, unfiltered)."""
    verify_reference_hashes()
    wd = workdir or tempfile.mkdtemp(prefix="oracle_")
    fd = os.path.join(wd, "fd"); os.makedirs(fd, exist_ok=True)
    for y, p in parquet_by_year.items():
        dst = os.path.join(fd, f"NQ_1min_{y}.parquet")
        if os.path.lexists(dst):
            os.remove(dst)
        os.symlink(os.path.abspath(p), dst)
    for name in SCRIPTS:
        s = open(os.path.join(REF, name)).read()
        s = s.replace("/tmp/futures_data", fd).replace("/home/user/futures_quant_lab", wd)
        if name == "prepare_fast_matrix.py":                   # years present; the 2026 Yahoo file is not public
            s = re.sub(r"\+ \['[^']*NQ_2026_1m\.parquet'\]", "", s)
            s = re.sub(r"range\(2020, 2026\)", f"{sorted(parquet_by_year)!r}", s)
        open(os.path.join(wd, name), "w").write(s)
    for name in SCRIPTS:
        r = subprocess.run([sys.executable, os.path.join(wd, name)], capture_output=True, text=True, cwd=wd)
        if r.returncode != 0:
            raise RuntimeError(f"{name} failed:\n{r.stderr[-2000:]}")
        if not quiet:
            print(r.stdout[-1500:])
    out = pd.read_csv(os.path.join(wd, "apex_omega_v2_master_trades.csv"))
    return out


def run_oracle_sizing(hp_csv_path: str):
    """Execute the original stage12d functions (without its module-level prints) on an hp ledger."""
    src = open(os.path.join(REF, "stage12d_optimal_gear_shift.py")).read()
    src = src.split("unique_days = sorted")[0].replace("/home/user/futures_quant_lab/apex_omega_hp_trades.csv", hp_csv_path)
    ns: dict = {}
    exec(compile(src, "stage12d_functions", "exec"), ns)
    return ns["simulate_optimal_gear_compounding"]
