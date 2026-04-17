#!/usr/bin/env python3
"""
FAST OPTIMAL Z FINDER — Single Pass, Analytical Solution
=========================================================
Instead of brute-force grid search (hours), this runs the engine ONCE
through all M1 data with a VERY low Z threshold to capture EVERY possible
entry signal. For each signal it records:
  - The Z-score at entry
  - The spread sigma at entry
  - The cost of the trade
  - The actual P&L outcome (did it win or lose?)

Then it analytically finds the OPTIMAL Z entry threshold by sweeping the
Z cutoff across the recorded signals — which takes microseconds.

Result: precise optimal Z for each pair in ~30 seconds per pair.
"""

import sys, math, time, json
import numpy as np
import pandas as pd
from pathlib import Path
from collections import deque

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shf_core

# ============================================================================
# 5%ERS BROKER SPECS
# ============================================================================
PAIRS = {
    'Gold_Silver': {
        'sym_a': 'XAUUSD', 'sym_b': 'XAGUSD',
        'cs_a': 100, 'cs_b': 5000,
        'comm_pct': 0.000009, 'spread_a_pts': 30, 'spread_b_pts': 3,
        'hmm_hold': 10,
    },
    'Oil_Spread': {
        'sym_a': 'XTIUSD', 'sym_b': 'XBRUSD',
        'cs_a': 100, 'cs_b': 100,
        'comm_pct': 0.0003, 'spread_a_pts': 4, 'spread_b_pts': 5,
        'hmm_hold': 10,
    },
    'NAS_DAX': {
        'sym_a': 'US100', 'sym_b': 'DE40',
        'cs_a': 1, 'cs_b': 1,
        'comm_pct': 0, 'spread_a_pts': 2, 'spread_b_pts': 2,
        'hmm_hold': 20,
    },
}

WELFORD_SPAN = 100; GAMMA = 6.0; HURST_WINDOW = 512; EXIT_GAMMA = 2.0
KALMAN_TOLERANCE = 0.15; HMM_LOOKBACK = 100
MIN_WARMUP_BARS = 200; BAL = 100_000.0
HUBER_SIGMA = 4.815
SESSION_START_HOUR = 7; SESSION_END_HOUR = 20
ROLLOVER_LOCKOUT_MIN = 30

# We test exit Z values analytically too
EXIT_Z_VALUES = [0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75]
DWELL_VALUES = [1, 5, 10, 20, 30]


class HMMRegimeDetector:
    def __init__(self, lookback=100, min_regime_hold=20):
        self._lookback = lookback; self._current_regime = 0
        self._return_buffer = []; self._regime_hold_count = 0
        self._min_regime_hold = min_regime_hold
    def update(self, sr):
        self._return_buffer.append(sr)
        if len(self._return_buffer) > self._lookback * 3:
            self._return_buffer = self._return_buffer[-self._lookback * 2:]
        if len(self._return_buffer) < 50: return 0
        recent = np.array(self._return_buffer[-self._lookback:])
        n = len(recent); ws = min(20, n // 3)
        if ws < 5: return 0
        vols = [np.std(recent[i:i+ws]) for i in range(0, n - ws + 1, ws)]
        if len(vols) < 3: return 0
        v40 = np.percentile(vols, 40); v80 = np.percentile(vols, 80)
        nr = 0 if vols[-1] <= v40 else (1 if vols[-1] <= v80 else 2)
        self._regime_hold_count += 1
        if nr != self._current_regime and self._regime_hold_count >= self._min_regime_hold:
            self._current_regime = nr; self._regime_hold_count = 0
        return self._current_regime
    @property
    def is_blocked(self): return self._current_regime >= 2


def get_spread_mult(hour):
    if 0 <= hour < 7: return 2.0
    elif 7 <= hour < 9: return 1.3
    elif 9 <= hour < 17: return 1.0
    elif 17 <= hour < 20: return 1.2
    else: return 1.8


def calc_cost(ps, lots, hour, pa, pb):
    sm = get_spread_mult(hour)
    sc = (ps['spread_a_pts'] + ps['spread_b_pts']) * lots * 2 * sm
    comm = 0
    if ps['comm_pct'] > 0:
        comm = ps['comm_pct'] * (pa * ps['cs_a'] + pb * ps['cs_b']) * lots * 2
    return sc + comm


def load_pair(sa, sb):
    d = Path(__file__).resolve().parent.parent / "data" / "historical"
    a = pd.read_csv(d / f"{sa}_M1.csv", parse_dates=['time']).rename(columns={'close': 'close_a'})
    b = pd.read_csv(d / f"{sb}_M1.csv", parse_dates=['time']).rename(columns={'close': 'close_b'})
    m = pd.merge(a[['time', 'close_a']], b[['time', 'close_b']], on='time', how='inner')
    m = m.sort_values('time').reset_index(drop=True)
    return m[(m['close_a'] > 0) & (m['close_b'] > 0)].reset_index(drop=True)


def calc_notional(ps, pa, pb):
    return (ps['cs_a'] * pa + ps['cs_b'] * pb) / 2.0


def single_pass_collect(df, ps, notional, avg_pa, avg_pb, hmm_hold, exit_z_base, dwell_bars):
    """
    Single pass through ALL M1 data. Uses a VERY low Z threshold (0.5) so
    we capture every possible entry. For each completed trade, records:
      - entry_z: absolute Z at entry
      - gross_pnl: spread P&L (before costs)
      - cost: round-trip cost
      - net_pnl: gross - cost
      - sigma: spread sigma at entry
      - hold_bars: how long the trade was held
    
    This gives us the raw material to find the optimal Z cutoff analytically.
    """
    n = len(df)
    
    # Use very low Z to capture everything (0.5 = basically any signal)
    eng = shf_core.CointegrationEngine(
        span=WELFORD_SPAN, beta=1.0, entry_z=0.5,
        exit_z=exit_z_base, z_base=0.5, gamma=0.0,  # gamma=0 → fixed low Z threshold
        hurst_window=HURST_WINDOW,
        dynamic_z=False,  # FIXED low Z so we capture everything
        exit_z_base=exit_z_base, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
    hmm = HMMRegimeDetector(lookback=HMM_LOOKBACK, min_regime_hold=hmm_hold)

    pos = 0; ez = 0.0; es = 0.0; ebar = 0
    lsp = 0.0; psp = 0.0; sa = False
    lcb = -9999; lch = 0.5; eh = 0
    entry_sigma = 0.0
    
    # Collect all trade outcomes
    trade_records = []

    pa_arr = df['close_a'].values.astype(np.float64)
    pb_arr = df['close_b'].values.astype(np.float64)
    tp = pd.DatetimeIndex(df['time'])
    hrs = tp.hour.values; mins = tp.minute.values

    for bar in range(n):
        p1 = pa_arr[bar]; p2 = pb_arr[bar]
        bh = int(hrs[bar]); bm = int(mins[bar])

        psp = lsp
        sig = eng.update(p1, p2)
        z = sig.z_score; s = sig.signal; sp = sig.spread; lsp = sp
        h = eng.last_hurst; exz = eng.last_exit_z

        la = math.log(p1) if p1 > 0 else 0; lb = math.log(p2) if p2 > 0 else 0
        bk, abt = sen.update(la, lb)
        if abt and not sa:
            sa = True
            if pos != 0:
                gr = (sp - es) * pos * notional  # gross per lot
                c = calc_cost(ps, 1.0, eh, avg_pa, avg_pb)  # cost per lot
                trade_records.append({
                    'entry_z': abs(ez),
                    'entry_sigma': entry_sigma,
                    'gross': gr, 'cost': c, 'net': gr - c,
                    'hold_bars': bar - ebar, 'exit_type': 'sentinel',
                    'hour': eh,
                })
                pos = 0; lcb = bar; lch = h
            continue
        if sa and not abt: sa = False
        if sa: continue

        hblocked = False
        if psp != 0: hmm.update(sp - psp); hblocked = hmm.is_blocked
        if bar < MIN_WARMUP_BARS: continue

        # ENTRY — capture ALL signals (Z > 0.5)
        if pos == 0 and s != 0:
            if hblocked: continue
            bms = bh * 60 + bm
            if bms < ROLLOVER_LOCKOUT_MIN or (1440 - bms) < ROLLOVER_LOCKOUT_MIN: continue
            if not (SESSION_START_HOUR <= bh < SESSION_END_HOUR): continue
            if lcb >= 0:
                da = max(1, dwell_bars * (lch / 0.3))
                if (bar - lcb) < da: continue
            
            entry_sigma = eng.last_std if hasattr(eng, 'last_std') else 0
            pos = s; ez = z; es = sp; ebar = bar; eh = bh

        # EXIT
        elif pos != 0:
            ex = False
            ss = eng.last_std if hasattr(eng, 'last_std') else 0
            if ss > 0:
                uz = (sp - es) * pos / ss
                if uz < -HUBER_SIGMA: ex = True
            if not ex and abs(z) > abs(ez) * 2.5: ex = True
            if not ex and bh >= SESSION_END_HOUR - 1 and bm >= 45: ex = True
            if not ex:
                hbs = bar - ebar
                da = max(1, dwell_bars * (h / 0.3))
                if hbs < da: continue
                if pos == 1 and z > -exz: ex = True
                elif pos == -1 and z < exz: ex = True
            if ex:
                gr = (sp - es) * pos * notional
                c = calc_cost(ps, 1.0, eh, avg_pa, avg_pb)
                trade_records.append({
                    'entry_z': abs(ez),
                    'entry_sigma': entry_sigma,
                    'gross': gr, 'cost': c, 'net': gr - c,
                    'hold_bars': bar - ebar, 'exit_type': 'normal',
                    'hour': eh,
                })
                pos = 0; lcb = bar; lch = h

    return trade_records


def analyze_z_cutoff(records, z_min=0.5, z_max=5.0, z_step=0.1):
    """
    Given all trade records, find the optimal Z entry cutoff.
    For each Z threshold, compute: trades taken, WR, PF, net P&L, $/trade.
    Returns sorted results (best first).
    """
    if not records:
        return []
    
    z_thresholds = np.arange(z_min, z_max + z_step, z_step)
    results = []
    
    for z_thresh in z_thresholds:
        # Only include trades where entry |Z| >= threshold
        filtered = [r for r in records if r['entry_z'] >= z_thresh]
        if len(filtered) < 3:
            continue
        
        nets = [r['net'] for r in filtered]
        wins = [n for n in nets if n > 0]
        losses = [n for n in nets if n <= 0]
        total = len(nets)
        wr = len(wins) / total * 100
        gp = sum(wins) if wins else 0
        gl = abs(sum(losses)) if losses else 0.001
        pf = gp / gl
        net_total = sum(nets)
        avg_net = net_total / total
        
        results.append({
            'z': round(z_thresh, 2),
            'trades': total,
            'wr': round(wr, 1),
            'pf': round(pf, 2),
            'net': round(net_total, 2),
            'avg': round(avg_net, 2),
            'avg_gross': round(sum(r['gross'] for r in filtered) / total, 2),
            'avg_cost': round(sum(r['cost'] for r in filtered) / total, 2),
        })
    
    return results


def main():
    t0 = time.time()
    print("=" * 120)
    print("  FAST OPTIMAL Z FINDER — Single Pass, Analytical Solution")
    print("  Runs engine ONCE, then finds optimal Z cutoff analytically (~30s per pair)")
    print("=" * 120)

    all_results = {}

    for pname, ps in PAIRS.items():
        print(f"\n{'=' * 120}")
        print(f"  {pname} ({ps['sym_a']}/{ps['sym_b']}) | HMM={ps['hmm_hold']}")
        print(f"{'=' * 120}")

        df = load_pair(ps['sym_a'], ps['sym_b'])
        if df is None or len(df) < 2000:
            print(f"  SKIP: insufficient data"); continue

        avg_a = df['close_a'].mean(); avg_b = df['close_b'].mean()
        notional = calc_notional(ps, avg_a, avg_b)
        days = (df['time'].iloc[-1] - df['time'].iloc[0]).days
        months = max(0.1, days / 30.0)

        print(f"  M1 bars: {len(df):,} | {df['time'].iloc[0]} to {df['time'].iloc[-1]} ({days}d)")
        print(f"  Avg prices: {ps['sym_a']}=${avg_a:.2f} {ps['sym_b']}=${avg_b:.2f}")
        print(f"  Notional: ${notional:,.0f}")

        # Test each ExitZ × Dwell combo (fast — each is ~30s)
        best_overall = None
        best_score = -999999
        best_config = None

        for exit_z in EXIT_Z_VALUES:
            for dwell in DWELL_VALUES:
                pt = time.time()
                records = single_pass_collect(df, ps, notional, avg_a, avg_b,
                                              ps['hmm_hold'], exit_z, dwell)
                elapsed = time.time() - pt
                
                if len(records) < 5:
                    continue

                # Analyze Z cutoffs for this ExitZ/Dwell combo
                z_results = analyze_z_cutoff(records, z_min=0.5, z_max=4.5, z_step=0.05)
                
                # Find best Z for this config
                profitable = [r for r in z_results if r['net'] > 0 and r['trades'] >= 5]
                if not profitable:
                    continue
                
                # Score by net P&L × trade frequency
                for zr in profitable:
                    score = zr['net'] * min(1.0, zr['trades'] / 10.0)
                    if score > best_score:
                        best_score = score
                        best_overall = zr
                        best_config = {'exit_z': exit_z, 'dwell': dwell}

        if best_overall is None:
            print(f"\n  NO profitable config found for {pname}")
            continue

        # Now do the detailed analysis for the best ExitZ/Dwell
        print(f"\n  Best ExitZ={best_config['exit_z']} Dwell={best_config['dwell']}")
        records = single_pass_collect(df, ps, notional, avg_a, avg_b,
                                      ps['hmm_hold'], best_config['exit_z'], best_config['dwell'])
        z_results = analyze_z_cutoff(records, z_min=0.5, z_max=4.5, z_step=0.05)

        print(f"\n  Collected {len(records)} total trades (Z >= 0.5)")
        z_dist = [r['entry_z'] for r in records]
        print(f"  Z distribution: min={min(z_dist):.2f} median={np.median(z_dist):.2f} max={max(z_dist):.2f}")

        # Show the Z cutoff analysis table
        print(f"\n  {'Z≥':>6} {'Trades':>7} {'WR':>6} {'PF':>6} {'Net':>10} {'$/Trade':>8} {'AvgGross':>9} {'AvgCost':>8}")
        print(f"  {'-'*70}")
        
        profitable_results = [r for r in z_results if r['trades'] >= 3]
        for zr in profitable_results:
            marker = " <<<" if zr['z'] == best_overall['z'] else ""
            profitable_flag = "+" if zr['net'] > 0 else "-"
            print(f"  {zr['z']:>5.2f} {zr['trades']:>7} {zr['wr']:>5.1f}% {zr['pf']:>5.2f} "
                  f"${zr['net']:>9,.2f} ${zr['avg']:>7.2f} ${zr['avg_gross']:>8.2f} ${zr['avg_cost']:>7.2f} "
                  f"{profitable_flag}{marker}")

        # Find the sweet spot
        best_z = best_overall['z']
        print(f"\n  ╔═══════════════════════════════════════════════════════════╗")
        print(f"  ║  OPTIMAL Z = {best_z:.2f}  ExitZ = {best_config['exit_z']:.3f}  Dwell = {best_config['dwell']}  ║")
        print(f"  ╚═══════════════════════════════════════════════════════════╝")
        print(f"    Trades: {best_overall['trades']} ({best_overall['trades']/months:.0f}/mo)")
        print(f"    WR: {best_overall['wr']}% | PF: {best_overall['pf']}")
        print(f"    Net: ${best_overall['net']:,.2f} ({best_overall['net']/months:.0f}/mo)")
        print(f"    $/trade: ${best_overall['avg']:.2f}")
        print(f"    Avg gross: ${best_overall['avg_gross']:.2f} | Avg cost: ${best_overall['avg_cost']:.2f}")
        print(f"    Cost/Gross ratio: {abs(best_overall['avg_cost']/best_overall['avg_gross'])*100:.0f}%" if best_overall['avg_gross'] != 0 else "")

        # At $5K
        scale = 0.05
        monthly_5k = best_overall['avg'] * scale * (best_overall['trades'] / months)
        print(f"\n    At $5K:   ~${monthly_5k:.0f}/month")
        print(f"    At $100K: ~${best_overall['avg'] * (best_overall['trades']/months):.0f}/month")

        all_results[pname] = {
            'optimal_z': best_z,
            'exit_z': best_config['exit_z'],
            'dwell': best_config['dwell'],
            'hmm_hold': ps['hmm_hold'],
            'notional': notional,
            'result': best_overall,
            'total_signals': len(records),
        }

    # ══════════════════════════════════════════
    # FINAL SUMMARY
    # ══════════════════════════════════════════
    print(f"\n\n{'=' * 120}")
    print("  FINAL: OPTIMAL Z VALUES FOR ALL PAIRS")
    print(f"{'=' * 120}\n")

    for pname, rd in all_results.items():
        r = rd['result']
        print(f"  {pname}: Z={rd['optimal_z']:.2f} ExitZ={rd['exit_z']:.3f} Dwell={rd['dwell']} HMM={rd['hmm_hold']}")
        print(f"    {r['trades']} trades | WR={r['wr']}% | PF={r['pf']} | ${r['avg']:.2f}/trade | Net=${r['net']:,.2f}")
        print()

    elapsed = time.time() - t0
    print(f"  Completed in {elapsed:.0f}s ({elapsed/60:.1f}min)")

    save = Path(__file__).resolve().parent.parent / "Results" / "optimal_z_fast.json"
    with open(save, 'w') as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"  Saved to {save}")


if __name__ == "__main__":
    main()
