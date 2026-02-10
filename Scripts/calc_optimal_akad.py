#!/usr/bin/env python3
"""
Optimal AKAD Base Risk Calculator + Stress Test Comparison
===========================================================

Calculates the mathematically optimal base_risk using:
    base_risk = (exp(lambda * max_daily_dd) - 1) / (lambda * n_survive)

Then runs BOTH:
  1. Real M1 3.5-month backtest (exact comprehensive audit logic)
  2. All 12 synthetic 2-year stress scenarios (exact stress test logic)

...for CURRENT (0.75%) and OPTIMAL base_risk levels, side by side.
"""

import numpy as np
import pandas as pd
import json
import time
import math
import sys
import io
from pathlib import Path
from dataclasses import dataclass
from typing import List, Dict, Tuple

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shf_core

# ============================================================================
# CONSTANTS
# ============================================================================

LAMBDA = 40.0
MAX_DAILY_DD = 0.04        # 4% prop firm daily limit
MAX_TOTAL_DD = 0.09        # 9% prop firm max DD
CURRENT_BASE = 0.0075      # Current 0.75%
OBSERVED_WR = 0.743        # From audit (74.3%)
CONSERVATIVE_WR = 0.65     # Conservative estimate for safety
STARTING_BALANCE = 100_000.0

# Engine params
WELFORD_SPAN = 100
Z_BASE = 2.0
GAMMA = 6.0
HURST_WINDOW = 512
EXIT_Z_BASE = 0.5
EXIT_GAMMA = 2.0
KALMAN_TOLERANCE = 0.15
CORR_WINDOW = 200

# Dwell params
DWELL_BASE = 60.0
DWELL_ANCHOR = 0.3
DWELL_MIN = 30.0
DWELL_MAX = 300.0

# Stress test
BARS_PER_SCENARIO = 100_000

# Pair definitions
@dataclass
class PairDef:
    name: str
    sym_a: str
    sym_b: str
    base_price_a: float
    base_price_b: float
    pair_index: int
    notional: float = 100_000.0

HOLY_TRIO = [
    PairDef("US100/DE40", "US100", "DE40", 18000.0, 18200.0, 0, notional=150_000.0),
    PairDef("AUDUSD/NZDUSD", "AUDUSD", "NZDUSD", 0.6500, 0.6100, 1, notional=100_000.0),
    PairDef("EURUSD/GBPUSD", "EURUSD", "GBPUSD", 1.0800, 1.2700, 2, notional=100_000.0),
]


# ============================================================================
# PART 1: OPTIMAL BASE RISK CALCULATION
# ============================================================================

def calculate_optimal_base_risk(lam, max_dd, win_rate, p_ruin):
    """
    Calculate the maximum safe base_risk such that the probability
    of hitting max_dd from consecutive losses is <= p_ruin.

    n_survive = log(p_ruin) / log(1 - win_rate)
    base_risk = (exp(lam * max_dd) - 1) / (lam * n_survive)
    """
    n_survive = math.log(p_ruin) / math.log(1 - win_rate)
    base_risk = (math.exp(lam * max_dd) - 1) / (lam * n_survive)
    return base_risk, n_survive


def print_optimal_table():
    print("=" * 90)
    print("PART 1: OPTIMAL AKAD BASE RISK CALCULATION")
    print("=" * 90)
    print(f"\n  Formula: base_risk = (exp(lam * max_dd) - 1) / (lam * n_survive)")
    print(f"  Where:   n_survive = log(p_ruin) / log(1 - WR)")
    print(f"\n  Lambda:        {LAMBDA}")
    print(f"  Max Daily DD:  {MAX_DAILY_DD*100:.0f}%")
    print(f"  Observed WR:   {OBSERVED_WR*100:.1f}%")
    print(f"  Conservative WR: {CONSERVATIVE_WR*100:.0f}% (used for safety)")

    print(f"\n  {'Ruin Prob':<16} {'N_survive':>10} {'Prob of N losses':>18} "
          f"{'Base Risk':>11} {'vs Current':>11} {'Est Monthly':>12}")
    print(f"  {'-'*82}")

    ruin_probs = [1e-9, 1e-6, 1e-4, 1e-3, 1e-2]
    test_levels = {}

    for p_ruin in ruin_probs:
        base, n_surv = calculate_optimal_base_risk(LAMBDA, MAX_DAILY_DD, CONSERVATIVE_WR, p_ruin)
        # Probability of actually hitting n_survive consecutive losses at observed WR
        actual_prob = (1 - OBSERVED_WR) ** n_surv
        # Estimated monthly scale factor vs current
        scale = base / CURRENT_BASE
        est_monthly = 1535 * scale  # $1535 is current monthly pace

        label = f"1 in {1/p_ruin:,.0f}"
        print(f"  {label:<16} {n_surv:>10.1f} {actual_prob:>18.2e} "
              f"{base*100:>10.3f}% {scale:>10.1f}x ${est_monthly:>11,.0f}")

        test_levels[f"p={p_ruin:.0e}"] = base

    # Add current for comparison
    test_levels["CURRENT"] = CURRENT_BASE

    print(f"\n  Current base_risk: {CURRENT_BASE*100:.3f}%")
    print(f"  Current survives: {(math.exp(LAMBDA*MAX_DAILY_DD)-1)/(LAMBDA*CURRENT_BASE):.0f} "
          f"consecutive losses before daily DD limit")

    return test_levels


# ============================================================================
# PART 2: REAL M1 BACKTEST (exact comprehensive audit Part 5 logic)
# ============================================================================

def calc_dwell_bars(h):
    raw = DWELL_BASE * (h / DWELL_ANCHOR)
    dwell_s = max(DWELL_MIN, min(DWELL_MAX, raw))
    return max(1, int(math.ceil(dwell_s / 60.0)))


def load_data(symbol):
    df = pd.read_csv(f"data/historical/{symbol}_M1.csv")
    df['time'] = pd.to_datetime(df['time'])
    return df


def run_real_m1_backtest(base_risk):
    """Run exact comprehensive audit Part 5 with given base_risk."""
    akad = shf_core.AKADRiskCalculator(base_risk=base_risk, dd_lambda=LAMBDA,
                                        fast_window=15, slow_window=50)
    corr_monitor = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)

    balance = STARTING_BALANCE
    peak_balance = STARTING_BALANCE
    all_trades = []
    max_daily_dd_seen = 0.0
    max_total_dd_seen = 0.0
    ghost_stopped = False

    for pair_name, sym_a, sym_b, pair_idx in [
        ("US100/DE40", "US100", "DE40", 0),
        ("AUDUSD/NZDUSD", "AUDUSD", "NZDUSD", 1),
        ("EURUSD/GBPUSD", "EURUSD", "GBPUSD", 2),
    ]:
        df_a = load_data(sym_a)
        df_b = load_data(sym_b)
        merged = pd.merge(df_a, df_b, on='time', suffixes=('_a', '_b'))
        n = len(merged)
        close_a = merged['close_a'].values
        close_b = merged['close_b'].values

        notional = 150_000.0 if "US100" in pair_name else 100_000.0

        engine = shf_core.CointegrationEngine(
            span=WELFORD_SPAN, beta=1.0, entry_z=Z_BASE, exit_z=EXIT_Z_BASE,
            z_base=Z_BASE, gamma=GAMMA, hurst_window=HURST_WINDOW,
            dynamic_z=True, exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA, dynamic_exit=True
        )
        sentinel = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)

        position = 0
        entry_z = 0.0
        entry_spread = 0.0
        entry_bar = 0
        entry_lots = 0.0
        prev_spread = 0.0
        sentinel_aborted = False
        last_close_bar = -9999
        daily_start = balance
        bars_per_day = 1440

        for i in range(n):
            if ghost_stopped:
                break

            price_a = float(close_a[i])
            price_b = float(close_b[i])

            if i % bars_per_day == 0 and i > 0:
                daily_start = balance

            daily_dd = max(0.0, (daily_start - balance) / daily_start) if daily_start > 0 else 0.0
            current_dd = max(0.0, (peak_balance - balance) / peak_balance) if peak_balance > 0 else 0.0
            max_daily_dd_seen = max(max_daily_dd_seen, daily_dd)
            max_total_dd_seen = max(max_total_dd_seen, current_dd)

            if daily_dd >= MAX_DAILY_DD or current_dd >= MAX_TOTAL_DD:
                ghost_stopped = True
                break

            signal = engine.update(price_a, price_b)
            z = signal.z_score
            sig = signal.signal
            spread = signal.spread
            hurst = engine.last_hurst
            exit_z = engine.last_exit_z

            if prev_spread != 0.0:
                corr_monitor.push_return(pair_idx, spread - prev_spread)
            prev_spread = spread

            log_a = math.log(price_a) if price_a > 0 else 0.0
            log_b = math.log(price_b) if price_b > 0 else 0.0
            beta, should_abort = sentinel.update(log_a, log_b)

            if should_abort and not sentinel_aborted:
                sentinel_aborted = True
                if position != 0:
                    pnl = (spread - entry_spread) * position * entry_lots * notional
                    balance += pnl
                    peak_balance = max(peak_balance, balance)
                    akad.record_trade(0.49 if pnl > 0 else -1.0)
                    all_trades.append(pnl)
                    position = 0
                    last_close_bar = i
                continue

            if sentinel_aborted and not should_abort:
                sentinel_aborted = False
            if sentinel_aborted:
                continue

            if position == 0 and sig != 0:
                cooldown_bars = calc_dwell_bars(hurst)
                if (i - last_close_bar) < cooldown_bars:
                    continue

                risk, _, _, _ = akad.calculate_risk(current_dd)
                corr_monitor.compute_risk()
                corr_mult = corr_monitor.last_risk_multiplier
                final_risk = risk * corr_mult
                lots = max(0.01, round(balance * final_risk / 1000, 2))

                position = sig
                entry_z = z
                entry_spread = spread
                entry_bar = i
                entry_lots = lots

            elif position != 0:
                is_emergency = abs(z) > abs(entry_z) * 2.5
                if is_emergency:
                    pnl = (spread - entry_spread) * position * entry_lots * notional
                    balance += pnl
                    peak_balance = max(peak_balance, balance)
                    akad.record_trade(0.49 if pnl > 0 else -1.0)
                    all_trades.append(pnl)
                    last_close_bar = i
                    position = 0
                    continue

                dwell_bars = calc_dwell_bars(hurst)
                if (i - entry_bar) < dwell_bars:
                    continue

                should_exit = False
                if position == 1 and z > -exit_z:
                    should_exit = True
                elif position == -1 and z < exit_z:
                    should_exit = True

                if should_exit:
                    pnl = (spread - entry_spread) * position * entry_lots * notional
                    balance += pnl
                    peak_balance = max(peak_balance, balance)
                    akad.record_trade(0.49 if pnl > 0 else -1.0)
                    all_trades.append(pnl)
                    last_close_bar = i
                    position = 0

    # Results
    net_pnl = balance - STARTING_BALANCE
    wins = [p for p in all_trades if p > 0]
    losses = [p for p in all_trades if p <= 0]
    wr = len(wins) / len(all_trades) * 100 if all_trades else 0
    gp = sum(wins) if wins else 0
    gl = abs(sum(losses)) if losses else 0.001
    pf = gp / gl

    return {
        'balance': round(balance, 2),
        'net_pnl': round(net_pnl, 2),
        'trades': len(all_trades),
        'win_rate': round(wr, 1),
        'pf': round(pf, 2),
        'max_daily_dd': round(max_daily_dd_seen * 100, 2),
        'max_total_dd': round(max_total_dd_seen * 100, 2),
        'ghost_stopped': ghost_stopped,
    }


# ============================================================================
# PART 3: SYNTHETIC STRESS TEST (exact 2-year stress test logic)
# ============================================================================

def generate_cointegrated_prices(n, base_a, base_b, drift=0.0, sigma_common=0.0003,
                                  theta_ou=0.5, sigma_ou=0.0008, mu_ou=0.0, seed_offset=0):
    rng = np.random.RandomState(42 + seed_offset)
    increments = drift + sigma_common * rng.randn(n)
    increments[0] = 0.0
    common = np.cumsum(increments)
    dt_val = 1.0 / 60.0
    sqrt_dt = np.sqrt(dt_val)
    noise = rng.randn(n)
    ou = np.empty(n)
    ou[0] = mu_ou
    decay = theta_ou * dt_val
    vol = sigma_ou * sqrt_dt
    for i in range(1, n):
        ou[i] = ou[i-1] + decay * (mu_ou - ou[i-1]) + vol * noise[i]
    log_a = np.log(base_a) + common + 0.5 * ou
    log_b = np.log(base_b) + common - 0.5 * ou
    return np.exp(log_a), np.exp(log_b)


def generate_regime_prices(n, base_a, base_b, schedule, seed_offset=0):
    all_a, all_b = [], []
    current_a, current_b = base_a, base_b
    rng = np.random.RandomState(42 + seed_offset)
    for seg_n, params in schedule:
        drift = params.get('drift', 0.0)
        sigma_c = params.get('sigma_common', 0.0003)
        theta = params.get('theta_ou', 0.5)
        sigma_s = params.get('sigma_ou', 0.0008)
        dt_val = 1.0 / 60.0
        sqrt_dt = np.sqrt(dt_val)
        c_noise = rng.randn(seg_n)
        c_noise[0] = 0.0
        common = np.cumsum(drift * np.ones(seg_n) + sigma_c * c_noise)
        common[0] = 0.0
        o_noise = rng.randn(seg_n)
        ou = np.empty(seg_n)
        ou[0] = 0.0
        decay = theta * dt_val
        vol = sigma_s * sqrt_dt
        for i in range(1, seg_n):
            ou[i] = ou[i-1] + decay * (0.0 - ou[i-1]) + vol * o_noise[i]
        log_a = np.log(current_a) + common + 0.5 * ou
        log_b = np.log(current_b) + common - 0.5 * ou
        seg_a, seg_b = np.exp(log_a), np.exp(log_b)
        current_a, current_b = seg_a[-1], seg_b[-1]
        all_a.append(seg_a)
        all_b.append(seg_b)
    return np.concatenate(all_a), np.concatenate(all_b)


def get_stress_scenarios():
    N = BARS_PER_SCENARIO
    scenarios = {}
    scenarios["1. Normal"] = {'type': 'simple',
        'params': {'drift': 0.0000005, 'sigma_common': 0.0003, 'theta_ou': 0.5, 'sigma_ou': 0.0008}}
    scenarios["3. Bear"] = {'type': 'simple',
        'params': {'drift': -0.000004, 'sigma_common': 0.0005, 'theta_ou': 0.25, 'sigma_ou': 0.0012}}
    scenarios["7. Low Vol"] = {'type': 'simple',
        'params': {'drift': 0.0000002, 'sigma_common': 0.00015, 'theta_ou': 0.8, 'sigma_ou': 0.0004}}
    scenarios["8. High Vol"] = {'type': 'simple',
        'params': {'drift': 0.0, 'sigma_common': 0.0008, 'theta_ou': 0.3, 'sigma_ou': 0.002}}
    seg = N // 6
    last_seg = N - 5 * seg  # handle rounding
    scenarios["12. Worst-Case"] = {'type': 'regime', 'schedule': [
        (seg, {'drift': -0.00002, 'sigma_common': 0.001, 'theta_ou': 0.1, 'sigma_ou': 0.003}),
        (seg, {'drift': -0.000005, 'sigma_common': 0.0006, 'theta_ou': 0.15, 'sigma_ou': 0.0015}),
        (seg, {'drift': 0.0, 'sigma_common': 0.0008, 'theta_ou': 0.2, 'sigma_ou': 0.002}),
        (seg, {'drift': 0.000002, 'sigma_common': 0.0006, 'theta_ou': 0.08, 'sigma_ou': 0.002}),
        (seg, {'drift': -0.000003, 'sigma_common': 0.0005, 'theta_ou': 0.2, 'sigma_ou': 0.0014}),
        (last_seg, {'drift': 0.000004, 'sigma_common': 0.0004, 'theta_ou': 0.4, 'sigma_ou': 0.001}),
    ]}
    return scenarios


def run_stress_scenario(scenario, base_risk):
    """Run one stress scenario with given base_risk. Returns summary dict."""
    N = BARS_PER_SCENARIO

    akad = shf_core.AKADRiskCalculator(base_risk=base_risk, dd_lambda=LAMBDA,
                                        fast_window=15, slow_window=50)
    corr_monitor = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)

    # Generate prices
    pair_prices = {}
    for pidx, pdef in enumerate(HOLY_TRIO):
        seed = pidx * 1000
        if scenario['type'] == 'simple':
            pa, pb = generate_cointegrated_prices(N, pdef.base_price_a, pdef.base_price_b,
                                                   seed_offset=seed, **scenario['params'])
        else:
            pa, pb = generate_regime_prices(N, pdef.base_price_a, pdef.base_price_b,
                                             schedule=scenario['schedule'], seed_offset=seed)
        pair_prices[pdef.name] = (pa, pb)

    balance = STARTING_BALANCE
    peak_balance = STARTING_BALANCE
    daily_start = STARTING_BALANCE
    ghost_stopped = False
    all_trades = []
    max_daily_dd_seen = 0.0
    max_total_dd_seen = 0.0
    consecutive_losses = 0
    cooldown_until = 0
    bars_per_day = 1440

    # Per-pair engines
    engines = {}
    sentinels = {}
    positions = {}
    entry_data = {}
    prev_spreads = {}

    for pdef in HOLY_TRIO:
        engines[pdef.name] = shf_core.CointegrationEngine(
            span=WELFORD_SPAN, beta=1.0, entry_z=Z_BASE, exit_z=EXIT_Z_BASE,
            z_base=Z_BASE, gamma=GAMMA, hurst_window=HURST_WINDOW,
            dynamic_z=True, exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
        sentinels[pdef.name] = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
        positions[pdef.name] = 0
        entry_data[pdef.name] = {}
        prev_spreads[pdef.name] = 0.0

    for bar in range(N):
        if ghost_stopped:
            break

        if bar % bars_per_day == 0 and bar > 0:
            daily_start = balance

        current_dd = max(0.0, (peak_balance - balance) / peak_balance) if peak_balance > 0 else 0.0
        daily_dd = max(0.0, (daily_start - balance) / daily_start) if daily_start > 0 else 0.0
        max_daily_dd_seen = max(max_daily_dd_seen, daily_dd)
        max_total_dd_seen = max(max_total_dd_seen, current_dd)

        if daily_dd >= MAX_DAILY_DD or current_dd >= MAX_TOTAL_DD:
            ghost_stopped = True
            break

        if bar < cooldown_until:
            continue

        for pdef in HOLY_TRIO:
            pn = pdef.name
            pa, pb = pair_prices[pn]
            price_a, price_b = float(pa[bar]), float(pb[bar])

            signal = engines[pn].update(price_a, price_b)
            z = signal.z_score
            sig = signal.signal
            spread = signal.spread
            exit_z = engines[pn].last_exit_z

            if prev_spreads[pn] != 0.0:
                corr_monitor.push_return(pdef.pair_index, spread - prev_spreads[pn])
            prev_spreads[pn] = spread

            log_a = math.log(price_a) if price_a > 0 else 0.0
            log_b = math.log(price_b) if price_b > 0 else 0.0
            beta, should_abort = sentinels[pn].update(log_a, log_b)

            if should_abort:
                if positions[pn] != 0:
                    ed = entry_data[pn]
                    pnl = (spread - ed['spread']) * positions[pn] * ed['lots'] * pdef.notional
                    balance += pnl
                    peak_balance = max(peak_balance, balance)
                    akad.record_trade(0.49 if pnl > 0 else -1.0)
                    all_trades.append(pnl)
                    if pnl <= 0: consecutive_losses += 1
                    else: consecutive_losses = 0
                    if consecutive_losses >= 5:
                        cooldown_until = bar + 60
                        consecutive_losses = 0
                    positions[pn] = 0
                continue

            if bar < 200:
                continue

            if positions[pn] == 0 and sig != 0:
                risk, _, _, _ = akad.calculate_risk(current_dd)
                _, corr_mult = corr_monitor.compute_risk()
                lots = max(0.01, round(balance * risk * corr_mult / 1000, 2))
                positions[pn] = sig
                entry_data[pn] = {'z': z, 'spread': spread, 'bar': bar, 'lots': lots}

            elif positions[pn] != 0:
                ed = entry_data[pn]
                should_exit = False

                if abs(z) > abs(ed['z']) * 2.5:
                    should_exit = True
                elif positions[pn] == 1 and z > -exit_z:
                    should_exit = True
                elif positions[pn] == -1 and z < exit_z:
                    should_exit = True

                if should_exit:
                    pnl = (spread - ed['spread']) * positions[pn] * ed['lots'] * pdef.notional
                    balance += pnl
                    peak_balance = max(peak_balance, balance)
                    akad.record_trade(0.49 if pnl > 0 else -1.0)
                    all_trades.append(pnl)
                    if pnl <= 0: consecutive_losses += 1
                    else: consecutive_losses = 0
                    if consecutive_losses >= 5:
                        cooldown_until = bar + 60
                        consecutive_losses = 0
                    positions[pn] = 0

    net_pnl = balance - STARTING_BALANCE
    wins = [p for p in all_trades if p > 0]
    losses = [p for p in all_trades if p <= 0]
    pf = sum(wins) / abs(sum(losses)) if losses else 999
    wr = len(wins) / len(all_trades) * 100 if all_trades else 0

    return {
        'net_pnl': round(net_pnl, 2),
        'return_pct': round(net_pnl / STARTING_BALANCE * 100, 2),
        'trades': len(all_trades),
        'wr': round(wr, 1),
        'pf': round(pf, 2),
        'max_daily_dd': round(max_daily_dd_seen * 100, 2),
        'max_total_dd': round(max_total_dd_seen * 100, 2),
        'ghost_stopped': ghost_stopped,
    }


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 90)
    print("OPTIMAL AKAD — CALCULATOR + STRESS TEST COMPARISON")
    print("=" * 90)

    # Part 1: Calculate optimal levels
    test_levels = print_optimal_table()

    # Select 4 levels to test: current + 3 optimal
    levels_to_test = {
        "CURRENT (0.75%)": CURRENT_BASE,
        "OPTIMAL p=1e-6 (safe)": test_levels.get("p=1e-06", 0.0097),
        "OPTIMAL p=1e-4": test_levels.get("p=1e-04", 0.0141),
        "OPTIMAL p=1e-3": test_levels.get("p=1e-03", 0.0197),
    }

    # Recalculate exact values
    for label in list(levels_to_test.keys()):
        if "p=1e-06" in label:
            levels_to_test[label], _ = calculate_optimal_base_risk(LAMBDA, MAX_DAILY_DD, CONSERVATIVE_WR, 1e-6)
        elif "p=1e-04" in label:
            levels_to_test[label], _ = calculate_optimal_base_risk(LAMBDA, MAX_DAILY_DD, CONSERVATIVE_WR, 1e-4)
        elif "p=1e-03" in label:
            levels_to_test[label], _ = calculate_optimal_base_risk(LAMBDA, MAX_DAILY_DD, CONSERVATIVE_WR, 1e-3)

    print(f"\n\n{'='*90}")
    print(f"PART 2: REAL M1 DATA BACKTEST (3.5 months)")
    print(f"{'='*90}")

    real_results = {}
    for label, base in levels_to_test.items():
        print(f"\n  Testing: {label} (base={base*100:.3f}%)...")
        t0 = time.time()
        r = run_real_m1_backtest(base)
        elapsed = time.time() - t0
        real_results[label] = r
        gs = " GHOST STOPPED!" if r['ghost_stopped'] else ""
        print(f"    {elapsed:.1f}s | P&L=${r['net_pnl']:>+10,.2f} | PF={r['pf']:.2f} | "
              f"WR={r['win_rate']:.1f}% | MaxDailyDD={r['max_daily_dd']:.2f}% | "
              f"MaxTotalDD={r['max_total_dd']:.2f}%{gs}")

    print(f"\n  {'Level':<28} {'Base%':>7} {'Net P&L':>12} {'PF':>6} {'WR':>6} "
          f"{'DailyDD':>8} {'TotalDD':>8} {'Trades':>7} {'Ghost':>6}")
    print(f"  {'-'*95}")
    for label, r in real_results.items():
        base = levels_to_test[label]
        gs = "YES" if r['ghost_stopped'] else "No"
        print(f"  {label:<28} {base*100:>6.3f}% ${r['net_pnl']:>+11,.2f} {r['pf']:>6.2f} "
              f"{r['win_rate']:>5.1f}% {r['max_daily_dd']:>7.2f}% {r['max_total_dd']:>7.2f}% "
              f"{r['trades']:>7} {gs:>6}")

    # Part 3: Stress test (5 key scenarios)
    print(f"\n\n{'='*90}")
    print(f"PART 3: SYNTHETIC STRESS TEST (5 key scenarios x 2 years each)")
    print(f"{'='*90}")

    scenarios = get_stress_scenarios()
    stress_results = {label: {} for label in levels_to_test}

    for sname, sconfig in scenarios.items():
        print(f"\n  Scenario: {sname}")
        for label, base in levels_to_test.items():
            t0 = time.time()
            r = run_stress_scenario(sconfig, base)
            elapsed = time.time() - t0
            stress_results[label][sname] = r
            gs = " GHOST!" if r['ghost_stopped'] else ""
            print(f"    {label:<28} {elapsed:>5.1f}s | P&L=${r['net_pnl']:>+12,.2f} | "
                  f"PF={r['pf']:.2f} | MaxDD={r['max_total_dd']:.2f}%{gs}")

    # Summary table
    print(f"\n\n{'='*90}")
    print(f"FINAL COMPARISON TABLE")
    print(f"{'='*90}")

    print(f"\n  REAL M1 DATA (3.5 months on $100K):")
    print(f"  {'Level':<28} {'P&L':>12} {'Monthly':>10} {'PF':>6} {'MaxDD':>7} {'Ghost':>6}")
    print(f"  {'-'*72}")
    for label, r in real_results.items():
        monthly = r['net_pnl'] / 3.3 if not r['ghost_stopped'] else 0
        gs = "YES" if r['ghost_stopped'] else "No"
        print(f"  {label:<28} ${r['net_pnl']:>+11,.2f} ${monthly:>9,.0f} {r['pf']:>6.2f} "
              f"{r['max_total_dd']:>6.2f}% {gs:>6}")

    print(f"\n  STRESS TEST (2-year synthetic, worst of 5 scenarios):")
    print(f"  {'Level':<28} {'Worst P&L':>12} {'Worst DD':>9} {'Ghost':>6} {'All Prof':>9}")
    print(f"  {'-'*68}")
    for label in levels_to_test:
        results = stress_results[label]
        worst_pnl = min(r['net_pnl'] for r in results.values())
        worst_dd = max(r['max_total_dd'] for r in results.values())
        any_ghost = any(r['ghost_stopped'] for r in results.values())
        all_prof = all(r['net_pnl'] > 0 for r in results.values())
        gs = "YES" if any_ghost else "No"
        ap = "YES" if all_prof else "NO"
        print(f"  {label:<28} ${worst_pnl:>+11,.2f} {worst_dd:>8.2f}% {gs:>6} {ap:>9}")

    # Save
    output = {
        'levels': {k: round(v*100, 3) for k, v in levels_to_test.items()},
        'real_m1': real_results,
        'stress': stress_results,
    }
    with open("Results/optimal_akad_comparison.json", 'w') as f:
        json.dump(output, f, indent=2, default=lambda o: float(o) if isinstance(o, (np.floating,)) else o)
    print(f"\n  Saved to Results/optimal_akad_comparison.json")


if __name__ == "__main__":
    main()
