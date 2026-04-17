import pandas as pd

raw = 'data/duka_raw/xagusd-m1-bid-2024-02-19-2026-02-18.csv'
df = pd.read_csv(raw)
print(f'Raw: {len(df):,} rows')

df['time'] = pd.to_datetime(df['timestamp'], unit='ms')
df['tick_volume'] = (df['volume']*1000).astype(int).clip(lower=1)
out = df[['time','open','high','low','close','tick_volume']]
out = out.sort_values('time').drop_duplicates('time').reset_index(drop=True)

days = (out['time'].iloc[-1] - out['time'].iloc[0]).days
print(f'Clean: {len(out):,} bars | {out.time.iloc[0]} to {out.time.iloc[-1]} ({days}d)')

# Write to absolute path
target = r'c:\Users\lukeb\OneDrive\Desktop\PropBot\data\historical\XAGUSD_M1.csv'
out.to_csv(target, index=False)
print(f'Written to {target}')

# Verify immediately
v = pd.read_csv(target)
print(f'VERIFY: {len(v):,} bars, first={v.iloc[0]["time"]}')
