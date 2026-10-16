"""CLI:  python -m apex_omega <run|once|eod|seed-history|replay|test-webhook|status>"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import os
import sys

from . import __version__
from . import alerts as A
from .eod import run_eod
from .history import seed_from_yahoo_5m
from .providers import make_provider
from .runner import Config, Runner
from .state import State
from .timeutil import now_et


def _cfg():
    eq = os.environ.get("ACCOUNT_EQUITY")
    return Config(high_precision=os.environ.get("HIGH_PRECISION", "1") not in ("0", "false", "False"),
                  account_equity=float(eq) if eq else None)


def _common(a):
    prov = make_provider(a.provider or os.environ.get("PROVIDER", "yahoo"))
    notifier = A.Notifier(os.environ.get("DISCORD_WEBHOOK_URL"), dry=a.dry)
    state = State(a.state_dir or os.environ.get("STATE_DIR", "state"))
    return prov, notifier, state


def main(argv=None):
    ap = argparse.ArgumentParser(prog="apex_omega")
    ap.add_argument("cmd", choices=["run", "once", "eod", "seed-history", "replay", "test-webhook", "status"])
    ap.add_argument("--provider"); ap.add_argument("--state-dir"); ap.add_argument("--dry", action="store_true",
                    help="print alerts instead of posting to Discord")
    ap.add_argument("--poll", type=float, default=5.0); ap.add_argument("--max-minutes", type=float)
    ap.add_argument("--date"); ap.add_argument("--parquet", nargs="*"); ap.add_argument("--ledger", default="ledger/trades.csv")
    a = ap.parse_args(argv)

    if a.cmd == "replay":
        from .loaders import iter_valid_sessions, load_parquet_frames
        from .replay import replay_sessions
        files = sorted(sum((glob.glob(p) for p in a.parquet), []))
        led, _ = replay_sessions(iter_valid_sessions(load_parquet_frames(files)), high_precision=_cfg().high_precision)
        led = led[led.emit]
        r = led.net_r.astype(float)
        print(f"{len(led)} trades | WR {(r > 0).mean()*100:.2f}% | avg {r.mean():+.3f}R | net {r.sum():+.1f}R | "
              f"PF {r[r > 0].sum() / abs(r[r <= 0].sum()):.3f}")
        return 0

    prov, notifier, state = _common(a)
    cfg = _cfg()
    seeder = lambda: seed_from_yahoo_5m()                                   # noqa: E731
    if a.cmd == "seed-history":
        rows = seed_from_yahoo_5m()
        merged = {r["date"]: r for r in state.history()}
        for r in rows:
            merged.setdefault(r["date"], r)
        state.save_history(list(merged.values()))
        print(f"history: {len(merged)} sessions, latest {max(merged)}"); return 0
    if a.cmd == "test-webhook":
        ok = notifier.send(A.info_message("APEX-OMEGA test", f"Webhook OK. Bot v{__version__}. This is a test message; no trade.", dict(source="test")))
        print("sent" if ok else "failed"); return 0 if ok else 1
    if a.cmd == "status":
        h = state.history()
        print(f"now {now_et():%Y-%m-%d %H:%M:%S ET} | history {len(h)} sessions (latest {h[-1]['date'] if h else 'none'}) | "
              f"webhook {'set' if os.environ.get('DISCORD_WEBHOOK_URL') else 'NOT set (dry run)'} | provider {prov.name}")
        return 0
    runner = Runner(prov, notifier, state, cfg, seeder=seeder)
    if a.cmd == "once":
        r = runner.step(); print(r.date, r.note, "sent", r.sent, "lag", r.lag_s); return 0
    if a.cmd == "run":
        return runner.run(poll_s=a.poll, max_minutes=a.max_minutes)
    if a.cmd == "eod":
        date = dt.date.fromisoformat(a.date) if a.date else now_et().date()
        run_eod(prov, notifier, state, date, cfg, a.ledger, seeder=seeder); return 0


if __name__ == "__main__":
    sys.exit(main())
