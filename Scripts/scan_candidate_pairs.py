#!/usr/bin/env python3
"""
Targeted Pair Evaluation — Fundamentally-Linked Candidates Only
================================================================

Tests ONLY pairs with real economic ties (no blind data mining):
  1. XAUUSD vs XAGUSD  — Precious metals (same safe-haven complex)
  2. US30 vs US500      — US large-cap indices (same economy)
  3. UK100 vs DE40      — European index siblings

Uses the same Rust CointegrationEngine as production to evaluate:
  - Cointegration quality (Welford Z-score distribution)
  - Hurst exponent (mean-reversion strength)
  - Dynamic Z entry/exit signal quality
  - Win rate, profit factor, max drawdown
  - Comparison against Holy Trio benchmarks

Run: python Scripts/scan_candidate_pairs.py
"""

import sys
import json
import math
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    from shf_core import CointegrationEngine, KalmanSentinel
    RUST = True
except ImportError:
    RUST = False
    print("WARNING: shf_core not available — using pure Python fallback")

DATA_DIR = Path("data/historical")
RESULTS_DIR = Path("Results")


# =============================================================================
# CANDIDATE PAIRS (fundamentally linked ONLY)
# =============================================================================

CANDIDATES = [
    # (Name, Symbol_A, Symbol_B, Fundamental Reason)
    ("Gold/Silver",    "XAUUSD", "XAGUSD", "Precious metals — same safe-haven demand drivers"),
    ("Dow/S&P500",     "US30",   "US500",  "US large-cap indices — same economy, 95%+ overlap"),
    ("FTSE/DAX",       "UK100",  "DE40",   "European index siblings — correlated macro cycle"),
]

# Holy Trio benchmarks (from v5.6 validation) for comparison
HOLY_TRIO_BENCHMARKS = {
    "US100/DE40":     {"wr": 70.3, "pf": 1.41, "avg_hurst": 0.584, "avg_z_crit": 3.01},
    "AUDUSD/NZDUSD":  {"wr": 81.9, "pf": 3.82, "avg_hurst": 0.512, "avg_z_crit": 2.15},
    "EURUSD/GBPUSD":  {"wr": 78.6, "pf": 2.29, "avg_hurst": 0.539, "avg_z_crit": 2.47},
}

# Engine parameters (same as production)
PARAMS = {
    'span': 100,
    'beta': 1.0,
    'entry_z': 2.0,
    'exit_z': 0.5,
    'z_base': 2.0,
    'gamma': 6.0,
    'hurst_window': 512,
    'dynamic_z': True,
    'exit_z_base': 0.5,
    'exit_gamma': 2.0,
    'dynamic_exit': True,
}


def load_data(symbol: str) -> pd.DataFrame:
    """Load M1 CSV data for a symbol."""
    path = DATA_DIR / f"{symbol}_M1.csv"
    if not path.exists():
        raise FileNotFoundError(f"No data for {symbol}: {path}")
    df = pd.read_csv(path, parse_dates=['time'])
    return df


def align_data(df_a: pd.DataFrame, df_b: pd.DataFrame) -> tuple:
    """Align two dataframes by time, return matched close prices."""
    merged = pd.merge(df_a[['time', 'close']], df_b[['time', 'close']],
                       on='time', suffixes=('_a', '_b'), how='inner')
    return merged['close_a'].values, merged['close_b'].values, len(merged)


def evaluate_pair(name: str, sym_a: str, sym_b: str, reason: str) -> dict:
    """Run full cointegration evaluation on a candidate pair."""
    print(f"\n{'='*70}")
    print(f"  {name}: {sym_a} vs {sym_b}")
    print(f"  Reason: {reason}")
    print(f"{'='*70}")

    # Load data
    try:
        df_a = load_data(sym_a)
        df_b = load_data(sym_b)
    except FileNotFoundError as e:
        print(f"  SKIP: {e}")
        return {'name': name, 'status': 'NO_DATA', 'error': str(e)}

    prices_a, prices_b, n_bars = align_data(df_a, df_b)
    print(f"  Aligned bars: {n_bars:,}")

    if n_bars < 5000:
        print(f"  SKIP: Not enough overlapping data ({n_bars} < 5000)")
        return {'name': name, 'status': 'INSUFFICIENT_DATA', 'bars': n_bars}

    # Basic correlation check
    log_a = np.log(prices_a)
    log_b = np.log(prices_b)
    log_spread = log_a - log_b
    price_corr = np.corrcoef(prices_a, prices_b)[0, 1]
    return_corr = np.corrcoef(np.diff(log_a), np.diff(log_b))[0, 1]

    print(f"  Price correlation: {price_corr:.4f}")
    print(f"  Return correlation: {return_corr:.4f}")

    # Run through Rust CointegrationEngine (same as production)
    if not RUST:
        print("  SKIP: No Rust engine available")
        return {'name': name, 'status': 'NO_RUST'}

    engine = CointegrationEngine(**PARAMS)
    sentinel = KalmanSentinel(static_beta=1.0, beta_tolerance=0.15)

    # Track signals and performance
    z_scores = []
    hursts = []
    z_crits = []
    z_exits = []
    signals = []
    sentinel_aborts = 0

    # Simulated trading
    position = 0  # 0=flat, 1=long, -1=short
    entry_z = 0.0
    trades = []
    current_trade_entry_bar = 0

    for i in range(n_bars):
        sig = engine.update(prices_a[i], prices_b[i])
        z = sig.z_score
        s = sig.signal
        h = engine.last_hurst
        zc = engine.last_z_crit
        ze = engine.last_exit_z

        z_scores.append(z)
        hursts.append(h)
        z_crits.append(zc)
        z_exits.append(ze)
        signals.append(s)

        # Kalman sentinel
        la = math.log(prices_a[i]) if prices_a[i] > 0 else 0
        lb = math.log(prices_b[i]) if prices_b[i] > 0 else 0
        beta, abort = sentinel.update(la, lb)
        if abort:
            sentinel_aborts += 1
            if position != 0:
                # Emergency close
                spread_at_close = log_spread[i]
                spread_at_entry = log_spread[current_trade_entry_bar]
                if position == -1:
                    pnl = spread_at_entry - spread_at_close
                else:
                    pnl = spread_at_close - spread_at_entry
                trades.append({'pnl': pnl, 'bars': i - current_trade_entry_bar, 'reason': 'sentinel'})
                position = 0

        if i < 600:  # Warmup: need 512+ bars for Hurst
            continue

        # Entry logic
        if position == 0 and s != 0:
            position = s
            entry_z = z
            current_trade_entry_bar = i

        # Exit logic
        elif position != 0:
            # Emergency exit
            if abs(z) > abs(entry_z) * 2.5:
                spread_at_close = log_spread[i]
                spread_at_entry = log_spread[current_trade_entry_bar]
                if position == -1:
                    pnl = spread_at_entry - spread_at_close
                else:
                    pnl = spread_at_close - spread_at_entry
                trades.append({'pnl': pnl, 'bars': i - current_trade_entry_bar, 'reason': 'emergency'})
                position = 0
                continue

            # Normal exit
            should_exit = False
            if position == 1 and z > -ze:
                should_exit = True
            elif position == -1 and z < ze:
                should_exit = True

            if should_exit:
                spread_at_close = log_spread[i]
                spread_at_entry = log_spread[current_trade_entry_bar]
                if position == -1:
                    pnl = spread_at_entry - spread_at_close
                else:
                    pnl = spread_at_close - spread_at_entry
                trades.append({'pnl': pnl, 'bars': i - current_trade_entry_bar, 'reason': 'signal'})
                position = 0

    # Compute metrics
    z_arr = np.array(z_scores[600:])  # Skip warmup
    h_arr = np.array(hursts[600:])
    zc_arr = np.array(z_crits[600:])

    n_trades = len(trades)
    if n_trades == 0:
        print(f"  RESULT: NO TRADES generated")
        return {
            'name': name, 'status': 'NO_TRADES', 'sym_a': sym_a, 'sym_b': sym_b,
            'bars': n_bars, 'price_corr': round(price_corr, 4),
            'return_corr': round(return_corr, 4),
            'avg_hurst': round(float(np.mean(h_arr)), 4),
            'avg_z_crit': round(float(np.mean(zc_arr)), 2),
        }

    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    wr = len(wins) / n_trades * 100
    gross_win = sum(t['pnl'] for t in wins) if wins else 0
    gross_loss = abs(sum(t['pnl'] for t in losses)) if losses else 0.001
    pf = gross_win / gross_loss if gross_loss > 0 else 99.0

    # Max drawdown (cumulative PnL)
    cum_pnl = np.cumsum([t['pnl'] for t in trades])
    peak = np.maximum.accumulate(cum_pnl)
    dd = peak - cum_pnl
    max_dd = float(np.max(dd)) if len(dd) > 0 else 0

    avg_hold = np.mean([t['bars'] for t in trades])
    emergency_exits = len([t for t in trades if t['reason'] == 'emergency'])
    sentinel_exits = len([t for t in trades if t['reason'] == 'sentinel'])

    result = {
        'name': name,
        'status': 'EVALUATED',
        'sym_a': sym_a,
        'sym_b': sym_b,
        'reason': reason,
        'bars': n_bars,
        'price_corr': round(price_corr, 4),
        'return_corr': round(return_corr, 4),
        'total_trades': n_trades,
        'win_rate': round(wr, 1),
        'profit_factor': round(pf, 2),
        'net_pnl_log': round(float(cum_pnl[-1]), 6),
        'max_dd_log': round(max_dd, 6),
        'avg_hurst': round(float(np.mean(h_arr)), 4),
        'avg_z_crit': round(float(np.mean(zc_arr)), 2),
        'avg_z_exit': round(float(np.mean(z_exits[600:])), 3),
        'avg_hold_bars': round(avg_hold, 1),
        'emergency_exits': emergency_exits,
        'sentinel_aborts': sentinel_aborts,
        'sentinel_exits': sentinel_exits,
    }

    # Print results
    print(f"\n  RESULTS:")
    print(f"    Trades: {n_trades}")
    print(f"    Win Rate: {wr:.1f}%")
    print(f"    Profit Factor: {pf:.2f}")
    print(f"    Avg Hurst: {np.mean(h_arr):.4f}")
    print(f"    Avg Z_crit: {np.mean(zc_arr):.2f}")
    print(f"    Avg Z_exit: {np.mean(z_exits[600:]):.3f}")
    print(f"    Avg Hold: {avg_hold:.0f} bars ({avg_hold:.0f} min)")
    print(f"    Max DD (log): {max_dd:.6f}")
    print(f"    Sentinel Aborts: {sentinel_aborts}")
    print(f"    Emergency Exits: {emergency_exits}")

    # Grade
    grade = "FAIL"
    if wr >= 65 and pf >= 1.3 and np.mean(h_arr) < 0.6:
        grade = "PASS -- CANDIDATE FOR STRESS TEST"
    elif wr >= 60 and pf >= 1.1:
        grade = "MARGINAL -- Needs further analysis"

    print(f"\n    GRADE: {grade}")
    result['grade'] = grade
    return result


def main():
    print("=" * 70)
    print("SHF v5.6 — TARGETED PAIR EVALUATION")
    print("Fundamentally-linked candidates only (no blind scanning)")
    print("=" * 70)
    print(f"Date: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Data dir: {DATA_DIR}")
    print(f"Rust available: {RUST}")

    results = []
    for name, sym_a, sym_b, reason in CANDIDATES:
        r = evaluate_pair(name, sym_a, sym_b, reason)
        results.append(r)

    # Summary comparison
    print(f"\n\n{'='*70}")
    print(f"COMPARISON: CANDIDATES vs HOLY TRIO BENCHMARKS")
    print(f"{'='*70}")

    print(f"\n  {'Pair':<20} {'Trades':>7} {'WR':>7} {'PF':>7} {'Hurst':>7} {'Z_crit':>7} {'Grade'}")
    print(f"  {'-'*70}")

    # Holy Trio benchmarks
    for name, bench in HOLY_TRIO_BENCHMARKS.items():
        print(f"  {name:<20} {'---':>7} {bench['wr']:>6.1f}% {bench['pf']:>7.2f} {bench['avg_hurst']:>7.3f} {bench['avg_z_crit']:>7.2f} HOLY TRIO")

    print(f"  {'-'*70}")

    # Candidates
    for r in results:
        if r['status'] == 'EVALUATED':
            print(f"  {r['name']:<20} {r['total_trades']:>7} {r['win_rate']:>6.1f}% {r['profit_factor']:>7.2f} "
                  f"{r['avg_hurst']:>7.3f} {r['avg_z_crit']:>7.2f} {r['grade']}")
        else:
            print(f"  {r['name']:<20} {'---':>7} {'---':>7} {'---':>7} "
                  f"{r.get('avg_hurst', 0):>7.3f} {'---':>7} {r['status']}")

    # Save results
    RESULTS_DIR.mkdir(exist_ok=True)
    out_path = RESULTS_DIR / "candidate_pairs_evaluation.json"
    with open(out_path, 'w') as f:
        json.dump({
            'timestamp': datetime.utcnow().isoformat(),
            'candidates': results,
            'holy_trio_benchmarks': HOLY_TRIO_BENCHMARKS,
        }, f, indent=2)
    print(f"\n  Results saved to {out_path}")

    # Recommendation
    passed = [r for r in results if r.get('grade', '').startswith('PASS')]
    if passed:
        print(f"\n  RECOMMENDATION: {len(passed)} pair(s) passed screening:")
        for r in passed:
            print(f"    -> {r['name']} ({r['sym_a']}/{r['sym_b']}): WR={r['win_rate']:.1f}%, PF={r['profit_factor']:.2f}")
        print(f"    NEXT: Run 12-scenario stress test on these candidates")
    else:
        print(f"\n  RECOMMENDATION: No candidates passed the screen.")
        print(f"    The Holy Trio remains optimal. Don't fix what isn't broken.")


if __name__ == "__main__":
    main()
