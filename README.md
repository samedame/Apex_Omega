# APEX-OMEGA Discord Alert Bot

Alert-only bot for the APEX-OMEGA 6-pillar 1-minute NQ/MNQ futures strategy. It watches 1-minute bars, applies the exact backtest rules
and posts to a Discord webhook:

* **ENTRY**: pillar, direction, entry, stop, target, R, breakeven trigger, time-exit clock (and suggested MNQ size if `ACCOUNT_EQUITY` is set)
* **MOVE STOP**: breakeven ratchet reached
* **EXIT**: stop / target / breakeven stop / time exit, with net points and net R
* plus an "armed" message at the open, the opening-drive regime, and an end-of-day reconciliation with a running forward record

**It never places orders.** No broker credentials are used.

## Why you can trust it behaves like the strategy
The "strategy" is the backtest engine `stage10_apex_omega_v2.py` (+ the High-Precision filter). The bot is a streaming re-implementation of that engine,
and `python verification/verify_against_backtest.py --data <parquet dir>` proves equivalence against the **unmodified original scripts**
(SHA-256 pinned in `verification/reference/`). Details and results: [`verification/VERIFICATION_REPORT.md`](verification/VERIFICATION_REPORT.md).
Exact rules: [`docs/STRATEGY_SPEC.md`](docs/STRATEGY_SPEC.md). What can still differ in real life: [`docs/LIVE_VS_BACKTEST.md`](docs/LIVE_VS_BACKTEST.md).

## Quick start
```bash
pip install -r requirements.txt
python -m pytest -q                        # 24 tests, includes golden comparison with the original engine
python -m apex_omega test-webhook          # needs DISCORD_WEBHOOK_URL
python -m apex_omega seed-history          # one-off
python -m apex_omega run                   # during the session (dry-run prints if no webhook)
```
See [`docs/SETUP.md`](docs/SETUP.md) for the scheduler, GitHub Actions and real-time data options.

## Read this before acting on an alert
The free Yahoo feed is ~10 minutes delayed, so alerts from the default provider are labeled **DELAYED FEED** and are for forward-testing only.
A real-time bar source is required to trade the alerts. The backtest is in-sample and was selected from 804 variants; treat the live forward record
(`ledger/trades.csv`) as the real test.

## Layout
```
apex_omega/    params, session (indicators), engine (the 6 pillars), runner (live loop), providers, alerts, state, eod, sizing, calendar
verification/  oracle.py (runs the original scripts), harness.py, verify_against_backtest.py, reference/ (pinned originals), results.json
tests/         24 tests + golden data
docs/          STRATEGY_SPEC, LIVE_VS_BACKTEST, SETUP, reference blueprint & Pine script
.github/       tests / session / EOD workflows
```
