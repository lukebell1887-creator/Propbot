#!/usr/bin/env python3
"""
Download 2 years of M1 data from Dukascopy using dukascopy-node.
Converts to our standard CSV format: time,open,high,low,close,tick_volume
"""
import subprocess, os, sys, shutil
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

DATA_DIR = Path("data/historical")
BACKUP_DIR = Path("data/historical_backup")
TEMP_DIR = Path("data/duka_raw")

# Dukascopy symbol -> our CSV name
SYMBOLS = {
    "usatechidxusd": "US100",   # Nasdaq 100
    "deuidxeur": "DE40",        # DAX 40
    "lightcmdusd": "XTIUSD",   # WTI Crude Oil
    "brentcmdusd": "XBRUSD",   # Brent Crude Oil
}

YEARS_BACK = 2

def download_and_convert(duka_sym, save_name):
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=YEARS_BACK * 365)).strftime("%Y-%m-%d")
    
    print(f"\n  {duka_sym} -> {save_name}_M1.csv")
    print(f"    Range: {start_date} to {end_date}")
    
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    
    # Download from Dukascopy
    cmd = [
        "npx", "dukascopy-node",
        "-i", duka_sym,
        "-from", start_date,
        "-to", end_date,
        "-t", "m1",
        "-f", "csv",
        "-v", "true",
        "-dir", str(TEMP_DIR.resolve()),
    ]
    
    print(f"    Downloading... (this may take a few minutes)")
    result = subprocess.run(cmd, capture_output=True, text=True, shell=True)
    
    if result.returncode != 0:
        print(f"    ERROR: {result.stderr[:200]}")
        return None
    
    # Find the downloaded file
    files = list(TEMP_DIR.glob(f"{duka_sym}*.csv"))
    if not files:
        print(f"    ERROR: No file found after download")
        print(f"    stdout: {result.stdout[:300]}")
        return None
    
    raw_file = files[0]
    print(f"    Downloaded: {raw_file.name} ({raw_file.stat().st_size / 1024 / 1024:.1f} MB)")
    
    # Convert format: epoch ms -> datetime, volume -> tick_volume
    df = pd.read_csv(raw_file)
    df['time'] = pd.to_datetime(df['timestamp'], unit='ms')
    df['tick_volume'] = (df['volume'] * 1000).astype(int).clip(lower=1)  # Scale volumes
    df = df[['time', 'open', 'high', 'low', 'close', 'tick_volume']]
    df = df.sort_values('time').drop_duplicates(subset=['time']).reset_index(drop=True)
    
    # Backup existing
    out_path = DATA_DIR / f"{save_name}_M1.csv"
    if out_path.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"{save_name}_M1_pre2year.csv"
        shutil.copy2(out_path, backup)
        print(f"    Backed up existing -> {backup.name}")
    
    # Save
    df.to_csv(out_path, index=False)
    
    first = df['time'].iloc[0]
    last = df['time'].iloc[-1]
    days = (last - first).days
    
    print(f"    SAVED: {len(df):,} bars | {first} to {last} ({days}d = {days/365:.1f}y)")
    
    # Cleanup raw file
    raw_file.unlink()
    
    return {'bars': len(df), 'from': str(first), 'to': str(last), 'days': days}


def main():
    print("=" * 70)
    print("SHF — 2-YEAR M1 DATA DOWNLOAD (Dukascopy)")
    print("=" * 70)
    
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    results = {}
    for duka_sym, save_name in SYMBOLS.items():
        r = download_and_convert(duka_sym, save_name)
        results[save_name] = r if r else {'bars': 0}
    
    # Summary
    print(f"\n{'=' * 70}")
    print("DOWNLOAD SUMMARY")
    print(f"{'=' * 70}")
    print(f"\n  {'Symbol':<10} {'Bars':>10} {'Days':>6} {'Years':>6}")
    print(f"  {'-' * 40}")
    
    for sym, r in results.items():
        if r and r.get('bars', 0) > 0:
            print(f"  {sym:<10} {r['bars']:>10,} {r['days']:>6} {r['days']/365:>5.1f}y")
        else:
            print(f"  {sym:<10} {'FAILED':>10}")
    
    ok = sum(1 for r in results.values() if r and r.get('bars', 0) > 0)
    print(f"\n  {ok}/{len(SYMBOLS)} symbols downloaded")
    
    if ok == len(SYMBOLS):
        print(f"\n  Run backtest: python Scripts/test_oil_index_live.py")
    
    # Cleanup temp dir
    if TEMP_DIR.exists() and not list(TEMP_DIR.iterdir()):
        TEMP_DIR.rmdir()


if __name__ == "__main__":
    main()
