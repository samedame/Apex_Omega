"""Single source of truth for strategy constants.

Every number here is copied from the reference backtest engine
(``stage10_apex_omega_v2.py``) and the High-Precision-Core filter in
``stage12_compounding_masterpiece.py``.  Do not tune anything here: the bot is
only valid while it matches the backtest that produced the published record.
"""
from __future__ import annotations

M_BARS = 390                  # RTH minutes 09:30..15:59 ET, index m = minutes since 09:30
RTH_START_HM = 570            # 09:30 ET in minutes since midnight
RTH_END_HM = 960              # 16:00 ET
MIN_BARS_VALID_DAY = 350      # a session counts as a full day only with >= 350 RTH 1m bars
SIGNAL_M_FIRST, SIGNAL_M_LAST = 5, 374   # `for m in range(5, 375)` in the reference loop

FRICTION_PTS = 1.0            # round-turn slippage + commission charged in NQ points
MIN_STOP_FLOOR = 8.0

# High-Precision Core (stage12): P2 only if stop <= 28 pts; every trade only if stop <= 68 pts
HP_P2_MAX_RISK = 28.0
HP_MAX_RISK = 68.0

POINT_VALUE_MNQ = 2.0
POINT_VALUE_NQ = 20.0

PILLARS = {
    "P1_OD10_Pullback":   dict(label="Opening Drive + EMA9 Pullback", target_r=4.0, be_trig=1.8, be_lock=0.25, exit_m=365),
    "P2_ORB_5m_Delta":    dict(label="5m Delta ORB",               target_r=6.0, be_trig=2.0, be_lock=0.30, exit_m=387),
    "P3_ORB_15m_Asym":    dict(label="15m Asymmetric ORB",         target_r=6.0, be_trig=2.0, be_lock=0.30, exit_m=387),
    "P4_ORB_20m_HTF":     dict(label="20m HTF-Aligned ORB",        target_r=6.0, be_trig=2.0, be_lock=0.30, exit_m=387),
    "P5_VWAP_3SD_Rev":    dict(label="VWAP +/-3 SD Reversal",      target_r=2.5, be_trig=None, be_lock=0.0, exit_m=None),  # exit_m = min(signal_m+75, 380)
    "P6_PM_NSH_Pullback": dict(label="PM New-High/Low Pullback",   target_r=4.5, be_trig=1.8, be_lock=0.25, exit_m=388),
}

# Two-gear sizing (stage12d_optimal_gear_shift.py, base risk 6.5%)
P_MULT = {
    "P1_OD10_Pullback": 1.30, "P2_ORB_5m_Delta": 0.90, "P3_ORB_15m_Asym": 1.05,
    "P4_ORB_20m_HTF": 1.10, "P5_VWAP_3SD_Rev": 0.95, "P6_PM_NSH_Pullback": 1.10,
}
GEAR1_CEIL = 2050.0
GEAR1_MAX_RISK_1MNQ = 104.0
BASE_RISK_PCT = 0.065
MAX_SINGLE_TRADE_RISK_PCT = 0.105
MARGIN_PER_MNQ = 100.0
MARGIN_FRACTION = 0.85
MAX_MNQ = 800
