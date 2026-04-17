#!/usr/bin/env python3
"""
OPTUNA CONTINUOUS OPTIMIZER + FIXED vs ADAPTIVE COMPARISON
============================================================
Phase 1: Optuna on 100% data → finds PERFECT continuous params
         (Z=2.174, ExitZ=0.274, Dwell=17.3 — not grid points)

Phase 2: Run full 3.5 months TWICE with Phase 1's optimal base:
         Mode A (FIXED):    Params locked — never change
         Mode B (ADAPTIVE): Starts with optimal, then adjusts Z/ExitZ
                            every N trades based on rolling sigma/costs/WR
         → Directly answers "does adaptation help?"

Optimizes ALL 6 key params per pair:
  Z_entry, Exit_Z, Dwell, HMM_hold, Amp_Hurdle, Amp_Max_Mult
"""

import sys, math, time, json, logging
import numpy as np
import pandas as pd
from pathlib import Path
from collections import deque

logging.getLogger("optuna").setLevel(logging.ERROR)

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shf_core
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)  # No per-trial spam

# ============================================================================
# 5%ERS BROKER SPECS
# ============================================================================
PAIRS = {
    'Gold_Silver': {
        'sym_a': 'XAUUSD', 'sym_b': 'XAGUSD',
        'cs_a': 100, 'cs_b': 5000,
        'comm_pct': 0.000009, 'spread_a_pts': 30, 'spread_b_pts': 3,
    },
    'Oil_Spread': {
        'sym_a': 'XTIUSD', 'sym_b': 'XBRUSD',
        'cs_a': 100, 'cs_b': 100,
        'comm_pct': 0.0003, 'spread_a_pts': 4, 'spread_b_pts': 5,
    },
    'NAS_DAX': {
        'sym_a': 'US100', 'sym_b': 'DE40',
        'cs_a': 1, 'cs_b': 1,
        'comm_pct': 0, 'spread_a_pts': 2, 'spread_b_pts': 2,
    },
}

# Fixed engine params
WELFORD_SPAN = 100; GAMMA = 6.0; HURST_WINDOW = 512; EXIT_GAMMA = 2.0
DAKAD_LAMBDA = 40.0; DAKAD_P_RUIN = 1e-4
DAKAD_DAILY_DD_CEIL = 0.04; DAKAD_RESULT_WINDOW = 50    # 4% daily DD ceiling
DAKAD_MIN_WR = 0.50; DAKAD_MAX_WR = 0.85
DAKAD_MIN_BASE = 0.003; DAKAD_MAX_BASE = 0.03; DAKAD_RISK_FLOOR = 0.0005
GHOST_DAILY_DD = 0.04; GHOST_MAX_DD = 0.09   # 4% daily, 9% max
KALMAN_TOLERANCE = 0.15; CORR_WINDOW = 200
ROLLOVER_LOCKOUT_MIN = 30; HMM_LOOKBACK = 100
MAX_CONSEC_LOSSES = 5; COOLDOWN_BARS = 60
MIN_WARMUP_BARS = 200; BAL = 100_000.0
HUBER_SIGMA = 4.815
SESSION_START_HOUR = 7; SESSION_END_HOUR = 20

# Optuna config
N_TRIALS = 150

# Adaptive config
ADAPT_EVERY_N_TRADES = 10      # Re-evaluate Z every N trades
ADAPT_ROLLING_WINDOW = 20      # Look at last N trades for adaptation
ADAPT_Z_STEP = 0.05            # Max Z adjustment per adaptation
ADAPT_EXITZ_STEP = 0.025       # Max ExitZ adjustment per adaptation


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


def run_backtest(df, ps, notional, avg_pa, avg_pb, hmm_hold,
                 z_entry, exit_z, dwell_bars, amp_hurdle=1.0, amp_max_mult=2.0,
                 adaptive=False):
    """
    Full backtest. If adaptive=True, adjusts z_entry and exit_z every
    ADAPT_EVERY_N_TRADES trades based on rolling performance.
    """
    bal = BAL; peak = BAL; ds = BAL; dd = None; ghost = False
    dakad = DynamicAKAD()
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec = 0; gcool = 0; n = len(df)

    # Adaptive state
    base_z = z_entry; base_exz = exit_z
    current_z = z_entry; current_exz = exit_z
    trade_pnls = []  # rolling trade P&L history for adaptation
    trade_grosses = []  # rolling gross P&L
    trade_costs_hist = []  # rolling cost history
    adapt_log = []  # log of adaptations

    eng = shf_core.CointegrationEngine(
        span=WELFORD_SPAN, beta=1.0, entry_z=current_z,
        exit_z=current_exz, z_base=current_z, gamma=GAMMA, hurst_window=HURST_WINDOW,
        dynamic_z=True, exit_z_base=current_exz, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
    hmm = HMMRegimeDetector(lookback=HMM_LOOKBACK, min_regime_hold=hmm_hold)

    pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0
    lsp = 0.0; psp = 0.0; sa = False; lcb = -9999; lch = 0.5; eh = 0
    trades = []

    pa_arr = df['close_a'].values.astype(np.float64)
    pb_arr = df['close_b'].values.astype(np.float64)
    tp = pd.DatetimeIndex(df['time'])
    hrs = tp.hour.values; mins = tp.minute.values; dates = tp.date

    def record_trade(pnl, gross, cost):
        trades.append(pnl)
        trade_pnls.append(pnl)
        trade_grosses.append(gross)
        trade_costs_hist.append(cost)
        w = pnl > 0; dakad.record(w)
        nonlocal consec, gcool
        if not w: consec += 1
        else: consec = 0
        if consec >= MAX_CONSEC_LOSSES: gcool = bar + COOLDOWN_BARS; consec = 0

        # Adaptation logic
        if adaptive and len(trades) >= ADAPT_ROLLING_WINDOW and len(trades) % ADAPT_EVERY_N_TRADES == 0:
            adapt_z(len(trades))

    def adapt_z(trade_num):
        nonlocal current_z, current_exz
        window = min(ADAPT_ROLLING_WINDOW, len(trade_pnls))
        recent_pnls = list(trade_pnls)[-window:]
        recent_grosses = list(trade_grosses)[-window:]
        recent_costs = list(trade_costs_hist)[-window:]

        recent_wr = sum(1 for p in recent_pnls if p > 0) / len(recent_pnls)
        avg_gross = np.mean(recent_grosses) if recent_grosses else 0
        avg_cost = np.mean(recent_costs) if recent_costs else 0

        old_z = current_z; old_exz = current_exz

        # Z adaptation logic:
        # If WR is dropping → raise Z (be more selective, fewer trades)
        # If WR is high → lower Z (capture more opportunities)
        # If costs are eating edge → raise Z (need bigger signals)
        if recent_wr < 0.55:
            current_z = min(base_z * 1.5, current_z + ADAPT_Z_STEP)
        elif recent_wr > 0.80 and avg_gross > avg_cost * 2:
            current_z = max(base_z * 0.75, current_z - ADAPT_Z_STEP)

        # ExitZ adaptation:
        # If trades are profitable but gross is barely above cost → tighten exit
        # If losing → widen exit (let trades run more)
        cost_ratio = avg_cost / avg_gross if avg_gross > 0 else 999
        if cost_ratio > 0.7:
            current_exz = max(0.0, current_exz - ADAPT_EXITZ_STEP)
        elif cost_ratio < 0.3 and recent_wr > 0.7:
            current_exz = min(base_exz * 1.5, current_exz + ADAPT_EXITZ_STEP)

        if current_z != old_z or current_exz != old_exz:
            adapt_log.append({
                'trade': trade_num, 'wr': round(recent_wr, 3),
                'z': round(old_z, 3), 'new_z': round(current_z, 3),
                'exz': round(old_exz, 3), 'new_exz': round(current_exz, 3),
            })

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
                c = calc_cost(ps, elots, eh, avg_pa, avg_pb)
                pnl = gr - c; bal += pnl; peak = max(peak, bal)
                record_trade(pnl, gr, c)
                pos = 0; lcb = bar; lch = h
            continue
        if sa and not abt: sa = False
        if sa: continue
        hb2 = False
        if psp != 0: hmm.update(sp - psp); hb2 = hmm.is_blocked
        if bar < MIN_WARMUP_BARS: continue

        if pos == 0 and s != 0:
            # Engine already applies dynamic Z threshold internally (z_base * (1 + gamma * hurst))
            # No redundant Z check — matches optimize_pairs_full.py exactly
            if hb2: continue
            bms = bh * 60 + bm
            if bms < ROLLOVER_LOCKOUT_MIN or (1440-bms) < ROLLOVER_LOCKOUT_MIN: continue
            if not (SESSION_START_HOUR <= bh < SESSION_END_HOUR): continue
            if lcb >= 0:
                da = max(1, dwell_bars * (lch / 0.3))
                if (bar - lcb) < da: continue
            risk = dakad.calc(cdd, ddd)
            corr.compute_risk(); cm = corr.last_risk_multiplier
            lots = max(0.01, round(bal * risk * cm / 1000.0, 2))
            if amp_hurdle > 0:
                ss = eng.last_std if hasattr(eng, 'last_std') else 0
                if ss > 0:
                    effective_exz = current_exz if adaptive else exit_z
                    zc = max(0.0, abs(z) - effective_exz)
                    ep = zc * ss * lots * notional
                    tc = calc_cost(ps, lots, bh, avg_pa, avg_pb)
                    ratio = ep / tc if tc > 0 else 999
                    if ratio < amp_hurdle: continue
                    if amp_max_mult > 1.0 and ratio > amp_hurdle:
                        exc = (ratio - amp_hurdle) / amp_hurdle
                        ml = min(amp_max_mult, 1.0 + 0.5 * exc)
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
                da = max(1, dwell_bars * (h / 0.3))
                if hbs < da: continue
                effective_exz = current_exz if adaptive else exit_z
                if pos == 1 and z > -effective_exz: ex = True
                elif pos == -1 and z < effective_exz: ex = True
            if ex:
                gr = (sp - es) * pos * elots * notional
                c = calc_cost(ps, elots, eh, avg_pa, avg_pb)
                pnl = gr - c; bal += pnl; peak = max(peak, bal)
                record_trade(pnl, gr, c)
                pos = 0; lcb = bar; lch = h

    t = len(trades)
    if t < 3:
        return {'score': -100 + t * 10, 'trades': t, 'ghost': ghost, 'adapt_log': adapt_log,
                'wr': 0, 'pf': 0, 'net': 0, 'avg': 0, 'dd': 0, 'trpm': 0, 'mopct': 0, 'months': 0}
    wins = [p for p in trades if p > 0]; losses = [p for p in trades if p <= 0]
    gp = sum(wins) if wins else 0; gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins)/t*100; pf = gp/gl; net = bal - BAL
    d1 = df['time'].iloc[0]; d2 = df['time'].iloc[-1]
    days = max(1, (d2-d1).days); months = max(0.1, days/30.0)
    eq = BAL; eqp = eq; mdd = 0
    for p in trades: eq += p; eqp = max(eqp, eq); mdd = max(mdd, eqp - eq)

    # Score: EXACTLY like optimize_pairs_full.py — net P&L with trade count penalty
    score = net * min(1.0, t / 20.0)
    if ghost: score *= 0.1

    return {
        'score': score, 'trades': t, 'wr': round(wr, 1), 'pf': round(pf, 2),
        'net': round(net, 2), 'avg': round(net / t, 2),
        'dd': round(mdd / BAL * 100, 2), 'ghost': ghost,
        'trpm': round(t / months, 1), 'mopct': round(net / BAL / months * 100, 2),
        'months': round(months, 1), 'adapt_log': adapt_log,
    }


def fmt_result(r, label=""):
    if r['trades'] == 0:
        return f"  {label}: NO TRADES"
    return (f"  {label}: {r['trades']} trades ({r['trpm']:.0f}/mo) | "
            f"WR={r['wr']}% | PF={r['pf']} | Net=${r['net']:,.2f} | "
            f"$/trade=${r['avg']:.2f} | DD={r['dd']}%"
            + (" | GHOST!" if r['ghost'] else ""))


def main():
    t0 = time.time()
    print("=" * 120)
    print("  OPTUNA OPTIMIZER + FIXED vs ADAPTIVE COMPARISON")
    print("=" * 120)
    print(f"\n  Phase 1: Optuna {N_TRIALS} trials on 100% data → optimal base params")
    print(f"  Phase 2: Fixed vs Adaptive comparison on 100% data")
    print(f"  Optimizing 6 params: Z, ExitZ, Dwell, HMM, Hurdle, Mult")

    all_results = {}

    for pname, ps in PAIRS.items():
        print(f"\n\n{'=' * 120}")
        print(f"  PAIR: {pname} ({ps['sym_a']}/{ps['sym_b']})")
        print(f"{'=' * 120}")

        df = load_pair(ps['sym_a'], ps['sym_b'])
        if df is None or len(df) < 2000:
            print(f"  SKIP: insufficient data"); continue

        avg_a = df['close_a'].mean(); avg_b = df['close_b'].mean()
        notional = calc_notional(ps, avg_a, avg_b)
        days = (df['time'].iloc[-1] - df['time'].iloc[0]).days

        print(f"  M1 bars: {len(df):,} | {df['time'].iloc[0]} to {df['time'].iloc[-1]} ({days}d)")
        print(f"  Notional: ${notional:,.0f}")

        # ══════════════════════════════════════════════════════
        # PHASE 1: OPTUNA ON 100% DATA
        # ══════════════════════════════════════════════════════
        print(f"\n  {'━' * 60}")
        print(f"  PHASE 1: Optuna on 100% data ({N_TRIALS} trials)")
        print(f"  {'━' * 60}")

        trial_count = [0]
        phase1_start = time.time()

        def objective(trial):
            # Ranges match optimize_pairs_full.py proven space
            z = trial.suggest_float('z_entry', 1.5, 3.5)
            exz = trial.suggest_float('exit_z', 0.0, 0.75)
            dwell = trial.suggest_float('dwell', 1.0, 60.0)
            hmm_h = trial.suggest_int('hmm_hold', 5, 50)
            hurdle = trial.suggest_float('amp_hurdle', 0.0, 3.5)
            mult = trial.suggest_float('amp_max_mult', 1.0, 2.5)

            r = run_backtest(df, ps, notional, avg_a, avg_b, hmm_h,
                             z_entry=z, exit_z=exz, dwell_bars=dwell,
                             amp_hurdle=hurdle, amp_max_mult=mult, adaptive=False)

            trial_count[0] += 1
            if trial_count[0] % 10 == 0:
                elapsed = time.time() - phase1_start
                best_so_far = trial.study.best_value if trial.study.best_trial else 0
                print(f"    Trial {trial_count[0]}/{N_TRIALS} ({elapsed:.0f}s) "
                      f"best_score={best_so_far:.1f}", flush=True)

            return r['score']

        # n_startup_trials=15: only 15 random trials, then full Bayesian exploitation
        # Lower = faster convergence, less random noise
        study = optuna.create_study(direction='maximize',
                                     sampler=optuna.samplers.TPESampler(seed=42, n_startup_trials=15))
        study.optimize(objective, n_trials=N_TRIALS, show_progress_bar=False)

        bp = study.best_params
        phase1_elapsed = time.time() - phase1_start
        print(f"\n  Phase 1 complete in {phase1_elapsed:.0f}s ({phase1_elapsed/60:.1f}min)")
        print(f"\n  ╔══════════════════════════════════════════════════════════════════════════════════╗")
        print(f"  ║  OPTIMAL: Z={bp['z_entry']:.3f}  ExitZ={bp['exit_z']:.3f}  Dwell={bp['dwell']:.1f}  "
              f"HMM={bp['hmm_hold']}  Hrdl={bp['amp_hurdle']:.2f}  Mult={bp['amp_max_mult']:.2f}  ║")
        print(f"  ╚══════════════════════════════════════════════════════════════════════════════════╝")

        # ══════════════════════════════════════════════════════
        # PHASE 2: FIXED vs ADAPTIVE ON 100% DATA
        # ══════════════════════════════════════════════════════
        print(f"\n  {'━' * 60}")
        print(f"  PHASE 2: Fixed vs Adaptive comparison (100% data)")
        print(f"  {'━' * 60}")

        # Mode A: FIXED — exact optimal params, never change
        print(f"\n  Running Mode A (FIXED)...")
        fixed_result = run_backtest(df, ps, notional, avg_a, avg_b, bp['hmm_hold'],
                                     z_entry=bp['z_entry'], exit_z=bp['exit_z'],
                                     dwell_bars=bp['dwell'], amp_hurdle=bp['amp_hurdle'],
                                     amp_max_mult=bp['amp_max_mult'], adaptive=False)

        # Mode B: ADAPTIVE — starts with optimal, adapts every N trades
        print(f"  Running Mode B (ADAPTIVE)...")
        adaptive_result = run_backtest(df, ps, notional, avg_a, avg_b, bp['hmm_hold'],
                                        z_entry=bp['z_entry'], exit_z=bp['exit_z'],
                                        dwell_bars=bp['dwell'], amp_hurdle=bp['amp_hurdle'],
                                        amp_max_mult=bp['amp_max_mult'], adaptive=True)

        # ── COMPARISON ──
        print(f"\n{fmt_result(fixed_result, 'FIXED   ')}")
        print(f"{fmt_result(adaptive_result, 'ADAPTIVE')}")

        if fixed_result['trades'] > 0 and adaptive_result['trades'] > 0:
            print(f"\n  {'Metric':<12} {'FIXED':>12} {'ADAPTIVE':>12} {'Delta':>10} {'Winner':>8}")
            print(f"  {'─'*60}")
            comparisons = [
                ('Trades', fixed_result['trades'], adaptive_result['trades']),
                ('WR', fixed_result['wr'], adaptive_result['wr']),
                ('PF', fixed_result['pf'], adaptive_result['pf']),
                ('Net $', fixed_result['net'], adaptive_result['net']),
                ('$/trade', fixed_result['avg'], adaptive_result['avg']),
                ('MaxDD%', fixed_result['dd'], adaptive_result['dd']),
            ]
            for name, fv, av in comparisons:
                delta = av - fv
                # For MaxDD, lower is better
                if name == 'MaxDD%':
                    winner = "ADAPT" if delta < 0 else "FIXED" if delta > 0 else "TIE"
                else:
                    winner = "ADAPT" if delta > 0 else "FIXED" if delta < 0 else "TIE"
                if name in ('Net $', '$/trade'):
                    print(f"  {name:<12} ${fv:>10,.2f} ${av:>10,.2f} {delta:>+10.2f} {winner:>8}")
                elif name == 'Trades':
                    print(f"  {name:<12} {fv:>12} {av:>12} {delta:>+10} {winner:>8}")
                else:
                    print(f"  {name:<12} {fv:>12.2f} {av:>12.2f} {delta:>+10.2f} {winner:>8}")

            # Verdict
            adapt_better = adaptive_result['net'] > fixed_result['net']
            print(f"\n  {'═' * 60}")
            if adapt_better:
                improvement = adaptive_result['net'] - fixed_result['net']
                print(f"  ✅ ADAPTIVE WINS by ${improvement:,.2f}")
                print(f"     → Adaptation HELPS on this pair. Use adaptive Z live.")
            else:
                degradation = fixed_result['net'] - adaptive_result['net']
                print(f"  ❌ FIXED WINS by ${degradation:,.2f}")
                print(f"     → Adaptation HURTS on this pair. Use fixed optimal Z live.")

            # Show adaptation log
            adapt_log = adaptive_result.get('adapt_log', [])
            if adapt_log:
                print(f"\n  Adaptation history ({len(adapt_log)} adjustments):")
                for al in adapt_log[:10]:
                    print(f"    Trade {al['trade']}: WR={al['wr']:.1%} | "
                          f"Z: {al['z']:.3f}→{al['new_z']:.3f} | "
                          f"ExitZ: {al['exz']:.3f}→{al['new_exz']:.3f}")
                if len(adapt_log) > 10:
                    print(f"    ... and {len(adapt_log)-10} more")

        # Store results
        all_results[pname] = {
            'notional': notional,
            'optimal_params': {
                'z_entry': round(bp['z_entry'], 4),
                'exit_z': round(bp['exit_z'], 4),
                'dwell': round(bp['dwell'], 2),
                'hmm_hold': bp['hmm_hold'],
                'amp_hurdle': round(bp['amp_hurdle'], 4),
                'amp_max_mult': round(bp['amp_max_mult'], 4),
            },
            'fixed_result': {k: v for k, v in fixed_result.items() if k != 'adapt_log'},
            'adaptive_result': {k: v for k, v in adaptive_result.items() if k != 'adapt_log'},
            'adaptive_wins': adaptive_result['net'] > fixed_result['net'] if fixed_result['trades'] > 0 and adaptive_result['trades'] > 0 else False,
            'adapt_adjustments': len(adaptive_result.get('adapt_log', [])),
        }

    # ══════════════════════════════════════════
    # FINAL SUMMARY
    # ══════════════════════════════════════════
    print(f"\n\n{'=' * 120}")
    print("  FINAL SUMMARY")
    print(f"{'=' * 120}\n")

    for pname, rd in all_results.items():
        p = rd['optimal_params']
        fr = rd['fixed_result']
        ar = rd['adaptive_result']
        winner = "ADAPTIVE ✅" if rd['adaptive_wins'] else "FIXED ✅"

        print(f"  {pname} → {winner}")
        print(f"    Optimal: Z={p['z_entry']:.3f}  ExitZ={p['exit_z']:.3f}  Dwell={p['dwell']:.1f}  "
              f"HMM={p['hmm_hold']}  Hrdl={p['amp_hurdle']:.2f}  Mult={p['amp_max_mult']:.2f}")
        if fr['trades'] > 0:
            print(f"    Fixed:    {fr['trades']} trades | WR={fr['wr']}% | PF={fr['pf']} | Net=${fr['net']:,.2f}")
        if ar['trades'] > 0:
            print(f"    Adaptive: {ar['trades']} trades | WR={ar['wr']}% | PF={ar['pf']} | Net=${ar['net']:,.2f}")
        print(f"    Adaptations: {rd['adapt_adjustments']}")
        print()

    elapsed = time.time() - t0
    print(f"  Completed in {elapsed:.0f}s ({elapsed/60:.1f}min)")

    save = Path(__file__).resolve().parent.parent / "Results" / "optuna_optimization.json"
    with open(save, 'w') as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"  Saved to {save}")


if __name__ == "__main__":
    main()
