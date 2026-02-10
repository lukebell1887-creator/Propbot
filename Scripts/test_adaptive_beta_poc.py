#!/usr/bin/env python3
"""
ADAPTIVE BETA — Proof of Concept Test (READ-ONLY, no code changes)
===================================================================

Tests the "PhD approach": Instead of blocking when beta drifts,
use the Kalman-estimated beta to recalculate the spread.

This test compares 3 approaches on real M1 data:

Mode A: FIXED BETA=1.0, NO SENTINEL  (baseline PF=2.13)
Mode B: FIXED BETA=1.0 + SENTINEL    (current production PF=1.53)
Mode C: ADAPTIVE BETA from Kalman    (proposed fix — what PF?)

Mode C Logic:
  - Every bar, get Kalman's estimate of beta
  - Use that beta to compute spread: ln(A) - beta_kalman * ln(B)
  - Only block if beta is changing too FAST (rate > threshold)
  - This way we trade with correct math, not stale beta=1.0

KEY QUESTION: Does using Kalman beta improve PF back toward 2.13?
Or does it introduce noise and make things WORSE?

WARNING: This is a SIMULATION only. Does NOT modify any production code.
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
WELFORD_SPAN = 100
Z_BASE = 2.0
GAMMA = 6.0
EXIT_Z_BASE = 0.5
EXIT_GAMMA = 2.0
HURST_WINDOW = 512


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


def dynamic_z_critical(hurst):
    return Z_BASE * (1.0 + GAMMA * max(0.0, hurst - 0.5))


def dynamic_exit_z(hurst):
    raw = EXIT_Z_BASE * (1.0 + EXIT_GAMMA * (hurst - 0.5))
    return max(0.1, min(1.0, raw))


def compute_hurst_rs(log_prices, window=512):
    """R/S Hurst estimator matching Rust implementation."""
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
            segment = returns[seg * n:(seg + 1) * n]
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


class AdaptiveBetaEngine:
    """
    Pure-Python simulation of adaptive beta approach.
    Uses Kalman-estimated beta to compute spread, with Welford EMA normalizer.
    """
    def __init__(self, span=100, hurst_window=512):
        self.span = span
        self.hurst_window = hurst_window
        self.alpha = 2.0 / (span + 1)
        self.count = 0
        self.w_mean = 0.0
        self.w_m2 = 0.0
        self.w_var = 1e-10
        self.spread_buffer = []
        self.last_z = 0.0
        self.last_spread = 0.0
        self.last_hurst = 0.5
        self.last_std = 0.0

    def update(self, price_a, price_b, beta):
        """Update with current prices and Kalman-estimated beta."""
        if price_a <= 0 or price_b <= 0:
            return 0.0, 0, 0.0  # z, signal, spread

        spread = math.log(price_a) - beta * math.log(price_b)
        self.last_spread = spread
        self.count += 1

        # Welford EMA normalizer
        if self.count == 1:
            self.w_mean = spread
            self.w_m2 = 0.0
            self.w_var = 1e-10
            z = 0.0
        else:
            delta = spread - self.w_mean
            self.w_mean += self.alpha * delta
            delta2 = spread - self.w_mean
            self.w_m2 = (1 - self.alpha) * self.w_m2 + self.alpha * delta * delta2
            self.w_var = max(self.w_m2, 1e-10)
            self.last_std = math.sqrt(self.w_var)
            z = (spread - self.w_mean) / max(self.last_std, 1e-8)

        self.last_z = z

        # Hurst (every 50 bars to save time)
        self.spread_buffer.append(spread)
        if len(self.spread_buffer) > self.hurst_window * 2:
            self.spread_buffer = self.spread_buffer[-self.hurst_window * 2:]

        if self.count % 50 == 0 and len(self.spread_buffer) >= self.hurst_window:
            self.last_hurst = compute_hurst_rs(
                np.array(self.spread_buffer), self.hurst_window)

        h = self.last_hurst
        z_crit = dynamic_z_critical(h)
        exit_z_val = dynamic_exit_z(h)

        # Signal generation (after warmup)
        signal = 0
        if self.count >= 200:
            if z > z_crit:
                signal = -1  # Short spread
            elif z < -z_crit:
                signal = 1   # Long spread

        return z, signal, spread, exit_z_val, h


def run_adaptive_mode(pair_name, close_a, close_b, n, mode='adaptive',
                      beta_rate_limit=0.02):
    """
    mode='fixed_no_sentinel': Fixed beta=1.0, no sentinel (Mode A)
    mode='fixed_sentinel':    Fixed beta=1.0 + sentinel (Mode B)
    mode='adaptive':          Use Kalman beta (Mode C)
    """
    # Kalman sentinel (always running to get beta estimates)
    sentinel = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=0.15)

    if mode in ('fixed_no_sentinel', 'fixed_sentinel'):
        # Use Rust engine with fixed beta
        engine = shf_core.CointegrationEngine(
            span=100, beta=1.0, entry_z=2.0, exit_z=0.5,
            z_base=2.0, gamma=6.0, hurst_window=512,
            dynamic_z=True, exit_z_base=0.5, exit_gamma=2.0, dynamic_exit=True
        )
    else:
        # Python adaptive engine
        engine = AdaptiveBetaEngine(span=100, hurst_window=512)

    position = 0
    entry_z = 0.0
    entry_spread = 0.0
    entry_bar = 0
    last_close_bar = -9999
    sentinel_aborted = False
    sentinel_blocked = 0
    beta_updates = 0
    prev_beta = 1.0
    trades = []
    beta_history = []

    for i in range(n):
        pa = float(close_a[i])
        pb = float(close_b[i])

        # Always run Kalman to get beta estimate
        log_a = math.log(pa) if pa > 0 else 0.0
        log_b = math.log(pb) if pb > 0 else 0.0
        kalman_beta, should_abort = sentinel.update(log_a, log_b)

        if i % 1000 == 0:
            beta_history.append((i, kalman_beta))

        if mode == 'fixed_no_sentinel':
            # Mode A: Ignore sentinel entirely
            signal_obj = engine.update(pa, pb)
            z = signal_obj.z_score
            sig = signal_obj.signal
            spread = signal_obj.spread
            hurst = engine.last_hurst
            exit_z = engine.last_exit_z

        elif mode == 'fixed_sentinel':
            # Mode B: Use sentinel to block
            signal_obj = engine.update(pa, pb)
            z = signal_obj.z_score
            sig = signal_obj.signal
            spread = signal_obj.spread
            hurst = engine.last_hurst
            exit_z = engine.last_exit_z

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
                sentinel_blocked += 1
                continue

        elif mode == 'adaptive':
            # Mode C: Use Kalman beta for spread calculation
            # Rate limit: only update beta if it changed by >beta_rate_limit
            current_beta = kalman_beta

            # Safety: block if beta rate of change is too extreme (>5% per bar)
            beta_change_rate = abs(current_beta - prev_beta) / max(abs(prev_beta), 0.01)
            if beta_change_rate > 0.05:
                # Beta changing too fast — skip this bar
                sentinel_blocked += 1
                prev_beta = current_beta
                continue

            if abs(current_beta - prev_beta) > beta_rate_limit:
                beta_updates += 1

            prev_beta = current_beta

            # Run adaptive engine with Kalman beta
            z, sig, spread, exit_z, hurst = engine.update(pa, pb, current_beta)

        # Skip warmup
        if i < 200:
            continue

        # ENTRY
        if position == 0 and sig != 0:
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

    return trades, sentinel_blocked, beta_updates, beta_history


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
        'emergency': sum(1 for t in trades if t.get('reason') == 'EMERGENCY'),
    }


def main():
    print("=" * 90)
    print("ADAPTIVE BETA — PROOF OF CONCEPT TEST")
    print("=" * 90)
    print("\nThis test checks: Does using Kalman-estimated beta improve PF?")
    print("No code changes are made — this is simulation only.\n")

    pair_data = {}
    for pname, sym_a, sym_b, pidx in PAIRS:
        ca, cb, n = load_pair(sym_a, sym_b)
        pair_data[pname] = (ca, cb, n)
        print(f"  {pname}: {n:,} bars")

    modes = [
        ("A: Fixed β=1.0, No Sentinel", "fixed_no_sentinel"),
        ("B: Fixed β=1.0 + Sentinel",   "fixed_sentinel"),
        ("C: Adaptive β (Kalman)",       "adaptive"),
    ]

    all_results = {}

    for mode_label, mode_key in modes:
        print(f"\n{'='*90}")
        print(f"MODE: {mode_label}")
        print(f"{'='*90}")

        all_trades = []
        for pname, sym_a, sym_b, pidx in PAIRS:
            ca, cb, n = pair_data[pname]
            t0 = time.time()
            trades, blocked, beta_upd, beta_hist = run_adaptive_mode(
                pname, ca, cb, n, mode=mode_key)
            elapsed = time.time() - t0
            m = calc_metrics(trades)
            all_trades.extend(trades)

            extra = ""
            if mode_key == 'fixed_sentinel':
                extra = f" | Blocked: {blocked:,} bars ({blocked/n*100:.1f}%)"
            elif mode_key == 'adaptive':
                if beta_hist:
                    betas = [b for _, b in beta_hist]
                    extra = (f" | β range: [{min(betas):.3f}, {max(betas):.3f}]"
                             f" | β updates: {beta_upd} | Blocked: {blocked}")

            print(f"  {pname:<22} Trades={m['trades']:>5}  WR={m['wr']:>5.1f}%  "
                  f"PF={m['pf']:>5.2f}  P&L=${m['pnl']:>8.2f}  ({elapsed:.1f}s){extra}")

        pm = calc_metrics(all_trades)
        print(f"\n  PORTFOLIO:            Trades={pm['trades']:>5}  WR={pm['wr']:>5.1f}%  "
              f"PF={pm['pf']:>5.2f}  P&L=${pm['pnl']:>8.2f}")
        print(f"  Avg Win: ${pm['avg_win']:.4f}  |  Avg Loss: ${pm['avg_loss']:.4f}")

        all_results[mode_label] = pm

    # FINAL COMPARISON
    print(f"\n\n{'='*90}")
    print("FINAL COMPARISON")
    print(f"{'='*90}")
    print(f"\n  {'Mode':<40} {'Trades':>7} {'WR':>7} {'PF':>7} {'P&L':>10}")
    print(f"  {'-'*75}")
    for label, pm in all_results.items():
        print(f"  {label:<40} {pm['trades']:>7} {pm['wr']:>6.1f}% {pm['pf']:>7.2f} ${pm['pnl']:>9.2f}")

    ra = all_results["A: Fixed β=1.0, No Sentinel"]
    rb = all_results["B: Fixed β=1.0 + Sentinel"]
    rc = all_results["C: Adaptive β (Kalman)"]

    print(f"\n\n{'='*90}")
    print("VERDICT")
    print(f"{'='*90}")

    if rc['pf'] > rb['pf'] and rc['pf'] > 1.5:
        print(f"\n  ✅ ADAPTIVE BETA IMPROVES PF: {rb['pf']:.2f} → {rc['pf']:.2f} (+{rc['pf']-rb['pf']:.2f})")
        print(f"  ✅ Trades recovered: {rb['trades']} → {rc['trades']} (+{rc['trades']-rb['trades']})")
        if rc['pf'] >= ra['pf'] * 0.9:
            print(f"  ✅ PF within 10% of no-sentinel baseline ({ra['pf']:.2f})")
            print(f"\n  RECOMMENDATION: Adaptive beta is SAFE to implement.")
        else:
            print(f"  ⚠️ PF still below no-sentinel baseline ({ra['pf']:.2f})")
            print(f"\n  RECOMMENDATION: Adaptive beta helps but doesn't fully recover quality.")
    elif rc['pf'] > 1.0:
        print(f"\n  ⚠️ Adaptive beta: PF={rc['pf']:.2f} (still profitable but marginal improvement)")
        print(f"  Current sentinel: PF={rb['pf']:.2f}")
        print(f"\n  RECOMMENDATION: Marginal — consider if complexity is worth it.")
    else:
        print(f"\n  ❌ Adaptive beta HURTS: PF={rc['pf']:.2f} (worse than sentinel approach)")
        print(f"\n  RECOMMENDATION: Do NOT implement. Sentinel approach is safer.")

    print(f"\n  Note: This is a pure-Python simulation. Results are indicative, not exact.")
    print(f"  A Rust implementation would match the exact Welford/Hurst behavior.")


if __name__ == "__main__":
    main()
