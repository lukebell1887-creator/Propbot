#!/usr/bin/env python3
"""PROOF: Fintokei vs Dukascopy spread data comparison."""
import pandas as pd, numpy as np

print('='*90)
print('PROOF: FINTOKEI vs DUKASCOPY DATA COMPARISON')
print('='*90)

# Load Fintokei (fresh MT5 download - now in data/historical)
mt5_a = pd.read_csv('data/historical/XTIUSD_M1.csv', parse_dates=['time'])
mt5_b = pd.read_csv('data/historical/XBRUSD_M1.csv', parse_dates=['time'])
mt5 = pd.merge(mt5_a[['time','close']], mt5_b[['time','close']], on='time', suffixes=('_wti','_brent'))
mt5 = mt5[(mt5.close_wti>0)&(mt5.close_brent>0)]
mt5['spread'] = np.log(mt5.close_wti) - np.log(mt5.close_brent)

# Load Dukascopy (backed up to data/historical_backup when MT5 download ran)
dk_a = pd.read_csv('data/historical_backup/XTIUSD_M1_backup.csv', parse_dates=['time'])
dk_b = pd.read_csv('data/historical_backup/XBRUSD_M1_backup.csv', parse_dates=['time'])
dk = pd.merge(dk_a[['time','close']], dk_b[['time','close']], on='time', suffixes=('_wti','_brent'))
dk = dk[(dk.close_wti>0)&(dk.close_brent>0)]
dk['spread'] = np.log(dk.close_wti) - np.log(dk.close_brent)

# Filter Dukascopy to same window as MT5
dk_w = dk[(dk.time >= mt5.time.min()) & (dk.time <= mt5.time.max())]

print(f'\n  FINTOKEI (MT5): {len(mt5):,} bars, {mt5.time.min()} to {mt5.time.max()}')
print(f'  DUKASCOPY:      {len(dk_w):,} bars (same time window)')

print(f'\n  RAW PRICES (same time window):')
print(f'  {"":16} {"WTI Mean":>12} {"Brent Mean":>12} {"Dollar Spread":>14}')
print(f'  {"-"*56}')
print(f'  {"Fintokei":16} ${mt5.close_wti.mean():>10.3f} ${mt5.close_brent.mean():>10.3f} ${(mt5.close_wti-mt5.close_brent).mean():>11.3f}')
print(f'  {"Dukascopy":16} ${dk_w.close_wti.mean():>10.3f} ${dk_w.close_brent.mean():>10.3f} ${(dk_w.close_wti-dk_w.close_brent).mean():>11.3f}')

print(f'\n  LOG SPREAD STATISTICS (log(WTI) - log(Brent)):')
print(f'  {"":16} {"Mean":>12} {"Std Dev":>12} {"Min":>12} {"Max":>12} {"Range":>12}')
print(f'  {"-"*76}')
for name, s in [('Fintokei', mt5.spread), ('Dukascopy', dk_w.spread)]:
    print(f'  {name:16} {s.mean():>12.6f} {s.std():>12.6f} {s.min():>12.6f} {s.max():>12.6f} {s.max()-s.min():>12.6f}')

std_ratio = mt5.spread.std() / dk_w.spread.std() if dk_w.spread.std() > 0 else 0
range_ratio = (mt5.spread.max()-mt5.spread.min()) / (dk_w.spread.max()-dk_w.spread.min()) if (dk_w.spread.max()-dk_w.spread.min()) > 0 else 0

print(f'\n  >>> SPREAD STD RATIO:   Fintokei is {std_ratio:.1f}x larger <<<')
print(f'  >>> SPREAD RANGE RATIO: Fintokei is {range_ratio:.1f}x larger <<<')

# Per-bar move size
mt5_d = mt5.spread.diff().dropna()
dk_d = dk_w.spread.diff().dropna()
move_ratio = mt5_d.abs().mean() / dk_d.abs().mean() if dk_d.abs().mean() > 0 else 0

print(f'\n  PER-BAR SPREAD MOVES:')
print(f'  {"":16} {"Avg |move|":>14} {"Std":>14} {"Max |move|":>14}')
print(f'  {"-"*60}')
print(f'  {"Fintokei":16} {mt5_d.abs().mean():>14.8f} {mt5_d.std():>14.8f} {mt5_d.abs().max():>14.8f}')
print(f'  {"Dukascopy":16} {dk_d.abs().mean():>14.8f} {dk_d.std():>14.8f} {dk_d.abs().max():>14.8f}')
print(f'\n  >>> PER-BAR MOVE RATIO: Fintokei moves are {move_ratio:.1f}x larger <<<')

if abs(std_ratio - 1) > 0.3:
    print(f'\n  VERDICT: DATA IS FUNDAMENTALLY DIFFERENT')
    print(f'  The Dukascopy 2-year backtest was testing a DIFFERENT MARKET.')
    print(f'  The MT5/Fintokei data is the correct reference for your live bot.')
else:
    print(f'\n  VERDICT: Data is similar. The 2-year results may be representative.')
