#!/usr/bin/env python3
"""
Download 3.5 months of M1 data from MT5 for all Holy Trio pairs.
Backs up existing data, then replaces with fresh download.
"""

import MetaTrader5 as mt5
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import sys

# MT5 symbol name → CSV file name mapping
SYMBOL_MAP = {
    "XTIUSD": "XTIUSD",
    "XBRUSD": "XBRUSD",
    "NAS100": "US100",
    "DAX40": "DE40",
    "AUDUSD": "AUDUSD",
    "NZDUSD": "NZDUSD",
    "EURUSD": "EURUSD",
    "GBPUSD": "GBPUSD",
}
DATA_DIR = Path("data/historical")
BACKUP_DIR = Path("data/historical_backup")
MONTHS_BACK = 4  # 4 months to be safe (gives 3.5+ months of overlap)


def main():
    print("=" * 70)
    print("MT5 M1 DATA DOWNLOADER")
    print("=" * 70)

    # Initialize MT5
    if not mt5.initialize():
        print(f"  ERROR: MT5 initialize failed: {mt5.last_error()}")
        print(f"  Make sure MetaTrader 5 terminal is running!")
        sys.exit(1)

    info = mt5.terminal_info()
    print(f"  MT5 connected: {info.name}")
    print(f"  Server: {mt5.account_info().server if mt5.account_info() else 'N/A'}")
    print(f"  Account: {mt5.account_info().login if mt5.account_info() else 'N/A'}")

    import time as _time
    # MT5 caps at <100K bars per request
    MAX_BARS = 99_000
    print(f"\n  Requesting last {MAX_BARS:,} M1 bars per symbol")
    print(f"  Timeframe: M1 (1-minute bars)")

    # Backup existing data
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for mt5_sym, csv_name in SYMBOL_MAP.items():
        src = DATA_DIR / f"{csv_name}_M1.csv"
        if src.exists():
            dst = BACKUP_DIR / f"{csv_name}_M1_backup.csv"
            shutil.copy2(src, dst)
    print(f"\n  Existing data backed up to {BACKUP_DIR}/")

    # Download each symbol
    results = {}
    for mt5_sym, csv_name in SYMBOL_MAP.items():
        print(f"\n  Downloading {mt5_sym} (saves as {csv_name}_M1.csv)...")

        # Check symbol is available
        sym_info = mt5.symbol_info(mt5_sym)
        if sym_info is None:
            print(f"    ERROR: Symbol {mt5_sym} not found in MT5!")
            results[csv_name] = {'status': 'NOT_FOUND', 'bars': 0}
            continue

        # Always select symbol and wait for data
        mt5.symbol_select(mt5_sym, True)
        _time.sleep(0.5)

        # Download rates - use copy_rates_from_pos (0 = most recent bar, counting back)
        rates = mt5.copy_rates_from_pos(mt5_sym, mt5.TIMEFRAME_M1, 0, MAX_BARS)

        if rates is None or len(rates) == 0:
            print(f"    ERROR: No data returned for {mt5_sym}: {mt5.last_error()}")
            results[csv_name] = {'status': 'NO_DATA', 'bars': 0}
            continue

        # Convert to DataFrame
        df = pd.DataFrame(rates)
        df['time'] = pd.to_datetime(df['time'], unit='s')
        df = df[['time', 'open', 'high', 'low', 'close', 'tick_volume']]
        df.columns = ['time', 'open', 'high', 'low', 'close', 'tick_volume']

        # Save
        out_path = DATA_DIR / f"{csv_name}_M1.csv"
        df.to_csv(out_path, index=False)

        first = df['time'].iloc[0]
        last = df['time'].iloc[-1]
        days = (last - first).days

        print(f"    Saved: {len(df):,} bars")
        print(f"    Range: {first} to {last} ({days} days)")

        results[csv_name] = {
            'status': 'OK',
            'bars': len(df),
            'from': str(first),
            'to': str(last),
            'days': days,
        }

    mt5.shutdown()

    # Summary
    print(f"\n{'='*70}")
    print(f"DOWNLOAD SUMMARY")
    print(f"{'='*70}")
    print(f"\n  {'Symbol':<12} {'Status':<12} {'Bars':>10} {'From':>22} {'To':>22}")
    print(f"  {'-'*80}")
    for sym, r in results.items():
        if r['status'] == 'OK':
            print(f"  {sym:<12} {r['status']:<12} {r['bars']:>10,} {r['from']:>22} {r['to']:>22}")
        else:
            print(f"  {sym:<12} {r['status']:<12} {r['bars']:>10}")

    ok = sum(1 for r in results.values() if r['status'] == 'OK')
    print(f"\n  {ok}/{len(SYMBOL_MAP)} symbols downloaded successfully")

    if ok == len(SYMBOL_MAP):
        print(f"  All data ready -- run calc_100k_real_data.py for updated projection")
    else:
        print(f"  Some symbols failed -- check MT5 terminal")
        print(f"  Backup data available in {BACKUP_DIR}/")


if __name__ == "__main__":
    main()
