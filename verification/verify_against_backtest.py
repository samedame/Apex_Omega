"""Verification suite: does the alert bot do exactly what the APEX-OMEGA backtest did?

Sections (each cached in results.json, re-run individually with --only):
  hashes, calendar, replay, saved_ledger, sizing, live_path, lookahead, fuzz, mutation
Usage:  python verification/verify_against_backtest.py --data DIR_WITH_NQ_1min_YYYY.parquet [--saved-dir DIR] [--only a,b]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import pickle
import random
import sys
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE)); sys.path.insert(0, HERE)

import harness as H                                     # noqa: E402
import oracle as OR                                     # noqa: E402
from apex_omega import params as P                      # noqa: E402
from apex_omega.calendar_cme import is_full_session     # noqa: E402
from apex_omega.engine import SessionEngine             # noqa: E402
from apex_omega.loaders import iter_valid_sessions, load_parquet_frames   # noqa: E402
from apex_omega.replay import History, replay_sessions, run_session        # noqa: E402
from apex_omega.sizing import two_gear_contracts        # noqa: E402

RES_PATH = os.path.join(HERE, "results.json")


def hp_filter(df, emit_col=None):
    return df[((df.pillar != "P2_ORB_5m_Delta") | (df.risk_pts <= P.HP_P2_MAX_RISK)) & (df.risk_pts <= P.HP_MAX_RISK)]


def load_sessions(data_dir):
    files = sorted(glob.glob(os.path.join(data_dir, "NQ_1min_20*.parquet")))
    return files, list(iter_valid_sessions(load_parquet_frames(files)))


# ----------------------------------------------------------------------------- sections
def sec_hashes(ctx):
    OR.verify_reference_hashes()
    return dict(ok=True, files=[l.split()[1] for l in open(os.path.join(OR.REF, "SHA256SUMS"))])


def sec_calendar(ctx):
    rth = load_parquet_frames(ctx["files"])
    cnt = rth.groupby("date")["hm"].count()
    lo, hi = min(cnt.index), max(cnt.index)
    bad = []
    n = 0
    for d in pd.bdate_range(lo, hi):
        k = d.strftime("%Y-%m-%d"); c = int(cnt.get(k, 0)); n += 1
        if is_full_session(d.date()) != (c >= P.MIN_BARS_VALID_DAY):
            bad.append((k, c, bool(is_full_session(d.date()))))
    return dict(weekdays_checked=n, mismatches=bad,
                note="the mismatches listed are days the exchange was open but the data vendor has no/partial data")


def sec_replay(ctx):
    t = time.time()
    led, _ = replay_sessions(iter(ctx["sessions"]), high_precision=True)
    ctx["bot_ledger"] = led
    oracle = OR.run_oracle({int(os.path.basename(f)[8:12]): f for f in ctx["files"]}, workdir=os.path.join(ctx["work"], "oracle_main"))
    ctx["oracle_ledger"] = oracle
    all_cmp = H.compare_ledgers(led, oracle)
    hp_cmp = H.compare_ledgers(led[led.emit], hp_filter(oracle))
    emitted = led[led.emit].astype({"net_r": float})
    r = emitted.net_r
    stats = dict(n=int(len(r)), wr=float((r > 0).mean()), pf=float(r[r > 0].sum() / abs(r[r <= 0].sum())),
                 avg_r=float(r.mean()), net_r=float(r.sum()))
    return dict(seconds=round(time.time() - t, 1), sessions=len(ctx["sessions"]), all_trades=all_cmp,
                high_precision_trades=hp_cmp, hp_stats=stats,
                exit_reasons=led.exit_reason.value_counts().to_dict(),
                by_pillar=led.groupby("pillar").size().to_dict())


def sec_saved_ledger(ctx):
    """Compare with the user's own saved ledgers (apex_omega_v2_master_trades.csv / _hp_)."""
    out = {}
    if not ctx.get("saved_dir"):
        return dict(skipped="no --saved-dir")
    led = ctx["bot_ledger"]; last = led.date.max()
    for name, flt in (("apex_omega_v2_master_trades.csv", False), ("apex_omega_hp_trades.csv", True)):
        p = os.path.join(ctx["saved_dir"], name)
        if not os.path.exists(p):
            out[name] = "missing"; continue
        saved = pd.read_csv(p)
        saved = saved[saved.date <= last]
        mine = led[led.emit] if flt else led
        out[name] = dict(through=last, **H.compare_ledgers(mine, saved))
    return out


def sec_sizing(ctx):
    """The bot's two-gear sizer vs the original stage12d function on the saved hp ledger."""
    if not ctx.get("saved_dir"):
        return dict(skipped="no --saved-dir")
    p = os.path.join(ctx["saved_dir"], "apex_omega_hp_trades.csv")
    hp = pd.read_csv(p)
    f = OR.run_oracle_sizing(p)
    res = {}
    for y in range(2020, 2026):
        sub = hp[hp.year == y]
        o = f(sub, start_equity=1500.0, base_risk_pct=0.065, gear1_ceil=2050.0, max_mnq_cap=800)
        eq, peak = 1500.0, 1500.0
        contracts = []
        for row in sub.itertuples():
            c = two_gear_contracts(eq, peak, row.risk_pts, row.pillar)
            contracts.append(c)
            pnl = c * float(row.net_pts) * 2.0
            if c >= 10:
                pnl += (c // 10) * 10 * 0.76
            if eq >= 200.0:
                eq += pnl
                peak = max(peak, eq)
        res[y] = dict(oracle_final=round(float(o.equity.iloc[-1]), 2), bot_final=round(eq, 2),
                      contracts_identical=bool(np.array_equal(np.array([c for c in contracts if c > 0]), o.mnq_contracts[o.mnq_contracts > 0].to_numpy())))
    blueprint = {2020: 4752.57, 2021: 30122.26, 2022: 197096.33, 2023: 3161.81, 2024: 2573173.01}
    for y, v in blueprint.items():
        res[y]["blueprint_claim"] = v
        res[y]["oracle_matches_blueprint"] = abs(res[y]["oracle_final"] - v) < 0.01
    return res


def _expected_for_day(date, oracle_hp, bars):
    out = []
    for r in oracle_hp[oracle_hp.date == date].itertuples():
        entry = float(bars[r.entry_m][0]) if bars[r.entry_m] else None
        out.append(r)
    return out


def check_live_day(date, bars, oracle_hp, got_log, label=""):
    """Return list of discrepancy strings (empty == identical)."""
    problems = []
    start0 = H.start0_ts(date)
    exp = list(oracle_hp[oracle_hp.date == date].itertuples())
    gap = {m for m, b in enumerate(bars) if b is None}       # minutes the feed never printed
    ent = [(o, H.parse_payload(p)) for o, p in got_log if H.parse_payload(p)["kind"] == "ENTRY"]
    exi = [(o, H.parse_payload(p)) for o, p in got_log if H.parse_payload(p)["kind"] == "EXIT"]
    mov = [(o, H.parse_payload(p)) for o, p in got_log if H.parse_payload(p)["kind"] == "MOVE_STOP"]
    if len(ent) != len(exp): problems.append(f"{date}: {len(ent)} ENTRY alerts vs {len(exp)} oracle trades")
    if len(exi) != len(exp): problems.append(f"{date}: {len(exi)} EXIT alerts vs {len(exp)} oracle trades")
    # a pillar can legitimately trade twice in a day (P1 wave 2): pair alerts with oracle trades in time order
    seen = {}
    for r in sorted(exp, key=lambda t: (t.pillar, t.entry_m)):
        code = r.pillar.split("_")[0]
        k = seen.get(code, 0); seen[code] = k + 1
        n_code = sum(1 for t in exp if t.pillar == r.pillar)
        e = sorted((a for a in ent if f"| {code} " in a[1]["title"]), key=lambda a: a[0])
        x = sorted((a for a in exi if f"| {code} " in a[1]["title"]), key=lambda a: a[0])
        if len(e) != n_code or len(x) != n_code:
            problems.append(f"{date} {code}: entry alerts {len(e)}, exit alerts {len(x)}, oracle trades {n_code}"); continue
        eo, ed = e[k]; xo, xd = x[k]
        o_px = float(bars[r.entry_m][0]) if bars[r.entry_m] else None
        spec = P.PILLARS[r.pillar]
        if o_px is not None and abs(ed["entry"] - o_px) > 0.006: problems.append(f"{date} {code}: entry {ed['entry']} vs bar open {o_px}")
        d = int(r.dir)
        if abs(ed["stop"] - (ed["entry"] - d * max(r.risk_pts, 8.0))) > 0.011: problems.append(f"{date} {code}: stop {ed['stop']}")
        if abs(ed["target"] - (ed["entry"] + d * r.risk_pts * spec["target_r"])) > 0.011: problems.append(f"{date} {code}: target {ed['target']}")
        if (ed["long"]) != (d == 1): problems.append(f"{date} {code}: direction")
        if abs(xd["exit"] - (ed["entry"] + d * (r.net_pts + 1.0))) > 0.011: problems.append(f"{date} {code}: exit px {xd['exit']}")
        if abs(xd["pts"] - r.net_pts) > 0.0051 or abs(xd["r"] - r.net_r) > 0.0051: problems.append(f"{date} {code}: result {xd['pts']}/{xd['r']} vs {r.net_pts:.2f}/{r.net_r:.2f}")
        # timing: ENTRY inside the first 5 s of its bar; EXIT inside the first 40 s after the exit bar closed
        if r.entry_m not in gap and not (60 * r.entry_m <= eo < 60 * r.entry_m + 40): problems.append(f"{date} {code}: ENTRY alert at +{eo}s vs bar start {60*r.entry_m}")
        if r.exit_m not in gap and not (60 * (r.exit_m + 1) <= xo < 60 * (r.exit_m + 1) + 40): problems.append(f"{date} {code}: EXIT alert at +{xo}s vs bar close {60*(r.exit_m+1)}")
    return problems


def pick_live_days(sessions, oracle):
    dates = [d for d, _ in sessions]
    hp = hp_filter(oracle)
    gap = [d for d, b in sessions if any(x is None for x in b)]
    many = hp.groupby("date").size().sort_values(ascending=False).index.tolist()[:8]
    p5 = hp[hp.pillar == "P5_VWAP_3SD_Rev"].date.unique().tolist()[:5]
    p1w2 = oracle[oracle.pillar == "P1_OD10_Pullback"].groupby("date").size()
    p1w2 = p1w2[p1w2 >= 2].index.tolist()[:4]
    rnd = random.Random(7).sample(dates[40:], 4)
    seen, out = set(), []
    for d in gap + many + p5 + p1w2 + rnd:
        if d not in seen and dates.index(d) >= 30:
            seen.add(d); out.append(d)
    return out


def sec_live_path(ctx):
    sessions, oracle = ctx["sessions"], ctx["oracle_ledger"]
    dates = [d for d, _ in sessions]
    hp = hp_filter(oracle)
    days = pick_live_days(sessions, oracle)
    problems, n_alerts, rows = [], 0, []
    t = time.time()
    for k, d in enumerate(days):
        i = dates.index(d)
        runner, notifier, log = H.simulate_live_day(d, sessions[i][1], sessions[max(0, i - 25):i], offsets=(2, 33))
        pr = check_live_day(d, sessions[i][1], hp, log)
        dup = len(notifier.sent_payloads) - len({json.dumps(p, sort_keys=True) for p in notifier.sent_payloads})
        if dup: pr.append(f"{d}: {dup} duplicate payloads")
        problems += pr; n_alerts += len(log)
        rows.append(dict(date=d, alerts=len(log), problems=len(pr)))
    # crash/restart robustness on a few busy days
    restart_days = days[:3]
    for d in restart_days:
        i = dates.index(d)
        runner, notifier, log = H.simulate_live_day(d, sessions[i][1], sessions[max(0, i - 25):i], offsets=(1, 20, 59), restart_every=37)
        pr = check_live_day(d, sessions[i][1], hp, log)
        problems += [f"[restart] {p}" for p in pr]
        rows.append(dict(date=d, alerts=len(log), problems=len(pr), variant="3 polls/min + restart every 37 polls"))
    return dict(days_tested=len(days), restart_variant_days=len(restart_days), alerts_checked=n_alerts,
                problems=problems[:30], n_problems=len(problems), seconds=round(time.time() - t, 1), days=rows,
                day_selection="all gap days, busiest days, P5 days, P1 wave-2 days, random days",
                forming_bar_poisoned=True)


def _ev_sig(e):
    return (e.kind, e.m, e.pillar, round(e.data.get("entry_px", 0.0), 4), round(e.data.get("sl", 0.0), 4),
            e.data.get("reason"), round(e.data.get("net_r", 0.0), 5))


def _poison_future(files, outdir, seed, stride=25, offset=0, years=None):
    """For every `stride`-th session, replace all bars from minute k+2 onwards with wild random data.
    Returns ({year: path}, {date: k})."""
    rng = np.random.default_rng(seed)
    out, cut = {}, {}
    all_days = sorted(set(sum([list(pd.to_datetime(pd.read_parquet(f, columns=["timestamp"])["timestamp"], utc=True)
                                    .dt.tz_convert("America/New_York").dt.strftime("%Y-%m-%d").unique()) for f in files], [])))
    sel = {d for i, d in enumerate(all_days) if i % stride == offset and i > 40}
    for f in files:
        y = int(os.path.basename(f)[8:12])
        df = pd.read_parquet(f).copy()
        ny = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
        day = ny.dt.strftime("%Y-%m-%d").to_numpy(); mm = (ny.dt.hour * 60 + ny.dt.minute - 570).to_numpy()
        for d in np.unique(day):
            if d not in sel: continue
            k = int(rng.integers(20, 370)); cut[d] = k
            idx = np.where((day == d) & (mm >= k + 2) & (mm < 390))[0]
            if len(idx) == 0: continue
            base = float(df["close"].iloc[idx[0] - 1]) if idx[0] > 0 else float(df["open"].iloc[idx[0]])
            steps = rng.normal(0, 30, len(idx)); c = base + np.cumsum(steps); o = np.concatenate([[base], c[:-1]])
            df.loc[df.index[idx], "open"] = np.round(o * 4) / 4; df.loc[df.index[idx], "close"] = np.round(c * 4) / 4
            hi = np.maximum(o, c) + np.abs(rng.normal(0, 12, len(idx))); lo = np.minimum(o, c) - np.abs(rng.normal(0, 12, len(idx)))
            df.loc[df.index[idx], "high"] = np.round(hi * 4) / 4; df.loc[df.index[idx], "low"] = np.round(lo * 4) / 4
            df.loc[df.index[idx], "volume"] = rng.integers(1, 6000, len(idx))
        p = os.path.join(outdir, f"NQ_1min_{y}.parquet"); df.to_parquet(p); out[y] = p
    return out, cut


NPOIS = 4


def sec_lookahead(ctx):
    sessions = ctx["sessions"]; rng = random.Random(11)
    # --- Part 1: streaming engine -- events emitted up to bar k never change as later bars arrive
    hist = History(); hists = {}
    picks = set(rng.sample(range(30, len(sessions)), 40))
    for i, (d, bars) in enumerate(sessions):
        if i in picks: hists[i] = (list(hist.closes), list(hist.ranges))
        eng = run_session(d, bars, hist.context(bars[0][0]), True)
        c, r = eng.S.day_close_and_range(); hist.push(c, r)
    checked, bad = 0, []
    for i in sorted(picks):
        d, bars = sessions[i]; daily = History(*hists[i]).context(bars[0][0])
        full = run_session(d, bars, daily, True)
        for k in rng.sample(range(5, 388), 8):
            eng = SessionEngine(d, daily, True)
            for m in range(0, k + 1):
                b = bars[m] if bars[m] is not None else (eng.S.O[m-1], eng.S.H[m-1], eng.S.L[m-1], eng.S.C[m-1], np.float32(0))
                eng.on_open(m, b[0]); eng.on_close(m, *b)
            got = [_ev_sig(e) for e in eng.events]
            want = [_ev_sig(e) for e in full.events if e.m <= k]
            if got != want: bad.append((d, k))
            checked += 1
    part1 = dict(sessions=len(picks), cutoffs_checked=checked, violations=bad[:10], n_violations=len(bad))
    # --- Part 2: causality of the ORIGINAL backtest + the bot: wreck every bar after minute k+1 of selected
    # sessions and require every entry decided before that point (entry_m <= k+1) to be unchanged.
    base_oracle = ctx["oracle_ledger"]
    tot, viol, bot_mismatch = 0, [], 0
    for off in range(NPOIS):
        d_ = os.path.join(ctx["work"], f"poison_{off}"); os.makedirs(d_, exist_ok=True)
        pm, cut = _poison_future(ctx["files"], d_, seed=500 + off, stride=45, offset=off * 11)   # stride 45 >> 20-session daily window: poisoned days never feed each other's SMA20/ATR14
        orc = OR.run_oracle(pm, workdir=os.path.join(d_, "oracle"))
        bot, _ = replay_sessions(iter_valid_sessions(load_parquet_frames(sorted(pm.values()))), high_precision=True)
        if not H.compare_ledgers(bot, orc)["identical"]: bot_mismatch += 1
        for d, k in cut.items():
            a = base_oracle[(base_oracle.date == d) & (base_oracle.entry_m <= k + 1)].sort_values(["entry_m", "pillar"])
            b = orc[(orc.date == d) & (orc.entry_m <= k + 1)].sort_values(["entry_m", "pillar"])
            cols = ["entry_m", "pillar", "dir", "risk_pts"]
            same = len(a) == len(b) and (a[cols].reset_index(drop=True).astype(str) == b[cols].reset_index(drop=True).astype(str)).all().all()
            tot += 1
            if not same: viol.append((d, k))
    part2 = dict(sessions_poisoned=tot, entries_changed_by_future_data=len(viol), examples=viol[:5],
                 bot_vs_oracle_on_poisoned_data_identical_runs=f"{NPOIS - bot_mismatch}/{NPOIS}")
    return dict(prefix_stability=part1, backtest_causality=part2)


def _perturb(files, seed, outdir):
    rng = np.random.default_rng(seed)
    out = {}
    for f in files:
        y = int(os.path.basename(f)[8:12])
        df = pd.read_parquet(f).copy()
        ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("America/New_York")
        day = ts.dt.strftime("%Y-%m-%d").to_numpy()
        days = np.unique(day)
        scale = dict(zip(days, np.exp(rng.normal(0.0, 0.45, len(days)))))
        shift = dict(zip(days, rng.normal(0, 150, len(days))))
        pivot = df.groupby(day)["open"].transform("first").to_numpy()
        s = np.array([scale[d] for d in day]); sh = np.array([shift[d] for d in day])
        for c in ("open", "high", "low", "close"):
            df[c] = np.round((pivot + s * (df[c].to_numpy() - pivot) + sh) * 4) / 4
        vm = np.exp(rng.normal(0, 0.8, len(df)))
        df["volume"] = np.maximum((df["volume"].to_numpy() * vm).round(), 0).astype("int64")
        hm_ = (ts.dt.hour * 60 + ts.dt.minute).to_numpy()
        drop = (rng.random(len(df)) < 0.012) & (hm_ != 570)                       # ~1.2% of minutes missing (never the 09:30 bar: the bot fails closed there)
        df = df.loc[~drop].reset_index(drop=True)
        p = os.path.join(outdir, f"NQ_1min_{y}.parquet"); df.to_parquet(p); out[y] = p
    return out


def sec_fuzz(ctx, seeds=(101, 202, 303), years=(2022, 2023, 2024)):
    res = []
    files = [f for f in ctx["files"] if int(os.path.basename(f)[8:12]) in years]
    for sd in seeds:
        t = time.time()
        d = os.path.join(ctx["work"], f"fuzz_{sd}"); os.makedirs(d, exist_ok=True)
        pm = _perturb(files, sd, d)
        orc = OR.run_oracle(pm, workdir=os.path.join(d, "oracle"))
        rth = load_parquet_frames(sorted(pm.values()))
        led, _ = replay_sessions(iter_valid_sessions(rth), high_precision=True)
        cmpd = H.compare_ledgers(led, orc)
        cmp_hp = H.compare_ledgers(led[led.emit], hp_filter(orc))
        res.append(dict(seed=sd, trades=cmpd["n_oracle"], identical_all=cmpd["identical"], identical_hp=cmp_hp["identical"],
                        detail=None if cmpd["identical"] else cmpd, by_pillar=orc.groupby("pillar").size().to_dict(),
                        seconds=round(time.time() - t, 1)))
    return dict(runs=res, total_trades=sum(r["trades"] for r in res))


MUTANTS = [
    ("engine.py", "F32(1.8) * S.ATR14[10]", "F32(1.7) * S.ATR14[10]", "P1 drive threshold 1.8 -> 1.7 ATR"),
    ("engine.py", "CLV[m] > 0.40 and EMA9[m] > EMA21[m]", "CLV[m] > 0.41 and EMA9[m] > EMA21[m]", "P1 CLV 0.40 -> 0.41"),
    ("engine.py", "c_px > orb5_h and CLV[m] > 0.45", "c_px > orb5_h and CLV[m] > 0.46", "P2 CLV 0.45 -> 0.46"),
    ("engine.py", "CUM_DELTA[m] < 0 and RVOL[m] > 1.30", "CUM_DELTA[m] < 0 and RVOL[m] > 1.31", "P2 RVOL 1.30 -> 1.31"),
    ("engine.py", "orb15_rng <= 0.55 * d.atr14", "orb15_rng <= 0.56 * d.atr14", "P3 range cap 0.55 -> 0.56 daily ATR"),
    ("engine.py", "O[0] < d.sma20 and RVOL[m] > 1.40", "O[0] < d.sma20 and RVOL[m] > 1.39", "P3 RVOL 1.40 -> 1.39"),
    ("engine.py", "orb20_rng <= 0.55 * d.atr14", "orb20_rng <= 0.50 * d.atr14", "P4 range cap 0.55 -> 0.50"),
    ("engine.py", "up_b = VWAP[m] + 3.0 * VWAP_STD[m]", "up_b = VWAP[m] + 2.9 * VWAP_STD[m]", "P5 upper band 3.0 -> 2.9 SD"),
    ("engine.py", "RSI5[m - 1] < 20", "RSI5[m - 1] < 21", "P5 RSI long 20 -> 21"),
    ("engine.py", "np.max(H[m - 10:m])", "np.max(H[m - 11:m])", "P6 recent-high window 10 -> 11"),
    ("engine.py", "np.max(H[:m - 5])", "np.max(H[:m - 4])", "P6 session-high lag 5 -> 4"),
    ("engine.py", "self.p5_free_m <= m <= 355", "self.p5_free_m <= m <= 354", "P6 window end 355 -> 354"),
    ("engine.py", "if net_r <= 0.5:", "if net_r <= 0.6:", "P1 wave-2 threshold 0.5 -> 0.6R"),
    ("engine.py", "self.p1_free_m = m + 3", "self.p1_free_m = m + 2", "P1 re-arm delay 3 -> 2 bars"),
    ("engine.py", "min(min(p[\"signal_m\"] + 75, 380)", "min(min(p[\"signal_m\"] + 74, 380)", "P5 max hold 75 -> 74"),
    ("engine.py", "atr = max(S.ATR14[m], 4.0)", "atr = max(S.ATR14[m], 5.0)", "ATR floor 4 -> 5"),
    ("engine.py", "min_sl = max(1.0 * atr, 10.0)", "min_sl = max(1.0 * atr, 9.0)", "min stop 10 -> 9"),
    ("engine.py", "max_sl = max(3.8 * atr, 35.0)", "max_sl = max(3.8 * atr, 36.0)", "max stop 35 -> 36"),
    ("engine.py", "if bl <= pos.sl:\n                        ex = ((pos.sl - pos.entry_px)", "if bh >= pos.tp:\n                        ex = ((pos.tp - pos.entry_px) - P.FRICTION_PTS, 'TARGET', pos.tp)\n                    elif bl <= pos.sl:\n                        ex = ((pos.sl - pos.entry_px)", "target checked before stop (long)"),
    ("params.py", "FRICTION_PTS = 1.0", "FRICTION_PTS = 0.5", "friction 1.0 -> 0.5 pt"),
    ("params.py", "target_r=4.0, be_trig=1.8, be_lock=0.25, exit_m=365", "target_r=4.0, be_trig=1.8, be_lock=0.25, exit_m=366", "P1 time exit 365 -> 366"),
    ("params.py", "target_r=6.0, be_trig=2.0, be_lock=0.30, exit_m=387),\n    \"P3", "target_r=6.0, be_trig=2.1, be_lock=0.30, exit_m=387),\n    \"P3", "P2 BE trigger 2.0 -> 2.1R"),
    ("params.py", "target_r=4.5, be_trig=1.8, be_lock=0.25, exit_m=388", "target_r=4.5, be_trig=1.8, be_lock=0.30, exit_m=388", "P6 BE lock 0.25 -> 0.30R"),
    ("params.py", "target_r=2.5, be_trig=None", "target_r=2.6, be_trig=None", "P5 target 2.5 -> 2.6R"),
    ("session.py", "_A9 = F32(2.0 / 10.0);  _B9 = F32(1 - 2.0 / 10.0)", "_A9 = F32(2.0 / 11.0);  _B9 = F32(1 - 2.0 / 11.0)", "EMA9 span 9 -> 10"),
    ("session.py", "F32(0.25))\n        self.VWAP[m] = vwap", "F32(0.30))\n        self.VWAP[m] = vwap", "VWAP variance floor 0.25 -> 0.30"),
    ("session.py", "w0 = max(0, m - 19)", "w0 = max(0, m - 18)", "RVOL window 20 -> 19"),
    ("session.py", "c = np.asarray(closes[-20:], dtype=np.float32)", "c = np.asarray(closes[-19:], dtype=np.float32)", "daily SMA20 -> SMA19"),
    ("session.py", "r = np.asarray(ranges[-14:], dtype=np.float32)", "r = np.asarray(ranges[-13:], dtype=np.float32)", "daily ATR14 -> 13"),
    ("engine.py", "if pillar == \"P2_ORB_5m_Delta\" and risk > P.HP_P2_MAX_RISK:", "if pillar == \"P2_ORB_5m_Delta\" and risk > P.HP_P2_MAX_RISK + 1:", "HP filter P2 28 -> 29 (caught by the HP-trade comparison)"),
]


def _run_mutant(args):
    idx, work, files, years = args
    import importlib, shutil
    fname, old, new, desc = MUTANTS[idx]
    root = os.path.dirname(HERE)
    md = os.path.join(work, f"mut_{idx}"); shutil.rmtree(md, ignore_errors=True)
    shutil.copytree(os.path.join(root, "apex_omega"), os.path.join(md, "apex_omega"))
    path = os.path.join(md, "apex_omega", fname); src = open(path).read()
    if src.count(old) != 1:
        return idx, desc, "NOT-APPLICABLE (pattern count %d)" % src.count(old), None
    open(path, "w").write(src.replace(old, new))
    code = f"""
import sys; sys.path.insert(0, {md!r}); sys.path.insert(1, {HERE!r})
import pandas as pd, pickle
from apex_omega.loaders import load_parquet_frames, iter_valid_sessions
from apex_omega.replay import replay_sessions
import harness as H
rth = load_parquet_frames({files!r})
led,_ = replay_sessions(iter_valid_sessions(rth), high_precision=True)
orc = pd.read_pickle({os.path.join(work, 'oracle_subset.pkl')!r})
a = H.compare_ledgers(led, orc)
hp = orc[((orc.pillar!='P2_ORB_5m_Delta')|(orc.risk_pts<=28.0))&(orc.risk_pts<=68.0)]
b = H.compare_ledgers(led[led.emit], hp)
print(int(a['identical']), int(b['identical']), a['n_only_bot'], a['n_only_oracle'], sum(a['field_mismatches'].values()))
"""
    import subprocess
    r = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    if r.returncode != 0:
        return idx, desc, "CRASH: " + r.stderr[-300:], None
    a, b, ob, oo, mm = (int(x) for x in r.stdout.split()[-5:])
    caught = not (a and b)
    return idx, desc, "CAUGHT" if caught else "SURVIVED", dict(only_mutant=ob, only_oracle=oo, field_mismatch_cells=mm)


def sec_mutation(ctx, years=(2022, 2023)):
    # optional re-test of selected mutants on other years: MUT_YEARS=2020,2021,... MUT_ONLY="desc1|desc2"
    if os.environ.get("MUT_YEARS"): years = tuple(int(y) for y in os.environ["MUT_YEARS"].split(","))
    only = [x for x in os.environ.get("MUT_ONLY", "").split("|") if x]
    sel = [i for i, m in enumerate(MUTANTS) if not only or m[3] in only]
    files = [f for f in ctx["files"] if int(os.path.basename(f)[8:12]) in years]
    pm = {int(os.path.basename(f)[8:12]): f for f in files}
    orc = OR.run_oracle(pm, workdir=os.path.join(ctx["work"], "oracle_mut"))
    orc.to_pickle(os.path.join(ctx["work"], "oracle_subset.pkl"))
    import concurrent.futures as cf
    out = []
    with cf.ProcessPoolExecutor(max_workers=2) as ex:
        for r in ex.map(_run_mutant, [(i, ctx["work"], files, years) for i in sel]):
            out.append(r)
    caught = sum(1 for r in out if r[2] == "CAUGHT")
    return dict(window=f"{min(years)}-{max(years)} ({orc.shape[0]} oracle trades)", mutants=len(sel), caught=caught,
                survivors=[(r[1], r[2]) for r in out if r[2] != "CAUGHT"],
                detail=[dict(desc=r[1], verdict=r[2], effect=r[3]) for r in out])


SECTIONS = ["hashes", "calendar", "replay", "saved_ledger", "sizing", "live_path", "lookahead", "fuzz", "mutation"]


def main():
    global RES_PATH
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True); ap.add_argument("--saved-dir"); ap.add_argument("--only")
    ap.add_argument("--work", default=os.path.join(HERE, "_work"))
    ap.add_argument("--results", default=RES_PATH)
    a = ap.parse_args()
    RES_PATH = a.results
    os.makedirs(a.work, exist_ok=True)
    files, sessions = load_sessions(a.data)
    ctx = dict(files=files, sessions=sessions, saved_dir=a.saved_dir, work=a.work)
    results = json.load(open(RES_PATH)) if os.path.exists(RES_PATH) else {}
    want = a.only.split(",") if a.only else SECTIONS
    need_replay = any(s in want for s in ("replay", "saved_ledger", "sizing", "live_path", "mutation")) and "replay" not in want
    if need_replay or "replay" in want:
        pass
    for name in SECTIONS:
        if name not in want:
            continue
        if name in ("saved_ledger", "live_path", "lookahead") and "bot_ledger" not in ctx:
            results["replay"] = globals()["sec_replay"](ctx)
        t = time.time()
        results[name] = globals()[f"sec_{name}"](ctx)
        print(f"[{name}] done in {time.time()-t:.0f}s", flush=True)
        json.dump(results, open(RES_PATH, "w"), indent=1, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
