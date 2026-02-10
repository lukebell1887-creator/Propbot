#!/usr/bin/env python3
"""
$100K Prop Challenge Projection — Based on Real Backtest Data
==============================================================

Takes the ACTUAL trade-by-trade results from the production backtest
and scales them to a $100K account with proper AKAD lot sizing.
"""

import numpy as np
import math
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shf_core
import pandas as pd

# Re-run the EXACT production backtest but with proper $ P&L scaling

PAIRS = [
    ("US100/DE40", "US100", "DE40", 0, 10.0),     # $10 per point per lot
    ("AUDUSD/NZDUSD", "AUDUSD", "NZDUSD", 1, 100000.0),  # $100K per lot (standard FX)
    ("EURUSD/GBPUSD", "EURUSD", "GBPUSD", 2, 100000.0),
]

DWELL_BASE = 60.0
DWELL_ANCHOR = 0.3
DWELL_MIN = 30.0
DWELL_MAX = 300.0
STARTING_BALANCE = 100000.0


def calc_dwell_bars(h):
    raw = DWELL_BASE * (h / DWELL_ANCHOR)
    dwell_s = max(DWELL_MIN, min(DWELL_MAX, raw))
    return max(1, int(math.ceil(dwell_s / 60.0)))


def load_pair(sym_a, sym_b):
    da = pd.read_csv(f"data/historical/{sym_a}_M1.csv")
    db = pd.read_csv(f"data/historical/{sym_b}_M1.csv")
    da['time'] = pd.to_datetime(da['time'])
    db['time'] = pd.to_datetime(db['time'])
    m = pd.merge(da, db, on='time', suffixes=('_a', '_b'))
    return m['close_a'].values, m['close_b'].values, len(m)


def main():
    print("=" * 90)
    print("$100K PROP CHALLENGE PROJECTION")
    print("=" * 90)
    print(f"\nStarting Balance: ${STARTING_BALANCE:,.0f}")
    print(f"AKAD: base=0.75%, lambda=40")
    print(f"Ghost Stop: 4% daily / 9% max DD")
    print(f"All safety layers active (Sentinel + Dwell + AKAD + Corr)\n")

    balance = STARTING_BALANCE
    peak_balance = STARTING_BALANCE
    daily_start = STARTING_BALANCE
    bars_per_day = 1440

    akad = shf_core.AKADRiskCalculator(base_risk=0.0075, dd_lambda=40.0,
                                        fast_window=15, slow_window=50)
    corr_monitor = shf_core.CorrelationRiskMonitor(n_pairs=3, window=200)

    all_trades = []
    equity_curve = [(0, balance)]
    daily_pnls = []
    current_day_pnl = 0.0
    ghost_stopped = False
    total_bars = 0

    for pair_name, sym_a, sym_b, pair_idx, pip_value in PAIRS:
        ca, cb, n = load_pair(sym_a, sym_b)
        print(f"  {pair_name}: {n:,} bars")

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

        for i in range(n):
            if ghost_stopped:
                break

            pa = float(ca[i])
            pb = float(cb[i])

            # Daily reset
            if i % bars_per_day == 0 and i > 0:
                daily_pnls.append(current_day_pnl)
                current_day_pnl = 0.0
                daily_start = balance

            # Ghost stop
            daily_dd = max(0.0, (daily_start - balance) / daily_start) if daily_start > 0 else 0.0
            current_dd = max(0.0, (peak_balance - balance) / peak_balance) if peak_balance > 0 else 0.0
            if daily_dd >= 0.04 or current_dd >= 0.09:
                ghost_stopped = True
                break

            # Engine
            signal = engine.update(pa, pb)
            z = signal.z_score
            sig = signal.signal
            spread = signal.spread
            hurst = engine.last_hurst
            exit_z = engine.last_exit_z

            # Corr
            if prev_spread != 0.0:
                corr_monitor.push_return(pair_idx, spread - prev_spread)
            prev_spread = spread

            # Sentinel
            log_a = math.log(pa) if pa > 0 else 0.0
            log_b = math.log(pb) if pb > 0 else 0.0
            beta, should_abort = sentinel.update(log_a, log_b)

            if should_abort and not sentinel_aborted:
                sentinel_aborted = True
                if position != 0:
                    # Force close — P&L based on spread change × lots × notional
                    spread_change = spread - entry_spread
                    pnl = spread_change * position * entry_lots * pip_value
                    balance += pnl
                    peak_balance = max(peak_balance, balance)
                    current_day_pnl += pnl
                    akad.record_trade(0.49 if pnl > 0 else -1.0)
                    all_trades.append({
                        'pair': pair_name, 'pnl': pnl, 'lots': entry_lots,
                        'reason': 'SENTINEL', 'hold_bars': i - entry_bar
                    })
                    position = 0
                continue
            if sentinel_aborted and not should_abort:
                sentinel_aborted = False
            if sentinel_aborted:
                continue

            # Dwell
            dwell_bars = calc_dwell_bars(hurst)

            # ENTRY
            if position == 0 and sig != 0:
                if (i - last_close_bar) < dwell_bars:
                    continue

                risk, _, _, _ = akad.calculate_risk(current_dd)
                corr_monitor.compute_risk()
                corr_mult = corr_monitor.last_risk_multiplier
                final_risk = risk * corr_mult

                # ACTUAL lot sizing for $100K account
                lots = max(0.01, round(balance * final_risk / 1000.0, 2))

                position = sig
                entry_z = z
                entry_spread = spread
                entry_bar = i
                entry_lots = lots

            # EXIT
            elif position != 0:
                is_emergency = abs(z) > abs(entry_z) * 2.5
                if is_emergency:
                    spread_change = spread - entry_spread
                    pnl = spread_change * position * entry_lots * pip_value
                    balance += pnl
                    peak_balance = max(peak_balance, balance)
                    current_day_pnl += pnl
                    akad.record_trade(0.49 if pnl > 0 else -1.0)
                    all_trades.append({
                        'pair': pair_name, 'pnl': pnl, 'lots': entry_lots,
                        'reason': 'EMERGENCY', 'hold_bars': i - entry_bar
                    })
                    last_close_bar = i
                    position = 0
                    continue

                if (i - entry_bar) < dwell_bars:
                    continue

                should_exit = False
                if position == 1 and z > -exit_z:
                    should_exit = True
                elif position == -1 and z < exit_z:
                    should_exit = True

                if should_exit:
                    spread_change = spread - entry_spread
                    pnl = spread_change * position * entry_lots * pip_value
                    balance += pnl
                    peak_balance = max(peak_balance, balance)
                    current_day_pnl += pnl
                    akad.record_trade(0.49 if pnl > 0 else -1.0)
                    all_trades.append({
                        'pair': pair_name, 'pnl': pnl, 'lots': entry_lots,
                        'reason': 'DYNAMIC_EXIT', 'hold_bars': i - entry_bar
                    })
                    last_close_bar = i
                    position = 0

            if i % 5000 == 0:
                equity_curve.append((total_bars + i, balance))

        total_bars += n

    # Last day
    daily_pnls.append(current_day_pnl)

    # Results
    print(f"\n{'='*90}")
    print(f"$100K CHALLENGE RESULTS")
    print(f"{'='*90}")

    net_pnl = balance - STARTING_BALANCE
    max_dd_usd = 0.0
    peak = STARTING_BALANCE
    for _, bal in equity_curve:
        peak = max(peak, bal)
        dd = peak - bal
        max_dd_usd = max(max_dd_usd, dd)
    max_dd_pct = max_dd_usd / STARTING_BALANCE * 100

    n_trades = len(all_trades)
    if n_trades > 0:
        pnls = [t['pnl'] for t in all_trades]
        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p <= 0]
        gp = sum(winners) if winners else 0
        gl = abs(sum(losers)) if losers else 0.001
        wr = len(winners) / n_trades * 100
        pf = gp / gl
        avg_lots = np.mean([t['lots'] for t in all_trades])
    else:
        wr = pf = 0
        avg_lots = 0

    # Time period
    max_bars = max(len(load_pair(s[1], s[2])[0]) for s in PAIRS)
    trading_days = max_bars / bars_per_day
    months = trading_days / 21.0

    print(f"\n  Period:          ~{trading_days:.0f} trading days (~{months:.1f} months)")
    print(f"  Starting:        ${STARTING_BALANCE:>12,.2f}")
    print(f"  Ending:          ${balance:>12,.2f}")
    print(f"  Net P&L:         ${net_pnl:>12,.2f}")
    print(f"  Return:          {net_pnl/STARTING_BALANCE*100:>11.2f}%")
    print(f"  Monthly Return:  {net_pnl/STARTING_BALANCE*100/max(months,0.1):>11.2f}%")
    print(f"  Monthly $:       ${net_pnl/max(months,0.1):>12,.2f}")
    print(f"  Ghost Stopped:   {'YES' if ghost_stopped else 'No'}")
    print(f"  Max Drawdown:    ${max_dd_usd:>12,.2f} ({max_dd_pct:.2f}%)")

    print(f"\n  Total Trades:    {n_trades}")
    print(f"  Win Rate:        {wr:.1f}%")
    print(f"  Profit Factor:   {pf:.2f}")
    print(f"  Avg Lot Size:    {avg_lots:.2f}")

    if n_trades > 0:
        print(f"  Avg Win:         ${np.mean(winners):>12,.2f}" if winners else "")
        print(f"  Avg Loss:        ${np.mean(losers):>12,.2f}" if losers else "")

    # Per-pair breakdown
    print(f"\n  Per-Pair Breakdown:")
    for pname in set(t['pair'] for t in all_trades):
        pt = [t for t in all_trades if t['pair'] == pname]
        pp = [t['pnl'] for t in pt]
        pw = [p for p in pp if p > 0]
        pl = [p for p in pp if p <= 0]
        ppf = sum(pw) / abs(sum(pl)) if pl else 999
        print(f"    {pname:<22} Trades={len(pt):>4}  WR={len(pw)/len(pt)*100:>5.1f}%  "
              f"PF={ppf:>5.2f}  P&L=${sum(pp):>10,.2f}  Avg lots={np.mean([t['lots'] for t in pt]):.2f}")

    # Prop firm context
    print(f"\n{'='*90}")
    print(f"PROP FIRM CHALLENGE CONTEXT")
    print(f"{'='*90}")
    print(f"\n  Typical $100K Challenge Requirements:")
    print(f"    Profit target:  $8,000-$10,000 (8-10%)")
    print(f"    Max daily DD:   $4,000-$5,000 (4-5%)")
    print(f"    Max total DD:   $9,000-$10,000 (9-10%)")
    print(f"    Time limit:     30-60 days (or unlimited)")

    if net_pnl > 0:
        months_to_8k = 8000 / (net_pnl / max(months, 0.1))
        months_to_10k = 10000 / (net_pnl / max(months, 0.1))
        print(f"\n  At current pace:")
        print(f"    Time to $8K target:   ~{months_to_8k:.1f} months")
        print(f"    Time to $10K target:  ~{months_to_10k:.1f} months")
        print(f"    Max DD seen:          ${max_dd_usd:,.2f} ({max_dd_pct:.2f}%)")
        if max_dd_pct < 9:
            print(f"    DD safety margin:     {9 - max_dd_pct:.2f}% under 9% limit")
    else:
        print(f"\n  ⚠️ Negative P&L in this period — no target projection")

    # Daily P&L stats
    if daily_pnls:
        dp = np.array(daily_pnls)
        dp_nonzero = dp[dp != 0]
        if len(dp_nonzero) > 0:
            print(f"\n  Daily P&L Stats:")
            print(f"    Best day:      ${np.max(dp_nonzero):>10,.2f}")
            print(f"    Worst day:     ${np.min(dp_nonzero):>10,.2f}")
            print(f"    Avg day:       ${np.mean(dp_nonzero):>10,.2f}")
            print(f"    Profitable days: {np.sum(dp_nonzero > 0)}/{len(dp_nonzero)} ({np.sum(dp_nonzero > 0)/len(dp_nonzero)*100:.0f}%)")


if __name__ == "__main__":
    main()
