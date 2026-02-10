#!/usr/bin/env python3
"""
Dump ALL available symbols from The5ers MT5 server + download M1 data for any we don't have.

Run on VPS where MT5 is logged in:
  python Scripts/dump_all_symbols.py

Output:
  - Prints full symbol list organized by category
  - Downloads M1 data for any symbols not already in data/historical/
  - Saves symbol list to Results/broker_symbols.json
"""

import MetaTrader5 as mt5
import pandas as pd
import json
import time as _time
from pathlib import Path
from datetime import datetime
import sys

DATA_DIR = Path("data/historical")
RESULTS_DIR = Path("Results")
MAX_BARS = 99_000  # MT5 limit per request

# Map broker names to our canonical CSV names (for symbols we already have)
KNOWN_ALIASES = {
    "NAS100": "US100",
    "DAX40": "DE40",
    # All others use their MT5 name as-is
}


def get_canonical_name(mt5_sym: str) -> str:
    """Convert broker symbol name to our canonical CSV name."""
    return KNOWN_ALIASES.get(mt5_sym, mt5_sym)


def main():
    print("=" * 80)
    print("THE5ERS / FIVEPERCENTONLINE — FULL SYMBOL DUMP + DATA DOWNLOAD")
    print("=" * 80)

    # Initialize MT5
    if not mt5.initialize():
        print(f"  ERROR: MT5 initialize failed: {mt5.last_error()}")
        print(f"  Make sure MetaTrader 5 terminal is running and logged in!")
        sys.exit(1)

    acct = mt5.account_info()
    term = mt5.terminal_info()
    print(f"  Server: {acct.server if acct else 'N/A'}")
    print(f"  Account: {acct.login if acct else 'N/A'}")
    print(f"  Terminal: {term.name if term else 'N/A'}")

    # =========================================================================
    # Step 1: Get ALL symbols from the server
    # =========================================================================
    all_symbols = mt5.symbols_get()
    if all_symbols is None or len(all_symbols) == 0:
        print("  ERROR: No symbols returned!")
        mt5.shutdown()
        sys.exit(1)

    print(f"\n  Total symbols on server: {len(all_symbols)}")

    # Categorize symbols
    categories = {
        'forex_major': [],
        'forex_minor': [],
        'forex_exotic': [],
        'indices': [],
        'commodities': [],
        'metals': [],
        'crypto': [],
        'energy': [],
        'other': [],
    }

    forex_majors = {'EURUSD', 'GBPUSD', 'USDJPY', 'USDCHF', 'AUDUSD', 'USDCAD', 'NZDUSD'}
    forex_minors = {
        'EURGBP', 'EURJPY', 'GBPJPY', 'EURAUD', 'EURCAD', 'EURCHF', 'EURNZD',
        'GBPAUD', 'GBPCAD', 'GBPCHF', 'GBPNZD', 'AUDCAD', 'AUDCHF', 'AUDJPY',
        'AUDNZD', 'CADJPY', 'CADCHF', 'CHFJPY', 'NZDCAD', 'NZDCHF', 'NZDJPY',
    }
    indices_keywords = ['US100', 'US500', 'US30', 'NAS100', 'DAX', 'DE40', 'DE30',
                        'UK100', 'JP225', 'AUS200', 'EU50', 'FR40', 'HK50', 'SP35',
                        'STOXX', 'CHINA', 'USTEC', 'GER']
    metals_keywords = ['XAU', 'XAG', 'XPT', 'XPD', 'GOLD', 'SILVER']
    energy_keywords = ['OIL', 'BRENT', 'NGAS', 'WTI', 'CRUDE']
    crypto_keywords = ['BTC', 'ETH', 'LTC', 'XRP', 'BCH', 'ADA', 'DOT', 'SOL',
                       'DOGE', 'LINK', 'AVAX', 'MATIC', 'CRYPTO']

    symbol_data = []
    for sym in all_symbols:
        name = sym.name
        info = {
            'name': name,
            'description': sym.description if hasattr(sym, 'description') else '',
            'path': sym.path if hasattr(sym, 'path') else '',
            'digits': sym.digits if hasattr(sym, 'digits') else 0,
            'spread': sym.spread if hasattr(sym, 'spread') else 0,
            'trade_mode': sym.trade_mode if hasattr(sym, 'trade_mode') else 0,
            'visible': sym.visible if hasattr(sym, 'visible') else False,
        }
        symbol_data.append(info)

        # Categorize
        name_upper = name.upper().replace('.', '').replace('_', '')

        if any(k in name_upper for k in crypto_keywords):
            categories['crypto'].append(name)
        elif any(k in name_upper for k in metals_keywords):
            categories['metals'].append(name)
        elif any(k in name_upper for k in energy_keywords):
            categories['energy'].append(name)
        elif any(k in name_upper for k in indices_keywords):
            categories['indices'].append(name)
        elif name_upper in forex_majors or name_upper.rstrip('M') in forex_majors:
            categories['forex_major'].append(name)
        elif name_upper in forex_minors or name_upper.rstrip('M') in forex_minors:
            categories['forex_minor'].append(name)
        elif len(name) == 6 and name.isalpha():
            categories['forex_exotic'].append(name)
        elif len(name) == 7 and name[:-1].isalpha() and name[-1].lower() == 'm':
            categories['forex_exotic'].append(name)
        else:
            categories['other'].append(name)

    # Print categorized list
    print(f"\n{'='*80}")
    print(f"ALL SYMBOLS BY CATEGORY")
    print(f"{'='*80}")

    tradeable_symbols = []
    for cat_name, symbols in categories.items():
        if not symbols:
            continue
        symbols.sort()
        print(f"\n  {cat_name.upper().replace('_', ' ')} ({len(symbols)}):")
        for s in symbols:
            # Check if we already have data
            canonical = get_canonical_name(s)
            has_data = (DATA_DIR / f"{canonical}_M1.csv").exists()
            marker = "[HAS DATA]" if has_data else "[NEED DATA]"
            print(f"    {s:<20} {marker}")
            tradeable_symbols.append(s)

    # =========================================================================
    # Step 2: Download data for symbols we DON'T have
    # =========================================================================
    missing = []
    for sym_name in tradeable_symbols:
        canonical = get_canonical_name(sym_name)
        csv_path = DATA_DIR / f"{canonical}_M1.csv"
        if not csv_path.exists():
            missing.append(sym_name)

    print(f"\n{'='*80}")
    print(f"DATA DOWNLOAD — {len(missing)} symbols need M1 data")
    print(f"{'='*80}")

    if missing:
        DATA_DIR.mkdir(parents=True, exist_ok=True)

        for i, sym_name in enumerate(missing, 1):
            canonical = get_canonical_name(sym_name)
            print(f"\n  [{i}/{len(missing)}] Downloading {sym_name} -> {canonical}_M1.csv ...")

            # Enable symbol in Market Watch
            mt5.symbol_select(sym_name, True)
            _time.sleep(0.3)

            # Download
            rates = mt5.copy_rates_from_pos(sym_name, mt5.TIMEFRAME_M1, 0, MAX_BARS)

            if rates is None or len(rates) == 0:
                err = mt5.last_error()
                print(f"    SKIP: No data for {sym_name} (error: {err})")
                continue

            df = pd.DataFrame(rates)
            df['time'] = pd.to_datetime(df['time'], unit='s')
            df = df[['time', 'open', 'high', 'low', 'close', 'tick_volume']]

            out_path = DATA_DIR / f"{canonical}_M1.csv"
            df.to_csv(out_path, index=False)

            first = df['time'].iloc[0]
            last = df['time'].iloc[-1]
            days = (last - first).days
            print(f"    OK: {len(df):,} bars | {first} to {last} ({days} days)")
    else:
        print("  All symbols already have data!")

    # =========================================================================
    # Step 3: Save full symbol list to JSON
    # =========================================================================
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    output = {
        'server': acct.server if acct else 'unknown',
        'account': acct.login if acct else 0,
        'timestamp': datetime.utcnow().isoformat(),
        'total_symbols': len(all_symbols),
        'categories': {k: sorted(v) for k, v in categories.items() if v},
        'all_symbols': sorted([s['name'] for s in symbol_data]),
        'symbol_details': symbol_data,
    }

    json_path = RESULTS_DIR / "broker_symbols.json"
    with open(json_path, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"\n  Full symbol list saved to {json_path}")

    # =========================================================================
    # Summary
    # =========================================================================
    print(f"\n{'='*80}")
    print(f"SUMMARY")
    print(f"{'='*80}")

    # Count data files
    csv_files = list(DATA_DIR.glob("*_M1.csv"))
    print(f"\n  Symbols on server: {len(all_symbols)}")
    print(f"  M1 data files: {len(csv_files)}")
    print(f"  Categories:")
    for cat, syms in categories.items():
        if syms:
            print(f"    {cat}: {len(syms)} ({', '.join(sorted(syms)[:5])}{'...' if len(syms) > 5 else ''})")

    print(f"\n  NEXT STEP: Run 'python Scripts/scan_pairs.py' to find cointegrated pairs")

    mt5.shutdown()
    print("\nDone!")


if __name__ == "__main__":
    main()
