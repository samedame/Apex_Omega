"""GitHub Actions guard: DST-safe gating for UTC cron schedules.
Prints go=true only on full CME equity sessions inside the allowed ET window (or when forced)."""
import argparse, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from apex_omega.calendar_cme import is_full_session
from apex_omega.timeutil import now_et

ap = argparse.ArgumentParser()
ap.add_argument("--earliest", default="09:05"); ap.add_argument("--latest", default="09:45")
ap.add_argument("--force", default="false")
a = ap.parse_args()
n = now_et(); hm = f"{n.hour:02d}:{n.minute:02d}"
go = a.force == "true" or (is_full_session(n.date()) and a.earliest <= hm <= a.latest)
print(f"now {n:%Y-%m-%d %H:%M} ET full_session={is_full_session(n.date())} -> go={str(go).lower()}")
if os.environ.get("GITHUB_OUTPUT"):
    open(os.environ["GITHUB_OUTPUT"], "a").write(f"go={str(go).lower()}\n")
