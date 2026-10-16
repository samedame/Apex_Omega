"""End-of-day job: re-run the session on the final bars, reconcile against what the live loop
alerted, append the forward-test ledger, extend the prior-session history, post a summary."""
from __future__ import annotations

import csv
import datetime as dt
import json
import os

import numpy as np

from . import alerts as A
from . import params as P
from .calendar_cme import is_full_session
from .replay import run_session
from .runner import history_from_rows, session_bars_from_snapshot
from .history import ensure_history
from .timeutil import bar_start_et

F32 = np.float32
LEDGER_COLS = ["date", "pillar", "dir", "entry_time", "entry_px", "exit_time", "exit_px", "exit_reason",
               "risk_pts", "net_pts", "net_r"]


def _bars_list(snapshot, date):
    by_m, _ = session_bars_from_snapshot(snapshot, date)
    arr = [None] * P.M_BARS
    for m, b in by_m.items():
        arr[m] = (F32(b.o), F32(b.h), F32(b.l), F32(b.c), F32(b.v))
    return arr, len(by_m)


def forward_stats(path):
    if not os.path.exists(path):
        return None
    with open(path) as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return None
    r = np.array([float(x["net_r"]) for x in rows])
    w = r > 0
    gl = abs(r[~w].sum())
    return dict(n=len(r), wr=float(w.mean()), pf=float(r[w].sum() / gl) if gl > 0 else float("inf"),
                avg_r=float(r.mean()), net_r=float(r.sum()))


def run_eod(provider, notifier, state, date: dt.date, cfg, ledger_path="ledger/trades.csv", seeder=None, log=print):
    iso = date.isoformat()
    if not is_full_session(date):
        log("not a full session; nothing to do"); return None
    snap = provider.fetch()
    bars, nbars = _bars_list(snap, date)
    feed = dict(source=snap.source, lag_s=None, delayed=False)
    if nbars < P.MIN_BARS_VALID_DAY:
        notifier.send(A.info_message("APEX-OMEGA EOD", f"{iso}: only {nbars} RTH bars from the feed (< {P.MIN_BARS_VALID_DAY}); "
                                     "session not used for history/ledger.", feed, color=A.RED))
        return None
    hist = history_from_rows(ensure_history(state, date, seeder))
    eng = run_session(iso, bars, hist.context(first_open=bars[0][0] if bars[0] else None), cfg.high_precision)
    final_entries = {e.key for e in eng.events if e.kind == "ENTRY" and e.emit}
    sent = state.sent(iso)
    live_entries = {k.split("|", 1)[1] for k in sent if k.startswith(f"{iso}|ENTRY|")}
    missed, phantom = sorted(final_entries - live_entries), sorted(live_entries - final_entries)

    new = not os.path.exists(ledger_path)
    os.makedirs(os.path.dirname(ledger_path) or ".", exist_ok=True)
    done = set()
    if not new:
        with open(ledger_path) as f:
            done = {(r["date"], r["pillar"], r["entry_time"]) for r in csv.DictReader(f)}
    today = [t for t in eng.trades if t["emit"]]
    with open(ledger_path, "a", newline="") as f:
        w = csv.DictWriter(f, LEDGER_COLS)
        if new:
            w.writeheader()
        for t in sorted(today, key=lambda t: (t["entry_m"], t["pillar"])):
            et = bar_start_et(date, t["entry_m"]).strftime("%H:%M")
            if (iso, t["pillar"], et) in done:
                continue
            w.writerow(dict(date=iso, pillar=t["pillar"], dir=t["dir"], entry_time=et,
                            entry_px=float(t["entry_px"]), exit_time=bar_start_et(date, t["exit_m"] + 1).strftime("%H:%M"),
                            exit_px=float(t["exit_px"]), exit_reason=t["exit_reason"], risk_pts=float(t["risk_pts"]),
                            net_pts=float(t["net_pts"]), net_r=float(t["net_r"])))
    # extend history with this (valid) session
    close, rng = eng.S.day_close_and_range()
    rows = {r["date"]: r for r in state.history()}
    rows[iso] = dict(date=iso, open=float(eng.S.O[0]), high=float(np.max(eng.S.H)), low=float(np.min(eng.S.L)), close=float(close))
    state.save_history(list(rows.values()))

    net_r = sum(float(t["net_r"]) for t in today)
    lines = [f"{bar_start_et(date, t['entry_m']).strftime('%H:%M')} {'L' if t['dir'] == 1 else 'S'} {A.PILLAR_SHORT[t['pillar']]}: "
             f"{t['exit_reason']} {float(t['net_r']):+.2f}R ({float(t['net_pts']):+.1f} pts)" for t in sorted(today, key=lambda t: t['entry_m'])]
    txt = (f"{len(today)} trades | net **{net_r:+.2f}R**\n" + ("\n".join(lines) if lines else "No qualifying setups today.") +
           f"\n\nLive vs final-bar reconcile: missed by live **{len(missed)}**, phantom live alerts **{len(phantom)}**.")
    fs = forward_stats(ledger_path)
    if fs:
        txt += (f"\nForward record: {fs['n']} trades, WR {fs['wr']*100:.1f}%, PF {fs['pf']:.2f}, avg {fs['avg_r']:+.3f}R, net {fs['net_r']:+.1f}R")
    ref = os.path.join(os.path.dirname(__file__), "reference_stats.json")
    if os.path.exists(ref):
        r = json.load(open(ref))
        txt += (f"\nBacktest (High-Precision Core, 2020-07/2025): {r['n']} trades, WR {r['wr']*100:.1f}%, PF {r['pf']:.2f}, avg {r['avg_r']:+.3f}R")
    notifier.send(A.info_message(f"APEX-OMEGA EOD {iso}", txt, feed, color=A.GREEN if net_r > 0 else A.RED))
    os.makedirs(state.root, exist_ok=True)
    json.dump(dict(date=iso, missed=missed, phantom=phantom, n_final=len(final_entries), n_live=len(live_entries), net_r=net_r),
              open(os.path.join(state.root, f"reconcile-{iso}.json"), "w"), indent=1)
    return dict(missed=missed, phantom=phantom, trades=today)
