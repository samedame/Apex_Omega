import pandas as pd
import numpy as np
import glob
import time
import gc

t0 = time.time()
files = [f'/tmp/futures_data/NQ_1min_{y}.parquet' for y in range(2020, 2026)] + ['/tmp/futures_data/NQ_2026_1m.parquet']

# For each trading day in 2020-2026, we compute overnight Globex High/Low (18:00-09:29 ET)
# and store the full 1-minute RTH + London bars in a compact fast table.
day_summaries = []
rth_bars_list = []

for f in files:
    df = pd.read_parquet(f)
    ts_utc = pd.to_datetime(df['timestamp'], utc=True)
    ts_ny = ts_utc.dt.tz_convert('America/New_York')
    dow = ts_ny.dt.dayofweek
    mask = dow < 5
    df = df.loc[mask].copy()
    ts_ny = ts_ny.loc[mask]
    
    df['date_str'] = ts_ny.dt.strftime('%Y-%m-%d')
    df['year'] = ts_ny.dt.year.astype(np.int16)
    df['hm'] = (ts_ny.dt.hour * 60 + ts_ny.dt.minute).astype(np.int16)
    df['open'] = df['open'].astype(np.float32)
    df['high'] = df['high'].astype(np.float32)
    df['low'] = df['low'].astype(np.float32)
    df['close'] = df['close'].astype(np.float32)
    df['volume'] = df['volume'].astype(np.float32)
    
    # Keep 02:00 ET to 16:00 ET (hm 120 to 960) plus compute overnight 00:00-09:29 stats
    # Group by date_str
    on_mask = (df['hm'] >= 0) & (df['hm'] < 570)
    on_grp = df.loc[on_mask].groupby('date_str').agg(
        on_high=('high', 'max'),
        on_low=('low', 'min'),
        on_open=('open', 'first'),
        on_close=('close', 'last'),
        on_vol=('volume', 'sum')
    ).reset_index()
    
    asia_mask = (df['hm'] >= 0) & (df['hm'] < 120)
    asia_grp = df.loc[asia_mask].groupby('date_str').agg(
        asia_high=('high', 'max'),
        asia_low=('low', 'min')
    ).reset_index()
    
    rth_df = df.loc[(df['hm'] >= 120) & (df['hm'] < 960), ['date_str', 'year', 'hm', 'open', 'high', 'low', 'close', 'volume']].copy()
    rth_df = rth_df.merge(on_grp, on='date_str', how='left').merge(asia_grp, on='date_str', how='left')
    rth_bars_list.append(rth_df)
    
    del df, ts_utc, ts_ny
    gc.collect()

full_active = pd.concat(rth_bars_list, ignore_index=True)
full_active = full_active.drop_duplicates(subset=['date_str', 'hm']).sort_values(['date_str', 'hm']).reset_index(drop=True)
full_active.to_parquet('/tmp/futures_data/nq_2020_2026_active_1m.parquet')
print(f"Saved compact active 1m dataset: {len(full_active):,} rows across {full_active['date_str'].nunique():,} trading days in {time.time()-t0:.2f}s!")
