#!/usr/bin/env python3
"""Convert remaining duka raw files to our standard CSV format."""
import pandas as pd
import shutil
from pathlib import Path

raw_dir = Path("data/duka_raw")
out_dir = Path("data/historical")
bak_dir = Path("data/historical_backup")

todo = {
    "deuidxeur-m1-bid-2024-02-14-2026-02-13.csv": "DE40_M1.csv",
    "lightcmdusd-m1-bid-2024-02-14-2026-02-13.csv": "XTIUSD_M1.csv",
}

for raw_name, save_name in todo.items():
    raw = raw_dir / raw_name
    if not raw.exists():
        print(f"MISSING: {raw_name}")
        continue
    
    print(f"Reading {raw_name}...")
    df = pd.read_csv(raw)
    print(f"  Raw rows: {len(df):,} | Columns: {list(df.columns)}")
    print(f"  First row: {df.iloc[0].to_dict()}")
    
    df["time"] = pd.to_datetime(df["timestamp"], unit="ms")
    df["tick_volume"] = (df["volume"] * 1000).astype(int).clip(lower=1)
    df = df[["time", "open", "high", "low", "close", "tick_volume"]]
    df = df.sort_values("time").drop_duplicates(subset=["time"]).reset_index(drop=True)
    
    out = out_dir / save_name
    if out.exists():
        bak_dir.mkdir(parents=True, exist_ok=True)
        backup = bak_dir / save_name.replace(".csv", "_pre2year.csv")
        if not backup.exists():
            shutil.copy2(out, backup)
            print(f"  Backed up -> {backup.name}")
    
    df.to_csv(out, index=False)
    first = df["time"].iloc[0]
    last = df["time"].iloc[-1]
    days = (last - first).days
    print(f"  SAVED: {save_name} | {len(df):,} bars | {first} to {last} ({days}d = {days/365:.1f}y)")

# Verify all 4
print("\n=== FINAL VERIFICATION ===")
for f in ["US100_M1.csv", "DE40_M1.csv", "XTIUSD_M1.csv", "XBRUSD_M1.csv"]:
    p = out_dir / f
    if p.exists():
        df = pd.read_csv(p, parse_dates=["time"])
        days = (df["time"].iloc[-1] - df["time"].iloc[0]).days
        print(f"  {f:<18} {len(df):>10,} bars | {days:>4}d = {days/365:.1f}y | {df['time'].iloc[0].date()} to {df['time'].iloc[-1].date()}")
    else:
        print(f"  {f:<18} MISSING!")
