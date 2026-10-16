import datetime as dt

import pandas as pd

import oracle as OR
from apex_omega.calendar_cme import is_full_session
from apex_omega.sizing import two_gear_contracts


def test_known_sessions():
    assert is_full_session("2026-10-16") and is_full_session("2024-07-05")
    for d in ("2024-07-03", "2024-07-04", "2026-11-26", "2026-11-27", "2026-12-24", "2026-12-25", "2027-01-01",
              "2027-03-26", "2025-01-09", "2022-06-20", "2021-12-24", "2026-10-17", "2026-10-18"):
        assert not is_full_session(d), d


def test_sizing_matches_original_function(tmp_path):
    rows = []
    for i, (risk, pil) in enumerate([(20, "P1_OD10_Pullback"), (60, "P6_PM_NSH_Pullback"), (30, "P2_ORB_5m_Delta"),
                                     (10, "P3_ORB_15m_Asym"), (55, "P4_ORB_20m_HTF"), (25, "P5_VWAP_3SD_Rev")] * 30):
        rows.append(dict(d=i, date=f"2024-01-{1 + i % 28:02d}", year=2024, pillar=pil, risk_pts=risk,
                         net_pts=(-1 - risk) if i % 3 else risk * 1.5))
    df = pd.DataFrame(rows); p = tmp_path / "hp.csv"; df.to_csv(p, index=False)
    f = OR.run_oracle_sizing(str(p))
    sim = f(df, start_equity=1500.0, base_risk_pct=0.065, gear1_ceil=2050.0, max_mnq_cap=800)
    eq, peak, out = 1500.0, 1500.0, []
    for r in df.itertuples():
        c = two_gear_contracts(eq, peak, r.risk_pts, r.pillar)
        out.append(c)
        pnl = c * r.net_pts * 2.0 + ((c // 10) * 10 * 0.76 if c >= 10 else 0)
        if eq >= 200.0:
            eq += pnl; peak = max(peak, eq)
    assert round(eq, 6) == round(float(sim.equity.iloc[-1]), 6)
