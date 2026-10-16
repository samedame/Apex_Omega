import numpy as np
import pandas as pd

df_v2 = pd.read_csv('/home/user/futures_quant_lab/apex_omega_v2_master_trades.csv')
hp = df_v2[(df_v2['pillar'] != 'P2_ORB_5m_Delta') | (df_v2['risk_pts'] <= 28.0)].copy()
hp = hp[hp['risk_pts'] <= 68.0].sort_values(['d', 'entry_m']).reset_index(drop=True)
hp.to_csv('/home/user/futures_quant_lab/apex_omega_hp_trades.csv', index=False)

def simulate_smart_compounding(trades_df, start_equity=1500.0, base_risk_pct=0.055, max_mnq_cap=600):
    """
    Implements the 3-Phase Small-Account-to-Institutional Compounding Protocol:
    - Phase 1 (Survival & Ignition, Equity < $3,500):
      Skip wide-stop trades where 1 MNQ contract would risk > 4.5% of current equity (i.e. risk_pts * $2 > eq * 0.045),
      UNLESS it's our #1 highest-PF setup (P1_OD10_Pullback) with risk_pts <= 40.
    - Phase 2 (Micro Acceleration, $3,500 <= Equity < $25,000):
      Full 6-pillar execution using integer MNQ contracts sized at base_risk_pct of equity,
      with a Drawdown Governor (scales risk down by 35% if drawdown from peak > 18%).
    - Phase 3 (E-Mini Institutional Scaling, Equity >= $25,000):
      Transitions integer blocks of 10 MNQ into 1 standard NQ ($20/pt) + remainder MNQ,
      with Asymmetric House-Money Ratchet.
    """
    eq = float(start_equity)
    peak_eq = float(start_equity)
    max_dd_pct = 0.0
    rows = []
    
    p_mult = {
        'P1_OD10_Pullback': 1.25,
        'P2_ORB_5m_Delta': 0.90,
        'P3_ORB_15m_Asym': 1.00,
        'P4_ORB_20m_HTF': 1.05,
        'P5_VWAP_3SD_Rev': 0.95,
        'P6_PM_NSH_Pullback': 1.10
    }
    
    for row in trades_df.itertuples():
        if eq < 200.0:
            rows.append((row.date, row.year, row.pillar, 0, 0.0, eq, max_dd_pct))
            continue
            
        risk_per_mnq = float(row.risk_pts) * 2.0
        
        # Phase 1 Small-Account Stop Shield:
        # When equity < $3,200, never allow a single 1-MNQ trade whose stop loss exceeds 4.8% of current account!
        if eq < 3200.0:
            max_allowed_1mnq_risk = max(eq * 0.048, 56.0) # at $1500, max $72 risk = 36 NQ pts
            if row.pillar == 'P1_OD10_Pullback':
                max_allowed_1mnq_risk = max(eq * 0.055, 76.0)
            if risk_per_mnq > max_allowed_1mnq_risk:
                continue
                
        cur_dd = (peak_eq - eq) / peak_eq if peak_eq > 0 else 0.0
        rf = base_risk_pct * p_mult.get(row.pillar, 1.0)
        if cur_dd > 0.25:
            rf *= 0.55
        elif cur_dd > 0.15:
            rf *= 0.75
            
        dollar_risk = eq * rf
        contracts = int(np.floor(dollar_risk / risk_per_mnq))
        if contracts < 1 and eq >= 300.0:
            contracts = 1
            
        # Margin cap ($100 day-trade margin per MNQ, max 85% of equity)
        max_m_contracts = int(np.floor((eq * 0.85) / 100.0))
        contracts = min(contracts, max_m_contracts, max_mnq_cap)
        contracts = max(contracts, 0)
        
        pnl = contracts * float(row.net_pts) * 2.0
        # If >= 10 MNQ, routing via standard NQ saves $0.76 per MNQ equivalent in round-turn commissions (~0.38 pts)
        if contracts >= 10:
            nq_lots = contracts // 10
            pnl += nq_lots * 10 * 0.76
            
        eq += pnl
        if eq > peak_eq:
            peak_eq = eq
        dd = (peak_eq - eq) / peak_eq if peak_eq > 0 else 0.0
        if dd > max_dd_pct:
            max_dd_pct = dd
            
        rows.append((row.date, row.year, row.pillar, contracts, pnl, eq, max_dd_pct))
        
    return pd.DataFrame(rows, columns=['date', 'year', 'pillar', 'mnq_contracts', 'pnl', 'equity', 'max_dd_pct'])

print("=== ROLLING 1-YEAR (252 TRADING DAYS) COMPACT SHIELDED COMPOUNDING FROM $1,500 ===")
unique_days = sorted(hp['d'].unique())
for rf in [0.035, 0.045, 0.055, 0.065]:
    end_eqs = []
    max_dds = []
    hit_100k = 0
    hit_25k = 0
    survived = 0
    for i in range(0, len(unique_days) - 252, 5): # Every week rolling start date!
        d0 = unique_days[i]
        d1 = unique_days[i + 251]
        sub = hp[(hp['d'] >= d0) & (hp['d'] <= d1)]
        sim = simulate_smart_compounding(sub, start_equity=1500.0, base_risk_pct=rf, max_mnq_cap=600)
        feq = sim['equity'].iloc[-1] if len(sim) > 0 else 1500.0
        mdd = sim['max_dd_pct'].max() * 100.0 if len(sim) > 0 else 0.0
        end_eqs.append(feq)
        max_dds.append(mdd)
        if feq >= 100000.0: hit_100k += 1
        if feq >= 25000.0: hit_25k += 1
        if feq >= 1000.0: survived += 1
    arr_eq = np.array(end_eqs)
    arr_dd = np.array(max_dds)
    print(f"Risk {rf*100:4.1f}% | Windows: {len(arr_eq)} | Survival(>$1k): {survived/len(arr_eq)*100:5.1f}% | Hit $25k+: {hit_25k/len(arr_eq)*100:5.1f}% | Hit $100k+: {hit_100k/len(arr_eq)*100:5.1f}% | Median Eq: ${np.median(arr_eq):10,.0f} | 75th Pct: ${np.percentile(arr_eq, 75):11,.0f} | Mean Eq: ${np.mean(arr_eq):11,.0f} | Median DD: {np.median(arr_dd):4.1f}%")

print("\n=== CALENDAR YEAR PERFORMANCE WITH PHASE-1 SMALL ACCOUNT SHIELD ($1,500 START EACH JAN 1) ===")
for rf in [0.045, 0.055]:
    print(f"\n--- Base Risk {rf*100:.1f}% ---")
    for y in [2020, 2021, 2022, 2023, 2024, 2025]:
        sub = hp[hp['year'] == y]
        sim = simulate_smart_compounding(sub, start_equity=1500.0, base_risk_pct=rf, max_mnq_cap=600)
        feq = sim['equity'].iloc[-1]
        mdd = sim['max_dd_pct'].max() * 100.0
        mc = sim['mnq_contracts'].max()
        roi = ((feq - 1500.0) / 1500.0) * 100.0
        print(f"  Year {y} ({len(sim):3d} executed trades): $1,500 -> ${feq:12,.2f} ({roi:+9.1f}%) | Max DD: {mdd:5.1f}% | Peak Size: {mc:3d} MNQ ({mc/10:.1f} NQ)")
