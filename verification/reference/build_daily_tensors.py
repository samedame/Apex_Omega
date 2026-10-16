import pandas as pd
import numpy as np
import time

t0 = time.time()
df = pd.read_parquet('/tmp/futures_data/nq_2020_2026_active_1m.parquet')

# Separate RTH (hm 570..959 -> 09:30..15:59 ET, 390 mins) and London (hm 120..299 -> 02:00..04:59 ET, 180 mins)
rth = df[(df['hm'] >= 570) & (df['hm'] < 960)].copy()
rth['m_idx'] = rth['hm'] - 570

# Keep trading days with at least 350 RTH 1-minute bars (excludes half-day holidays/missing data days)
day_counts = rth.groupby('date_str')['m_idx'].count()
valid_days = sorted(day_counts[day_counts >= 350].index.tolist())
print(f"Found {len(valid_days)} full RTH trading days (2020-2026)")

rth = rth[rth['date_str'].isin(valid_days)].copy()
day_to_idx = {d: i for i, d in enumerate(valid_days)}
rth['d_idx'] = rth['date_str'].map(day_to_idx)

N = len(valid_days)
M = 390

O = np.full((N, M), np.nan, dtype=np.float32)
H = np.full((N, M), np.nan, dtype=np.float32)
L = np.full((N, M), np.nan, dtype=np.float32)
C = np.full((N, M), np.nan, dtype=np.float32)
V = np.zeros((N, M), dtype=np.float32)

d_idx = rth['d_idx'].to_numpy()
m_idx = rth['m_idx'].to_numpy()

O[d_idx, m_idx] = rth['open'].to_numpy()
H[d_idx, m_idx] = rth['high'].to_numpy()
L[d_idx, m_idx] = rth['low'].to_numpy()
C[d_idx, m_idx] = rth['close'].to_numpy()
V[d_idx, m_idx] = rth['volume'].to_numpy()

# Forward-fill any isolated missing minute inside a day
for d in range(N):
    for arr in (O, H, L, C):
        row = arr[d]
        mask = np.isnan(row)
        if np.any(mask):
            idx = np.where(~mask, np.arange(M), 0)
            np.maximum.accumulate(idx, out=idx)
            first_valid = np.where(~mask)[0][0]
            row[:first_valid] = row[first_valid]
            row[:] = row[idx]

# Extract overnight stats per valid day
day_meta = rth.groupby('d_idx').first()[['date_str', 'year', 'on_high', 'on_low', 'on_open', 'on_close', 'asia_high', 'asia_low']].reset_index()

# Also extract London session tensor (hm 120..299 -> 180 mins)
lon = df[(df['hm'] >= 120) & (df['hm'] < 300) & (df['date_str'].isin(valid_days))].copy()
lon['d_idx'] = lon['date_str'].map(day_to_idx)
lon['m_idx'] = lon['hm'] - 120

LO = np.full((N, 180), np.nan, dtype=np.float32)
LH = np.full((N, 180), np.nan, dtype=np.float32)
LL = np.full((N, 180), np.nan, dtype=np.float32)
LC = np.full((N, 180), np.nan, dtype=np.float32)
LV = np.zeros((N, 180), dtype=np.float32)

LO[lon['d_idx'].to_numpy(), lon['m_idx'].to_numpy()] = lon['open'].to_numpy()
LH[lon['d_idx'].to_numpy(), lon['m_idx'].to_numpy()] = lon['high'].to_numpy()
LL[lon['d_idx'].to_numpy(), lon['m_idx'].to_numpy()] = lon['low'].to_numpy()
LC[lon['d_idx'].to_numpy(), lon['m_idx'].to_numpy()] = lon['close'].to_numpy()
LV[lon['d_idx'].to_numpy(), lon['m_idx'].to_numpy()] = lon['volume'].to_numpy()

# Precompute causal intraday RTH indicators (N, 390)
typ = (H + L + C) / 3.0
cum_pv = np.cumsum(typ * V, axis=1)
cum_v = np.maximum(np.cumsum(V, axis=1), 1.0)
VWAP = (cum_pv / cum_v).astype(np.float32)

# Causal Session VWAP standard deviation (volume-weighted variance)
cum_p2v = np.cumsum((typ ** 2) * V, axis=1)
vwap_var = np.maximum((cum_p2v / cum_v) - (VWAP ** 2), 0.25)
VWAP_STD = np.sqrt(vwap_var).astype(np.float32)

# Causal EMAs, ATR14, RSI5, Volume MA20
EMA9 = np.zeros_like(C)
EMA21 = np.zeros_like(C)
EMA50 = np.zeros_like(C)
ATR14 = np.zeros_like(C)
RSI5 = np.full_like(C, 50.0)
VOL_MA20 = np.zeros_like(V)
BB_WIDTH = np.zeros_like(C)
SQUEEZE_ON = np.zeros((N, M), dtype=np.bool_)

TR = np.maximum(H - L, np.maximum(np.abs(H - np.roll(C, 1, axis=1)), np.abs(L - np.roll(C, 1, axis=1))))
TR[:, 0] = H[:, 0] - L[:, 0]

EMA9[:, 0] = C[:, 0]
EMA21[:, 0] = C[:, 0]
EMA50[:, 0] = C[:, 0]
ATR14[:, 0] = TR[:, 0]

a9 = 2.0 / 10.0
a21 = 2.0 / 22.0
a50 = 2.0 / 51.0
a_atr = 1.0 / 14.0
a_rsi = 1.0 / 5.0

avg_gain = np.zeros(N, dtype=np.float32)
avg_loss = np.full(N, 0.001, dtype=np.float32)

for m in range(1, M):
    EMA9[:, m] = a9 * C[:, m] + (1 - a9) * EMA9[:, m-1]
    EMA21[:, m] = a21 * C[:, m] + (1 - a21) * EMA21[:, m-1]
    EMA50[:, m] = a50 * C[:, m] + (1 - a50) * EMA50[:, m-1]
    ATR14[:, m] = a_atr * TR[:, m] + (1 - a_atr) * ATR14[:, m-1]
    
    diff = C[:, m] - C[:, m-1]
    gain = np.maximum(diff, 0.0)
    loss = np.maximum(-diff, 0.0)
    avg_gain = a_rsi * gain + (1 - a_rsi) * avg_gain
    avg_loss = a_rsi * loss + (1 - a_rsi) * avg_loss
    rs = avg_gain / np.maximum(avg_loss, 1e-6)
    RSI5[:, m] = 100.0 - (100.0 / (1.0 + rs))
    
    w0 = max(0, m - 19)
    VOL_MA20[:, m] = np.mean(V[:, w0:m+1], axis=1)
    if m >= 19:
        std20 = np.std(C[:, m-19:m+1], axis=1)
        BB_WIDTH[:, m] = 4.0 * std20
        SQUEEZE_ON[:, m] = (2.0 * std20) < (1.5 * ATR14[:, m])

VOL_MA20[:, 0] = np.maximum(V[:, 0], 1.0)
RVOL = V / np.maximum(VOL_MA20, 1.0)
CLV = np.where((H - L) > 0, (2.0 * C - H - L) / (H - L), 0.0).astype(np.float32)
DELTA_PROXY = CLV * V
CUM_DELTA = np.cumsum(DELTA_PROXY, axis=1).astype(np.float32)

# Daily prior-day features (strictly causal: uses day d-1 and earlier)
rth_day_high = np.max(H, axis=1)
rth_day_low = np.min(L, axis=1)
rth_day_close = C[:, -1]
rth_day_open = O[:, 0]
rth_day_range = rth_day_high - rth_day_low

prev_close = np.roll(rth_day_close, 1); prev_close[0] = rth_day_open[0]
prev_high = np.roll(rth_day_high, 1); prev_high[0] = rth_day_high[0]
prev_low = np.roll(rth_day_low, 1); prev_low[0] = rth_day_low[0]
prev_range = np.roll(rth_day_range, 1); prev_range[0] = rth_day_range[0]

# Daily SMA20 and Daily ATR14 (strictly prior days d-1..d-20)
daily_sma20 = np.zeros(N, dtype=np.float32)
daily_atr14 = np.zeros(N, dtype=np.float32)
nr4_prev = np.zeros(N, dtype=np.bool_)
nr7_prev = np.zeros(N, dtype=np.bool_)

for d in range(N):
    if d == 0:
        daily_sma20[d] = rth_day_open[d]
        daily_atr14[d] = 150.0
    else:
        s20 = max(0, d - 20)
        s14 = max(0, d - 14)
        daily_sma20[d] = np.mean(rth_day_close[s20:d])
        daily_atr14[d] = np.mean(rth_day_range[s14:d])
        if d >= 4:
            nr4_prev[d] = rth_day_range[d-1] == np.min(rth_day_range[d-4:d])
        if d >= 7:
            nr7_prev[d] = rth_day_range[d-1] == np.min(rth_day_range[d-7:d])

np.savez_compressed(
    '/tmp/futures_data/nq_rth_tensors.npz',
    O=O, H=H, L=L, C=C, V=V,
    LO=LO, LH=LH, LL=LL, LC=LC, LV=LV,
    VWAP=VWAP, VWAP_STD=VWAP_STD,
    EMA9=EMA9, EMA21=EMA21, EMA50=EMA50,
    ATR14=ATR14, RSI5=RSI5, RVOL=RVOL, CLV=CLV, CUM_DELTA=CUM_DELTA,
    BB_WIDTH=BB_WIDTH, SQUEEZE_ON=SQUEEZE_ON,
    prev_close=prev_close, prev_high=prev_high, prev_low=prev_low, prev_range=prev_range,
    daily_sma20=daily_sma20, daily_atr14=daily_atr14, nr4_prev=nr4_prev, nr7_prev=nr7_prev,
    dates=day_meta['date_str'].to_numpy(),
    years=day_meta['year'].to_numpy(dtype=np.int16),
    on_high=day_meta['on_high'].fillna(pd.Series(rth_day_open)).to_numpy(dtype=np.float32),
    on_low=day_meta['on_low'].fillna(pd.Series(rth_day_open)).to_numpy(dtype=np.float32),
    asia_high=day_meta['asia_high'].fillna(pd.Series(rth_day_open)).to_numpy(dtype=np.float32),
    asia_low=day_meta['asia_low'].fillna(pd.Series(rth_day_open)).to_numpy(dtype=np.float32)
)
print(f"Built and saved full 1-minute tensors for {N} trading days ({N*390:,} RTH 1m bars + {N*180:,} London 1m bars) in {time.time()-t0:.2f}s!")
