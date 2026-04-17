#!/usr/bin/env python3
"""
BASELINE OPTIMAL Z FINDER — 100% Data
======================================
Runs ALL M1 data through fine-grid sweep to find the PRECISE optimal
Z entry, Exit Z, and Dwell for each pair.

These values become the STARTING POINT for the adaptive Z engine.
Yes, this overfits to historical data — that's the point. The live
engine then adapts these values in real-time based on current sigma/cost.

Pairs:
  Gold/Silver: HMM=10, Hurdle=1.0, Mult=2.0
  Oil:         HMM=10, Hurdle=1.0, Mult=2.0
  NAS/DAX:     HMM=20, Hurdle=1.0, Mult=2.0
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
        'swap_long_a': -91, 'swap_short_a': -68,
        'swap_long_b': -20, 'swap_short_b': -17,
        'swap_friday_mult': 3, 'hmm_hold': 10,
    },
    'Oil_Spread': {
        'sym_a': 'XTIUSD', 'sym_b': 'XBRUSD',
        'cs_a': 100, 'cs_b': 100,
        'comm_pct': 0.0003, 'spread_a_pts': 4, 'spread_b_pts': 5,
        'swap_long_a': -70, 'swap_short_a': -40,
        'swap_long_b': -70, 'swap_short_b': -40,
        'swap_friday_mult': 10, 'hmm_hold': 10,
    },
    'NAS_DAX': {
        'sym_a': 'US100', 'sym_b': 'DE40',
        'cs_a': 1, 'cs_b': 1,
        'comm_pct': 0, 'spread_a_pts': 2, 'spread_b_pts': 2,
        'swap_long_a': -300, 'swap_short_a': -300,
        'swap_long_b': -500, 'swap_short_b': -500,
        'swap_friday_mult': 3, 'hmm_hold': 20,
    },
}

WELFORD_SPAN = 100; GAMMA = 6.0; HURST_WINDOW = 512; EXIT_GAMMA = 2.0
DAKAD_LAMBDA = 40.0; DAKAD_P_RUIN = 1e-4
DAKAD_DAILY_DD_CEIL = 0.04; DAKAD_RESULT_WINDOW = 50
DAKAD_MIN_WR = 0.50; DAKAD_MAX_WR = 0.85
DAKAD_MIN_BASE = 0.003; DAKAD_MAX_BASE = 0.03; DAKAD_RISK_FLOOR = 0.0005
GHOST_DAILY_DD = 0.04; GHOST_MAX_DD = 0.09
KALMAN_TOLERANCE = 0.15; CORR_WINDOW = 200
ROLLOVER_LOCKOUT_MIN = 30; HMM_LOOKBACK = 100
MAX_CONSEC_LOSSES = 5; COOLDOWN_BARS = 60
MIN_WARMUP_BARS = 200; BAL = 100_000.0
HUBER_SIGMA = 4.815; AMP_HURDLE = 1.0; AMP_MAX_MULT = 2.0
SESSION_START_HOUR = 7; SESSION_END_HOUR = 20

# FINE GRID — finds the true optimal, not just nearest grid point
Z_GRID = np.arange(1.25, 4.01, 0.25).tolist()       # 1.25 to 4.0 step 0.25 = 12 values
EXIT_Z_GRID = np.arange(0.0, 1.01, 0.125).tolist()   # 0.0 to 1.0 step 0.125 = 9 values
DWELL_GRID = [1, 3, 5, 10, 15, 20, 30, 45, 60]       # 9 values
# Total per pair: 12 × 9 × 9 = 972 configs


class DynamicAKAD:
    def __init__(self):
        self._results = deque(maxlen=DAKAD_RESULT_WINDOW)
        for _ in range(10): self._results.append(1)
        for _ in range(5): self._results.append(0)
    def record(self, win): self._results.append(1 if win else 0)
    def calc(self, tdd, ddd):
        ddr = max(0.001, DAKAD_DAILY_DD_CEIL - ddd)
        wr = max(DAKAD_MIN_WR, min(DAKAD_MAX_WR, sum(self._results)/max(len(self._results),1)))
        ns = math.log(DAKAD_P_RUIN) / math.log(1.0 - wr)
        base = max(DAKAD_MIN_BASE, min(DAKAD_MAX_BASE, (math.exp(DAKAD_LAMBDA*ddr)-1)/(DAKAD_LAMBDA*ns)))
        return max(DAKAD_RISK_FLOOR, base * math.exp(-DAKAD_LAMBDA*tdd))


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
    m = pd.merge(a[['time','close_a']], b[['time','close_b']], on='time', how='inner')
    m = m.sort_values('time').reset_index(drop=True)
    return m[(m['close_a']>0)&(m['close_b']>0)].reset_index(drop=True)

def calc_notional(ps, pa, pb):
    return (ps['cs_a'] * pa + ps['cs_b'] * pb) / 2.0


def run_bt(df, ps, notional, pa, pb, z_ent, exit_z, hmm_h, dwell):
    bal = BAL; peak = BAL; ds = BAL; dd = None; ghost = False
    dakad = DynamicAKAD()
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec = 0; gcool = 0; n = len(df)

    eng = shf_core.CointegrationEngine(span=WELFORD_SPAN, beta=1.0, entry_z=z_ent,
        exit_z=exit_z, z_base=z_ent, gamma=GAMMA, hurst_window=HURST_WINDOW,
        dynamic_z=True, exit_z_base=exit_z, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
    hmm = HMMRegimeDetector(lookback=HMM_LOOKBACK, min_regime_hold=hmm_h)

    pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0
    lsp = 0.0; psp = 0.0; sa = False; lcb = -9999; lch = 0.5; eh = 0
    trades = []

    pa_arr = df['close_a'].values; pb_arr = df['close_b'].values
    tp = pd.DatetimeIndex(df['time'])
    hrs = tp.hour.values; mins = tp.minute.values; dates = tp.date

    for bar in range(n):
        if ghost: break
        p1 = pa_arr[bar]; p2 = pb_arr[bar]
        bh = int(hrs[bar]); bm = int(mins[bar])
        cd = dates[bar]
        if cd != dd: dd = cd; ds = bal
        cdd = max(0, (peak-bal)/peak) if peak > 0 else 0
        ddd = max(0, (ds-bal)/ds) if ds > 0 else 0
        if ddd >= GHOST_DAILY_DD: ghost = True; break
        if cdd >= GHOST_MAX_DD: ghost = True; break
        if bar < gcool: continue
        psp = lsp
        sig = eng.update(p1, p2)
        z = sig.z_score; s = sig.signal; sp = sig.spread; lsp = sp
        h = eng.last_hurst; exz = eng.last_exit_z
        if psp != 0: corr.push_return(0, sp - psp)
        la = math.log(p1) if p1 > 0 else 0; lb = math.log(p2) if p2 > 0 else 0
        bk, abt = sen.update(la, lb)
        if abt and not sa:
            sa = True
            if pos != 0:
                gr = (sp - es) * pos * elots * notional
                c = calc_cost(ps, elots, eh, pa, pb)
                pnl = gr - c; bal += pnl; peak = max(peak, bal)
                w = pnl > 0; dakad.record(w)
                if not w: consec += 1
                else: consec = 0
                if consec >= MAX_CONSEC_LOSSES: gcool = bar + COOLDOWN_BARS; consec = 0
                trades.append(pnl); pos = 0; lcb = bar; lch = h
            continue
        if sa and not abt: sa = False
        if sa: continue
        hb2 = False
        if psp != 0: hmm.update(sp - psp); hb2 = hmm.is_blocked
        if bar < MIN_WARMUP_BARS: continue

        if pos == 0 and s != 0:
            if hb2: continue
            bms = bh * 60 + bm
            if bms < ROLLOVER_LOCKOUT_MIN or (1440-bms) < ROLLOVER_LOCKOUT_MIN: continue
            if not (SESSION_START_HOUR <= bh < SESSION_END_HOUR): continue
            if lcb >= 0:
                da = max(1, dwell * (lch / 0.3))
                if (bar - lcb) < da: continue
            risk = dakad.calc(cdd, ddd)
            corr.compute_risk(); cm = corr.last_risk_multiplier
            lots = max(0.01, round(bal * risk * cm / 1000.0, 2))
            if AMP_HURDLE > 0:
                ss = eng.last_std if hasattr(eng, 'last_std') else 0
                if ss > 0:
                    zc = max(0.0, abs(z) - exz)
                    ep = zc * ss * lots * notional
                    tc = calc_cost(ps, lots, bh, pa, pb)
                    ratio = ep / tc if tc > 0 else 999
                    if ratio < AMP_HURDLE: continue
                    if AMP_MAX_MULT > 1.0 and ratio > AMP_HURDLE:
                        exc = (ratio - AMP_HURDLE) / AMP_HURDLE
                        ml = min(AMP_MAX_MULT, 1.0 + 0.5 * exc)
                        lots = max(0.01, round(lots * ml, 2))
            pos = s; ez = z; es = sp; ebar = bar; elots = lots; eh = bh
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
                da = max(1, dwell * (h / 0.3))
                if hbs < da: continue
                if pos == 1 and z > -exz: ex = True
                elif pos == -1 and z < exz: ex = True
            if ex:
                gr = (sp - es) * pos * elots * notional
                c = calc_cost(ps, elots, eh, pa, pb)
                pnl = gr - c; bal += pnl; peak = max(peak, bal)
                w = pnl > 0; dakad.record(w)
                if not w: consec += 1
                else: consec = 0
                if consec >= MAX_CONSEC_LOSSES: gcool = bar + COOLDOWN_BARS; consec = 0
                trades.append(pnl); pos = 0; lcb = bar; lch = h

    t = len(trades)
    if t < 3: return None
    wins = [p for p in trades if p > 0]; losses = [p for p in trades if p <= 0]
    gp = sum(wins) if wins else 0; gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins)/t*100; pf = gp/gl
    d1 = df['time'].iloc[0]; d2 = df['time'].iloc[-1]
    days = max(1, (d2-d1).days); months = max(0.1, days/30.0)
    eq = BAL; eqp = eq; mdd = 0
    for p in trades: eq += p; eqp = max(eqp, eq); mdd = max(mdd, eqp - eq)
    return {
        'trades': t, 'wr': round(wr,1), 'pf': round(pf,2),
        'net': round(bal-BAL,2), 'avg': round((bal-BAL)/t,2),
        'ghost': ghost, 'dd': round(mdd/BAL*100,2),
        'trpm': round(t/months,1), 'mopct': round((bal-BAL)/BAL/months*100,2),
    }


def main():
    t0 = time.time()
    print("=" * 120)
    print("  BASELINE OPTIMAL Z FINDER — 100% Data (All Pairs)")
    print("  Finding the PRECISE optimal Z/ExitZ/Dwell for adaptive engine")
    print("=" * 120)
    total = len(Z_GRID) * len(EXIT_Z_GRID) * len(DWELL_GRID)
    print(f"\n  Z grid:     {[f'{z:.2f}' for z in Z_GRID]}")
    print(f"  ExitZ grid: {[f'{z:.3f}' for z in EXIT_Z_GRID]}")
    print(f"  Dwell grid: {DWELL_GRID}")
    print(f"  Total per pair: {total}")
    print(f"  Locked: Hurdle={AMP_HURDLE}, Mult={AMP_MAX_MULT}")

    results = {}

    for pname, ps in PAIRS.items():
        print(f"\n{'=' * 120}")
        print(f"  {pname} ({ps['sym_a']}/{ps['sym_b']}) | HMM={ps['hmm_hold']}")
        print(f"{'=' * 120}")

        df = load_pair(ps['sym_a'], ps['sym_b'])
        if df is None or len(df) < 2000:
            print(f"  SKIP: insufficient data"); continue

        pa = df['close_a'].mean(); pb = df['close_b'].mean()
        notional = calc_notional(ps, pa, pb)
        print(f"  M1 bars: {len(df):,} | {df['time'].iloc[0]} to {df['time'].iloc[-1]}")
        print(f"  Avg prices: {ps['sym_a']}=${pa:.2f} {ps['sym_b']}=${pb:.2f}")
        print(f"  Notional: ${notional:,.0f}")

        best = None; best_score = -999999; best_p = None; count = 0
        pt = time.time()

        # Track ALL profitable configs for robustness analysis
        profitable = []

        for z in Z_GRID:
            for exz in EXIT_Z_GRID:
                for dw in DWELL_GRID:
                    count += 1
                    if count % 100 == 0:
                        print(f"    {count}/{total} ({time.time()-pt:.0f}s)...", flush=True)
                    r = run_bt(df, ps, notional, pa, pb, z, exz, ps['hmm_hold'], dw)
                    if r is None: continue
                    if r['trades'] < 3: continue
                    score = r['net'] * min(1.0, r['trades']/10.0)
                    if r['ghost']: score *= 0.1
                    if r['net'] > 0 and r['trades'] >= 5 and not r['ghost']:
                        profitable.append({'z': z, 'exz': exz, 'dwell': dw, **r})
                    if score > best_score and not r['ghost']:
                        best_score = score; best = r
                        best_p = {'z': z, 'exit_z': exz, 'dwell': dw}

        el = time.time() - pt
        print(f"  Completed {count} configs in {el:.0f}s ({el/60:.1f}min)")

        if best_p is None:
            print(f"  NO profitable config found"); continue

        print(f"\n  ╔══════════════════════════════════════════════════╗")
        print(f"  ║  OPTIMAL: Z={best_p['z']:.2f}  ExitZ={best_p['exit_z']:.3f}  Dwell={best_p['dwell']}  ║")
        print(f"  ╚══════════════════════════════════════════════════╝")
        print(f"    Trades: {best['trades']} ({best['trpm']:.0f}/mo)")
        print(f"    WR: {best['wr']}% | PF: {best['pf']} | Net: ${best['net']:,.2f}")
        print(f"    $/trade: ${best['avg']:.2f} | Monthly: {best['mopct']:.2f}% | MaxDD: {best['dd']}%")

        # Robustness: how many nearby configs also profitable?
        if profitable:
            profitable.sort(key=lambda x: -x['net'])
            z_values = sorted(set(p['z'] for p in profitable))
            exz_values = sorted(set(p['exz'] for p in profitable))
            print(f"\n    Robustness: {len(profitable)} profitable configs out of {count}")
            print(f"    Profitable Z range: {min(z_values):.2f} to {max(z_values):.2f}")
            print(f"    Profitable ExZ range: {min(exz_values):.3f} to {max(exz_values):.3f}")

            # Top 5
            print(f"\n    Top 5:")
            for i, p in enumerate(profitable[:5]):
                print(f"      #{i+1}: Z={p['z']:.2f} ExZ={p['exz']:.3f} Dw={p['dwell']} | "
                      f"{p['trades']}tr PF={p['pf']} WR={p['wr']}% ${p['avg']:.2f}/tr Net=${p['net']:,.2f}")

        results[pname] = {
            'optimal': best_p,
            'hmm_hold': ps['hmm_hold'],
            'notional': notional,
            'result': best,
            'profitable_count': len(profitable),
            'top5': profitable[:5] if profitable else [],
        }

    # ══════════════════════════════════════════
    # FINAL SUMMARY — ready to paste into engine
    # ══════════════════════════════════════════
    print(f"\n\n{'=' * 120}")
    print("  BASELINE VALUES FOR ADAPTIVE ENGINE")
    print(f"{'=' * 120}")
    print()
    print("  Paste these into engine.py PairConfig:")
    print()
    for pname, rd in results.items():
        p = rd['optimal']
        r = rd['result']
        print(f"  {pname}:")
        print(f"    z_optimal={p['z']:.2f}, exit_z_optimal={p['exit_z']:.3f}, dwell_optimal={p['dwell']}")
        print(f"    # PF={r['pf']} WR={r['wr']}% ${r['avg']:.2f}/trade {r['trpm']:.0f}trades/mo DD={r['dd']}%")
        print()

    elapsed = time.time() - t0
    print(f"  Completed in {elapsed:.0f}s ({elapsed/60:.1f}min)")

    # Also output the engine config snippet
    print("\n  ═══ COPY-PASTE ENGINE CONFIG SNIPPET ═══\n")
    for pname, rd in results.items():
        p = rd['optimal']
        print(f"    # {pname}")
        print(f"    z_optimal={p['z']:.2f}, exit_z_optimal={p['exit_z']:.3f},")
    print()

    save = Path(__file__).resolve().parent.parent / "Results" / "baseline_optimal_z.json"
    with open(save, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"  Saved to {save}")


if __name__ == "__main__":
    main()
