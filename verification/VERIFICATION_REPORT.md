# Verification report: does the bot behave exactly like the strategy?

Definition used: the strategy is whatever generated your published trade ledgers, i.e. `stage10_apex_omega_v2.py` (+ the High-Precision filter from
`stage12`). The bot is a separate, streaming re-implementation. **Oracle** = your original scripts, SHA-256 pinned in `verification/reference/`
(verified unmodified before every run), executed on throw-away path-patched copies against the same data. Data: `mdelcristo/NQ-F_1min_OHLCV_Parquet`, 2020-01 to 2025-07-24.
Everything below is reproducible with `python verification/verify_against_backtest.py --data <parquet dir>`; raw numbers are in `results.json`.

## Result: PASS on every check

| # | Check | What was compared | Result |
|---|---|---|---|
| 1 | Reference integrity | SHA-256 of the 5 original scripts | match |
| 2 | Full replay, 2020-2025 (1,384 sessions) | every trade of the bot vs the oracle: entry bar, exit bar, direction, stop size, net points, net R | **4,190 / 4,190 trades identical** (0 extra, 0 missing, 0 field mismatches); High-Precision subset 2,741 / 2,741 identical |
| 3 | Your saved ledgers | `apex_omega_v2_master_trades.csv` and `apex_omega_hp_trades.csv` (through 2025-07-24) vs the bot | **identical** (HP: n 2,741, win rate 41.88%, PF 1.326, +534.16 R) |
| 4 | Indicators | VWAP, sigma, EMA9/21, ATR14, RSI5, RVOL, CLV, cumulative delta, SMA20/ATR vs the original tensors | bit-identical (float32) |
| 5 | Sizing (stage12d) | contract counts and final equity per year | identical; the blueprint's 2020-2024 end-values reproduce to the cent ($4,752.57; $30,122.26; $197,096.33; $3,161.81; $2,573,173.01) |
| 6 | Live path | 23 real sessions (all data-gap days, the busiest days, P5 days, P1 two-wave days, random days) played minute by minute through the real runner + Discord payload builder, forming bar **poisoned with garbage** high/low/close/volume, polls 3x/min, restarts mid-day | **221 alerts, 0 discrepancies**: every ENTRY/EXIT matches an oracle trade (price, stop, target, direction, net points/R); ENTRY lands in the first 40 s of its bar, EXIT within 40 s of the exit bar's close; no duplicates after restarts |
| 7 | No look-ahead (bot) | 40 sessions x 8 cutoffs: events emitted by minute k never change when later bars arrive | 320 cutoffs, **0 violations** |
| 8 | No look-ahead (original) | 150 sessions with every bar after minute k+1 replaced by noise: all entries decided earlier must be unchanged | **0 changes**; bot still equals oracle on all 4 poisoned datasets |
| 9 | Differential fuzzing | 3 seeds of perturbed data (daily rescale x e^N(0,.45), +/-150 pt shifts, volume noise, 1.2% of minutes deleted), 2022-24 | **6,784 trades, all identical** to the oracle (all six pillars exercised) |
| 10 | Mutation testing | 30 deliberately-wrong variants of the bot (thresholds off by one tick, stop/target order swapped, wrong windows, wrong friction...) | 23 caught by the real-data comparison, 7 survived it. Resolution of the 7 below |

### The 7 mutants that survived the 2022-23 real-data comparison
Re-run over all of 2020-2025 (4,190 trades): 2 more were caught (P1 CLV 0.40->0.41, P5 max hold 75->74). The other 5 never change a single historical trade, so I handled them separately:

* P1 wave-2 threshold 0.5->0.6R, target-before-stop, VWAP variance floor: no historical trade sits on those boundaries, but each is **killed by a hand-built unit test**.
* P6 window end 355->354: no historical signal on bar 355. I added `test_p6_signal_allowed_on_last_window_bar_355_but_not_356`, which kills it.
* P6 session-high lag 5->4: **provably equivalent** (the 10-bar recent window always contains bar m-5), so no test can distinguish it.

## Defects found in my own verification harness (fixed, then everything re-run)
* The live-path checker assumed one trade per pillar per day and flagged P1 two-wave days (18 false "problems"); it now pairs each alert with its own trade.
* The look-ahead test poisoned sessions spaced so that one poisoned day fed another day's 20-session SMA20, producing one spurious "future dependence". Spacing widened (45 sessions); the artefact disappeared (0 of 150).
* Earlier in the build: a 1-ULP float32 mismatch in the VWAP sigma (`typ*typ` vs `typ**2`) was found by the indicator comparison and fixed, and all long runs were restarted after it, so the numbers above come from the final code.

## What this does NOT prove
* **Calendar**: 3 weekdays (2020-07-21, 2025-02-13, 2025-06-20) are full exchange sessions that the dataset lacks or has partial; the backtest ignored them, the bot would trade them if the live feed has data.
* **49 trades after 2025-07-24** in your saved ledger could not be re-derived (no public data past that date).
* **Live data**: logic is identical, inputs are not. Another vendor's volume can flip borderline RVOL/delta/VWAP-sigma signals; Yahoo's free feed is ~10 min delayed. See `docs/LIVE_VS_BACKTEST.md`.
* The GitHub Actions workflows were syntax-checked only, never executed on GitHub. The Discord webhook was exercised only against a mocked HTTP endpoint (retry/429 handling), not a real server.
* This proves the bot equals the backtest. It does not show the backtest's edge is real: it is in-sample and picked from 804 variants.
* The TradingView Pine script is **not** equivalent to the backtest (see `docs/STRATEGY_SPEC.md`); the bot follows the backtest.
