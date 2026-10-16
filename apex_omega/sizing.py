"""Two-gear MNQ position sizing (``stage12d_optimal_gear_shift.py``, base risk 6.5%).

Used only to print a *suggested* size in alerts when an account equity is configured.
Signals and exits do not depend on it.
"""
from __future__ import annotations

import numpy as np

from . import params as P


def two_gear_contracts(equity: float, peak_equity: float, risk_pts: float, pillar: str,
                       base_risk_pct: float = P.BASE_RISK_PCT, gear1_ceil: float = P.GEAR1_CEIL,
                       max_mnq: int = P.MAX_MNQ) -> int:
    eq = float(equity)
    if eq < 200.0:
        return 0
    risk_per_mnq = float(risk_pts) * P.POINT_VALUE_MNQ
    if eq < gear1_ceil:
        if risk_per_mnq > P.GEAR1_MAX_RISK_1MNQ:
            return 0                                    # Gear-1 shield: skip wide stops
        contracts = 1
    else:
        cur_dd = (peak_equity - eq) / peak_equity if peak_equity > 0 else 0.0
        rf = base_risk_pct * P.P_MULT.get(pillar, 1.0)
        if cur_dd > 0.30:
            rf *= 0.55
        elif cur_dd > 0.18:
            rf *= 0.75
        contracts = int(np.round(eq * rf / risk_per_mnq))
        if contracts < 1:
            contracts = 1
        while contracts > 1 and (contracts * risk_per_mnq) > (eq * P.MAX_SINGLE_TRADE_RISK_PCT):
            contracts -= 1
    max_m = int(np.floor((eq * P.MARGIN_FRACTION) / P.MARGIN_PER_MNQ))
    contracts = min(contracts, max_m, max_mnq)
    return max(contracts, 0)
