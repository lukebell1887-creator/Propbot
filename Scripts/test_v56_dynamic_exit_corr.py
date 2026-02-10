#!/usr/bin/env python3
"""
v5.6 Dynamic Exit Z + Cross-Pair Correlation Risk Validation
=============================================================

Two new features tested:

1. DYNAMIC EXIT Z (Hurst-adaptive exits):
   Z_exit = exit_z_base × (1 + exit_gamma × (H - 0.5))
   - H low  (strong MR) → lower exit Z → hold longer → squeeze more reversion
   - H = 0.5 (random walk) → standard exit Z = 0.5
   - H high (trending) → higher exit Z → take profit early

2. CROSS-PAIR CORRELATION RISK:
   Instead of independent AKAD per pair, model joint portfolio risk.
   If pairs become correlated (risk-off), reduce combined exposure.
   max_corr < 0.3 → 1.0x | 0.3-0.5 → 0.8x | 0.5-0.7 → 0.6x | >0.7 → 0.4x

Runs on Holy Trio: US100/DE40, AUDUSD/NZDUSD, EURUSD/GBPUSD
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
# CONFIGURATION
# ============================================================================

HURST_WINDOW = 512
Z_BASE = 2.0
GAMMA = 6.0
EXIT_Z_BASE = 0.5
EXIT_GAMMA = 2.0
WELFORD_SPAN = 100
CORR_WINDOW = 200

PAIRS = [
    ("US100/DE40", "US100", "DE40", 0),
    ("AUDUSD/NZDUSD", "AUDUSD", "NZDUSD", 1),
    ("EURUSD/GBPUSD", "EURUSD", "GBPUSD", 2),
]

# ============================================================================
# HURST (R/S Analysis)
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
    """
    Z_exit = exit_z_base × (1 + exit_gamma × (H - 0.5))
    Clamped to [0.1, 1.0].
    H=0.3 → 0.5*(1+2*(-0.2)) = 0.30 → hold longer
    H=0.5 → 0.50 → standard
    H=0.7 → 0.5*(1+2*(0.2)) = 0.70 → exit sooner
    """
    raw = exit_z_base * (1.0 + exit_gamma * (hurst - 0.5))
    return max(0.1, min(1.0, raw))


# ============================================================================
# CROSS-PAIR CORRELATION
# ============================================================================

def compute_rolling_correlation(spread_returns_a, spread_returns_b, window=CORR_WINDOW):
    """Compute rolling Pearson correlation between two spread return series."""
    n = min(len(spread_returns_a), len(spread_returns_b))
    if n < 50:
        return 0.0
    a = np.array(spread_returns_a[-window:] if n >= window else spread_returns_a)
    b = np.array(spread_returns_b[-window:] if n >= window else spread_returns_b)
    use_n = min(len(a), len(b))
    a, b = a[-use_n:], b[-use_n:]
    if len(a) < 50:
        return 0.0
    a_mean, b_mean = np.mean(a), np.mean(b)
    cov = np.sum((a - a_mean) * (b - b_mean))
    std_a = np.sqrt(np.sum((a - a_mean)**2))
    std_b = np.sqrt(np.sum((b - b_mean)**2))
    if std_a < 1e-10 or std_b < 1e-10:
        return 0.0
    return max(-1.0, min(1.0, cov / (std_a * std_b)))


def correlation_risk_multiplier(max_corr):
    if max_corr < 0.3:
        return 1.0
    elif max_corr < 0.5:
        return 0.8
    elif max_corr < 0.7:
        return 0.6
    else:
        return 0.4


# ============================================================================
# BACKTEST ENGINE
# ============================================================================

def run_backtest(pair_name, log_a, log_b, mode='v55_fixed_exit', hurst_values=None):
    """
    Modes:
    - 'v55_fixed_exit': Dynamic entry Z + fixed exit Z=0.5 (v5.5 baseline)
    - 'v56_dynamic_exit': Dynamic entry Z + dynamic exit Z (v5.6)
    """
    spread = log_a - 1.0 * log_b
    n = len(spread)

    w_mean, w_m2, w_var = 0.0, 0.0, 1.0
    alpha_w = 2.0 / (WELFORD_SPAN + 1)
    count = 0
    position = 0
    entry_spread = 0.0
    entry_z = 0.0
    trades = []
    exit_z_values = []
    spread_returns = []  # for correlation tracking

    for i in range(n):
        count += 1
        x = spread[i]

        # Track spread returns for correlation
        if i > 0:
            spread_returns.append(spread[i] - spread[i-1])

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

        # Exit threshold
        if mode == 'v56_dynamic_exit':
            exit_z = dynamic_exit_z(h)
        else:
            exit_z = EXIT_Z_BASE  # fixed 0.5

        if position == 0:
            if z > z_entry:
                position = -1
                entry_spread = x
                entry_z = z
                exit_z_values.append(exit_z)
            elif z < -z_entry:
                position = 1
                entry_spread = x
                entry_z = z
                exit_z_values.append(exit_z)
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
                trades.append({
                    'pnl': pnl, 'bar': i,
                    'entry_z': entry_z, 'exit_z_used': exit_z,
                    'hurst_at_exit': h,
                })
                position = 0

    return trades, exit_z_values, spread_returns


def calc_metrics(trades):
    if not trades:
        return {'trades': 0, 'win_rate': 0, 'profit_factor': 0,
                'total_pnl': 0, 'avg_win': 0, 'avg_loss': 0, 'max_dd': 0}
    pnls = [t['pnl'] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]
    gross_profit = sum(winners) if winners else 0
    gross_loss = abs(sum(losers)) if losers else 0.001
    equity = np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd = peak - equity
    max_dd = np.max(dd) if len(dd) > 0 else 0
    return {
        'trades': len(trades), 'winners': len(winners), 'losers': len(losers),
        'win_rate': len(winners) / len(trades) * 100,
        'profit_factor': gross_profit / gross_loss,
        'total_pnl': sum(pnls),
        'avg_win': np.mean(winners) if winners else 0,
        'avg_loss': np.mean(losers) if losers else 0,
        'max_dd': max_dd,
    }


# ============================================================================
# MAIN
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


def main():
    print("=" * 80)
    print("v5.6 DYNAMIC EXIT Z + CROSS-PAIR CORRELATION RISK TEST")
    print("=" * 80)
    print(f"\nDynamic Exit Formula: Z_exit = {EXIT_Z_BASE} * (1 + {EXIT_GAMMA} * (H - 0.5))")
    print(f"Correlation Window: {CORR_WINDOW} | Risk Bands: <0.3→1.0 | 0.3-0.5→0.8 | 0.5-0.7→0.6 | >0.7→0.4")

    all_results = {
        'version': '5.6',
        'features': ['dynamic_exit_z', 'cross_pair_correlation_risk'],
        'formulas': {
            'dynamic_exit_z': f'Z_exit = {EXIT_Z_BASE} * (1 + {EXIT_GAMMA} * (H - 0.5))',
            'dynamic_entry_z': f'Z_crit = {Z_BASE} * (1 + {GAMMA} * max(0, H - 0.5))',
        },
        'test_date': pd.Timestamp.now().isoformat(),
        'pairs': {},
    }

    pair_spread_returns = {}
    portfolio_v55 = []
    portfolio_v56 = []

    for pair_name, sym_a, sym_b, pair_idx in PAIRS:
        print(f"\n\n{'#'*80}")
        print(f"# PAIR: {pair_name}")
        print(f"{'#'*80}")

        df_a = load_data(sym_a)
        df_b = load_data(sym_b)
        log_a, log_b, times = align_data(df_a, df_b)
        spread = log_a - 1.0 * log_b
        n = len(log_a)
        print(f"Loaded {n:,} aligned bars")

        # Compute rolling Hurst
        print(f"Computing rolling Hurst (window={HURST_WINDOW})...")
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
        print(f"  Hurst mean={h_mean:.4f}, computed in {elapsed*1000:.0f}ms")

        # Show Dynamic Exit Z for different Hurst values
        print(f"\n  Dynamic Exit Z Reference:")
        for h_val in [0.30, 0.40, 0.50, h_mean, 0.60, 0.70]:
            ez = dynamic_exit_z(h_val)
            label = f"(avg H for {pair_name})" if abs(h_val - h_mean) < 0.01 else ""
            print(f"    H={h_val:.3f} → Z_exit={ez:.3f} {label}")

        # Run v5.5 baseline (dynamic entry, fixed exit)
        print(f"\n  Running v5.5 (dynamic entry, fixed exit=0.5)...")
        trades_v55, _, sr_v55 = run_backtest(pair_name, log_a, log_b, 'v55_fixed_exit', hurst_values)
        m55 = calc_metrics(trades_v55)

        # Run v5.6 (dynamic entry + dynamic exit)
        print(f"  Running v5.6 (dynamic entry + dynamic exit)...")
        trades_v56, exit_z_used, sr_v56 = run_backtest(pair_name, log_a, log_b, 'v56_dynamic_exit', hurst_values)
        m56 = calc_metrics(trades_v56)

        pair_spread_returns[pair_name] = sr_v56
        portfolio_v55.extend(trades_v55)
        portfolio_v56.extend(trades_v56)

        # Results
        print(f"\n  {'='*70}")
        print(f"  RESULTS: {pair_name}")
        print(f"  {'='*70}")
        print(f"  {'Metric':<25} {'v5.5 (fix exit)':>15} {'v5.6 (dyn exit)':>15} {'Delta':>15}")
        print(f"  {'-'*70}")
        print(f"  {'Total Trades':<25} {m55['trades']:>15} {m56['trades']:>15} {m56['trades']-m55['trades']:>+15}")
        print(f"  {'Win Rate':<25} {m55['win_rate']:>14.1f}% {m56['win_rate']:>14.1f}% {m56['win_rate']-m55['win_rate']:>+14.1f}%")
        print(f"  {'Profit Factor':<25} {m55['profit_factor']:>15.2f} {m56['profit_factor']:>15.2f} {m56['profit_factor']-m55['profit_factor']:>+15.2f}")
        print(f"  {'Total P&L':<25} ${m55['total_pnl']:>14.2f} ${m56['total_pnl']:>14.2f} ${m56['total_pnl']-m55['total_pnl']:>+14.2f}")
        print(f"  {'Max Drawdown':<25} ${m55['max_dd']:>14.2f} ${m56['max_dd']:>14.2f} ${m56['max_dd']-m55['max_dd']:>+14.2f}")

        if exit_z_used:
            print(f"\n  Dynamic Exit Z Stats:")
            print(f"    Mean Z_exit: {np.mean(exit_z_used):.3f}")
            print(f"    Min:  {np.min(exit_z_used):.3f}")
            print(f"    Max:  {np.max(exit_z_used):.3f}")

        all_results['pairs'][pair_name] = {
            'hurst_mean': float(h_mean),
            'v55': m55, 'v56': m56,
            'exit_z_stats': {
                'mean': float(np.mean(exit_z_used)) if exit_z_used else 0.5,
                'min': float(np.min(exit_z_used)) if exit_z_used else 0.5,
                'max': float(np.max(exit_z_used)) if exit_z_used else 0.5,
            },
        }

    # ========== CROSS-PAIR CORRELATION ANALYSIS ==========
    print(f"\n\n{'='*80}")
    print(f"CROSS-PAIR CORRELATION ANALYSIS")
    print(f"{'='*80}")

    pair_names = list(pair_spread_returns.keys())
    if len(pair_names) >= 2:
        # Compute pairwise correlations over the whole dataset
        for i in range(len(pair_names)):
            for j in range(i+1, len(pair_names)):
                sr_a = pair_spread_returns[pair_names[i]]
                sr_b = pair_spread_returns[pair_names[j]]
                # Full-period correlation
                full_corr = compute_rolling_correlation(sr_a, sr_b, window=len(sr_a))
                # Recent correlation (last 200 bars)
                recent_corr = compute_rolling_correlation(sr_a, sr_b, window=200)
                print(f"\n  {pair_names[i]} vs {pair_names[j]}:")
                print(f"    Full-period correlation: {full_corr:.4f}")
                print(f"    Recent (200-bar) corr:   {recent_corr:.4f}")

                # Simulate rolling correlation and risk multiplier
                min_len = min(len(sr_a), len(sr_b))
                corr_history = []
                risk_mult_history = []
                check_interval = 50
                for k in range(200, min_len, check_interval):
                    c = compute_rolling_correlation(sr_a[:k], sr_b[:k], window=200)
                    corr_history.append(c)
                    risk_mult_history.append(correlation_risk_multiplier(abs(c)))

                if corr_history:
                    corr_arr = np.array(corr_history)
                    risk_arr = np.array(risk_mult_history)
                    print(f"    Rolling corr: mean={np.mean(corr_arr):.4f}, std={np.std(corr_arr):.4f}")
                    print(f"    Max |corr|:   {np.max(np.abs(corr_arr)):.4f}")
                    print(f"    Risk mult:    mean={np.mean(risk_arr):.3f}, min={np.min(risk_arr):.3f}")
                    print(f"    Pct at 1.0x:  {np.sum(risk_arr == 1.0)/len(risk_arr)*100:.1f}%")
                    print(f"    Pct at 0.8x:  {np.sum(risk_arr == 0.8)/len(risk_arr)*100:.1f}%")
                    print(f"    Pct at 0.6x:  {np.sum(risk_arr == 0.6)/len(risk_arr)*100:.1f}%")
                    print(f"    Pct at 0.4x:  {np.sum(risk_arr == 0.4)/len(risk_arr)*100:.1f}%")

                    all_results[f'corr_{pair_names[i]}_{pair_names[j]}'] = {
                        'full_corr': float(full_corr),
                        'recent_corr': float(recent_corr),
                        'rolling_mean': float(np.mean(corr_arr)),
                        'rolling_max_abs': float(np.max(np.abs(corr_arr))),
                        'risk_mult_mean': float(np.mean(risk_arr)),
                    }

    # ========== PORTFOLIO SUMMARY ==========
    print(f"\n\n{'='*80}")
    print(f"PORTFOLIO SUMMARY")
    print(f"{'='*80}")

    pm55 = calc_metrics(portfolio_v55)
    pm56 = calc_metrics(portfolio_v56)

    print(f"\n  {'Metric':<25} {'v5.5':>15} {'v5.6':>15} {'Delta':>15}")
    print(f"  {'-'*70}")
    print(f"  {'Total Trades':<25} {pm55['trades']:>15} {pm56['trades']:>15} {pm56['trades']-pm55['trades']:>+15}")
    print(f"  {'Win Rate':<25} {pm55['win_rate']:>14.1f}% {pm56['win_rate']:>14.1f}% {pm56['win_rate']-pm55['win_rate']:>+14.1f}%")
    print(f"  {'Profit Factor':<25} {pm55['profit_factor']:>15.2f} {pm56['profit_factor']:>15.2f} {pm56['profit_factor']-pm55['profit_factor']:>+15.2f}")
    print(f"  {'Total P&L':<25} ${pm55['total_pnl']:>14.2f} ${pm56['total_pnl']:>14.2f} ${pm56['total_pnl']-pm55['total_pnl']:>+14.2f}")
    print(f"  {'Max Drawdown':<25} ${pm55['max_dd']:>14.2f} ${pm56['max_dd']:>14.2f} ${pm56['max_dd']-pm55['max_dd']:>+14.2f}")

    # Dynamic Exit Z reference table
    print(f"\n\n{'='*80}")
    print(f"DYNAMIC EXIT Z REFERENCE TABLE")
    print(f"{'='*80}")
    print(f"  Formula: Z_exit = {EXIT_Z_BASE} * (1 + {EXIT_GAMMA} * (H - 0.5))")
    print(f"\n  {'Hurst H':>10} | {'Z_exit':>10} | {'Effect'}")
    print(f"  {'-'*60}")
    for h in [0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75]:
        ez = dynamic_exit_z(h)
        if h < 0.5:
            effect = f"Hold longer (squeeze {(0.5-ez)/0.5*100:.0f}% more reversion)"
        elif h == 0.5:
            effect = "Standard exit"
        else:
            effect = f"Exit {(ez-0.5)/0.5*100:.0f}% sooner (take profit early)"
        print(f"  {h:>10.3f} | {ez:>10.3f} | {effect}")

    # Final verdict
    print(f"\n\n{'='*80}")
    print(f"FINAL VERDICT")
    print(f"{'='*80}")

    wr_delta = pm56['win_rate'] - pm55['win_rate']
    pf_delta = pm56['profit_factor'] - pm55['profit_factor']
    pnl_delta = pm56['total_pnl'] - pm55['total_pnl']
    dd_delta = pm56['max_dd'] - pm55['max_dd']

    print(f"\n  Dynamic Exit Z Impact:")
    print(f"    Win Rate:      {wr_delta:+.1f}%")
    print(f"    Profit Factor: {pf_delta:+.2f}")
    print(f"    P&L:           ${pnl_delta:+.2f}")
    print(f"    Max Drawdown:  ${dd_delta:+.2f}")

    if pf_delta > 0 and dd_delta <= 0:
        print(f"\n  VERDICT: STRONG WIN - Better PF and equal/lower drawdown")
    elif pf_delta > 0:
        print(f"\n  VERDICT: MODERATE WIN - Better PF but drawdown changed")
    elif wr_delta > 0:
        print(f"\n  VERDICT: MILD WIN - Better win rate")
    else:
        print(f"\n  VERDICT: NEEDS TUNING - Consider adjusting exit_gamma")

    all_results['portfolio'] = {'v55': pm55, 'v56': pm56}

    # Save
    output_path = Path("results/v56_dynamic_exit_corr_results.json")
    output_path.parent.mkdir(exist_ok=True)

    def convert_numpy(obj):
        if isinstance(obj, (np.integer,)): return int(obj)
        elif isinstance(obj, (np.floating,)): return float(obj)
        elif isinstance(obj, np.ndarray): return obj.tolist()
        elif isinstance(obj, np.bool_): return bool(obj)
        return obj

    with open(output_path, 'w') as f:
        json.dump(all_results, f, indent=2, default=convert_numpy)
    print(f"\nResults saved to: {output_path}")

    return all_results


if __name__ == "__main__":
    results = main()
