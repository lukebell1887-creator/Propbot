#!/usr/bin/env python3
"""
$100K Prop Challenge — EXACT COPY of Comprehensive Audit Part 5
================================================================

This is IDENTICAL to test_comprehensive_audit.py Part 5 backtest,
but with P&L scaled to $100K account using AKAD lot sizing.

NOTHING changed from the proven audit except:
  - P&L = spread_change × position × lots × notional (instead of × 1000)
  - Tracking balance, equity curve, DD for $100K context

Same sequential pair processing. Same AKAD/sentinel/dwell logic.
"""

import numpy as np
import pandas as pd
import json
import time
import math
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shf_core

STARTING_BALANCE = 100_000.0

# Notionals for converting log-spread P&L to real dollars
PAIR_NOTIONAL = {
    "US100/DE40": 150_000.0,
    "AUDUSD/NZDUSD": 100_000.0,
    "EURUSD/GBPUSD": 100_000.0,
}

PAIRS = [
    ("US100/DE40", "US100", "DE40", 0),
    ("AUDUSD/NZDUSD", "AUDUSD", "NZDUSD", 1),
    ("EURUSD/GBPUSD", "EURUSD", "GBPUSD", 2),
]

DWELL_BASE = 60.0
DWELL_ANCHOR = 0.3
DWELL_MIN = 30.0
DWELL_MAX = 300.0


def calc_dwell_bars(h):
    raw = DWELL_BASE * (h / DWELL_ANCHOR)
    dwell_s = max(DWELL_MIN, min(DWELL_MAX, raw))
    return max(1, int(math.ceil(dwell_s / 60.0)))


def load_data(symbol):
    df = pd.read_csv(f"data/historical/{symbol}_M1.csv")
    df['time'] = pd.to_datetime(df['time'])
    return df


def main():
    print("=" * 90)
    print("$100K PROP CHALLENGE — EXACT COMPREHENSIVE AUDIT BACKTEST")
    print("=" * 90)
    print(f"\nThis runs the EXACT same code as the proven audit (Part 5)")
    print(f"Only difference: P&L uses lots × notional instead of × 1000\n")
    print(f"Starting Balance: ${STARTING_BALANCE:,.0f}")
    print(f"AKAD: base=0.75%, lambda=40")
    print(f"Ghost Stop: 4% daily / 9% max DD\n")

    # ===== EXACT COPY OF COMPREHENSIVE AUDIT PART 5 =====

    total_start = time.time()
    portfolio_trades_unit = []     # Unit-size P&L (×1000) — for comparison
    portfolio_trades_real = []     # Real $ P&L (×lots×notional) — for $100K
    pair_results = {}

    # Shared components — SAME as audit
    akad = shf_core.AKADRiskCalculator(base_risk=0.0075, dd_lambda=40.0,
                                        fast_window=15, slow_window=50)
    corr_monitor = shf_core.CorrelationRiskMonitor(n_pairs=3, window=200)

    balance = STARTING_BALANCE
    peak_balance = STARTING_BALANCE
    equity_curve = [(0, balance)]

    for pair_name, sym_a, sym_b, pair_idx in PAIRS:
        print(f"\n  --- {pair_name} ---")

        df_a = load_data(sym_a)
        df_b = load_data(sym_b)
        merged = pd.merge(df_a, df_b, on='time', suffixes=('_a', '_b'))
        n = len(merged)
        print(f"  {n:,} aligned M1 bars (~{n/1440:.1f} days)")

        close_a = merged['close_a'].values
        close_b = merged['close_b'].values
        notional = PAIR_NOTIONAL[pair_name]

        # Initialize — SAME as audit
        engine = shf_core.CointegrationEngine(
            span=100, beta=1.0, entry_z=2.0, exit_z=0.5,
            z_base=2.0, gamma=6.0, hurst_window=512,
            dynamic_z=True, exit_z_base=0.5, exit_gamma=2.0, dynamic_exit=True
        )
        sentinel = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=0.15)

        position = 0
        entry_z = 0.0
        entry_spread = 0.0
        entry_bar = 0
        entry_lots = 0.0
        prev_spread = 0.0
        sentinel_aborted = False
        last_close_bar = -9999

        pair_balance_start = balance
        daily_start = balance
        bars_per_day = 1440
        trades_unit = []
        trades_real = []
        dwell_enforced = 0
        emergency_bypasses = 0
        cooldown_blocked = 0

        t0 = time.time()

        for i in range(n):
            price_a = float(close_a[i])
            price_b = float(close_b[i])

            # Daily reset — SAME as audit
            if i % bars_per_day == 0 and i > 0:
                daily_start = balance

            # Ghost stop — SAME as audit
            daily_dd = max(0.0, (daily_start - balance) / daily_start) if daily_start > 0 else 0.0
            current_dd = max(0.0, (peak_balance - balance) / peak_balance) if peak_balance > 0 else 0.0
            if daily_dd >= 0.04 or current_dd >= 0.09:
                print(f"  *** GHOST STOP at bar {i} ***")
                break

            # Run engine — SAME as audit
            signal = engine.update(price_a, price_b)
            z = signal.z_score
            sig = signal.signal
            spread = signal.spread
            hurst = engine.last_hurst
            exit_z = engine.last_exit_z

            # Corr monitor — SAME as audit
            if prev_spread != 0.0:
                corr_monitor.push_return(pair_idx, spread - prev_spread)
            prev_spread = spread

            # Kalman sentinel — SAME as audit
            log_a = math.log(price_a) if price_a > 0 else 0.0
            log_b = math.log(price_b) if price_b > 0 else 0.0
            beta, should_abort = sentinel.update(log_a, log_b)

            if should_abort and not sentinel_aborted:
                sentinel_aborted = True
                if position != 0:
                    pnl_unit = (spread - entry_spread) * position * 1000
                    pnl_real = (spread - entry_spread) * position * entry_lots * notional
                    balance += pnl_real
                    peak_balance = max(peak_balance, balance)
                    is_win = pnl_unit > 0
                    akad.record_trade(0.49 if is_win else -1.0)
                    trades_unit.append({'pnl': pnl_unit, 'bar': i, 'reason': 'SENTINEL',
                                        'hold_bars': i - entry_bar, 'lots': entry_lots})
                    trades_real.append({'pnl': pnl_real, 'bar': i, 'reason': 'SENTINEL',
                                        'hold_bars': i - entry_bar, 'lots': entry_lots})
                    position = 0
                    last_close_bar = i
                continue

            if sentinel_aborted and not should_abort:
                sentinel_aborted = False
            if sentinel_aborted:
                continue

            # ENTRY — SAME as audit
            if position == 0 and sig != 0:
                cooldown_bars = calc_dwell_bars(hurst)
                if (i - last_close_bar) < cooldown_bars:
                    cooldown_blocked += 1
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

            # EXIT — SAME as audit
            elif position != 0:
                is_emergency = abs(z) > abs(entry_z) * 2.5
                if is_emergency:
                    pnl_unit = (spread - entry_spread) * position * 1000
                    pnl_real = (spread - entry_spread) * position * entry_lots * notional
                    balance += pnl_real
                    peak_balance = max(peak_balance, balance)
                    is_win = pnl_unit > 0
                    akad.record_trade(0.49 if is_win else -1.0)
                    trades_unit.append({'pnl': pnl_unit, 'bar': i, 'reason': 'EMERGENCY',
                                        'hold_bars': i - entry_bar, 'lots': entry_lots})
                    trades_real.append({'pnl': pnl_real, 'bar': i, 'reason': 'EMERGENCY',
                                        'hold_bars': i - entry_bar, 'lots': entry_lots})
                    emergency_bypasses += 1
                    last_close_bar = i
                    position = 0
                    continue

                dwell_bars = calc_dwell_bars(hurst)
                hold_bars = i - entry_bar
                if hold_bars < dwell_bars:
                    continue

                should_exit = False
                if position == 1 and z > -exit_z:
                    should_exit = True
                elif position == -1 and z < exit_z:
                    should_exit = True

                if should_exit:
                    pnl_unit = (spread - entry_spread) * position * 1000
                    pnl_real = (spread - entry_spread) * position * entry_lots * notional
                    balance += pnl_real
                    peak_balance = max(peak_balance, balance)
                    is_win = pnl_unit > 0
                    akad.record_trade(0.49 if is_win else -1.0)
                    trades_unit.append({'pnl': pnl_unit, 'bar': i, 'reason': 'DYNAMIC_EXIT',
                                        'hold_bars': hold_bars, 'lots': entry_lots})
                    trades_real.append({'pnl': pnl_real, 'bar': i, 'reason': 'DYNAMIC_EXIT',
                                        'hold_bars': hold_bars, 'lots': entry_lots})
                    dwell_enforced += 1
                    last_close_bar = i
                    position = 0

            if i % 5000 == 0 and i > 0:
                equity_curve.append((i, balance))

        elapsed = time.time() - t0
        pair_pnl_real = balance - pair_balance_start

        # Metrics
        n_trades = len(trades_unit)
        if n_trades > 0:
            pnls_u = [t['pnl'] for t in trades_unit]
            pnls_r = [t['pnl'] for t in trades_real]
            w_u = [p for p in pnls_u if p > 0]
            l_u = [p for p in pnls_u if p <= 0]
            w_r = [p for p in pnls_r if p > 0]
            l_r = [p for p in pnls_r if p <= 0]
            wr = len(w_u) / n_trades * 100
            pf_u = sum(w_u) / abs(sum(l_u)) if l_u else 999
            pf_r = sum(w_r) / abs(sum(l_r)) if l_r else 999
            avg_lots = np.mean([t['lots'] for t in trades_unit])

            print(f"  {elapsed:.1f}s | Trades={n_trades} | WR={wr:.1f}%")
            print(f"  Unit PF={pf_u:.2f} P&L=${sum(pnls_u):.2f}  |  $100K PF={pf_r:.2f} P&L=${sum(pnls_r):,.2f}")
            print(f"  Avg lots={avg_lots:.2f} | Dwell={dwell_enforced} | Emerg={emergency_bypasses} | Cooldown={cooldown_blocked}")

            pair_results[pair_name] = {
                'trades': n_trades, 'win_rate': round(wr, 1),
                'pf_unit': round(pf_u, 2), 'pnl_unit': round(sum(pnls_u), 2),
                'pf_real': round(pf_r, 2), 'pnl_real': round(sum(pnls_r), 2),
                'avg_lots': round(avg_lots, 2),
            }
        else:
            print(f"  No trades")
            pair_results[pair_name] = {'trades': 0}

        portfolio_trades_unit.extend(trades_unit)
        portfolio_trades_real.extend(trades_real)

    equity_curve.append(('end', balance))
    total_elapsed = time.time() - total_start

    # ===== PORTFOLIO RESULTS =====
    print(f"\n{'='*90}")
    print(f"PORTFOLIO RESULTS — $100K ACCOUNT")
    print(f"{'='*90}")

    net_pnl = balance - STARTING_BALANCE
    max_dd_usd = 0.0
    peak = STARTING_BALANCE
    for _, bal in equity_curve:
        peak = max(peak, bal)
        dd = peak - bal
        max_dd_usd = max(max_dd_usd, dd)
    max_dd_pct = max_dd_usd / STARTING_BALANCE * 100

    # Unit-size portfolio (should match comprehensive audit exactly)
    all_u = [t['pnl'] for t in portfolio_trades_unit]
    w_u = [p for p in all_u if p > 0]
    l_u = [p for p in all_u if p <= 0]
    port_pf_u = sum(w_u) / abs(sum(l_u)) if l_u else 999
    port_wr = len(w_u) / len(all_u) * 100

    # Real $ portfolio
    all_r = [t['pnl'] for t in portfolio_trades_real]
    w_r = [p for p in all_r if p > 0]
    l_r = [p for p in all_r if p <= 0]
    port_pf_r = sum(w_r) / abs(sum(l_r)) if l_r else 999

    max_bars = max(len(pd.merge(load_data(s[1]), load_data(s[2]), on='time')) for s in PAIRS)
    trading_days = max_bars / 1440
    months = trading_days / 21.0

    print(f"\n  CROSS-CHECK (unit-size, should match audit):")
    print(f"    Trades: {len(all_u)} | WR: {port_wr:.1f}% | PF: {port_pf_u:.2f} | P&L: ${sum(all_u):.2f}")

    print(f"\n  $100K ACCOUNT:")
    print(f"    Period:          ~{trading_days:.0f} days (~{months:.1f} months)")
    print(f"    Starting:        ${STARTING_BALANCE:>12,.2f}")
    print(f"    Ending:          ${balance:>12,.2f}")
    print(f"    Net P&L:         ${net_pnl:>12,.2f}")
    print(f"    Return:          {net_pnl/STARTING_BALANCE*100:>11.2f}%")
    print(f"    Monthly Return:  {net_pnl/STARTING_BALANCE*100/max(months,0.1):>11.2f}%")
    print(f"    Monthly $:       ${net_pnl/max(months,0.1):>12,.2f}")
    print(f"    Win Rate:        {port_wr:.1f}%")
    print(f"    Profit Factor:   {port_pf_r:.2f}")
    print(f"    Max Drawdown:    ${max_dd_usd:>12,.2f} ({max_dd_pct:.2f}%)")
    print(f"    Ghost Stopped:   No")

    if w_r:
        print(f"    Avg Win:         ${np.mean(w_r):>12,.2f}")
    if l_r:
        print(f"    Avg Loss:        ${np.mean(l_r):>12,.2f}")

    # Per-pair table
    print(f"\n  Per-Pair:")
    print(f"  {'Pair':<22} {'Trades':>7} {'WR':>7} {'PF(unit)':>9} {'PF($100K)':>10} {'P&L($100K)':>12} {'Avg Lots':>9}")
    print(f"  {'-'*80}")
    for pname, pr in pair_results.items():
        if pr['trades'] > 0:
            print(f"  {pname:<22} {pr['trades']:>7} {pr['win_rate']:>6.1f}% {pr['pf_unit']:>9.2f} {pr['pf_real']:>10.2f} ${pr['pnl_real']:>11,.2f} {pr['avg_lots']:>9.2f}")

    # AKAD demonstration
    print(f"\n  AKAD Risk Curve:")
    for dd in [0, 1, 2, 3, 4, 5]:
        risk = 0.0075 * math.exp(-40.0 * dd/100)
        lots = max(0.01, round(100000 * risk / 1000, 2))
        print(f"    DD={dd}%: risk={risk*100:.3f}% → lots={lots:.2f}")

    # Prop firm
    print(f"\n{'='*90}")
    print(f"PROP FIRM CHALLENGE")
    print(f"{'='*90}")
    if net_pnl > 0:
        monthly = net_pnl / max(months, 0.1)
        print(f"\n  Monthly pace: ${monthly:,.0f}/month ({monthly/STARTING_BALANCE*100:.2f}%)")
        print(f"  Time to $8K:  ~{8000/monthly:.1f} months")
        print(f"  Time to $10K: ~{10000/monthly:.1f} months")
        print(f"  Max DD seen:  ${max_dd_usd:,.2f} ({max_dd_pct:.2f}%) — safety margin: {9-max_dd_pct:.2f}% under 9% limit")
    else:
        print(f"\n  Net P&L: ${net_pnl:,.2f}")

    # Save
    output = {
        'balance': round(balance, 2),
        'net_pnl': round(net_pnl, 2),
        'return_pct': round(net_pnl/STARTING_BALANCE*100, 2),
        'trades': len(all_r),
        'win_rate': round(port_wr, 1),
        'pf_unit': round(port_pf_u, 2),
        'pf_real': round(port_pf_r, 2),
        'max_dd_pct': round(max_dd_pct, 2),
        'pair_results': pair_results,
        'cross_check_unit_pnl': round(sum(all_u), 2),
    }
    with open("Results/100k_real_data_projection.json", 'w') as f:
        json.dump(output, f, indent=2)
    print(f"\n  Saved to Results/100k_real_data_projection.json")


if __name__ == "__main__":
    main()
