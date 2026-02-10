#!/usr/bin/env python3
"""
v5.6 Dynamic Dwell Backtest — 3.5-Month Real M1 Data
=====================================================

Compares three modes on real M1 data (Holy Trio):
  1. v5.6 baseline   — Dynamic Z entry + Dynamic Z exit (NO dwell)
  2. v5.6 + dwell    — Same + Dynamic Hurst-Adaptive Dwell + Re-entry Cooldown

Dynamic Dwell Formula (matches engine.py):
    dwell_seconds = 60.0 * (H / 0.3)
    clamped to [30, 300] seconds → [1, 5] M1 bars

Emergency exits (|Z| > 2.5× entry) ALWAYS bypass the dwell.
Sentinel aborts ALWAYS bypass the dwell.

Runs on: US100/DE40, AUDUSD/NZDUSD, EURUSD/GBPUSD
Data:    ~3.5 months of M1 bars from data/historical/
"""

import numpy as np
import pandas as pd
import json
import time
import math
from pathlib import Path
import sys
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ============================================================================
# CONFIGURATION (exact match to engine.py)
# ============================================================================

HURST_WINDOW = 512
Z_BASE = 2.0
GAMMA = 6.0
EXIT_Z_BASE = 0.5
EXIT_GAMMA = 2.0
WELFORD_SPAN = 100

# Dynamic Dwell parameters (exact match to engine.py)
DWELL_BASE_SECONDS = 60.0
DWELL_HURST_ANCHOR = 0.3
DWELL_MIN_SECONDS = 30.0
DWELL_MAX_SECONDS = 300.0

PAIRS = [
    ("US100/DE40", "US100", "DE40", 0),
    ("AUDUSD/NZDUSD", "AUDUSD", "NZDUSD", 1),
    ("EURUSD/GBPUSD", "EURUSD", "GBPUSD", 2),
]


# ============================================================================
# DYNAMIC DWELL (exact match to engine.py _calculate_dynamic_dwell)
# ============================================================================

def calculate_dynamic_dwell_seconds(hurst_value):
    """
    Hurst-adaptive minimum hold time in seconds.
    Formula: dwell = 60 * (H / 0.3), clamped [30, 300].
    """
    raw = DWELL_BASE_SECONDS * (hurst_value / DWELL_HURST_ANCHOR)
    return max(DWELL_MIN_SECONDS, min(DWELL_MAX_SECONDS, raw))


def calculate_dynamic_dwell_bars(hurst_value):
    """Convert dwell seconds to M1 bars (ceiling)."""
    seconds = calculate_dynamic_dwell_seconds(hurst_value)
    return max(1, int(math.ceil(seconds / 60.0)))


# ============================================================================
# HURST (R/S Analysis — exact match to Rust/Python reference)
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
        n_segments = len(returns) // n
        if n_segments == 0:
            continue
        rs_values = []
        for seg in range(n_segments):
            segment = returns[seg*n:(seg+1)*n]
            mean = np.mean(segment)
            std = np.std(segment, ddof=1)
            if std < 1e-10:
                continue
            cumsum = np.cumsum(segment - mean)
            R = np.max(cumsum) - np.min(cumsum)
            rs = R / std
            if np.isfinite(rs) and rs > 0:
                rs_values.append(rs)
        if rs_values:
            avg_rs = np.mean(rs_values)
            if avg_rs > 0:
                log_n.append(np.log(n))
                log_rs.append(np.log(avg_rs))
    if len(log_n) < 2:
        return 0.5
    log_n, log_rs = np.array(log_n), np.array(log_rs)
    n_mean, rs_mean = np.mean(log_n), np.mean(log_rs)
    cov = np.sum((log_n - n_mean) * (log_rs - rs_mean))
    var = np.sum((log_n - n_mean) ** 2)
    hurst = cov / var if var > 0 else 0.5
    return max(0.0, min(1.0, hurst))


def dynamic_z_critical(hurst, z_base=Z_BASE, gamma=GAMMA):
    return z_base * (1.0 + gamma * max(0.0, hurst - 0.5))


def dynamic_exit_z(hurst, exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA):
    raw = exit_z_base * (1.0 + exit_gamma * (hurst - 0.5))
    return max(0.1, min(1.0, raw))


# ============================================================================
# BACKTEST ENGINE
# ============================================================================

def run_backtest(pair_name, log_a, log_b, mode='v56_no_dwell', hurst_values=None):
    """
    Modes:
    - 'v56_no_dwell':  v5.6 Dynamic entry Z + Dynamic exit Z (baseline, no dwell)
    - 'v56_dwell':     v5.6 + Dynamic Hurst Dwell + Re-entry Cooldown
    """
    spread = log_a - 1.0 * log_b
    n = len(spread)
    use_dwell = (mode == 'v56_dwell')

    # Welford online normalizer
    w_mean, w_m2, w_var = 0.0, 0.0, 1.0
    alpha_w = 2.0 / (WELFORD_SPAN + 1)
    count = 0

    # Position state
    position = 0
    entry_spread = 0.0
    entry_z = 0.0
    entry_bar = 0
    entry_hurst = 0.5

    # Dwell tracking
    last_close_bar = -9999  # Allow immediate first entry

    trades = []
    dwell_stats = {'enforced': 0, 'bypassed_emergency': 0, 'cooldown_blocked': 0}

    for i in range(n):
        count += 1
        x = spread[i]

        if count == 1:
            w_mean = x
            w_m2, w_var = 0.0, 1e-10
            z = 0.0
        else:
            delta = x - w_mean
            w_mean += alpha_w * delta
            delta2 = x - w_mean
            w_m2 = (1 - alpha_w) * w_m2 + alpha_w * delta * delta2
            w_var = max(w_m2, 1e-10)
            z = (x - w_mean) / max(np.sqrt(w_var), 1e-8)

        if count < 200:
            continue

        h = hurst_values[i] if hurst_values is not None and not np.isnan(hurst_values[i]) else 0.5
        z_entry = dynamic_z_critical(h)
        exit_z_val = dynamic_exit_z(h)

        if position == 0:
            # --- ENTRY LOGIC ---
            signal = 0
            if z > z_entry:
                signal = -1
            elif z < -z_entry:
                signal = 1

            if signal != 0:
                # Re-entry cooldown check
                if use_dwell:
                    cooldown_bars = calculate_dynamic_dwell_bars(h)
                    if (i - last_close_bar) < cooldown_bars:
                        dwell_stats['cooldown_blocked'] += 1
                        continue  # Blocked by re-entry cooldown

                position = signal
                entry_spread = x
                entry_z = z
                entry_bar = i
                entry_hurst = h

        else:
            # --- EXIT LOGIC ---

            # Emergency exit: |Z| > 2.5× entry (ALWAYS bypasses dwell)
            is_emergency = abs(z) > abs(entry_z) * 2.5
            if is_emergency:
                pnl = (x - entry_spread) * position * 1000
                hold_bars = i - entry_bar
                dwell_bars = calculate_dynamic_dwell_bars(h) if use_dwell else 0
                trades.append({
                    'pnl': pnl, 'bar': i, 'entry_bar': entry_bar,
                    'entry_z': entry_z, 'exit_z_used': exit_z_val,
                    'hurst_at_entry': entry_hurst, 'hurst_at_exit': h,
                    'hold_bars': hold_bars, 'dwell_bars': dwell_bars,
                    'exit_reason': 'EMERGENCY_2.5X',
                })
                dwell_stats['bypassed_emergency'] += 1
                last_close_bar = i
                position = 0
                continue

            # Dwell enforcement (only for normal exits)
            if use_dwell:
                dwell_bars = calculate_dynamic_dwell_bars(h)
                hold_bars = i - entry_bar
                if hold_bars < dwell_bars:
                    continue  # Still within dwell — hold

            # Normal dynamic exit
            should_exit = False
            if position == 1 and z > -exit_z_val:
                should_exit = True
            elif position == -1 and z < exit_z_val:
                should_exit = True

            if should_exit:
                pnl = (x - entry_spread) * position * 1000
                hold_bars = i - entry_bar
                dwell_bars_val = calculate_dynamic_dwell_bars(h) if use_dwell else 0
                trades.append({
                    'pnl': pnl, 'bar': i, 'entry_bar': entry_bar,
                    'entry_z': entry_z, 'exit_z_used': exit_z_val,
                    'hurst_at_entry': entry_hurst, 'hurst_at_exit': h,
                    'hold_bars': hold_bars, 'dwell_bars': dwell_bars_val,
                    'exit_reason': 'DYNAMIC_EXIT',
                })
                if use_dwell:
                    dwell_stats['enforced'] += 1
                last_close_bar = i
                position = 0

    return trades, dwell_stats


def calc_metrics(trades):
    if not trades:
        return {'trades': 0, 'win_rate': 0, 'profit_factor': 0,
                'total_pnl': 0, 'avg_win': 0, 'avg_loss': 0, 'max_dd': 0,
                'avg_hold_bars': 0, 'min_hold_bars': 0, 'max_hold_bars': 0,
                'emergency_exits': 0}
    pnls = [t['pnl'] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]
    gross_profit = sum(winners) if winners else 0
    gross_loss = abs(sum(losers)) if losers else 0.001
    equity = np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd = peak - equity
    max_dd = np.max(dd) if len(dd) > 0 else 0
    hold_bars = [t['hold_bars'] for t in trades]
    emergency_exits = sum(1 for t in trades if t['exit_reason'] == 'EMERGENCY_2.5X')
    return {
        'trades': len(trades), 'winners': len(winners), 'losers': len(losers),
        'win_rate': len(winners) / len(trades) * 100,
        'profit_factor': gross_profit / gross_loss,
        'total_pnl': sum(pnls),
        'avg_win': np.mean(winners) if winners else 0,
        'avg_loss': np.mean(losers) if losers else 0,
        'max_dd': max_dd,
        'avg_hold_bars': np.mean(hold_bars) if hold_bars else 0,
        'min_hold_bars': min(hold_bars) if hold_bars else 0,
        'max_hold_bars': max(hold_bars) if hold_bars else 0,
        'emergency_exits': emergency_exits,
    }


# ============================================================================
# DATA LOADING
# ============================================================================

def load_data(symbol):
    path = Path(f"data/historical/{symbol}_M1.csv")
    if not path.exists():
        raise FileNotFoundError(f"Data not found: {path}")
    df = pd.read_csv(path)
    df['time'] = pd.to_datetime(df['time'])
    return df


def align_data(df_a, df_b):
    merged = pd.merge(df_a, df_b, on='time', suffixes=('_a', '_b'))
    log_a = np.log(merged['close_a'].values)
    log_b = np.log(merged['close_b'].values)
    return log_a, log_b, merged['time'].values


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 90)
    print("v5.6 DYNAMIC DWELL BACKTEST — 3.5-MONTH REAL M1 DATA")
    print("=" * 90)
    print(f"\nDynamic Dwell Formula: dwell = {DWELL_BASE_SECONDS} * (H / {DWELL_HURST_ANCHOR})")
    print(f"  Clamped: [{DWELL_MIN_SECONDS}s, {DWELL_MAX_SECONDS}s] → [{int(DWELL_MIN_SECONDS/60)}, {int(DWELL_MAX_SECONDS/60)}] M1 bars")
    print(f"  Emergency exits (|Z|>2.5x) bypass dwell")
    print(f"  Re-entry cooldown = same dynamic dwell period\n")
    print(f"Dynamic Exit: Z_exit = {EXIT_Z_BASE} * (1 + {EXIT_GAMMA} * (H - 0.5))")
    print(f"Dynamic Entry: Z_crit = {Z_BASE} * (1 + {GAMMA} * max(0, H - 0.5))")

    all_results = {
        'version': '5.6+dwell',
        'features': ['dynamic_exit_z', 'dynamic_dwell', 'reentry_cooldown', 'emergency_bypass'],
        'dwell_formula': f'dwell = {DWELL_BASE_SECONDS} * (H / {DWELL_HURST_ANCHOR}), clamped [{DWELL_MIN_SECONDS}, {DWELL_MAX_SECONDS}]s',
        'test_date': pd.Timestamp.now().isoformat(),
        'pairs': {},
    }

    portfolio_baseline = []
    portfolio_dwell = []

    for pair_name, sym_a, sym_b, pair_idx in PAIRS:
        print(f"\n\n{'#'*90}")
        print(f"# PAIR: {pair_name}")
        print(f"{'#'*90}")

        df_a = load_data(sym_a)
        df_b = load_data(sym_b)
        log_a, log_b, times = align_data(df_a, df_b)
        spread = log_a - 1.0 * log_b
        n = len(log_a)
        n_days = n / 1440.0
        print(f"  Loaded {n:,} aligned M1 bars (~{n_days:.1f} trading days, ~{n_days/21:.1f} months)")

        # Compute rolling Hurst
        print(f"  Computing rolling Hurst (window={HURST_WINDOW})...")
        hurst_values = np.full(n, np.nan)
        step = 50
        t0 = time.time()
        for i in range(HURST_WINDOW, n, step):
            hurst_values[i] = compute_hurst_rs(spread[:i], HURST_WINDOW)
        for i in range(HURST_WINDOW + 1, n):
            if np.isnan(hurst_values[i]):
                hurst_values[i] = hurst_values[i-1] if not np.isnan(hurst_values[i-1]) else 0.5
        elapsed = time.time() - t0

        valid_h = hurst_values[~np.isnan(hurst_values)]
        h_mean = np.mean(valid_h)
        print(f"  Hurst: mean={h_mean:.4f}, range=[{np.min(valid_h):.4f}, {np.max(valid_h):.4f}], computed in {elapsed*1000:.0f}ms")

        # Show expected dwell for this pair
        dwell_at_avg = calculate_dynamic_dwell_seconds(h_mean)
        dwell_bars_at_avg = calculate_dynamic_dwell_bars(h_mean)
        print(f"  Expected Dwell at avg H={h_mean:.3f}: {dwell_at_avg:.0f}s ({dwell_bars_at_avg} M1 bars)")

        # Run v5.6 baseline (no dwell)
        print(f"\n  Running v5.6 BASELINE (no dwell)...")
        t1 = time.time()
        trades_base, _ = run_backtest(pair_name, log_a, log_b, 'v56_no_dwell', hurst_values)
        m_base = calc_metrics(trades_base)
        print(f"    Done in {(time.time()-t1)*1000:.0f}ms | {m_base['trades']} trades")

        # Run v5.6 + dwell
        print(f"  Running v5.6 + DYNAMIC DWELL...")
        t2 = time.time()
        trades_dwell, dwell_stats = run_backtest(pair_name, log_a, log_b, 'v56_dwell', hurst_values)
        m_dwell = calc_metrics(trades_dwell)
        print(f"    Done in {(time.time()-t2)*1000:.0f}ms | {m_dwell['trades']} trades")

        portfolio_baseline.extend(trades_base)
        portfolio_dwell.extend(trades_dwell)

        # Print comparison
        print(f"\n  {'='*80}")
        print(f"  RESULTS: {pair_name}")
        print(f"  {'='*80}")
        print(f"  {'Metric':<25} {'v5.6 Baseline':>15} {'v5.6 + Dwell':>15} {'Delta':>15}")
        print(f"  {'-'*70}")
        print(f"  {'Total Trades':<25} {m_base['trades']:>15} {m_dwell['trades']:>15} {m_dwell['trades']-m_base['trades']:>+15}")
        print(f"  {'Win Rate':<25} {m_base['win_rate']:>14.1f}% {m_dwell['win_rate']:>14.1f}% {m_dwell['win_rate']-m_base['win_rate']:>+14.1f}%")
        print(f"  {'Profit Factor':<25} {m_base['profit_factor']:>15.2f} {m_dwell['profit_factor']:>15.2f} {m_dwell['profit_factor']-m_base['profit_factor']:>+15.2f}")
        print(f"  {'Total P&L':<25} ${m_base['total_pnl']:>14.2f} ${m_dwell['total_pnl']:>14.2f} ${m_dwell['total_pnl']-m_base['total_pnl']:>+14.2f}")
        print(f"  {'Max Drawdown':<25} ${m_base['max_dd']:>14.2f} ${m_dwell['max_dd']:>14.2f} ${m_dwell['max_dd']-m_base['max_dd']:>+14.2f}")
        print(f"  {'Avg Hold (bars)':<25} {m_base['avg_hold_bars']:>15.1f} {m_dwell['avg_hold_bars']:>15.1f} {m_dwell['avg_hold_bars']-m_base['avg_hold_bars']:>+15.1f}")
        print(f"  {'Min Hold (bars)':<25} {m_base['min_hold_bars']:>15} {m_dwell['min_hold_bars']:>15} {m_dwell['min_hold_bars']-m_base['min_hold_bars']:>+15}")
        print(f"  {'Emergency Exits':<25} {m_base['emergency_exits']:>15} {m_dwell['emergency_exits']:>15} {m_dwell['emergency_exits']-m_base['emergency_exits']:>+15}")

        if dwell_stats:
            print(f"\n  Dwell Stats:")
            print(f"    Dwell enforced (normal exits): {dwell_stats['enforced']}")
            print(f"    Emergency bypasses:            {dwell_stats['bypassed_emergency']}")
            print(f"    Re-entry cooldowns blocked:    {dwell_stats['cooldown_blocked']}")

        # Prop firm safety check
        if m_dwell['trades'] > 0:
            min_hold = m_dwell['min_hold_bars']
            sub_30s = sum(1 for t in trades_dwell if t['hold_bars'] < 1 and t['exit_reason'] != 'EMERGENCY_2.5X')
            print(f"\n  PROP FIRM SAFETY:")
            print(f"    Min hold (non-emergency): {min_hold} bars ({min_hold * 60}s)")
            print(f"    Trades < 30s (non-emergency): {sub_30s}")
            print(f"    Status: {'PASS' if sub_30s == 0 else 'FAIL'}")

        all_results['pairs'][pair_name] = {
            'hurst_mean': float(h_mean),
            'expected_dwell_seconds': float(dwell_at_avg),
            'expected_dwell_bars': int(dwell_bars_at_avg),
            'baseline': m_base,
            'dwell': m_dwell,
            'dwell_stats': dwell_stats,
        }

    # ========== PORTFOLIO SUMMARY ==========
    print(f"\n\n{'='*90}")
    print(f"PORTFOLIO SUMMARY (ALL 3 PAIRS)")
    print(f"{'='*90}")

    pm_base = calc_metrics(portfolio_baseline)
    pm_dwell = calc_metrics(portfolio_dwell)

    print(f"\n  {'Metric':<25} {'v5.6 Baseline':>15} {'v5.6 + Dwell':>15} {'Delta':>15}")
    print(f"  {'-'*70}")
    print(f"  {'Total Trades':<25} {pm_base['trades']:>15} {pm_dwell['trades']:>15} {pm_dwell['trades']-pm_base['trades']:>+15}")
    print(f"  {'Win Rate':<25} {pm_base['win_rate']:>14.1f}% {pm_dwell['win_rate']:>14.1f}% {pm_dwell['win_rate']-pm_base['win_rate']:>+14.1f}%")
    print(f"  {'Profit Factor':<25} {pm_base['profit_factor']:>15.2f} {pm_dwell['profit_factor']:>15.2f} {pm_dwell['profit_factor']-pm_base['profit_factor']:>+15.2f}")
    print(f"  {'Total P&L':<25} ${pm_base['total_pnl']:>14.2f} ${pm_dwell['total_pnl']:>14.2f} ${pm_dwell['total_pnl']-pm_base['total_pnl']:>+14.2f}")
    print(f"  {'Max Drawdown':<25} ${pm_base['max_dd']:>14.2f} ${pm_dwell['max_dd']:>14.2f} ${pm_dwell['max_dd']-pm_base['max_dd']:>+14.2f}")
    print(f"  {'Avg Hold (bars)':<25} {pm_base['avg_hold_bars']:>15.1f} {pm_dwell['avg_hold_bars']:>15.1f} {pm_dwell['avg_hold_bars']-pm_base['avg_hold_bars']:>+15.1f}")
    print(f"  {'Min Hold (bars)':<25} {pm_base['min_hold_bars']:>15} {pm_dwell['min_hold_bars']:>15} {pm_dwell['min_hold_bars']-pm_base['min_hold_bars']:>+15}")
    print(f"  {'Emergency Exits':<25} {pm_base['emergency_exits']:>15} {pm_dwell['emergency_exits']:>15} {pm_dwell['emergency_exits']-pm_base['emergency_exits']:>+15}")

    # Dynamic Dwell reference table
    print(f"\n\n{'='*90}")
    print(f"DYNAMIC DWELL REFERENCE TABLE")
    print(f"{'='*90}")
    print(f"  Formula: dwell = {DWELL_BASE_SECONDS} * (H / {DWELL_HURST_ANCHOR}), clamped [{DWELL_MIN_SECONDS}, {DWELL_MAX_SECONDS}]s")
    print(f"\n  {'Hurst H':>10} | {'Dwell (s)':>10} | {'Dwell (bars)':>12} | {'Effect'}")
    print(f"  {'-'*65}")
    for h in [0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.70, 0.80]:
        ds = calculate_dynamic_dwell_seconds(h)
        db = calculate_dynamic_dwell_bars(h)
        if h < 0.3:
            effect = f"Fast MR — quick turnaround"
        elif abs(h - 0.3) < 0.01:
            effect = f"Standard dwell"
        elif h < 0.5:
            effect = f"Slower MR — more patience"
        elif abs(h - 0.5) < 0.01:
            effect = f"Random walk — hold longer"
        else:
            effect = f"Trending — maximum patience"
        print(f"  {h:>10.3f} | {ds:>9.0f}s | {db:>11} bar{'s' if db > 1 else ' '} | {effect}")

    # Verdict
    print(f"\n\n{'='*90}")
    print(f"VERDICT")
    print(f"{'='*90}")

    wr_d = pm_dwell['win_rate'] - pm_base['win_rate']
    pf_d = pm_dwell['profit_factor'] - pm_base['profit_factor']
    pnl_d = pm_dwell['total_pnl'] - pm_base['total_pnl']
    dd_d = pm_dwell['max_dd'] - pm_base['max_dd']
    trade_d = pm_dwell['trades'] - pm_base['trades']

    print(f"\n  Dynamic Dwell Impact (vs baseline):")
    print(f"    Trade Count:   {trade_d:+d} ({trade_d/max(pm_base['trades'],1)*100:+.1f}%)")
    print(f"    Win Rate:      {wr_d:+.1f}%")
    print(f"    Profit Factor: {pf_d:+.2f}")
    print(f"    P&L:           ${pnl_d:+.2f}")
    print(f"    Max Drawdown:  ${dd_d:+.2f}")
    print(f"    Avg Hold:      {pm_dwell['avg_hold_bars']-pm_base['avg_hold_bars']:+.1f} bars")
    print(f"    Min Hold:      {pm_dwell['min_hold_bars']} bars ({pm_dwell['min_hold_bars']*60}s) — {'PROP-SAFE' if pm_dwell['min_hold_bars'] >= 1 else 'RISK'}")

    # Overall assessment
    prop_safe = pm_dwell['min_hold_bars'] >= 1  # At least 60s minimum hold
    quality_maintained = pm_dwell['profit_factor'] >= pm_base['profit_factor'] * 0.90  # PF within 10% of baseline

    if prop_safe and quality_maintained and pf_d >= 0:
        verdict = "STRONG WIN — Prop-safe + quality improved"
    elif prop_safe and quality_maintained:
        verdict = "WIN — Prop-safe + quality maintained (within 10%)"
    elif prop_safe:
        verdict = "ACCEPTABLE — Prop-safe but quality reduced >10%"
    else:
        verdict = "NEEDS TUNING — Minimum hold still too short"

    print(f"\n  VERDICT: {verdict}")

    all_results['portfolio'] = {
        'baseline': pm_base,
        'dwell': pm_dwell,
        'verdict': verdict,
    }

    # Save
    output_path = Path("Results/v56_dwell_backtest_results.json")
    output_path.parent.mkdir(exist_ok=True)

    def convert_numpy(obj):
        if isinstance(obj, (np.integer,)): return int(obj)
        elif isinstance(obj, (np.floating,)): return float(obj)
        elif isinstance(obj, np.ndarray): return obj.tolist()
        elif isinstance(obj, np.bool_): return bool(obj)
        return obj

    with open(output_path, 'w') as f:
        json.dump(all_results, f, indent=2, default=convert_numpy)
    print(f"\n  Results saved to: {output_path}")

    return all_results


if __name__ == "__main__":
    results = main()
