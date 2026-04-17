#!/usr/bin/env python3
"""
SHF — Download 2 Years of M1 Data from MT5 (Multi-Chunk)
=========================================================
Run this ON THE VPS where MT5 is running:
  cd C:\SHF && python Scripts\download_2year_m1.py

MT5 caps at ~99K bars per request (~69 days of M1).
This script makes multiple requests with date offsets to get ~2 years.
Data is saved in the SAME format as existing CSVs:
  time,open,high,low,close,tick_volume

Symbols: NAS100(->US100), DAX40(->DE40), XTIUSD, XBRUSD
"""

import MetaTrader5 as mt5
import pandas as pd
import time as _time
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ============================================================================
# CONFIG
# ============================================================================
DATA_DIR = Path("data/historical")
BACKUP_DIR = Path("data/historical_backup")

# MT5 symbol name -> CSV save name
SYMBOLS = {
    "NAS100": "US100",
    "DAX40": "DE40",
    "XTIUSD": "XTIUSD",
    "XBRUSD": "XBRUSD",
}

YEARS_BACK = 2
CHUNK_BARS = 90_000   # bars per request (MT5 caps at ~100K)
CHUNK_DAYS = 60       # ~60 days per chunk (conservative)


def download_symbol_chunked(mt5_sym, save_name, years=2):
    """Download M1 data in chunks going back N years."""
    
    # Enable symbol
    mt5.symbol_select(mt5_sym, True)
    _time.sleep(0.5)
    
    # Calculate date range
    end_date = datetime.now()
    start_date = end_date - timedelta(days=years * 365)
    
    print(f"\n  {mt5_sym} -> {save_name}_M1.csv")
    print(f"    Requested range: {start_date.date()} to {end_date.date()}")
    
    all_frames = []
    current_end = end_date
    chunk_num = 0
    
    while current_end > start_date:
        chunk_num += 1
        current_start = max(start_date, current_end - timedelta(days=CHUNK_DAYS))
        
        rates = mt5.copy_rates_range(
            mt5_sym, mt5.TIMEFRAME_M1,
            current_start, current_end
        )
        
        if rates is None or len(rates) == 0:
            err = mt5.last_error()
            print(f"    Chunk {chunk_num}: {current_start.date()} to {current_end.date()} -> NO DATA ({err})")
            # Move window back and try next chunk
            current_end = current_start
            continue
        
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df = df[['time', 'open', 'high', 'low', 'close', 'tick_volume']]
        all_frames.append(df)
        
        first = df['time'].iloc[0]
        last = df['time'].iloc[-1]
        print(f"    Chunk {chunk_num}: {first.date()} to {last.date()} -> {len(df):,} bars")
        
        # Move window back (overlap by 1 day to avoid gaps)
        current_end = current_start - timedelta(days=1)
        _time.sleep(0.3)  # Be nice to broker
    
    if not all_frames:
        print(f"    ERROR: No data received for {mt5_sym}")
        return None
    
    # Concatenate, deduplicate, sort
    full = pd.concat(all_frames, ignore_index=True)
    full = full.drop_duplicates(subset=['time']).sort_values('time').reset_index(drop=True)
    
    # Save
    out_path = DATA_DIR / f"{save_name}_M1.csv"
    
    # Backup existing file first
    if out_path.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"{save_name}_M1_pre2year.csv"
        import shutil
        shutil.copy2(out_path, backup)
        print(f"    Backed up existing -> {backup.name}")
    
    full.to_csv(out_path, index=False)
    
    first = full['time'].iloc[0]
    last = full['time'].iloc[-1]
    days = (last - first).days
    
    print(f"    SAVED: {len(full):,} bars | {first} to {last} ({days} days = {days/365:.1f} years)")
    
    return {
        'bars': len(full),
        'from': str(first),
        'to': str(last),
        'days': days,
    }


def main():
    print("=" * 70)
    print("SHF — 2-YEAR M1 DATA DOWNLOADER (Multi-Chunk)")
    print("=" * 70)
    
    # Initialize MT5
    if not mt5.initialize():
        print(f"  ERROR: MT5 initialize failed: {mt5.last_error()}")
        print(f"  Make sure MetaTrader 5 terminal is running!")
        sys.exit(1)
    
    info = mt5.terminal_info()
    acc = mt5.account_info()
    print(f"  MT5: {info.name}")
    print(f"  Server: {acc.server if acc else 'N/A'}")
    print(f"  Account: {acc.login if acc else 'N/A'}")
    print(f"  Target: {YEARS_BACK} years of M1 data per symbol")
    print(f"  Symbols: {', '.join(SYMBOLS.keys())}")
    
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    
    results = {}
    for mt5_sym, save_name in SYMBOLS.items():
        r = download_symbol_chunked(mt5_sym, save_name, years=YEARS_BACK)
        if r:
            results[save_name] = r
        else:
            results[save_name] = {'bars': 0, 'error': 'No data'}
    
    mt5.shutdown()
    
    # Summary
    print(f"\n{'=' * 70}")
    print("DOWNLOAD SUMMARY")
    print(f"{'=' * 70}")
    print(f"\n  {'Symbol':<10} {'Bars':>10} {'Days':>6} {'Years':>6} {'From':>22} {'To':>22}")
    print(f"  {'-' * 80}")
    
    all_ok = True
    for sym, r in results.items():
        if r.get('bars', 0) > 0:
            print(f"  {sym:<10} {r['bars']:>10,} {r['days']:>6} {r['days']/365:>5.1f}y {r['from']:>22} {r['to']:>22}")
        else:
            print(f"  {sym:<10} {'FAILED':>10}")
            all_ok = False
    
    ok = sum(1 for r in results.values() if r.get('bars', 0) > 0)
    print(f"\n  {ok}/{len(SYMBOLS)} symbols downloaded")
    
    if all_ok:
        print(f"\n  All data ready! Now run the backtest:")
        print(f"    python Scripts/test_oil_index_live.py")
    else:
        print(f"\n  Some symbols failed. Check:")
        print(f"    1. Is MT5 terminal running?")
        print(f"    2. Does the broker have 2 years of M1 history?")
        print(f"       (Some prop firm servers only keep 3-12 months)")
        print(f"    3. Try running on the VPS where MT5 is connected")


if __name__ == "__main__":
    main()
