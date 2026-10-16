import numpy as np
import pandas as pd

hp = pd.read_csv('/home/user/futures_quant_lab/apex_omega_hp_trades.csv')

def simulate_optimal_gear_compounding(trades_df, start_equity=1500.0, base_risk_pct=0.065, gear1_ceil=2050.0, max_mnq_cap=800):
    eq = float(start_equity)
    peak_eq = float(start_equity)
    max_dd_pct = 0.0
    rows = []
    
    p_mult = {
        'P1_OD10_Pullback': 1.30,
        'P2_ORB_5m_Delta': 0.90,
        'P3_ORB_15m_Asym': 1.05,
        'P4_ORB_20m_HTF': 1.10,
        'P5_VWAP_3SD_Rev': 0.95,
        'P6_PM_NSH_Pullback': 1.10
    }
    
    for row in trades_df.itertuples():
        if eq < 200.0:
            rows.append((row.date, row.year, row.pillar, 0, 0.0, eq, max_dd_pct))
            continue
            
        risk_per_mnq = float(row.risk_pts) * 2.0
        
        # Gear 1 (Equity < gear1_ceil, e.g. $2,050): Strict 1-MNQ Survival Shield with 52-pt ($104) stop cap
        if eq < gear1_ceil:
            if risk_per_mnq > 104.0:
                continue
            contracts = 1
        else:
            # Gear 2 & 3 (Equity >= $2,050): House-Money Nearest-Integer Compounding with Drawdown Governor
            cur_dd = (peak_eq - eq) / peak_eq if peak_eq > 0 else 0.0
            rf = base_risk_pct * p_mult.get(row.pillar, 1.0)
            if cur_dd > 0.30:
                rf *= 0.55
            elif cur_dd > 0.18:
                rf *= 0.75
            dollar_risk = eq * rf
            contracts = int(np.round(dollar_risk / risk_per_mnq))
            if contracts < 1:
                contracts = 1
            while contracts > 1 and (contracts * risk_per_mnq) > (eq * 0.105):
                contracts -= 1
                
        max_m_contracts = int(np.floor((eq * 0.85) / 100.0))
        contracts = min(contracts, max_m_contracts, max_mnq_cap)
        contracts = max(contracts, 0)
        
        pnl = contracts * float(row.net_pts) * 2.0
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

unique_days = sorted(hp['d'].unique())
for rf in [0.055, 0.065, 0.075]:
    end_eqs, max_dds = [], []
    hit_100k = hit_50k = hit_25k = survived = 0
    for i in range(0, len(unique_days) - 252, 5):
        d0, d1 = unique_days[i], unique_days[i + 251]
        sub = hp[(hp['d'] >= d0) & (hp['d'] <= d1)]
        sim = simulate_optimal_gear_compounding(sub, start_equity=1500.0, base_risk_pct=rf, gear1_ceil=2050.0, max_mnq_cap=800)
        feq = sim['equity'].iloc[-1] if len(sim) > 0 else 1500.0
        mdd = sim['max_dd_pct'].max() * 100.0 if len(sim) > 0 else 0.0
        end_eqs.append(feq); max_dds.append(mdd)
        if feq >= 100000.0: hit_100k += 1
        if feq >= 50000.0: hit_50k += 1
        if feq >= 25000.0: hit_25k += 1
        if feq >= 1000.0: survived += 1
    arr_eq = np.array(end_eqs); arr_dd = np.array(max_dds)
    print(f"Risk {rf*100:4.1f}% | Survival(>$1k): {survived/len(arr_eq)*100:5.1f}% | Hit $25k+: {hit_25k/len(arr_eq)*100:5.1f}% | Hit $50k+: {hit_50k/len(arr_eq)*100:5.1f}% | Hit $100k+: {hit_100k/len(arr_eq)*100:5.1f}% | Median Eq: ${np.median(arr_eq):10,.0f} | 75th Pct: ${np.percentile(arr_eq, 75):11,.0f} | Mean Eq: ${np.mean(arr_eq):11,.0f} | Median DD: {np.median(arr_dd):4.1f}%")

print("\n=== OPTIMAL TWO-GEAR CALENDAR YEAR PERFORMANCE ($1,500 START EACH JAN 1, RISK=6.5%) ===")
for y in [2020, 2021, 2022, 2023, 2024, 2025]:
    sub = hp[hp['year'] == y]
    sim = simulate_optimal_gear_compounding(sub, start_equity=1500.0, base_risk_pct=0.065, gear1_ceil=2050.0, max_mnq_cap=800)
    feq = sim['equity'].iloc[-1]
    mdd = sim['max_dd_pct'].max() * 100.0
    mc = sim['mnq_contracts'].max()
    roi = ((feq - 1500.0) / 1500.0) * 100.0
    print(f"  Year {y} ({len(sim):3d} trades): $1,500 -> ${feq:12,.2f} ({roi:+9.1f}%) | Max DD: {mdd:5.1f}% | Peak Size: {mc:3d} MNQ ({mc/10:.1f} NQ)")
