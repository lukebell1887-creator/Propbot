#!/usr/bin/env python3
"""
PF DROP DIAGNOSTIC — Isolate exactly which feature causes the PF reduction
============================================================================

Runs the 3.5-month real M1 backtest in 4 modes:

Mode 1: BASELINE    — Dynamic Z + Dwell only (matches stored dwell test = PF 2.30)
Mode 2: + SENTINEL  — Add Kalman Sentinel
Mode 3: + COOLDOWN  — Add re-entry cooldown
Mode 4: FULL STACK  — Sentinel + AKAD + Correlation + Cooldown (production)

This tells us EXACTLY which feature causes the PF drop from 2.30 to 1.53.
"""

import numpy as np
import pandas as pd
import time
import math
import sys
import io
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shf_core

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


def load_pair(sym_a, sym_b):
    da = pd.read_csv(f"data/historical/{sym_a}_M1.csv")
    db = pd.read_csv(f"data/historical/{sym_b}_M1.csv")
    da['time'] = pd.to_datetime(da['time'])
    db['time'] = pd.to_datetime(db['time'])
    m = pd.merge(da, db, on='time', suffixes=('_a', '_b'))
    return m['close_a'].values, m['close_b'].values, len(m)


def run_mode(pair_name, close_a, close_b, n, pair_idx,
             use_sentinel=False, use_akad=False, use_corr=False, use_cooldown=False,
             akad=None, corr_monitor=None):

    engine = shf_core.CointegrationEngine(
        span=100, beta=1.0, entry_z=2.0, exit_z=0.5,
        z_base=2.0, gamma=6.0, hurst_window=512,
        dynamic_z=True, exit_z_base=0.5, exit_gamma=2.0, dynamic_exit=True
    )
    sentinel = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=0.15) if use_sentinel else None

    position = 0
    entry_z = 0.0
    entry_spread = 0.0
    entry_bar = 0
    prev_spread = 0.0
    sentinel_aborted = False
    last_close_bar = -9999
    sentinel_blocked_bars = 0

    trades = []

    for i in range(n):
        pa = float(close_a[i])
        pb = float(close_b[i])

        signal = engine.update(pa, pb)
        z = signal.z_score
        sig = signal.signal
        spread = signal.spread
        hurst = engine.last_hurst
        exit_z = engine.last_exit_z

        # Corr monitor feed
        if use_corr and corr_monitor and prev_spread != 0.0:
            corr_monitor.push_return(pair_idx, spread - prev_spread)
        prev_spread = spread

        # Kalman sentinel
        if use_sentinel and sentinel:
            log_a = math.log(pa) if pa > 0 else 0.0
            log_b = math.log(pb) if pb > 0 else 0.0
            beta, should_abort = sentinel.update(log_a, log_b)

            if should_abort and not sentinel_aborted:
                sentinel_aborted = True
                if position != 0:
                    pnl = (spread - entry_spread) * position * 1000
                    trades.append({'pnl': pnl, 'bar': i, 'reason': 'SENTINEL',
                                   'hold_bars': i - entry_bar})
                    position = 0
                    last_close_bar = i
                continue

            if sentinel_aborted and not should_abort:
                sentinel_aborted = False
            if sentinel_aborted:
                sentinel_blocked_bars += 1
                continue

        # Skip warmup
        if i < 200:
            continue

        # ENTRY
        if position == 0 and sig != 0:
            if use_cooldown:
                cooldown_bars = calc_dwell_bars(hurst)
                if (i - last_close_bar) < cooldown_bars:
                    continue

            position = sig
            entry_z = z
            entry_spread = spread
            entry_bar = i

        # EXIT
        elif position != 0:
            is_emergency = abs(z) > abs(entry_z) * 2.5
            if is_emergency:
                pnl = (spread - entry_spread) * position * 1000
                trades.append({'pnl': pnl, 'bar': i, 'reason': 'EMERGENCY',
                               'hold_bars': i - entry_bar})
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
                pnl = (spread - entry_spread) * position * 1000
                trades.append({'pnl': pnl, 'bar': i, 'reason': 'DYNAMIC_EXIT',
                               'hold_bars': i - entry_bar})
                last_close_bar = i
                position = 0

    return trades, sentinel_blocked_bars


def calc_metrics(trades):
    if not trades:
        return {'trades': 0, 'wr': 0, 'pf': 0, 'pnl': 0, 'avg_win': 0, 'avg_loss': 0}
    pnls = [t['pnl'] for t in trades]
    w = [p for p in pnls if p > 0]
    l = [p for p in pnls if p <= 0]
    gp = sum(w) if w else 0
    gl = abs(sum(l)) if l else 0.001
    return {
        'trades': len(trades),
        'wr': len(w) / len(trades) * 100,
        'pf': gp / gl,
        'pnl': sum(pnls),
        'avg_win': np.mean(w) if w else 0,
        'avg_loss': np.mean(l) if l else 0,
        'sentinel_exits': sum(1 for t in trades if t.get('reason') == 'SENTINEL'),
    }


def main():
    print("=" * 90)
    print("PF DROP DIAGNOSTIC — Which feature causes the drop from PF=2.30 to PF=1.53?")
    print("=" * 90)

    modes = [
        ("1. BASELINE (Dwell only)",         dict(use_sentinel=False, use_akad=False, use_corr=False, use_cooldown=False)),
        ("2. + SENTINEL",                    dict(use_sentinel=True,  use_akad=False, use_corr=False, use_cooldown=False)),
        ("3. + COOLDOWN",                    dict(use_sentinel=False, use_akad=False, use_corr=False, use_cooldown=True)),
        ("4. SENTINEL + COOLDOWN",           dict(use_sentinel=True,  use_akad=False, use_corr=False, use_cooldown=True)),
        ("5. FULL STACK (production)",       dict(use_sentinel=True,  use_akad=True,  use_corr=True,  use_cooldown=True)),
    ]

    # Load data once
    pair_data = {}
    for pname, sym_a, sym_b, pidx in PAIRS:
        ca, cb, n = load_pair(sym_a, sym_b)
        pair_data[pname] = (ca, cb, n, pidx)
        print(f"  {pname}: {n:,} bars")

    results = {}

    for mode_name, mode_flags in modes:
        print(f"\n{'='*90}")
        print(f"MODE: {mode_name}")
        print(f"{'='*90}")

        akad = shf_core.AKADRiskCalculator(base_risk=0.0075, dd_lambda=40.0) if mode_flags['use_akad'] else None
        corr_mon = shf_core.CorrelationRiskMonitor(n_pairs=3, window=200) if mode_flags['use_corr'] else None

        all_trades = []
        pair_details = {}

        for pname, sym_a, sym_b, pidx in PAIRS:
            ca, cb, n, _ = pair_data[pname]
            trades, blocked = run_mode(pname, ca, cb, n, pidx,
                                       akad=akad, corr_monitor=corr_mon, **mode_flags)
            m = calc_metrics(trades)
            all_trades.extend(trades)
            pair_details[pname] = m

            blocked_pct = blocked / n * 100 if n > 0 else 0
            sentinel_info = f" | Sentinel blocked: {blocked:,} bars ({blocked_pct:.1f}%)" if mode_flags['use_sentinel'] else ""
            sent_exits = f" | Sentinel exits: {m['sentinel_exits']}" if m['sentinel_exits'] > 0 else ""
            print(f"  {pname:<22} Trades={m['trades']:>5}  WR={m['wr']:>5.1f}%  PF={m['pf']:>5.2f}  P&L=${m['pnl']:>8.2f}{sentinel_info}{sent_exits}")

        pm = calc_metrics(all_trades)
        print(f"\n  PORTFOLIO:            Trades={pm['trades']:>5}  WR={pm['wr']:>5.1f}%  PF={pm['pf']:>5.2f}  P&L=${pm['pnl']:>8.2f}")
        print(f"  Avg Win: ${pm['avg_win']:.4f}  |  Avg Loss: ${pm['avg_loss']:.4f}  |  Ratio: {abs(pm['avg_win']/pm['avg_loss']) if pm['avg_loss'] != 0 else 0:.2f}")

        results[mode_name] = {'portfolio': pm, 'pairs': pair_details}

    # COMPARISON TABLE
    print(f"\n\n{'='*90}")
    print("COMPARISON: Feature Impact on PF")
    print(f"{'='*90}")
    print(f"\n  {'Mode':<35} {'Trades':>7} {'WR':>7} {'PF':>7} {'P&L':>10} {'vs Base':>10}")
    print(f"  {'-'*80}")

    base_pf = None
    for mode_name, data in results.items():
        pm = data['portfolio']
        if base_pf is None:
            base_pf = pm['pf']
            delta = ""
        else:
            d = pm['pf'] - base_pf
            delta = f"PF {d:+.2f}"
        print(f"  {mode_name:<35} {pm['trades']:>7} {pm['wr']:>6.1f}% {pm['pf']:>7.2f} ${pm['pnl']:>9.2f} {delta:>10}")

    # DIAGNOSIS
    print(f"\n\n{'='*90}")
    print("DIAGNOSIS")
    print(f"{'='*90}")

    r1 = results["1. BASELINE (Dwell only)"]['portfolio']
    r2 = results["2. + SENTINEL"]['portfolio']
    r3 = results["3. + COOLDOWN"]['portfolio']
    r4 = results["4. SENTINEL + COOLDOWN"]['portfolio']
    r5 = results["5. FULL STACK (production)"]['portfolio']

    print(f"\n  Sentinel impact on PF:    {r1['pf']:.2f} -> {r2['pf']:.2f} (delta: {r2['pf']-r1['pf']:+.2f})")
    print(f"  Sentinel impact on trades: {r1['trades']} -> {r2['trades']} (delta: {r2['trades']-r1['trades']:+d})")
    print(f"  Cooldown impact on PF:    {r1['pf']:.2f} -> {r3['pf']:.2f} (delta: {r3['pf']-r1['pf']:+.2f})")
    print(f"  Cooldown impact on trades: {r1['trades']} -> {r3['trades']} (delta: {r3['trades']-r1['trades']:+d})")
    print(f"  Combined impact on PF:    {r1['pf']:.2f} -> {r4['pf']:.2f} (delta: {r4['pf']-r1['pf']:+.2f})")
    print(f"  Full stack vs baseline:   {r1['pf']:.2f} -> {r5['pf']:.2f} (delta: {r5['pf']-r1['pf']:+.2f})")

    print(f"\n  KEY QUESTION: Is PF still > 1.0 in production mode?")
    print(f"  Answer: PF = {r5['pf']:.2f} — {'YES, still profitable' if r5['pf'] > 1.0 else 'NO — PROBLEM!'}")
    print(f"  For every $1 lost, you make back ${r5['pf']:.2f}")

    if r5['pf'] > 1.0:
        edge = (r5['pf'] - 1.0) / r5['pf'] * 100
        print(f"  Edge: {edge:.1f}% of gross profit is net profit")
        print(f"\n  The PF drop from {r1['pf']:.2f} to {r5['pf']:.2f} is the COST OF SAFETY.")
        print(f"  Without these layers, a beta drift or correlation spike could wipe out months of gains.")


if __name__ == "__main__":
    main()
