#!/usr/bin/env python3
"""
v5.5 2022 Stress Test (Synthetic)
==================================

We don't have 2022 M1 data, so we construct a SYNTHETIC stress test
that models 2022's key characteristics:

1. TRENDING MARKETS: Strong USD rally, H >> 0.5 for extended periods
2. CORRELATION BREAKDOWN: Pairs that normally co-move diverge
3. FAT-TAIL EVENTS: GBP flash crash (Sep 2022), Ukraine invasion spikes
4. VOLATILITY REGIME SHIFTS: Calm -> extreme -> calm

Tests v5.3 (fixed Z=2.0) vs v5.5 (Dynamic Z) under these conditions.
The question: Does Dynamic Z protect us in the WORST market conditions?
"""

import numpy as np
import pandas as pd
import json
import time
from pathlib import Path
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ============================================================================
# CONFIG
# ============================================================================

Z_BASE = 2.0
GAMMA = 6.0
EXIT_Z = 0.5
WELFORD_SPAN = 100
HURST_WINDOW = 512
N_BARS = 50000  # ~35 trading days of M1 data

# ============================================================================
# HURST & DYNAMIC Z (same as production)
# ============================================================================

def compute_hurst_rs(log_prices, window=512):
    if len(log_prices) < window:
        return 0.5
    prices = log_prices[-window:]
    returns = np.diff(prices)
    if len(returns) < 16:
        return 0.5
    window_sizes = []
    size = 8
    while size <= len(returns) // 2:
        window_sizes.append(size)
        size *= 2
    if len(window_sizes) < 2:
        return 0.5
    log_n, log_rs = [], []
    for n in window_sizes:
        n_seg = len(returns) // n
        if n_seg == 0:
            continue
        rs_vals = []
        for seg in range(n_seg):
            s = returns[seg*n:(seg+1)*n]
            m = np.mean(s)
            sd = np.std(s, ddof=1)
            if sd < 1e-10:
                continue
            cs = np.cumsum(s - m)
            rs = (np.max(cs) - np.min(cs)) / sd
            if np.isfinite(rs) and rs > 0:
                rs_vals.append(rs)
        if rs_vals:
            avg = np.mean(rs_vals)
            if avg > 0:
                log_n.append(np.log(n))
                log_rs.append(np.log(avg))
    if len(log_n) < 2:
        return 0.5
    log_n, log_rs = np.array(log_n), np.array(log_rs)
    nm, rm = np.mean(log_n), np.mean(log_rs)
    cov = np.sum((log_n - nm) * (log_rs - rm))
    var = np.sum((log_n - nm) ** 2)
    h = cov / var if var > 0 else 0.5
    return max(0.0, min(1.0, h))

def dynamic_z_critical(h):
    return Z_BASE * (1.0 + GAMMA * max(0.0, h - 0.5))

# ============================================================================
# SYNTHETIC 2022 SCENARIO GENERATORS
# ============================================================================

def generate_mean_reverting_spread(n, mu=0.0, theta=0.5, sigma=0.001):
    """Normal OU process (what our system expects)."""
    spread = np.zeros(n)
    spread[0] = mu
    dt = 1/60  # M1
    for i in range(1, n):
        spread[i] = spread[i-1] + theta*(mu - spread[i-1])*dt + sigma*np.sqrt(dt)*np.random.randn()
    return spread

def generate_trending_spread(n, drift_per_bar=0.00002, sigma=0.001):
    """Trending spread (2022 USD rally style). H >> 0.5."""
    spread = np.zeros(n)
    for i in range(1, n):
        spread[i] = spread[i-1] + drift_per_bar + sigma*np.random.randn()*np.sqrt(1/60)
    return spread

def generate_crash_spike(n, crash_bar=None, crash_magnitude=0.05, recovery_bars=500, sigma=0.001):
    """Flash crash + recovery (GBP Sep 2022 style)."""
    if crash_bar is None:
        crash_bar = n // 2
    spread = generate_mean_reverting_spread(n, sigma=sigma)
    # Inject crash
    spread[crash_bar] += crash_magnitude
    # Gradual recovery
    for i in range(crash_bar+1, min(crash_bar+recovery_bars, n)):
        recovery_pct = (i - crash_bar) / recovery_bars
        spread[i] += crash_magnitude * (1 - recovery_pct) * 0.8
    return spread

def generate_regime_switching(n, segments=5, sigma=0.001):
    """Alternating MR and trending regimes (2022 uncertainty)."""
    spread = np.zeros(n)
    seg_len = n // segments
    for s in range(segments):
        start = s * seg_len
        end = min((s+1) * seg_len, n)
        if s % 2 == 0:
            # Mean-reverting segment
            for i in range(start+1, end):
                spread[i] = spread[i-1] + 0.5*(0 - spread[i-1])/60 + sigma*np.random.randn()*np.sqrt(1/60)
        else:
            # Trending segment with random drift direction
            drift = np.random.choice([-1, 1]) * 0.00003
            for i in range(start+1, end):
                spread[i] = spread[i-1] + drift + sigma*1.5*np.random.randn()*np.sqrt(1/60)
    return spread

def generate_correlation_breakdown(n, break_start=None, break_duration=5000, sigma=0.001):
    """Pair loses cointegration temporarily (Ukraine invasion style)."""
    if break_start is None:
        break_start = n // 3
    spread = generate_mean_reverting_spread(n, sigma=sigma)
    # During breakdown, add non-reverting component
    for i in range(break_start, min(break_start + break_duration, n)):
        pct = (i - break_start) / break_duration
        if pct < 0.5:
            spread[i] += 0.02 * pct * 2  # Diverge
        else:
            spread[i] += 0.02 * (1 - (pct - 0.5) * 2)  # Slowly reconverge
    return spread

def generate_high_vol_whipsaw(n, sigma=0.003):
    """High volatility whipsaw (Fed announcement days)."""
    spread = np.zeros(n)
    for i in range(1, n):
        # Occasional vol spikes
        vol_mult = 1.0
        if np.random.random() < 0.01:  # 1% chance of 5x vol
            vol_mult = 5.0
        spread[i] = spread[i-1] + 0.3*(0 - spread[i-1])/60 + sigma*vol_mult*np.random.randn()*np.sqrt(1/60)
    return spread


# ============================================================================
# BACKTEST ENGINE
# ============================================================================

def run_backtest_on_spread(spread, mode='fixed', use_hurst=True):
    n = len(spread)
    w_mean, w_m2, w_var = 0.0, 0.0, 1e-10
    alpha_w = 2.0 / (WELFORD_SPAN + 1)
    count = 0
    position = 0
    entry_spread = 0.0
    entry_z = 0.0
    trades = []

    # Pre-compute Hurst if needed
    hurst_values = np.full(n, 0.5)
    if mode == 'dynamic' and use_hurst:
        step = 100
        for i in range(HURST_WINDOW, n, step):
            hurst_values[i] = compute_hurst_rs(spread[:i], HURST_WINDOW)
        for i in range(HURST_WINDOW+1, n):
            if hurst_values[i] == 0.5 and i > HURST_WINDOW:
                hurst_values[i] = hurst_values[i-1]

    for i in range(n):
        count += 1
        x = spread[i]
        if count == 1:
            w_mean = x; w_m2 = 0.0; w_var = 1e-10; z = 0.0
        else:
            d = x - w_mean
            w_mean += alpha_w * d
            d2 = x - w_mean
            w_m2 = (1-alpha_w)*w_m2 + alpha_w*d*d2
            w_var = max(w_m2, 1e-10)
            z = (x - w_mean) / max(np.sqrt(w_var), 1e-8)

        if count < 200:
            continue

        z_entry = Z_BASE if mode == 'fixed' else dynamic_z_critical(hurst_values[i])

        if position == 0:
            if z > z_entry:
                position = -1; entry_spread = x; entry_z = z
            elif z < -z_entry:
                position = 1; entry_spread = x; entry_z = z
        else:
            should_exit = False
            if position == 1 and z > -EXIT_Z:
                should_exit = True
            elif position == -1 and z < EXIT_Z:
                should_exit = True
            if abs(z) > abs(entry_z) * 2.5:
                should_exit = True
            if should_exit:
                pnl = (x - entry_spread) * position * 1000
                trades.append(pnl)
                position = 0

    return trades


def calc_metrics(trades):
    if not trades:
        return {'trades': 0, 'wr': 0, 'pf': 0, 'pnl': 0, 'max_dd': 0, 'avg_win': 0, 'avg_loss': 0}
    pnls = np.array(trades)
    w = pnls[pnls > 0]
    l = pnls[pnls <= 0]
    gp = np.sum(w) if len(w) > 0 else 0
    gl = abs(np.sum(l)) if len(l) > 0 else 0.001
    eq = np.cumsum(pnls)
    pk = np.maximum.accumulate(eq)
    dd = pk - eq
    return {
        'trades': len(trades),
        'wr': len(w)/len(trades)*100,
        'pf': gp/gl,
        'pnl': np.sum(pnls),
        'max_dd': np.max(dd) if len(dd) > 0 else 0,
        'avg_win': np.mean(w) if len(w) > 0 else 0,
        'avg_loss': np.mean(l) if len(l) > 0 else 0,
    }


# ============================================================================
# MAIN
# ============================================================================

def main():
    np.random.seed(2022)  # Reproducible

    print("=" * 80)
    print("v5.5 SYNTHETIC 2022 STRESS TEST")
    print("=" * 80)
    print(f"\nModeling 2022 market characteristics:")
    print(f"  1. Strong Trending (USD Rally / Tech Crash)")
    print(f"  2. Flash Crash (GBP Sep 2022 / Liz Truss)")
    print(f"  3. Regime Switching (MR <-> Trending oscillation)")
    print(f"  4. Correlation Breakdown (Ukraine Invasion)")
    print(f"  5. High-Vol Whipsaw (Fed Announcements)")
    print(f"  6. Combined Worst-Case (All of the above)")
    print(f"\nBars per scenario: {N_BARS:,} (~35 trading days)")
    print(f"v5.3: Fixed Z=2.0 | v5.5: Dynamic Z = {Z_BASE}*(1+{GAMMA}*max(0,H-0.5))")

    scenarios = {
        "1. BASELINE (Normal MR)": lambda: generate_mean_reverting_spread(N_BARS, sigma=0.001),
        "2. STRONG TRENDING (USD Rally)": lambda: generate_trending_spread(N_BARS, drift_per_bar=0.00002, sigma=0.001),
        "3. FLASH CRASH (GBP Sep 2022)": lambda: generate_crash_spike(N_BARS, crash_magnitude=0.04, sigma=0.001),
        "4. REGIME SWITCHING (Uncertainty)": lambda: generate_regime_switching(N_BARS, segments=6, sigma=0.001),
        "5. CORRELATION BREAKDOWN (Ukraine)": lambda: generate_correlation_breakdown(N_BARS, break_duration=8000, sigma=0.001),
        "6. HIGH-VOL WHIPSAW (Fed Days)": lambda: generate_high_vol_whipsaw(N_BARS, sigma=0.002),
        "7. EXTREME TRENDING (H~0.7)": lambda: generate_trending_spread(N_BARS, drift_per_bar=0.00004, sigma=0.0008),
        "8. COMBINED WORST-CASE": None,  # Special handling
    }

    all_results = {}

    for name, generator in scenarios.items():
        print(f"\n\n{'='*70}")
        print(f"SCENARIO: {name}")
        print(f"{'='*70}")

        if name == "8. COMBINED WORST-CASE":
            # Concatenate worst scenarios
            parts = [
                generate_trending_spread(10000, drift_per_bar=0.00003, sigma=0.001),
                generate_crash_spike(10000, crash_bar=2000, crash_magnitude=0.04, sigma=0.001),
                generate_regime_switching(10000, segments=4, sigma=0.0015),
                generate_correlation_breakdown(10000, break_start=2000, break_duration=6000, sigma=0.001),
                generate_high_vol_whipsaw(10000, sigma=0.003),
            ]
            spread = np.concatenate(parts)
        else:
            spread = generator()

        # Compute Hurst on the synthetic data
        h_samples = []
        for i in range(HURST_WINDOW, len(spread), 500):
            h_samples.append(compute_hurst_rs(spread[:i], HURST_WINDOW))
        h_mean = np.mean(h_samples) if h_samples else 0.5
        z_crit_avg = dynamic_z_critical(h_mean)
        print(f"  Spread Hurst: {h_mean:.3f} | Dynamic Z at avg: {z_crit_avg:.2f}")

        # Run both versions
        trades_v53 = run_backtest_on_spread(spread, mode='fixed')
        trades_v55 = run_backtest_on_spread(spread, mode='dynamic')

        m53 = calc_metrics(trades_v53)
        m55 = calc_metrics(trades_v55)

        print(f"\n  {'Metric':<20} {'v5.3 (Z=2.0)':>15} {'v5.5 (Dyn-Z)':>15} {'Delta':>15}")
        print(f"  {'-'*65}")
        print(f"  {'Trades':<20} {m53['trades']:>15} {m55['trades']:>15} {m55['trades']-m53['trades']:>+15}")
        print(f"  {'Win Rate':<20} {m53['wr']:>14.1f}% {m55['wr']:>14.1f}% {m55['wr']-m53['wr']:>+14.1f}%")
        print(f"  {'Profit Factor':<20} {m53['pf']:>15.2f} {m55['pf']:>15.2f} {m55['pf']-m53['pf']:>+15.2f}")
        print(f"  {'Total P&L':<20} ${m53['pnl']:>14.2f} ${m55['pnl']:>14.2f} ${m55['pnl']-m53['pnl']:>+14.2f}")
        print(f"  {'Max Drawdown':<20} ${m53['max_dd']:>14.2f} ${m55['max_dd']:>14.2f} ${m55['max_dd']-m53['max_dd']:>+14.2f}")

        # Verdict
        better_wr = m55['wr'] >= m53['wr']
        better_pf = m55['pf'] >= m53['pf']
        better_dd = m55['max_dd'] <= m53['max_dd']
        less_loss = m55['pnl'] >= m53['pnl']

        if better_pf and better_dd:
            verdict = "v5.5 WINS (better risk-adjusted)"
        elif better_dd and not less_loss:
            verdict = "v5.5 SAFER (less drawdown, less P&L)"
        elif less_loss and not better_dd:
            verdict = "v5.3 WINS (more profit)"
        else:
            verdict = "MIXED"

        dd_reduction = ((m53['max_dd'] - m55['max_dd']) / max(m53['max_dd'], 0.01)) * 100

        print(f"\n  VERDICT: {verdict}")
        print(f"  DD Reduction: {dd_reduction:+.1f}%")

        all_results[name] = {
            'hurst_mean': float(h_mean),
            'z_crit_avg': float(z_crit_avg),
            'v53': m53,
            'v55': m55,
            'verdict': verdict,
            'dd_reduction_pct': float(dd_reduction),
        }

    # ========== FINAL SUMMARY ==========
    print(f"\n\n{'='*80}")
    print(f"2022 STRESS TEST - FINAL SUMMARY")
    print(f"{'='*80}")
    print(f"\n  {'Scenario':<40} {'v5.3 DD':>10} {'v5.5 DD':>10} {'DD Chg':>10} {'Verdict':>25}")
    print(f"  {'-'*95}")

    v55_wins = 0
    total = 0
    for name, r in all_results.items():
        dd53 = r['v53']['max_dd']
        dd55 = r['v55']['max_dd']
        dd_chg = r['dd_reduction_pct']
        print(f"  {name:<40} ${dd53:>9.2f} ${dd55:>9.2f} {dd_chg:>+9.1f}% {r['verdict']:>25}")
        total += 1
        if 'v5.5' in r['verdict']:
            v55_wins += 1

    print(f"\n  v5.5 wins {v55_wins}/{total} scenarios ({v55_wins/total*100:.0f}%)")

    # Average DD reduction
    avg_dd_reduction = np.mean([r['dd_reduction_pct'] for r in all_results.values()])
    print(f"  Average DD reduction: {avg_dd_reduction:+.1f}%")

    # Save
    output_path = Path("results/v55_2022_stress_results.json")
    output_path.parent.mkdir(exist_ok=True)

    def convert_np(obj):
        if isinstance(obj, (np.integer,)): return int(obj)
        elif isinstance(obj, (np.floating,)): return float(obj)
        elif isinstance(obj, np.ndarray): return obj.tolist()
        elif isinstance(obj, np.bool_): return bool(obj)
        return obj

    with open(output_path, 'w') as f:
        json.dump(all_results, f, indent=2, default=convert_np)
    print(f"\n  Results saved to: {output_path}")


if __name__ == "__main__":
    main()
