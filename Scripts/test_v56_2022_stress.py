#!/usr/bin/env python3
"""
v5.6 2022 Stress Test (Synthetic) — Dynamic Exit Z Edition
============================================================

Same 8 synthetic 2022 scenarios as v5.5 stress test, but now comparing:
- v5.3: Fixed entry Z=2.0, fixed exit Z=0.5
- v5.5: Dynamic entry Z (Hurst), fixed exit Z=0.5
- v5.6: Dynamic entry Z (Hurst) + Dynamic EXIT Z (Hurst)

Dynamic Exit Z: Z_exit = 0.5 × (1 + 2.0 × (H - 0.5))
  H low  → hold longer (squeeze reversion)
  H high → exit sooner (take profit before trend reasserts)
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
FIXED_EXIT_Z = 0.5
EXIT_Z_BASE = 0.5
EXIT_GAMMA = 2.0
WELFORD_SPAN = 100
HURST_WINDOW = 512
N_BARS = 50000

# ============================================================================
# HURST & DYNAMIC Z
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

def dynamic_exit_z(h):
    raw = EXIT_Z_BASE * (1.0 + EXIT_GAMMA * (h - 0.5))
    return max(0.1, min(1.0, raw))

# ============================================================================
# SYNTHETIC 2022 SCENARIO GENERATORS
# ============================================================================

def generate_mean_reverting_spread(n, mu=0.0, theta=0.5, sigma=0.001):
    spread = np.zeros(n)
    spread[0] = mu
    dt = 1/60
    for i in range(1, n):
        spread[i] = spread[i-1] + theta*(mu - spread[i-1])*dt + sigma*np.sqrt(dt)*np.random.randn()
    return spread

def generate_trending_spread(n, drift_per_bar=0.00002, sigma=0.001):
    spread = np.zeros(n)
    for i in range(1, n):
        spread[i] = spread[i-1] + drift_per_bar + sigma*np.random.randn()*np.sqrt(1/60)
    return spread

def generate_crash_spike(n, crash_bar=None, crash_magnitude=0.05, recovery_bars=500, sigma=0.001):
    if crash_bar is None:
        crash_bar = n // 2
    spread = generate_mean_reverting_spread(n, sigma=sigma)
    spread[crash_bar] += crash_magnitude
    for i in range(crash_bar+1, min(crash_bar+recovery_bars, n)):
        recovery_pct = (i - crash_bar) / recovery_bars
        spread[i] += crash_magnitude * (1 - recovery_pct) * 0.8
    return spread

def generate_regime_switching(n, segments=5, sigma=0.001):
    spread = np.zeros(n)
    seg_len = n // segments
    for s in range(segments):
        start = s * seg_len
        end = min((s+1) * seg_len, n)
        if s % 2 == 0:
            for i in range(start+1, end):
                spread[i] = spread[i-1] + 0.5*(0 - spread[i-1])/60 + sigma*np.random.randn()*np.sqrt(1/60)
        else:
            drift = np.random.choice([-1, 1]) * 0.00003
            for i in range(start+1, end):
                spread[i] = spread[i-1] + drift + sigma*1.5*np.random.randn()*np.sqrt(1/60)
    return spread

def generate_correlation_breakdown(n, break_start=None, break_duration=5000, sigma=0.001):
    if break_start is None:
        break_start = n // 3
    spread = generate_mean_reverting_spread(n, sigma=sigma)
    for i in range(break_start, min(break_start + break_duration, n)):
        pct = (i - break_start) / break_duration
        if pct < 0.5:
            spread[i] += 0.02 * pct * 2
        else:
            spread[i] += 0.02 * (1 - (pct - 0.5) * 2)
    return spread

def generate_high_vol_whipsaw(n, sigma=0.003):
    spread = np.zeros(n)
    for i in range(1, n):
        vol_mult = 5.0 if np.random.random() < 0.01 else 1.0
        spread[i] = spread[i-1] + 0.3*(0 - spread[i-1])/60 + sigma*vol_mult*np.random.randn()*np.sqrt(1/60)
    return spread

# ============================================================================
# BACKTEST ENGINE — 3 MODES
# ============================================================================

def run_backtest_on_spread(spread, mode='fixed'):
    """
    Modes:
    - 'fixed':   v5.3 — fixed entry Z=2.0, fixed exit Z=0.5
    - 'dynamic': v5.5 — dynamic entry Z (Hurst), fixed exit Z=0.5
    - 'v56':     v5.6 — dynamic entry Z (Hurst) + dynamic exit Z (Hurst)
    """
    n = len(spread)
    w_mean, w_m2, w_var = 0.0, 0.0, 1e-10
    alpha_w = 2.0 / (WELFORD_SPAN + 1)
    count = 0
    position = 0
    entry_spread = 0.0
    entry_z = 0.0
    trades = []
    exit_z_used_list = []

    # Pre-compute Hurst
    hurst_values = np.full(n, 0.5)
    if mode in ('dynamic', 'v56'):
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

        h = hurst_values[i]

        # Entry threshold
        if mode == 'fixed':
            z_entry = Z_BASE
        else:
            z_entry = dynamic_z_critical(h)

        # Exit threshold
        if mode == 'v56':
            exit_z = dynamic_exit_z(h)
        else:
            exit_z = FIXED_EXIT_Z

        if position == 0:
            if z > z_entry:
                position = -1; entry_spread = x; entry_z = z
                exit_z_used_list.append(exit_z)
            elif z < -z_entry:
                position = 1; entry_spread = x; entry_z = z
                exit_z_used_list.append(exit_z)
        else:
            should_exit = False
            if position == 1 and z > -exit_z:
                should_exit = True
            elif position == -1 and z < exit_z:
                should_exit = True
            if abs(z) > abs(entry_z) * 2.5:
                should_exit = True
            if should_exit:
                pnl = (x - entry_spread) * position * 1000
                trades.append(pnl)
                position = 0

    return trades, exit_z_used_list


def calc_metrics(trades):
    if not trades:
        return {'trades': 0, 'wr': 0, 'pf': 0, 'pnl': 0, 'max_dd': 0}
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
        'wr': len(w)/len(trades)*100 if len(trades) > 0 else 0,
        'pf': gp/gl,
        'pnl': float(np.sum(pnls)),
        'max_dd': float(np.max(dd)) if len(dd) > 0 else 0,
    }


# ============================================================================
# MAIN
# ============================================================================

def main():
    np.random.seed(2022)  # Same seed as v5.5 test for reproducibility

    print("=" * 90)
    print("v5.6 SYNTHETIC 2022 STRESS TEST — DYNAMIC EXIT Z EDITION")
    print("=" * 90)
    print(f"\nThree versions compared:")
    print(f"  v5.3: Fixed entry Z=2.0, fixed exit Z=0.5")
    print(f"  v5.5: Dynamic entry Z = {Z_BASE}*(1+{GAMMA}*max(0,H-0.5)), fixed exit Z=0.5")
    print(f"  v5.6: Dynamic entry Z + Dynamic exit Z = {EXIT_Z_BASE}*(1+{EXIT_GAMMA}*(H-0.5))")
    print(f"\nBars per scenario: {N_BARS:,}")

    scenarios = {
        "1. BASELINE (Normal MR)": lambda: generate_mean_reverting_spread(N_BARS, sigma=0.001),
        "2. STRONG TRENDING (USD Rally)": lambda: generate_trending_spread(N_BARS, drift_per_bar=0.00002, sigma=0.001),
        "3. FLASH CRASH (GBP Sep 2022)": lambda: generate_crash_spike(N_BARS, crash_magnitude=0.04, sigma=0.001),
        "4. REGIME SWITCHING (Uncertainty)": lambda: generate_regime_switching(N_BARS, segments=6, sigma=0.001),
        "5. CORRELATION BREAKDOWN (Ukraine)": lambda: generate_correlation_breakdown(N_BARS, break_duration=8000, sigma=0.001),
        "6. HIGH-VOL WHIPSAW (Fed Days)": lambda: generate_high_vol_whipsaw(N_BARS, sigma=0.002),
        "7. EXTREME TRENDING (H~0.7)": lambda: generate_trending_spread(N_BARS, drift_per_bar=0.00004, sigma=0.0008),
        "8. COMBINED WORST-CASE": None,
    }

    all_results = {}

    for name, generator in scenarios.items():
        print(f"\n\n{'='*90}")
        print(f"SCENARIO: {name}")
        print(f"{'='*90}")

        if name == "8. COMBINED WORST-CASE":
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

        # Compute Hurst stats
        h_samples = []
        for i in range(HURST_WINDOW, len(spread), 500):
            h_samples.append(compute_hurst_rs(spread[:i], HURST_WINDOW))
        h_mean = np.mean(h_samples) if h_samples else 0.5
        z_crit_avg = dynamic_z_critical(h_mean)
        exit_z_avg = dynamic_exit_z(h_mean)
        print(f"  Hurst: {h_mean:.3f} | Entry Z_crit: {z_crit_avg:.2f} | Exit Z_dyn: {exit_z_avg:.3f}")

        # Run all three versions
        t0 = time.time()
        trades_v53, _ = run_backtest_on_spread(spread, mode='fixed')
        trades_v55, _ = run_backtest_on_spread(spread, mode='dynamic')
        trades_v56, exit_zs = run_backtest_on_spread(spread, mode='v56')
        elapsed = time.time() - t0

        m53 = calc_metrics(trades_v53)
        m55 = calc_metrics(trades_v55)
        m56 = calc_metrics(trades_v56)

        print(f"  Computed in {elapsed*1000:.0f}ms")

        print(f"\n  {'Metric':<20} {'v5.3 (fixed)':>14} {'v5.5 (dyn-E)':>14} {'v5.6 (dyn-EX)':>14} {'v56 vs v55':>14}")
        print(f"  {'-'*76}")
        print(f"  {'Trades':<20} {m53['trades']:>14} {m55['trades']:>14} {m56['trades']:>14} {m56['trades']-m55['trades']:>+14}")
        print(f"  {'Win Rate':<20} {m53['wr']:>13.1f}% {m55['wr']:>13.1f}% {m56['wr']:>13.1f}% {m56['wr']-m55['wr']:>+13.1f}%")
        print(f"  {'Profit Factor':<20} {m53['pf']:>14.2f} {m55['pf']:>14.2f} {m56['pf']:>14.2f} {m56['pf']-m55['pf']:>+14.2f}")
        print(f"  {'Total P&L':<20} ${m53['pnl']:>13.2f} ${m55['pnl']:>13.2f} ${m56['pnl']:>13.2f} ${m56['pnl']-m55['pnl']:>+13.2f}")
        print(f"  {'Max Drawdown':<20} ${m53['max_dd']:>13.2f} ${m55['max_dd']:>13.2f} ${m56['max_dd']:>13.2f} ${m56['max_dd']-m55['max_dd']:>+13.2f}")

        if exit_zs:
            print(f"\n  Dynamic Exit Z: mean={np.mean(exit_zs):.3f}, min={np.min(exit_zs):.3f}, max={np.max(exit_zs):.3f}")

        # DD reduction from v5.3 baseline
        dd_red_v55 = ((m53['max_dd'] - m55['max_dd']) / max(m53['max_dd'], 0.01)) * 100
        dd_red_v56 = ((m53['max_dd'] - m56['max_dd']) / max(m53['max_dd'], 0.01)) * 100
        dd_v56_vs_v55 = ((m55['max_dd'] - m56['max_dd']) / max(m55['max_dd'], 0.01)) * 100

        print(f"\n  DD Reduction (vs v5.3): v5.5={dd_red_v55:+.1f}% | v5.6={dd_red_v56:+.1f}%")
        print(f"  DD Change (v5.6 vs v5.5): {dd_v56_vs_v55:+.1f}%")

        # Verdict
        if m56['pf'] > m55['pf'] and m56['max_dd'] <= m55['max_dd']:
            verdict_56 = "v5.6 WINS (better PF + lower DD)"
        elif m56['max_dd'] < m55['max_dd']:
            verdict_56 = "v5.6 SAFER (lower DD)"
        elif m56['pf'] > m55['pf']:
            verdict_56 = "v5.6 BETTER PF (but DD differs)"
        elif m56['wr'] > m55['wr']:
            verdict_56 = "v5.6 BETTER WR"
        elif abs(m56['pf'] - m55['pf']) < 0.01 and abs(m56['max_dd'] - m55['max_dd']) < 0.1:
            verdict_56 = "EQUIVALENT"
        else:
            verdict_56 = "v5.5 BETTER"

        print(f"  VERDICT (v5.6 vs v5.5): {verdict_56}")

        all_results[name] = {
            'hurst_mean': float(h_mean),
            'exit_z_avg': float(exit_z_avg),
            'v53': m53, 'v55': m55, 'v56': m56,
            'dd_red_v55': float(dd_red_v55),
            'dd_red_v56': float(dd_red_v56),
            'dd_v56_vs_v55': float(dd_v56_vs_v55),
            'verdict': verdict_56,
        }

    # ========== FINAL SUMMARY ==========
    print(f"\n\n{'='*90}")
    print(f"2022 STRESS TEST — FINAL SUMMARY (v5.3 vs v5.5 vs v5.6)")
    print(f"{'='*90}")

    print(f"\n  {'Scenario':<35} {'v5.3 DD':>9} {'v5.5 DD':>9} {'v5.6 DD':>9} {'v56/v55':>9} {'Verdict':>25}")
    print(f"  {'-'*96}")

    v56_wins = 0
    v56_equiv = 0
    total = 0
    for name, r in all_results.items():
        dd53 = r['v53']['max_dd']
        dd55 = r['v55']['max_dd']
        dd56 = r['v56']['max_dd']
        v56v55 = r['dd_v56_vs_v55']
        print(f"  {name:<35} ${dd53:>8.2f} ${dd55:>8.2f} ${dd56:>8.2f} {v56v55:>+8.1f}% {r['verdict']:>25}")
        total += 1
        if 'v5.6' in r['verdict']:
            v56_wins += 1
        elif 'EQUIV' in r['verdict']:
            v56_equiv += 1

    print(f"\n  v5.6 wins: {v56_wins}/{total} | Equivalent: {v56_equiv}/{total} | v5.5 better: {total-v56_wins-v56_equiv}/{total}")

    # P&L comparison
    print(f"\n  {'Scenario':<35} {'v5.3 P&L':>10} {'v5.5 P&L':>10} {'v5.6 P&L':>10} {'v56-v55':>10}")
    print(f"  {'-'*75}")
    for name, r in all_results.items():
        pnl53 = r['v53']['pnl']
        pnl55 = r['v55']['pnl']
        pnl56 = r['v56']['pnl']
        print(f"  {name:<35} ${pnl53:>9.2f} ${pnl55:>9.2f} ${pnl56:>9.2f} ${pnl56-pnl55:>+9.2f}")

    # Key insight
    print(f"\n\n  KEY INSIGHT:")
    print(f"  Dynamic Exit Z adapts exit aggressiveness to the current regime:")
    print(f"  • In trending scenarios (H>0.5): exits sooner → protects against trend reasserting")
    print(f"  • In MR scenarios (H<0.5): holds longer → squeezes more mean-reversion profit")
    print(f"  • In mixed scenarios: seamlessly transitions between the two")

    # Save
    output_path = Path("results/v56_2022_stress_results.json")
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
