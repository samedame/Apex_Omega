# APEX-OMEGA: the exact rules the bot implements

Source of truth: the backtest engine `stage10_apex_omega_v2.py` (every trade in the published ledgers came
from it) plus the High-Precision-Core filter from `stage12_compounding_masterpiece.py`. Where the blueprint prose
or the TradingView Pine script says something different, **the engine wins** (list at the bottom).
The code is `apex_omega/engine.py`; every line was translated from the reference and is checked against it bit-for-bit.

## Clock and data
* Instrument NQ (alerts quote NQ points; 1 point = $2 per MNQ, $20 per NQ). New-York time. RTH = 09:30-15:59, bar index
  `m` = minutes since 09:30 (`m = 0..389`).
* A session is traded only if it is a full session (no holiday, no 13:00 early close). The backtest contained only sessions
  with >= 350 RTH bars; `apex_omega/calendar_cme.py` reproduces that rule for every weekday of 2020-2025.
* Missing minutes are forward-filled exactly like the reference tensor builder (O/H/L/C copied from the previous bar, volume 0).
  A missing 09:30 bar: the bot **stands down** for the day (the reference silently back-fills it, which needs future data).
* Indicators are causal and computed in float32 like the reference: session VWAP and volume-weighted sigma (typical price
  (H+L+C)/3), EMA9/EMA21 (alpha 2/10, 2/22, seeded with bar 0's close), Wilder ATR14 (alpha 1/14), RSI5 (alpha 1/5),
  RVOL = volume / mean(last 20 volumes incl. current), CLV = (2C-H-L)/(H-L), cumulative delta proxy = running sum of CLV x volume.
  All reset at 09:30. Daily features use **prior** full sessions only: SMA20 of RTH closes and the mean of the last 14 RTH ranges.
* `atr = max(ATR14[m], 4)`, `min_sl = max(atr, 10)`, `max_sl = max(3.8 atr, 35)`.

## Execution convention (applies to every pillar)
Signal is evaluated on the **close of bar m** (m from 5 to 374). The trade is entered at the **open of bar m+1**
(`entry`). Stops are sized from that open for P1/P5/P6. `R` = the stop distance. Friction of **1.0 point** per round trip is charged.

Management, evaluated bar by bar starting with the entry bar itself, **stop first, then target, then the breakeven ratchet**:
1. bar low <= stop (long) -> exit at the stop price; 2. bar high >= target -> exit at the target price;
3. if the bar's excursion reached `be_trig` x R, the stop moves to `entry + be_lock x R` **from the next bar on**;
4. on bar `end_m` the position is closed at that bar's **close** (its own stop/target are not consulted).

| Pillar | Window (signal bars m) | Entry condition (long; short mirrors) | Stop (R) | Target | Ratchet | Time exit (bar close) |
|---|---|---|---|---|---|---|
| P1 Opening-drive pullback | 11-135 | `|C10 - O0| >= 1.8 ATR14[10]`; drive up; `L <= EMA9`, `C > EMA9`, CLV > 0.40, EMA9 > EMA21. Wave 2 only if wave 1 closed > +0.5R, re-armed 3 bars after exit | `clip((O[m+1] - min(L[m],L[m-1])) + 0.75 atr, min_sl, max_sl)` | 4.0R | +1.8R -> +0.25R | bar 365 (15:36) |
| P2 5m delta ORB | 5-45 | ORB5 range between 1.2 atr and 0.45 daily ATR; `C > ORB5 high`, CLV > 0.45, `C > VWAP`, cum-delta > 0. Short: below ORB low, CLV < -0.45, `C < VWAP`, cum-delta < 0, RVOL > 1.30 | `clip(0.75 ORB5, min_sl, max_sl)` | 6.0R | +2.0R -> +0.30R | bar 387 (15:58) |
| P3 15m asymmetric ORB | 15-90 | ORB15 range between 1.2 atr and 0.55 daily ATR; long: `C > ORB15 high`, CLV > 0.45, `C > VWAP`. Short: `C < ORB15 low`, CLV < -0.45, RTH open < SMA20, RVOL > 1.40 | `clip(ORB15, min_sl, 1.2 max_sl)` | 6.0R | +2.0R -> +0.30R | bar 387 |
| P4 20m HTF ORB | 20-95 | ORB20 range between 1.2 atr and 0.55 daily ATR; long: `C > ORB20 high`, CLV > 0.45, open > SMA20; short: `C < ORB20 low`, CLV < -0.45, open < SMA20 | `clip(0.75 ORB20, min_sl, max_sl)` | 6.0R | +2.0R -> +0.30R | bar 387 |
| P5 VWAP +/-3 SD reversal | 45-325 | `|C30 - O0| <= 2.8 ATR14[30]`; long: `L < VWAP - 3 sigma`, RSI5[m-1] < 20, green bar, CLV > 0.45, open > SMA20. Short: `H > VWAP + 3 sigma`, RSI5[m-1] > 78, red bar, CLV < -0.45 | `clip((O[m+1] - L[m]) + 0.8 atr, min_sl, max_sl)` (short: `(H[m] - O[m+1]) + 0.8 atr`) | 2.5R | none | signal bar + 75 (max 380) |
| P6 PM new-extreme pullback | 240-355 | a new session extreme printed in the last 10 bars (window m-10..m-1 vs bars 0..m-6); `L <= EMA9`, `C > EMA9`, CLV > 0.45, EMA9 > EMA21, `C > VWAP` | `clip((O[m+1] - min(L[m],L[m-1])) + 0.8 atr, min_sl, max_sl)` | 4.5R | +1.8R -> +0.25R | bar 388 (15:59) |

Each pillar fires at most once per day (P1: up to two waves, P6: once). Pillars are independent: several can be open at
once (the High-Precision backtest peaked at 5 simultaneous positions; 1 in 704 days... see report).

## High-Precision Core (default, `HIGH_PRECISION=1`)
Alerts are sent only for trades with stop <= 68 points, and for P2 only with stop <= 28 points. Filtered trades are still
simulated silently, because they can influence later state (for example a filtered P1 wave-1 result decides whether wave 2 is allowed).
**Wide-stop trades are skipped, not capped** (same as stage12).

## Suggested size (optional, only if `ACCOUNT_EQUITY` is set)
Two-gear sizing from `stage12d`: equity < $2,050 -> 1 MNQ and skip stops > 52 pts; otherwise round(equity x 6.5% x pillar weight
x drawdown multiplier / (stop x $2)) MNQ, never risking more than 10.5% of equity, 85% margin cap at $100/MNQ, 800 MNQ cap.
This is advice text in the alert; it never affects signals or exits.

## Where the blueprint prose / Pine script differ from the engine (the bot follows the engine)
* Blueprint omits the ORB range filters of P3/P4 (`1.2 atr <= range <= 0.55 daily ATR`) and P3's 1.2 x max_sl stop cap; the engine has them.
* Blueprint's `SL_max` formula includes a 68-point cap; in the engine it is a post-hoc **filter** (skip), not a clip.
* Time exits: the engine uses bar 365 for P1 (15:36) and a 75-minute max hold for P5; the blueprint only mentions 15:58.
* The Pine script is **not** equivalent: it enters at the signal bar's close, allows one position at a time, has no P1 wave 2,
  clips the stop at 68, uses TradingView's un-reset ATR/EMA/RSI and real daily bars for the SMA20/ATR. Its results are not the published ones.
