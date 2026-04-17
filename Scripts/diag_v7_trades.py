#!/usr/bin/env python3
"""Quick diagnostic on v7 trade log from the 5ers backtest."""
import json, statistics
from pathlib import Path
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
trades = json.load(open(ROOT / "Results" / "v7_5ers_mtb_3month_trades.json"))

print(f"\nTrades: {len(trades)}")
print(f"By symbol: {Counter(t['symbol'] for t in trades)}")
print(f"By side  : {Counter('long' if t['side']>0 else 'short' for t in trades)}")
print(f"By exit  : {Counter(t['exit_reason'] for t in trades)}")
print(f"By regime: {Counter(t['regime'] for t in trades)}")

print("\n-- Per-exit-reason mean R:")
exits = {}
for t in trades:
    exits.setdefault(t['exit_reason'], []).append(t['realised_R'])
for k, v in exits.items():
    print(f"  {k:>15}  n={len(v):>4}  meanR={statistics.mean(v):+.3f}  "
          f"netPnL=${sum(tt['net_pnl'] for tt in trades if tt['exit_reason']==k):+.2f}")

print("\n-- R-distribution percentiles:")
rs = sorted(t['realised_R'] for t in trades)
for pc in [1, 5, 25, 50, 75, 95, 99]:
    i = int(pc/100 * (len(rs)-1))
    print(f"  p{pc:>2} = {rs[i]:+.3f}")

print("\n-- Per-symbol stats:")
for sym in ['US100', 'DE40', 'XAUUSD']:
    sym_t = [t for t in trades if t['symbol']==sym]
    if not sym_t: continue
    wins = [t for t in sym_t if t['net_pnl']>0]
    pnl = sum(t['net_pnl'] for t in sym_t)
    print(f"  {sym:<8} n={len(sym_t):>4}  win%={100*len(wins)/len(sym_t):.1f}  "
          f"expR={statistics.mean(t['realised_R'] for t in sym_t):+.3f}  "
          f"PnL=${pnl:+.2f}")

print("\n-- Per-regime stats:")
for reg in sorted(set(t['regime'] for t in trades)):
    rt = [t for t in trades if t['regime'] == reg]
    wins = [t for t in rt if t['net_pnl'] > 0]
    pnl = sum(t['net_pnl'] for t in rt)
    print(f"  regime={reg}  n={len(rt):>4}  win%={100*len(wins)/max(len(rt),1):.1f}  "
          f"expR={statistics.mean(t['realised_R'] for t in rt):+.3f}  "
          f"PnL=${pnl:+.2f}")

print("\n-- Per-hour-UTC stats (from entry_time epoch):")
from datetime import datetime, timezone
hour_bucket = {}
for t in trades:
    h = datetime.fromtimestamp(t['entry_time'], tz=timezone.utc).hour
    hour_bucket.setdefault(h, []).append(t)
for h in sorted(hour_bucket):
    rt = hour_bucket[h]
    wins = [t for t in rt if t['net_pnl'] > 0]
    pnl = sum(t['net_pnl'] for t in rt)
    print(f"  hr {h:02d}Z  n={len(rt):>3}  win%={100*len(wins)/len(rt):.0f}  "
          f"expR={statistics.mean(t['realised_R'] for t in rt):+.2f}  "
          f"PnL=${pnl:+6.1f}")

print("\n-- Per-kalman-mu-bucket at entry (sign of drift alignment):")
kb = {'large+': [], 'small+': [], 'small-': [], 'large-': []}
for t in trades:
    signed_k = t['kalman_mu'] * t['side']   # positive if drift agreed with trade
    if signed_k > 2e-4: kb['large+'].append(t)
    elif signed_k > 0: kb['small+'].append(t)
    elif signed_k > -2e-4: kb['small-'].append(t)
    else: kb['large-'].append(t)
for k, rt in kb.items():
    if not rt: continue
    wins = [t for t in rt if t['net_pnl'] > 0]
    pnl = sum(t['net_pnl'] for t in rt)
    print(f"  {k:>7}  n={len(rt):>3}  win%={100*len(wins)/len(rt):.0f}  "
          f"expR={statistics.mean(t['realised_R'] for t in rt):+.2f}  "
          f"PnL=${pnl:+6.1f}")

print("\n-- Stop distances as % of price (first 10):")
for t in trades[:10]:
    pct = 100 * t['R_distance'] / t['entry_price']
    print(f"  {t['symbol']:<8}  R=${t['R_distance']:.2f}  ({pct:.3f}%)  "
          f"lots={t['lots']}  side={'L' if t['side']>0 else 'S'}  "
          f"exit={t['exit_reason']}  realR={t['realised_R']:+.2f}")
