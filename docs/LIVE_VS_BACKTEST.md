# What can differ between live alerts and the published backtest

Logic is identical (proved bit-for-bit in `verification/VERIFICATION_REPORT.md`). These are the places where *reality* differs from the backtest.

1. **Data feed (biggest one).** Yahoo's free NQ=F feed was measured ~10 minutes behind real time on 2026-10-15 (newest tick 00:46:23 UTC vs
   00:56:25 UTC wall clock). The bot detects this (lag > 150 s) and stamps every alert "DELAYED FEED: paper tracking only". Use it to forward-test,
   not to trade. For real-time alerts, feed the bot minute bars from a real-time source through the `csv:` provider (any platform that can append
   1-minute bars to a CSV) or add a provider class (`apex_omega/providers.py`, 20 lines).
2. **Volume differs by vendor.** RVOL (P2/P3 shorts), cumulative delta (P2), VWAP and its sigma (P2, P3, P5, P6) all use volume. The backtest used
   the HuggingFace `mdelcristo/NQ-F_1min_OHLCV_Parquet` volumes; a different vendor's volumes will flip some borderline signals. Not quantified
   (there is no overlapping period with a second vendor to measure it).
3. **Fills.** Backtest: enter at the exact next-bar open, exit at the exact stop/target price, total friction 1.0 point ($2 per MNQ round trip). Live:
   market-order slippage at the open, stop orders slipping through in fast markets, real commissions. Median 1R is ~30 points, so 1 point is ~0.03R on
   average but ~0.06R on P6 (median stop 16 points). Slippage beyond 1 point comes straight off the edge.
4. **Alert latency.** ENTRY alerts go out within seconds of the entry bar's first print (live feed); the backtest assumes you got the open price.
   Exit alerts arrive after the exit bar closes (at most 1 minute after the stop/target was touched). Place the stop and target as bracket orders at the broker
   when you enter; use the alerts for the breakeven stop move and as a safety net.
5. **Several positions at once.** The backtest runs the pillars independently (High-Precision peak: 5 simultaneous positions; mostly 1-2). The bot does not cap this.
   Total open risk is the sum of the open stops, which can exceed any single-trade risk limit.
6. **Sizing results in the blueprint** ($1,500 -> $2.57M in 2024, etc.) come from a sizing simulation that processes trades in entry order and credits each
   trade's full P&L to equity immediately, i.e. before earlier-entered trades have exited, and assumes 800-contract fills with $100/MNQ margin. The bot's sizing
   function is verified identical to that simulation, but the *achievable* compounding is a different question that has not been tested.
7. **Selection.** The thresholds come from an in-sample search across 804 variants (IS 2020-2023, OOS 2024+). The out-of-sample window is the same data the
   search looked at, so it is not a clean holdout. Only the forward record this bot produces is a clean test. EOD posts the running forward record next to the backtest.
8. **Calendar and gaps.** Only full sessions are traded (holidays and 13:00 closes skipped, as in the backtest). If the feed misses a minute, the bot forward-fills it like the
   reference and flags an entry that lands on a missing minute ("DATA GAP"). A missing 09:30 bar makes the bot stand down for the day.
9. **Trades after 2025-07-24.** Your saved ledgers include 49 trades after the public dataset ends; I could not reproduce those (no public data), but the logic that
   produced them is the logic verified here.
