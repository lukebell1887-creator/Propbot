import pandas as pd, sys, os
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

raw_dir = "data/duka_raw"
out_dir = "data/historical"

todo = {
    "deuidxeur_full.csv": "DE40_M1.csv",
    "lightcmdusd_full.csv": "XTIUSD_M1.csv",
}

for raw_name, save_name in todo.items():
    raw_path = os.path.join(raw_dir, raw_name)
    out_path = os.path.join(out_dir, save_name)
    
    raw_size = os.path.getsize(raw_path) if os.path.exists(raw_path) else 0
    print(f"\n{raw_name}: {raw_size/1024/1024:.1f} MB on disk")
    
    if raw_size < 1_000_000:
        print(f"  SKIPPING — file too small, probably still downloading")
        continue
    
    print(f"  Reading...")
    df = pd.read_csv(raw_path)
    print(f"  Raw rows: {len(df):,}")
    
    df['time'] = pd.to_datetime(df['timestamp'], unit='ms')
    df['tick_volume'] = (df['volume'] * 1000).astype(int).clip(lower=1)
    df = df[['time','open','high','low','close','tick_volume']]
    df = df.sort_values('time').drop_duplicates(subset=['time']).reset_index(drop=True)
    
    df.to_csv(out_path, index=False)
    
    out_size = os.path.getsize(out_path)
    first = df['time'].iloc[0]
    last = df['time'].iloc[-1]
    days = (last - first).days
    print(f"  SAVED: {out_path} | {len(df):,} bars | {out_size/1024/1024:.1f} MB")
    print(f"  Range: {first} to {last} ({days}d = {days/365:.1f}y)")

# Final check all 4
print("\n=== ALL 4 FILES ===")
for f in ["US100_M1.csv", "DE40_M1.csv", "XTIUSD_M1.csv", "XBRUSD_M1.csv"]:
    p = os.path.join(out_dir, f)
    sz = os.path.getsize(p) if os.path.exists(p) else 0
    print(f"  {f:<18} {sz/1024/1024:>6.1f} MB")
