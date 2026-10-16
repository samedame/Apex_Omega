"""Regenerate tests/golden/oracle_trades.csv by running the ORIGINAL backtest scripts on the golden RTH block."""
import os, sys
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "verification"))
import oracle as OR
import tempfile
out = OR.run_oracle({2099: os.path.join(ROOT, "tests", "golden", "rth_block.parquet")}, workdir=tempfile.mkdtemp())
out.to_csv(os.path.join(ROOT, "tests", "golden", "oracle_trades.csv"), index=False)
print(len(out), "oracle trades written")
