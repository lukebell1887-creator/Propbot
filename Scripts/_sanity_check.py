"""Quick sanity: was Jan-Apr 2026 a normal or unusual regime?"""
import json, sys, pandas as pd, numpy as np
from pathlib import Path
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

ROOT = Path(__file__).resolve().parents[1]

print("=" * 82)
print("  SAMPLE CHARACTER — Jan 20 to Apr 21, 2026  (the 3-month window we tuned on)")
print("=" * 82)
for sym in ['DE40','US30','US100','XAUUSD','US500']:
    df = pd.read_csv(ROOT/f'data/historical/{sym}_M1.csv', parse_dates=['time'])
    df['date'] = df['time'].dt.date
    daily = df.groupby('date').agg(open=('open','first'), close=('close','last'),
                                    high=('high','max'), low=('low','min')).reset_index()
    daily['ret'] = daily['close'].pct_change()
    daily['range_pct'] = (daily['high']-daily['low'])/daily['open']*100
    first, last = daily['close'].iloc[0], daily['close'].iloc[-1]
    tot_ret = (last/first-1)*100
    vol_ann = daily['ret'].std() * np.sqrt(252) * 100
    trendy = abs(tot_ret) / (daily['ret'].abs().sum()*100) if daily['ret'].abs().sum() > 0 else 0
    # up days vs down days
    up = int((daily['ret'] > 0).sum())
    dn = int((daily['ret'] < 0).sum())
    print(f"  {sym:6s}  total={tot_ret:+7.1f}%  ann_vol={vol_ann:5.1f}%  "
          f"med_range={daily['range_pct'].median():.2f}%  "
          f"trendy={trendy:.3f}  up:dn={up}:{dn}  days={len(daily)}")

print()
print("=" * 82)
print("  v24 TRADES — long vs short symmetry (does the bot trade BOTH sides?)")
print("=" * 82)
with open(ROOT/'Results/v24_trades.json') as f:
    trades = json.load(f)['trades']
long_n  = sum(1 for t in trades if t['side'] > 0)
short_n = sum(1 for t in trades if t['side'] < 0)
long_R  = sum(t['realised_R'] for t in trades if t['side'] > 0)
short_R = sum(t['realised_R'] for t in trades if t['side'] < 0)
long_w  = sum(1 for t in trades if t['side'] > 0 and t['realised_R'] > 0)
short_w = sum(1 for t in trades if t['side'] < 0 and t['realised_R'] > 0)

print(f"  LONGS:   n={long_n:3d}  WR={long_w/max(long_n,1)*100:5.1f}%  "
      f"sum(R)={long_R:+6.1f}  avg_R={long_R/max(long_n,1):+5.2f}")
print(f"  SHORTS:  n={short_n:3d}  WR={short_w/max(short_n,1)*100:5.1f}%  "
      f"sum(R)={short_R:+6.1f}  avg_R={short_R/max(short_n,1):+5.2f}")
print(f"  TOTAL:   n={len(trades):3d}  long/short ratio = {long_n/max(short_n,1):.2f}")

# per-symbol breakdown
print()
print("=" * 82)
print("  PER-SYMBOL TRADE STATS")
print("=" * 82)
by_sym = {}
for t in trades:
    by_sym.setdefault(t['symbol'], []).append(t)
for sym, trs in sorted(by_sym.items()):
    wins = sum(1 for t in trs if t['realised_R'] > 0)
    sumR = sum(t['realised_R'] for t in trs)
    longs = sum(1 for t in trs if t['side'] > 0)
    shorts = sum(1 for t in trs if t['side'] < 0)
    print(f"  {sym:8s}  n={len(trs):3d}  WR={wins/len(trs)*100:5.1f}%  "
          f"sum(R)={sumR:+6.1f}  long:short={longs}:{shorts}")

# Daily PnL distribution (so we know what 'worst day' looks like across sample)
print()
print("=" * 82)
print("  DAILY PnL DISTRIBUTION (in R-sum, positions sized uniformly)")
print("=" * 82)
from collections import defaultdict
from datetime import datetime
by_day = defaultdict(float)
by_day_n = defaultdict(int)
for t in trades:
    d = datetime.fromtimestamp(t['entry_time']).date()
    by_day[d] += t['realised_R']
    by_day_n[d] += 1
daily_R = list(by_day.values())
daily_R.sort()
print(f"  trading days:         {len(daily_R)}")
print(f"  median daily R:       {np.median(daily_R):+.2f}")
print(f"  mean daily R:         {np.mean(daily_R):+.2f}")
print(f"  std daily R:          {np.std(daily_R):.2f}")
print(f"  WORST 5 days:         {[f'{r:+.2f}' for r in daily_R[:5]]}")
print(f"  BEST 5 days:          {[f'{r:+.2f}' for r in daily_R[-5:]]}")
print(f"  % days with loss:     {sum(1 for r in daily_R if r<0)/len(daily_R)*100:.1f}%")
