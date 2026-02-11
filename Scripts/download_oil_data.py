#!/usr/bin/env python3
"""
Download XBRUSD (Brent Oil) M1 data from MT5.
Run this on the VPS where MT5 is running:
  python Scripts/download_oil_data.py

We already have USOIL_M1.csv. This downloads XBRUSD to pair with it.
Also tries XTIUSD in case broker uses different naming.
"""
import MetaTrader5 as mt5
import pandas as pd
import time, sys
from pathlib import Path

DATA_DIR = Path("data/historical")
MAX_BARS = 99_000

def download_symbol(mt5_name, save_name=None):
    if save_name is None: save_name = mt5_name
    mt5.symbol_select(mt5_name, True)
    time.sleep(0.5)
    rates = mt5.copy_rates_from_pos(mt5_name, mt5.TIMEFRAME_M1, 0, MAX_BARS)
    if rates is None or len(rates) == 0:
        print(f"  NO DATA for {mt5_name}: {mt5.last_error()}")
        return False
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df = df[['time','open','high','low','close','tick_volume']]
    out = DATA_DIR / f"{save_name}_M1.csv"
    df.to_csv(out, index=False)
    print(f"  DOWNLOADED {mt5_name} -> {out}: {len(df):,} bars | {df['time'].iloc[0]} to {df['time'].iloc[-1]}")
    return True

def main():
    if not mt5.initialize():
        print(f"ERROR: MT5 init failed: {mt5.last_error()}")
        sys.exit(1)
    
    print("MT5 Oil Data Downloader")
    print(f"  Server: {mt5.account_info().server}")
    
    # Search for oil symbols
    syms = mt5.symbols_get()
    print(f"\n  Searching {len(syms)} symbols for oil/brent...")
    for s in syms:
        n = s.name.upper()
        if any(k in n for k in ['XTI','XBR','OIL','BRENT','WTI','CRUDE']):
            print(f"    FOUND: {s.name} - {getattr(s,'description','')}")
    
    # Download
    print(f"\n  Downloading oil symbols...")
    for sym in ['XBRUSD', 'XTIUSD', 'UKOIL', 'BRENT']:
        download_symbol(sym)
    
    mt5.shutdown()
    print("\nDone! Now copy data/historical/ to your local machine and run:")
    print("  python Scripts/test_oil_pair.py")

if __name__ == "__main__":
    main()
