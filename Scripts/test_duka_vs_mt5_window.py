#!/usr/bin/env python3
"""Oct 2025 - Feb 2026 ONLY on Dukascopy data. Compare to MT5 3.5-month results."""
import sys, time
import pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from Scripts.test_oil_index_live import run_pair, load_pair, PAIRS

def main():
    oil = [p for p in PAIRS if p.name == "Oil Spread"][0]
    df = load_pair(oil)
    df = df[(df['time'] >= '2025-10-01') & (df['time'] <= '2026-02-13')].reset_index(drop=True)
    print(f"Oil Oct25-Feb26: {len(df):,} bars, {df['time'].iloc[0]} to {df['time'].iloc[-1]}")
    
    t0 = time.time()
    r = run_pair(df, oil, hmm_hold=10)
    print(f"\n  Trades: {r['trades']}, WR: {r['wr']}% net / {r['gross_wr']}% gross, PF: {r['pf']}")
    print(f"  Gross: ${r['gross_pnl']:,.2f}, Costs: ${r['total_costs']:,.2f}, Net: ${r['net_pnl']:,.2f}")
    print(f"  MaxDD: {r['max_dd_pct']}%, AvgHold: {r['avg_hold']}, $/Trade: ${r['net_pnl']/r['trades'] if r['trades']>0 else 0:.2f}")
    print(f"  HMM blocks: {r.get('hmm_blocks',0)}, Dwell blocks: {r.get('dwell_blocks',0)}")
    print(f"  Done in {time.time()-t0:.0f}s")

if __name__ == "__main__":
    main()
