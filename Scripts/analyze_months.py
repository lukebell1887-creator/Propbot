import sys, math, numpy as np, pandas as pd
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shf_core

d = Path(__file__).resolve().parent.parent / "data" / "historical"
a = pd.read_csv(d / "XAUUSD_M1.csv", parse_dates=['time']).rename(columns={'close': 'close_a'})
b = pd.read_csv(d / "XAGUSD_M1.csv", parse_dates=['time']).rename(columns={'close': 'close_b'})
df = pd.merge(a[['time','close_a']], b[['time','close_b']], on='time', how='inner').sort_values('time').reset_index(drop=True)
df = df[(df.close_a > 0) & (df.close_b > 0)].reset_index(drop=True)

eng = shf_core.CointegrationEngine(span=100, beta=1.0, entry_z=2.393, exit_z=0.432,
    z_base=2.393, gamma=6.0, hurst_window=512, dynamic_z=True,
    exit_z_base=0.432, exit_gamma=2.0, dynamic_exit=True)

tp = pd.DatetimeIndex(df['time'])
monthly = {}

for bar in range(len(df)):
    sig = eng.update(float(df.iloc[bar].close_a), float(df.iloc[bar].close_b))
    z = sig.z_score; s = sig.signal
    h = eng.last_hurst; std = eng.last_std
    m = tp[bar].strftime('%Y-%m')
    if m not in monthly:
        monthly[m] = {'zmax': 0, 'sigs': 0, 'sigma': [], 'hurst': [], 'ratio': []}
    monthly[m]['zmax'] = max(monthly[m]['zmax'], abs(z))
    if s != 0:
        monthly[m]['sigs'] += 1
    monthly[m]['sigma'].append(std)
    monthly[m]['hurst'].append(h)
    p1 = df.iloc[bar].close_a; p2 = df.iloc[bar].close_b
    if p2 > 0:
        monthly[m]['ratio'].append(p1 / p2)

print("WHY DID ONLY DECEMBER TRADE?")
print("=" * 85)
print(f"{'Month':<9} {'MaxZ':>6} {'Sigs':>5} {'AvgSigma':>10} {'AvgHurst':>10} {'DynZ_Thr':>10} {'G/S_Ratio':>10} {'RatRange':>10}")
print("-" * 85)

for m in sorted(monthly):
    d2 = monthly[m]
    ah = np.mean(d2['hurst'])
    asig = np.mean(d2['sigma'])
    ar = np.mean(d2['ratio'])
    rr = max(d2['ratio']) - min(d2['ratio'])
    dz = 2.393 * (1 + 6 * ah)
    can_trade = d2['zmax'] > dz
    marker = " <-TRADES" if m == '2025-12' else (" Z>DynZ!" if can_trade else " BLOCKED")
    print(f"{m:<9} {d2['zmax']:>6.2f} {d2['sigs']:>5} {asig:>10.6f} {ah:>10.3f} {dz:>10.1f} {ar:>10.1f} {rr:>10.2f}{marker}")

print()
print("KEY INSIGHT:")
print("  DynZ_Thr = Z_base * (1 + gamma * hurst) = 2.393 * (1 + 6 * hurst)")
print("  A signal fires ONLY when |Z| > DynZ_Thr")
print("  If hurst is high (trending), the threshold goes UP, making it harder to trade")
print("  If hurst is low (mean-reverting), the threshold goes DOWN, making it easier")
print()
print("  MaxZ must EXCEED DynZ_Thr for ANY signal to fire")
print("  Then the amplitude hurdle (2.86x cost) must ALSO be met")
