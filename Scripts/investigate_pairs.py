#!/usr/bin/env python3
"""Deep investigation of all pairs, especially oil."""
import pandas as pd
import numpy as np
import math, sys

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

d = 'data/historical/'

def analyze_pair(name, file_a, file_b):
    print(f"\n{'='*80}")
    print(f"  PAIR: {name} ({file_a} / {file_b})")
    print(f"{'='*80}")
    
    a = pd.read_csv(d+file_a, parse_dates=['time'])
    b = pd.read_csv(d+file_b, parse_dates=['time'])
    m = pd.merge(a[['time','close']], b[['time','close']], on='time', how='inner', suffixes=('_a','_b'))
    m = m.sort_values('time').reset_index(drop=True)
    m = m[(m.close_a>0)&(m.close_b>0)]
    
    days = (m.time.iloc[-1] - m.time.iloc[0]).days
    print(f"  Merged bars: {len(m)} | Days: {days} | Range: {m.time.iloc[0]} to {m.time.iloc[-1]}")
    print(f"  A price: {m.close_a.min():.4f} to {m.close_a.max():.4f} (mean={m.close_a.mean():.4f})")
    print(f"  B price: {m.close_b.min():.4f} to {m.close_b.max():.4f} (mean={m.close_b.mean():.4f})")
    
    # Log spread
    m['log_spread'] = np.log(m.close_a) - np.log(m.close_b)
    m['raw_spread'] = m.close_a - m.close_b
    print(f"\n  Log Spread: mean={m.log_spread.mean():.6f}, std={m.log_spread.std():.6f}")
    print(f"  Log Spread range: [{m.log_spread.min():.6f}, {m.log_spread.max():.6f}]")
    print(f"  Raw Spread: mean={m.raw_spread.mean():.2f}, std={m.raw_spread.std():.2f}")
    
    # Returns correlation
    ret_a = np.diff(np.log(m.close_a.values))
    ret_b = np.diff(np.log(m.close_b.values))
    ret_corr = np.corrcoef(ret_a, ret_b)[0,1]
    price_corr = np.corrcoef(m.close_a, m.close_b)[0,1]
    print(f"\n  Correlation: prices={price_corr:.6f}, returns={ret_corr:.6f}")
    
    # Spread returns
    m['spread_ret'] = m.log_spread.diff()
    sr = m.spread_ret.dropna().values
    print(f"\n  Spread Returns: mean={sr.mean():.10f}, std={sr.std():.8f}")
    
    # Autocorrelation
    ac1 = np.corrcoef(sr[:-1], sr[1:])[0,1]
    ac5 = np.corrcoef(sr[:-5], sr[5:])[0,1]
    ac10 = np.corrcoef(sr[:-10], sr[10:])[0,1]
    print(f"  Spread Return AC: lag1={ac1:.6f}, lag5={ac5:.6f}, lag10={ac10:.6f}")
    
    # Welford Z-score simulation
    alpha = 2.0 / 101.0
    w_mean = 0.0; w_m2 = 0.0
    z_scores = []
    spreads = m.log_spread.values
    for i, s in enumerate(spreads):
        if i == 0:
            w_mean = s; w_m2 = 0.0; z_scores.append(0.0)
        else:
            delta = s - w_mean
            w_mean += alpha * delta
            delta2 = s - w_mean
            w_m2 = (1.0-alpha)*w_m2 + alpha*delta*delta2
            v = max(w_m2, 1e-10)
            z = (s - w_mean) / max(v**0.5, 1e-8)
            z_scores.append(z)
    
    z = np.array(z_scores)
    print(f"\n  Z-Score Stats (Welford span=100):")
    print(f"    Mean={z.mean():.4f}, Std={z.std():.4f}")
    print(f"    Range: [{z.min():.2f}, {z.max():.2f}]")
    print(f"    |Z|>2: {(np.abs(z)>2).sum()} ({(np.abs(z)>2).mean()*100:.1f}%)")
    print(f"    |Z|>3: {(np.abs(z)>3).sum()} ({(np.abs(z)>3).mean()*100:.1f}%)")
    
    # Hurst estimation (quick method using spread autocorrelation)
    # Also check ADF-like stationarity
    from numpy.linalg import lstsq
    spread_vals = m.log_spread.values
    y = spread_vals[1:]
    x = spread_vals[:-1]
    # AR(1): y = a + b*x + e
    X = np.column_stack([np.ones(len(x)), x])
    coefs, _, _, _ = lstsq(X, y, rcond=None)
    beta_ar1 = coefs[1]
    half_life_ar1 = -np.log(2) / np.log(abs(beta_ar1)) if abs(beta_ar1) < 1 and abs(beta_ar1) > 0 else float('inf')
    print(f"\n  AR(1) Analysis:")
    print(f"    beta_AR1 = {beta_ar1:.6f}")
    print(f"    Half-life = {half_life_ar1:.1f} bars ({half_life_ar1/60:.1f} hours)")
    print(f"    Mean-reverting: {'YES' if beta_ar1 < 1 else 'NO'}")
    
    # KEY: Check what Hurst the engine would compute
    # Using the Rust R/S method on spread buffer
    def compute_hurst_rs(data, window=512):
        if len(data) < window:
            return 0.5
        prices = data[-window:]
        returns = np.diff(prices)
        if len(returns) < 16:
            return 0.5
        sizes = []
        s = 8
        while s <= len(returns) // 2:
            sizes.append(s)
            s *= 2
        if len(sizes) < 2:
            return 0.5
        log_n = []; log_rs = []
        for n in sizes:
            n_seg = len(returns) // n
            rs_vals = []
            for seg in range(n_seg):
                chunk = returns[seg*n:(seg+1)*n]
                mean_c = chunk.mean()
                std_c = chunk.std(ddof=1)
                if std_c < 1e-10: continue
                cumdev = np.cumsum(chunk - mean_c)
                rs = (cumdev.max() - cumdev.min()) / std_c
                if rs > 0 and np.isfinite(rs):
                    rs_vals.append(rs)
            if rs_vals:
                avg_rs = np.mean(rs_vals)
                if avg_rs > 0:
                    log_n.append(np.log(n))
                    log_rs.append(np.log(avg_rs))
        if len(log_n) < 2: return 0.5
        log_n = np.array(log_n); log_rs = np.array(log_rs)
        cov = np.sum((log_n - log_n.mean()) * (log_rs - log_rs.mean()))
        var = np.sum((log_n - log_n.mean())**2)
        return max(0, min(1, cov/var if var > 0 else 0.5))
    
    # Compute rolling Hurst
    hursts = []
    step = 5000
    for i in range(512, len(spreads), step):
        h = compute_hurst_rs(spreads[:i], 512)
        hursts.append(h)
    if hursts:
        print(f"\n  Hurst Exponent (R/S, window=512):")
        print(f"    Mean={np.mean(hursts):.4f}, Std={np.std(hursts):.4f}")
        print(f"    Range: [{min(hursts):.4f}, {max(hursts):.4f}]")
        z_crit_mean = 2.0 * (1 + 6.0 * max(0, np.mean(hursts) - 0.5))
        print(f"    Implied Z_crit (avg H): {z_crit_mean:.2f}")
    
    # CRITICAL: PnL per trade analysis
    # The backtest uses notional=100000. Let's check what the ACTUAL dollar moves are
    # For 1 unit of spread change, the PnL = direction * lots * notional * delta_spread
    # With Dynamic AKAD, lots = balance * risk / 1000
    # Let's estimate typical trade size
    print(f"\n  TRADE ECONOMICS (notional=100000):")
    # Average absolute Z-score move during a trade (rough: entry at Z_crit, exit at Z_exit)
    # Z_crit depends on H. With H=0.383, Z_crit = 2.0 * (1 + 6*max(0, 0.383-0.5)) = 2.0
    # Z_exit with H=0.383: 0.5 * (1 + 2*(0.383-0.5)) = 0.5 * (1 + 2*(-0.117)) = 0.5*0.766 = 0.383
    avg_h = np.mean(hursts) if hursts else 0.5
    z_crit = 2.0 * (1 + 6.0 * max(0, avg_h - 0.5))
    z_exit = max(0.1, min(1.0, 0.5 * (1 + 2.0 * (avg_h - 0.5))))
    z_delta = z_crit - z_exit  # Z-score traveled during a winning trade
    
    # Welford std of the spread
    # Last w_m2 value
    welford_std = max(w_m2, 1e-10)**0.5
    spread_move_per_trade = z_delta * welford_std
    
    # With 100k notional and 0.01 lots: PnL = direction * 0.01 * 100000 * spread_move = 1000 * spread_move
    pnl_per_lot_unit = spread_move_per_trade * 100000.0
    
    print(f"    Avg Hurst: {avg_h:.4f}")
    print(f"    Z_crit: {z_crit:.4f}, Z_exit: {z_exit:.4f}")
    print(f"    Z delta (entry->exit): {z_delta:.4f}")
    print(f"    Welford std (last): {welford_std:.8f}")
    print(f"    Spread move per trade: {spread_move_per_trade:.8f}")
    print(f"    PnL per 1 lot * notional: ${pnl_per_lot_unit:.2f}")
    
    return {
        'name': name, 'bars': len(m), 'days': days,
        'log_spread_std': m.log_spread.std(),
        'ret_corr': ret_corr, 'ac1': ac1, 'ac5': ac5,
        'beta_ar1': beta_ar1, 'half_life': half_life_ar1,
        'avg_hurst': np.mean(hursts) if hursts else 0.5,
        'z_crit': z_crit, 'z_exit': z_exit,
        'welford_std': welford_std,
    }

# Analyze all pairs
pairs = [
    ("US100/DE40", "US100_M1.csv", "DE40_M1.csv"),
    ("AUDUSD/NZDUSD", "AUDUSD_M1.csv", "NZDUSD_M1.csv"),
    ("EURUSD/GBPUSD", "EURUSD_M1.csv", "GBPUSD_M1.csv"),
    ("EURJPY/CHFJPY", "EURJPY_M1.csv", "CHFJPY_M1.csv"),
    ("XTIUSD/XBRUSD", "XTIUSD_M1.csv", "XBRUSD_M1.csv"),
    ("XAUUSD/XAGUSD", "XAUUSD_M1.csv", "XAGUSD_M1.csv"),
]

results = []
for name, fa, fb in pairs:
    try:
        r = analyze_pair(name, fa, fb)
        results.append(r)
    except Exception as e:
        print(f"\n  ERROR: {e}")

print(f"\n\n{'='*100}")
print("COMPARATIVE SUMMARY")
print(f"{'='*100}")
print(f"\n{'Pair':<20} {'Bars':>8} {'RetCorr':>8} {'SpreadStd':>10} {'AR1beta':>8} {'HalfLife':>9} {'Hurst':>7} {'Zcrit':>6} {'AC1':>8}")
print("-"*100)
for r in results:
    print(f"{r['name']:<20} {r['bars']:>8} {r['ret_corr']:>8.4f} {r['log_spread_std']:>10.6f} "
          f"{r['beta_ar1']:>8.6f} {r['half_life']:>8.1f}b {r['avg_hurst']:>7.4f} {r['z_crit']:>6.2f} {r['ac1']:>8.6f}")

print(f"\n\nKEY OBSERVATIONS:")
print(f"="*80)
