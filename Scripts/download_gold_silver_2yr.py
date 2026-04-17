#!/usr/bin/env python3
"""Download 2 years of Gold (XAUUSD) and Silver (XAGUSD) M1 from Dukascopy."""
import subprocess, shutil
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

DATA_DIR = Path("data/historical")
BACKUP_DIR = Path("data/historical_backup")
TEMP_DIR = Path("data/duka_raw")

SYMBOLS = {
    "xauusd": "XAUUSD",
    "xagusd": "XAGUSD",
}

def download(duka_sym, save_name):
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=730)).strftime("%Y-%m-%d")

    print(f"\n  Downloading {duka_sym} -> {save_name}_M1.csv")
    print(f"  Range: {start_date} to {end_date}")

    TEMP_DIR.mkdir(parents=True, exist_ok=True)

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

    print(f"  Running... (this takes 5-15 minutes per symbol)")
    result = subprocess.run(cmd, capture_output=True, text=True, shell=True)

    if result.returncode != 0:
        print(f"  ERROR: {result.stderr[:500]}")
        print(f"  stdout: {result.stdout[:500]}")
        return None

    files = list(TEMP_DIR.glob(f"{duka_sym}*.csv"))
    if not files:
        print(f"  ERROR: No file found. stdout: {result.stdout[:500]}")
        return None

    raw_file = max(files, key=lambda f: f.stat().st_size)
    print(f"  Downloaded: {raw_file.name} ({raw_file.stat().st_size / 1024 / 1024:.1f} MB)")

    df = pd.read_csv(raw_file)
    print(f"  Columns: {list(df.columns)}")
    print(f"  First row: {df.iloc[0].to_dict()}")

    if 'timestamp' in df.columns:
        df['time'] = pd.to_datetime(df['timestamp'], unit='ms')
    elif 'date' in df.columns:
        df['time'] = pd.to_datetime(df['date'])
    else:
        df['time'] = pd.to_datetime(df.iloc[:, 0], unit='ms')

    if 'volume' in df.columns:
        df['tick_volume'] = (df['volume'] * 1000).astype(int).clip(lower=1)
    else:
        df['tick_volume'] = 1

    df = df[['time', 'open', 'high', 'low', 'close', 'tick_volume']]
    df = df.sort_values('time').drop_duplicates(subset=['time']).reset_index(drop=True)

    out_path = DATA_DIR / f"{save_name}_M1.csv"
    if out_path.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"{save_name}_M1_mt5_backup.csv"
        shutil.copy2(out_path, backup)
        print(f"  Backed up existing MT5 data -> {backup.name}")

    df.to_csv(out_path, index=False)
    days = (df['time'].iloc[-1] - df['time'].iloc[0]).days
    print(f"  SAVED: {len(df):,} bars | {df['time'].iloc[0]} to {df['time'].iloc[-1]} ({days}d = {days/365:.1f}yr)")
    return len(df)


def main():
    print("=" * 60)
    print("  DOWNLOAD 2-YEAR GOLD/SILVER M1 (Dukascopy)")
    print("=" * 60)
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    for duka_sym, save_name in SYMBOLS.items():
        download(duka_sym, save_name)

    print("\n  Done! Now run: python Scripts/forex_scanner.py")


if __name__ == "__main__":
    main()
