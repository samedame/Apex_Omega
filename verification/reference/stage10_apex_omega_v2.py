import numpy as np
import pandas as pd
import time

data = np.load('/tmp/futures_data/nq_rth_tensors.npz', allow_pickle=True)
O, H, L, C, V = data['O'], data['H'], data['L'], data['C'], data['V']
VWAP, VWAP_STD = data['VWAP'], data['VWAP_STD']
EMA9, EMA21, EMA50 = data['EMA9'], data['EMA21'], data['EMA50']
ATR14, RSI5, RVOL, CLV, CUM_DELTA = data['ATR14'], data['RSI5'], data['RVOL'], data['CLV'], data['CUM_DELTA']
daily_sma20, daily_atr14 = data['daily_sma20'], data['daily_atr14']
dates, years = data['dates'], data['years']

N, M = C.shape
FRICTION_PTS = 1.0

def sim_trade_si(d, entry_m, direction, entry_px, stop_dist, target_dist, exit_m=388, be_trig_r=1.8, be_lock_r=0.25):
    stop_dist = max(float(stop_dist), 8.0)
    sl = entry_px - direction * stop_dist
    tp = entry_px + direction * target_dist
    highs, lows, closes = H[d], L[d], C[d]
    end_m = min(exit_m, M - 1)
    for m in range(entry_m, end_m):
        bh, bl = highs[m], lows[m]
        if direction == 1:
            if bl <= sl:
                net_pts = (sl - entry_px) - FRICTION_PTS
                return net_pts, net_pts / stop_dist, m
            if bh >= tp:
                net_pts = (tp - entry_px) - FRICTION_PTS
                return net_pts, net_pts / stop_dist, m
            if be_trig_r is not None and (bh - entry_px) >= be_trig_r * stop_dist:
                sl = max(sl, entry_px + be_lock_r * stop_dist)
        else:
            if bh >= sl:
                net_pts = (entry_px - sl) - FRICTION_PTS
                return net_pts, net_pts / stop_dist, m
            if bl <= tp:
                net_pts = (entry_px - tp) - FRICTION_PTS
                return net_pts, net_pts / stop_dist, m
            if be_trig_r is not None and (entry_px - bl) >= be_trig_r * stop_dist:
                sl = min(sl, entry_px - be_lock_r * stop_dist)
    net_pts = (closes[end_m] - entry_px) * direction - FRICTION_PTS
    return net_pts, net_pts / stop_dist, end_m

trades = []
for d in range(N):
    orb5_h, orb5_l = np.max(H[d, :5]), np.min(L[d, :5])
    orb5_rng = orb5_h - orb5_l
    orb15_h, orb15_l = np.max(H[d, :15]), np.min(L[d, :15])
    orb15_rng = orb15_h - orb15_l
    orb20_h, orb20_l = np.max(H[d, :20]), np.min(L[d, :20])
    orb20_rng = orb20_h - orb20_l
    
    drive10_pts = C[d, 10] - O[d, 0]
    has_drive10 = abs(drive10_pts) >= 1.8 * ATR14[d, 10]
    drive10_dir = 1 if drive10_pts > 0 else -1
    
    p1_waves = 0
    p1_free_m = 11
    p2_done = False
    p3_done = False
    p4_done = False
    p5_waves = 0
    p5_free_m = 240
    p6_done = False
    
    for m in range(5, 375):
        atr = max(ATR14[d, m], 4.0)
        min_sl = max(1.0 * atr, 10.0)
        max_sl = max(3.8 * atr, 35.0)
        
        # Pillar 1: 10m Opening Drive + EMA9 Institutional Pullback (Allows Wave 2 if Wave 1 won!)
        if p1_waves < 2 and has_drive10 and p1_free_m <= m <= 135:
            direction = 0
            if drive10_dir == 1 and L[d, m] <= EMA9[d, m] and C[d, m] > EMA9[d, m] and CLV[d, m] > 0.40 and EMA9[d, m] > EMA21[d, m]:
                direction = 1
                raw_sl = (O[d, m+1] - min(L[d, m], L[d, m-1])) + 0.75 * atr
            elif drive10_dir == -1 and H[d, m] >= EMA9[d, m] and C[d, m] < EMA9[d, m] and CLV[d, m] < -0.40 and EMA9[d, m] < EMA21[d, m]:
                direction = -1
                raw_sl = (max(H[d, m], H[d, m-1]) - O[d, m+1]) + 0.75 * atr
            if direction != 0 and m + 1 < M:
                stop_dist = np.clip(raw_sl, min_sl, max_sl)
                entry_px = O[d, m + 1]
                net_pts, net_r, ex_m = sim_trade_si(d, m + 1, direction, entry_px, stop_dist, stop_dist * 4.0, exit_m=365, be_trig_r=1.8, be_lock_r=0.25)
                trades.append((d, str(dates[d]), int(years[d]), m + 1, ex_m, direction, stop_dist, net_pts, net_r, 'P1_OD10_Pullback'))
                p1_waves += 1
                p1_free_m = ex_m + 3
                if net_r <= 0.5: # Only take a 2nd wave if Wave 1 was a solid winner
                    p1_waves = 2

        # Pillar 2: 5m Delta-Confirmed ORB
        if not p2_done and 5 <= m <= 45 and (1.2 * atr <= orb5_rng <= 0.45 * daily_atr14[d]):
            c_px = C[d, m]
            direction = 0
            if c_px > orb5_h and CLV[d, m] > 0.45 and c_px > VWAP[d, m] and CUM_DELTA[d, m] > 0:
                direction = 1
            elif c_px < orb5_l and CLV[d, m] < -0.45 and c_px < VWAP[d, m] and CUM_DELTA[d, m] < 0 and RVOL[d, m] > 1.30:
                direction = -1
            if direction != 0 and m + 1 < M:
                stop_dist = np.clip(orb5_rng * 0.75, min_sl, max_sl)
                entry_px = O[d, m + 1]
                net_pts, net_r, ex_m = sim_trade_si(d, m + 1, direction, entry_px, stop_dist, stop_dist * 6.0, exit_m=387, be_trig_r=2.0, be_lock_r=0.30)
                trades.append((d, str(dates[d]), int(years[d]), m + 1, ex_m, direction, stop_dist, net_pts, net_r, 'P2_ORB_5m_Delta'))
                p2_done = True

        # Pillar 3: 15m Asymmetric Regime ORB (Long-Bias / High-RVOL Short)
        if not p6_done and 15 <= m <= 90 and (1.2 * atr <= orb15_rng <= 0.55 * daily_atr14[d]):
            c_px = C[d, m]
            direction = 0
            if c_px > orb15_h and CLV[d, m] > 0.45 and c_px > VWAP[d, m]:
                direction = 1
            elif c_px < orb15_l and CLV[d, m] < -0.45 and O[d, 0] < daily_sma20[d] and RVOL[d, m] > 1.40:
                direction = -1
            if direction != 0 and m + 1 < M:
                stop_dist = np.clip(orb15_rng * 1.0, min_sl, max_sl * 1.2)
                entry_px = O[d, m + 1]
                net_pts, net_r, ex_m = sim_trade_si(d, m + 1, direction, entry_px, stop_dist, stop_dist * 6.0, exit_m=387, be_trig_r=2.0, be_lock_r=0.30)
                trades.append((d, str(dates[d]), int(years[d]), m + 1, ex_m, direction, stop_dist, net_pts, net_r, 'P3_ORB_15m_Asym'))
                p6_done = True

        # Pillar 4: 20m HTF-Aligned ORB
        if not p3_done and 20 <= m <= 95 and (1.2 * atr <= orb20_rng <= 0.55 * daily_atr14[d]):
            c_px = C[d, m]
            direction = 0
            if c_px > orb20_h and CLV[d, m] > 0.45 and O[d, 0] > daily_sma20[d]:
                direction = 1
            elif c_px < orb20_l and CLV[d, m] < -0.45 and O[d, 0] < daily_sma20[d]:
                direction = -1
            if direction != 0 and m + 1 < M:
                stop_dist = np.clip(orb20_rng * 0.75, min_sl, max_sl)
                entry_px = O[d, m + 1]
                net_pts, net_r, ex_m = sim_trade_si(d, m + 1, direction, entry_px, stop_dist, stop_dist * 6.0, exit_m=387, be_trig_r=2.0, be_lock_r=0.30)
                trades.append((d, str(dates[d]), int(years[d]), m + 1, ex_m, direction, stop_dist, net_pts, net_r, 'P4_ORB_20m_HTF'))
                p3_done = True

        # Pillar 5: VWAP +/- 3.0 SD Rubber-Band Exhaustion Reversal
        if not p4_done and 45 <= m <= 325 and abs(C[d, 30] - O[d, 0]) <= 2.8 * ATR14[d, 30]:
            up_b = VWAP[d, m] + 3.0 * VWAP_STD[d, m]
            dn_b = VWAP[d, m] - 3.0 * VWAP_STD[d, m]
            direction = 0
            if L[d, m] < dn_b and RSI5[d, m-1] < 20 and C[d, m] > O[d, m] and CLV[d, m] > 0.45 and O[d, 0] > daily_sma20[d]:
                direction = 1
                raw_sl = (O[d, m+1] - L[d, m]) + 0.8 * atr
            elif H[d, m] > up_b and RSI5[d, m-1] > 78 and C[d, m] < O[d, m] and CLV[d, m] < -0.45:
                direction = -1
                raw_sl = (H[d, m] - O[d, m+1]) + 0.8 * atr
            if direction != 0 and m + 1 < M:
                stop_dist = np.clip(raw_sl, min_sl, max_sl)
                entry_px = O[d, m + 1]
                net_pts, net_r, ex_m = sim_trade_si(d, m + 1, direction, entry_px, stop_dist, stop_dist * 2.5, exit_m=min(m + 75, 380), be_trig_r=None)
                trades.append((d, str(dates[d]), int(years[d]), m + 1, ex_m, direction, stop_dist, net_pts, net_r, 'P5_VWAP_3SD_Rev'))
                p4_done = True

        # Pillar 6: Afternoon New-Session-High/Low EMA9 Pullback Continuation
        if p5_waves < 1 and p5_free_m <= m <= 355:
            sess_h = np.max(H[d, :m-5])
            sess_l = np.min(L[d, :m-5])
            rec_h = np.max(H[d, m-10:m])
            rec_l = np.min(L[d, m-10:m])
            direction = 0
            if rec_h >= sess_h and L[d, m] <= EMA9[d, m] and C[d, m] > EMA9[d, m] and CLV[d, m] > 0.45 and EMA9[d, m] > EMA21[d, m] and C[d, m] > VWAP[d, m]:
                direction = 1
                raw_sl = (O[d, m+1] - min(L[d, m], L[d, m-1])) + 0.8 * atr
            elif rec_l <= sess_l and H[d, m] >= EMA9[d, m] and C[d, m] < EMA9[d, m] and CLV[d, m] < -0.45 and EMA9[d, m] < EMA21[d, m] and C[d, m] < VWAP[d, m]:
                direction = -1
                raw_sl = (max(H[d, m], H[d, m-1]) - O[d, m+1]) + 0.8 * atr
            if direction != 0 and m + 1 < 388:
                stop_dist = np.clip(raw_sl, min_sl, max_sl)
                entry_px = O[d, m + 1]
                net_pts, net_r, ex_m = sim_trade_si(d, m + 1, direction, entry_px, stop_dist, stop_dist * 4.5, exit_m=388, be_trig_r=1.8, be_lock_r=0.25)
                trades.append((d, str(dates[d]), int(years[d]), m + 1, ex_m, direction, stop_dist, net_pts, net_r, 'P6_PM_NSH_Pullback'))
                p5_waves += 1

df_v2 = pd.DataFrame(trades, columns=['d', 'date', 'year', 'entry_m', 'exit_m', 'dir', 'risk_pts', 'net_pts', 'net_r', 'pillar'])
w = df_v2['net_r'] > 0
pf = df_v2.loc[w, 'net_r'].sum() / abs(df_v2.loc[~w, 'net_r'].sum())
daily_r = df_v2.groupby('d')['net_r'].sum().reindex(range(N), fill_value=0.0).to_numpy()
cum_r = np.cumsum(daily_r)
max_dd = float(np.max(np.maximum.accumulate(np.maximum(0.0, cum_r)) - cum_r))
sharpe = (np.mean(daily_r) / np.std(daily_r)) * np.sqrt(252.0)
sortino = (np.mean(daily_r) / np.sqrt(np.mean(np.minimum(daily_r, 0.0)**2))) * np.sqrt(252.0)

print(f"=== APEX-OMEGA V2 (6-PILLAR MASTER ENGINE, 2020-2026) ===")
print(f"Total Trades: {len(df_v2):,} ({len(df_v2)/6.75:.1f}/yr) | WR: {w.mean()*100:.2f}% | Exp/Trade: {df_v2['net_r'].mean():+.3f}R | Exp/Day: {np.mean(daily_r):+.3f}R ({np.mean(daily_r)*252:+.1f}R/yr)")
print(f"Total Net R:  {df_v2['net_r'].sum():+.1f}R | Total Net Pts: {df_v2['net_pts'].sum():+,.1f} NQ pts (${df_v2['net_pts'].sum()*20:+,.0f} per 1 NQ!)")
print(f"Profit Factor: {pf:.3f} | MaxDD: {max_dd:.1f}R (Ret/DD: {df_v2['net_r'].sum()/max_dd:.2f}x) | Sharpe: {sharpe:.2f} | Sortino: {sortino:.2f}")

for p, grp in df_v2.groupby('pillar'):
    wp = grp['net_r'] > 0
    pfp = grp.loc[wp, 'net_r'].sum() / abs(grp.loc[~wp, 'net_r'].sum())
    is_g = grp[grp['year'] < 2024]
    oos_g = grp[grp['year'] >= 2024]
    is_pf = is_g.loc[is_g['net_r']>0, 'net_r'].sum() / abs(is_g.loc[is_g['net_r']<=0, 'net_r'].sum())
    oos_pf = oos_g.loc[oos_g['net_r']>0, 'net_r'].sum() / abs(oos_g.loc[oos_g['net_r']<=0, 'net_r'].sum())
    print(f"  {p:20s} | Trades: {len(grp):4d} | WR: {wp.mean()*100:5.2f}% | Exp: {grp['net_r'].mean():+6.3f}R | TotR: {grp['net_r'].sum():+6.1f}R | PF: {pfp:.3f} (IS: {is_pf:.3f}, OOS: {oos_pf:.3f})")

df_v2.to_csv('/home/user/futures_quant_lab/apex_omega_v2_master_trades.csv', index=False)
