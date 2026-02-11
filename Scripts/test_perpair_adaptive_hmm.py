#!/usr/bin/env python3
"""
SHF v5.6 — PER-PAIR ADAPTIVE HMM HOLD BACKTEST
=================================================
The "PhD Goldilocks" Method: Each pair gets its OWN HMM hold
calculated from its measured Hurst exponent + OU theta.

Physics:
  - MR pairs (H < 0.55): Vol spikes are noise in the reversion process
    → HIGH hold (be patient, don't false-block)
  - Trending pairs (H > 0.55): Vol regime changes are real structural shifts
    → LOW hold (respect regime transitions quickly)

Formula:
    sigmoid = 1 / (1 + exp(k * (H - H_mid)))
    base_hold = 5 + 95 * sigmoid
    
    Then OU theta refines: if theta > 2 (fast MR), the spread recovers
    from shocks quickly → can reduce hold slightly (shock dissipates fast,
    HMM can re-evaluate sooner).

The hold is recalculated every 500 bars as H and θ evolve.
Each pair independently adapts to its own characteristics.
"""

import sys, math, time, json
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from collections import deque
from dataclasses import dataclass
from typing import Dict, List, Optional

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shf_core

# ============================================================================
# v5.6 PARAMETERS — EXACT MATCH TO engine.py
# ============================================================================
WELFORD_SPAN = 100; Z_BASE = 2.0; GAMMA = 6.0; HURST_WINDOW = 512
EXIT_Z_BASE = 0.5; EXIT_GAMMA = 2.0
DAKAD_LAMBDA = 40.0; DAKAD_P_RUIN = 1e-4
DAKAD_DAILY_DD_CEIL = 0.04; DAKAD_RESULT_WINDOW = 50
DAKAD_MIN_WR = 0.50; DAKAD_MAX_WR = 0.85
DAKAD_MIN_BASE = 0.003; DAKAD_MAX_BASE = 0.03; DAKAD_RISK_FLOOR = 0.0005
GHOST_DAILY_DD = 0.04; GHOST_MAX_DD = 0.09
KALMAN_TOLERANCE = 0.15; CORR_WINDOW = 200
DWELL_BASE = 60.0; DWELL_ANCHOR = 0.3; DWELL_MIN = 30.0; DWELL_MAX = 300.0
ROLLOVER_LOCKOUT_MIN = 5
HMM_N_REGIMES = 3; HMM_LOOKBACK = 100
MAX_CONSEC_LOSSES = 5; COOLDOWN_BARS = 60
MIN_WARMUP_BARS = 200; STARTING_BALANCE = 100_000.0

# Adaptive HMM parameters
ADAPT_REFIT_INTERVAL = 500   # Re-evaluate every 500 bars
ADAPT_H_MIDPOINT = 0.55      # Sigmoid center — the MR/trending boundary
ADAPT_K = 50.0               # Sigmoid steepness
ADAPT_THETA_THRESHOLD = 2.0  # θ above this = "fast MR"
ADAPT_THETA_MAX_DISCOUNT = 0.30  # Max hold reduction from fast θ

@dataclass
class PairDef:
    name: str; sym_a: str; sym_b: str; file_a: str; file_b: str
    pair_index: int; notional: float = 100_000.0

ALL_PAIRS = [
    PairDef("Index Spread",    "US100","DE40",    "US100_M1.csv","DE40_M1.csv",     0, 150_000.0),
    PairDef("Forex Anchor",    "AUDUSD","NZDUSD", "AUDUSD_M1.csv","NZDUSD_M1.csv",  1, 100_000.0),
    PairDef("EUR/GBP Spread",  "EURUSD","GBPUSD", "EURUSD_M1.csv","GBPUSD_M1.csv",  2, 100_000.0),
    PairDef("EURJPY/CHFJPY",   "EURJPY","CHFJPY", "EURJPY_M1.csv","CHFJPY_M1.csv",  3, 100_000.0),
    PairDef("XTIUSD/XBRUSD",   "XTIUSD","XBRUSD", "XTIUSD_M1.csv","XBRUSD_M1.csv", 4, 100_000.0),
    PairDef("XAUUSD/XAGUSD",   "XAUUSD","XAGUSD", "XAUUSD_M1.csv","XAGUSD_M1.csv", 5, 100_000.0),
]

# ============================================================================
# PER-PAIR ADAPTIVE HMM HOLD
# ============================================================================

def adaptive_hold(hurst: float, theta: float) -> int:
    """
    Per-pair adaptive HMM hold using Hurst + OU theta.
    
    Physics:
    - MR pairs (H < 0.55): vol spikes are noise in mean reversion → high hold (be patient)
    - Trending pairs (H > 0.55): vol regimes are real structural shifts → low hold (respect them)
    - Fast MR (high θ): shocks dissipate quickly → slight hold reduction
    
    Returns: integer hold value in [5, 200]
    """
    # Sigmoid: smooth S-curve transition at H=0.55
    # When H < 0.55: sigmoid → 1.0 → hold → 100
    # When H > 0.55: sigmoid → 0.0 → hold → 5
    sigmoid = 1.0 / (1.0 + math.exp(ADAPT_K * (hurst - ADAPT_H_MIDPOINT)))
    base = 5 + int(95 * sigmoid)
    
    # Theta refinement: fast MR (high θ) → shock recovery is faster → can reduce hold
    # This only helps pairs with θ > 2 (Oil at θ=8.68 gets up to 30% reduction)
    if theta > ADAPT_THETA_THRESHOLD:
        discount = min(ADAPT_THETA_MAX_DISCOUNT, (theta - ADAPT_THETA_THRESHOLD) * 0.05)
        base = max(5, int(base * (1 - discount)))
    
    return max(5, min(200, base))


# ============================================================================
# COMPONENTS
# ============================================================================

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
    @property
    def current_wr(self): return sum(self._results)/max(len(self._results),1)


class HMMRegimeDetector:
    def __init__(self, lookback=100):
        self._lookback = lookback; self._current_regime = 0
        self._return_buffer = []; self._regime_hold_count = 0
        self._min_regime_hold = 100
    
    def set_hold(self, hold):
        self._min_regime_hold = hold
    
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


def calc_dwell(h):
    return max(DWELL_MIN, min(DWELL_MAX, DWELL_BASE * (h / DWELL_ANCHOR)))

def is_rollover(t):
    m = t.hour * 60 + t.minute
    return m < ROLLOVER_LOCKOUT_MIN or (1440 - m) < ROLLOVER_LOCKOUT_MIN

def load_pair(p):
    d = Path(__file__).resolve().parent.parent / "data" / "historical"
    a = pd.read_csv(d / p.file_a, parse_dates=['time']).rename(columns={'close':'close_a'})
    b = pd.read_csv(d / p.file_b, parse_dates=['time']).rename(columns={'close':'close_b'})
    m = pd.merge(a[['time','close_a']], b[['time','close_b']], on='time', how='inner').sort_values('time').reset_index(drop=True)
    return m[(m['close_a']>0)&(m['close_b']>0)].reset_index(drop=True)


def run_single_pair(df, pdef):
    """Run full simulation with per-pair adaptive HMM hold."""
    balance = STARTING_BALANCE; peak = STARTING_BALANCE; daily_start = STARTING_BALANCE
    daily_date = None; ghost = False; ghost_info = ""
    dakad = DynamicAKAD()
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec = 0; gcool = 0
    trades = []; hmm_blocks = 0; rollover_blocks = 0; dwell_blocks = 0; sentinel_exits = 0
    n = len(df)

    eng = shf_core.CointegrationEngine(span=WELFORD_SPAN, beta=1.0, entry_z=Z_BASE,
        exit_z=EXIT_Z_BASE, z_base=Z_BASE, gamma=GAMMA, hurst_window=HURST_WINDOW,
        dynamic_z=True, exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
    hmm = HMMRegimeDetector(lookback=HMM_LOOKBACK)

    pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0; erisk = 0.0
    lspread = 0.0; pspread = 0.0; sent_abort = False
    last_close_bar = -9999; last_close_h = 0.5
    hurst_sum = 0.0; hurst_count = 0
    
    # Per-pair adaptive tracking
    spread_history = []
    hold_history = []
    theta_history = []
    hurst_at_refit = []
    current_hold = 100  # default until first calibration
    last_refit_bar = -ADAPT_REFIT_INTERVAL

    for bar in range(n):
        if ghost: break
        row = df.iloc[bar]; bt = row['time']
        pa = float(row['close_a']); pb = float(row['close_b'])

        cd = bt.date() if hasattr(bt,'date') else None
        if cd and cd != daily_date: daily_date = cd; daily_start = balance

        cdd = max(0,(peak-balance)/peak) if peak>0 else 0
        ddd = max(0,(daily_start-balance)/daily_start) if daily_start>0 else 0

        if ddd >= GHOST_DAILY_DD:
            ghost = True; ghost_info = f"Daily DD {ddd*100:.2f}% at bar {bar}"; break
        if cdd >= GHOST_MAX_DD:
            ghost = True; ghost_info = f"Max DD {cdd*100:.2f}% at bar {bar}"; break
        if bar < gcool: continue

        pspread = lspread
        sig = eng.update(pa, pb)
        z = sig.z_score; s = sig.signal; spread = sig.spread; lspread = spread
        h = eng.last_hurst; exz = eng.last_exit_z

        spread_history.append(spread)
        if len(spread_history) > HURST_WINDOW + 500:
            spread_history = spread_history[-(HURST_WINDOW + 256):]

        if bar > MIN_WARMUP_BARS:
            hurst_sum += h; hurst_count += 1

        # PER-PAIR ADAPTIVE HMM HOLD CALIBRATION
        if bar - last_refit_bar >= ADAPT_REFIT_INTERVAL and len(spread_history) >= 200:
            try:
                ou_result = shf_core.fit_robust_ou_process(spread_history[-HURST_WINDOW:], 1.0/60.0)
                theta = ou_result.theta
                new_hold = adaptive_hold(h, theta)
                current_hold = new_hold
                hmm.set_hold(new_hold)
                last_refit_bar = bar
                hold_history.append(new_hold)
                theta_history.append(theta)
                hurst_at_refit.append(h)
            except Exception:
                pass

        pidx = min(pdef.pair_index, 2)
        if pspread != 0.0: corr.push_return(pidx, spread - pspread)

        la = math.log(pa) if pa>0 else 0; lb = math.log(pb) if pb>0 else 0
        beta, abort = sen.update(la, lb)
        if abort and not sent_abort:
            sent_abort = True
            if pos != 0:
                pnl = (spread-es)*pos*elots*pdef.notional; balance += pnl; peak = max(peak,balance)
                w = pnl>0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                month_key = bt.strftime('%Y-%m') if hasattr(bt,'strftime') else 'Unknown'
                trades.append({'pnl':pnl,'reason':'SENTINEL','bar':bar,'time':bt,'month':month_key})
                sentinel_exits += 1
                pos=0; last_close_bar=bar; last_close_h=h
            continue
        if sent_abort and not abort: sent_abort = False
        if sent_abort: continue

        hblocked = False
        if pspread != 0.0:
            hmm.update(spread - pspread); hblocked = hmm.is_blocked

        if bar < MIN_WARMUP_BARS: continue

        # ENTRY
        if pos == 0 and s != 0:
            if hblocked: hmm_blocks += 1; continue
            if is_rollover(bt): rollover_blocks += 1; continue
            if last_close_bar >= 0:
                cb = calc_dwell(last_close_h) / 60.0
                if (bar - last_close_bar) < cb: dwell_blocks += 1; continue

            risk = dakad.calc(cdd, ddd)
            corr.compute_risk(); cm = corr.last_risk_multiplier
            final_risk = risk * cm
            lots = max(0.01, round(balance * final_risk / 1000.0, 2))
            pos = s; ez = z; es = spread; ebar = bar; elots = lots; erisk = final_risk

        # EXIT
        elif pos != 0:
            ex = False; reason = ""
            if abs(z) > abs(ez) * 2.5: ex = True; reason = "EMERGENCY"
            if not ex:
                hb = bar - ebar; db = calc_dwell(h) / 60.0
                if hb < db: continue
                if pos == 1 and z > -exz: ex = True; reason = "DYNAMIC_EXIT"
                elif pos == -1 and z < exz: ex = True; reason = "DYNAMIC_EXIT"
            if ex:
                pnl = (spread-es)*pos*elots*pdef.notional; balance += pnl; peak = max(peak,balance)
                w = pnl>0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                month_key = bt.strftime('%Y-%m') if hasattr(bt,'strftime') else 'Unknown'
                trades.append({'pnl':pnl,'reason':reason,'bar':bar,'hold':bar-ebar,'time':bt,'month':month_key})
                pos=0; last_close_bar=bar; last_close_h=h

    # Results
    total = len(trades)
    if total == 0:
        return {'trades':0,'wr':0,'pf':0,'net_pnl':0,'return_pct':0,'max_dd_pct':0,
                'avg_hurst':0,'hmm_blocks':hmm_blocks,'ghost':ghost,'ghost_info':ghost_info,
                'avg_hold':0,'monthly':{},'avg_hmm_hold':0}

    pnls = [t['pnl'] for t in trades]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    gp = sum(wins) if wins else 0; gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins)/total*100; pf = gp/gl if gl>0 else 0

    eq_peak = STARTING_BALANCE; mdd = 0; eq = STARTING_BALANCE
    for t in trades:
        eq += t['pnl']; eq_peak = max(eq_peak, eq); mdd = max(mdd, eq_peak - eq)

    holds = [t.get('hold', 0) for t in trades if 'hold' in t]
    avg_hold_trade = np.mean(holds) if holds else 0

    monthly_data = {}
    for t in trades:
        mk = t.get('month', 'Unknown')
        if mk not in monthly_data: monthly_data[mk] = {'trades':0,'wins':0,'pnl':0.0}
        monthly_data[mk]['trades'] += 1
        if t['pnl'] > 0: monthly_data[mk]['wins'] += 1
        monthly_data[mk]['pnl'] += t['pnl']

    first_time = df['time'].iloc[0]; last_time = df['time'].iloc[-1]
    days = (last_time - first_time).days
    months_span = days / 30.0
    trades_per_month = total / months_span if months_span > 0 else 0
    
    avg_hmm_hold = np.mean(hold_history) if hold_history else 100
    min_hold = min(hold_history) if hold_history else 100
    max_hold = max(hold_history) if hold_history else 100
    avg_theta = np.mean(theta_history) if theta_history else 0
    avg_h_refit = np.mean(hurst_at_refit) if hurst_at_refit else 0

    return {
        'trades': total, 'wr': round(wr,1), 'pf': round(pf,2),
        'net_pnl': round(balance-STARTING_BALANCE,2),
        'return_pct': round((balance-STARTING_BALANCE)/STARTING_BALANCE*100,2),
        'max_dd_pct': round(mdd/STARTING_BALANCE*100,2),
        'avg_hurst': round(hurst_sum/max(hurst_count,1),3),
        'hmm_blocks': hmm_blocks, 'ghost': ghost, 'ghost_info': ghost_info,
        'avg_win': round(np.mean(wins),2) if wins else 0,
        'avg_loss': round(np.mean(losses),2) if losses else 0,
        'avg_hold_bars': round(avg_hold_trade,1),
        'gross_profit': round(gp,2), 'gross_loss': round(gl,2),
        'monthly': monthly_data, 'trades_per_month': round(trades_per_month,1),
        'days': days, 'dakad_wr': round(dakad.current_wr*100,1),
        'avg_hmm_hold': round(avg_hmm_hold,1),
        'min_hmm_hold': min_hold, 'max_hmm_hold': max_hold,
        'avg_theta': round(avg_theta,4),
        'avg_h_at_refit': round(avg_h_refit,3),
        'refits': len(hold_history),
        'rollover_blocks': rollover_blocks, 'dwell_blocks': dwell_blocks,
        'sentinel_exits': sentinel_exits,
    }


def main():
    print("="*120)
    print("SHF v5.6 — PER-PAIR ADAPTIVE HMM HOLD BACKTEST (Sigmoid + OU Method)")
    print(f"shf_core version: {shf_core.__version__}")
    print("="*120)
    print(f"  Formula: hold = 5 + 95 * sigmoid(-{ADAPT_K} * (H - {ADAPT_H_MIDPOINT}))")
    print(f"  + theta discount: up to {ADAPT_THETA_MAX_DISCOUNT*100:.0f}% reduction if theta > {ADAPT_THETA_THRESHOLD}")
    print(f"  Re-fit interval: {ADAPT_REFIT_INTERVAL} bars")
    print()
    
    # Show the calibration curve
    print("  CALIBRATION CURVE:")
    print(f"    {'H':>6} {'θ=0.5':>8} {'θ=1.5':>8} {'θ=5.0':>8} {'θ=10.0':>8}")
    for h_val in [0.35, 0.40, 0.45, 0.50, 0.52, 0.53, 0.54, 0.55, 0.56, 0.57, 0.58, 0.60, 0.65]:
        holds = [adaptive_hold(h_val, t) for t in [0.5, 1.5, 5.0, 10.0]]
        marker = " ◄" if abs(h_val - ADAPT_H_MIDPOINT) < 0.005 else ""
        print(f"    {h_val:>6.2f} {holds[0]:>8} {holds[1]:>8} {holds[2]:>8} {holds[3]:>8}{marker}")
    print()

    # Load previous results
    prev_path = Path("Results/faithful_live_backtest_results.json")
    prev_100 = {}  # Static HMM=100 results
    prev_best = {}  # Best static results
    if prev_path.exists():
        with open(prev_path) as f:
            prev_data = json.load(f)
        for bp in prev_data.get('best_pairs', []):
            prev_best[bp['pair']] = bp
        # Also get HMM=100 specifically
        for pair_name, results_list in prev_data.get('all_results', {}).items():
            for r in results_list:
                if r.get('hmm_hold') == 100:
                    prev_100[pair_name] = r
                    break

    pair_data = {}
    for p in ALL_PAIRS:
        try:
            df = load_pair(p)
            pair_data[p.name] = (df, p)
            first = df['time'].iloc[0]; last = df['time'].iloc[-1]
            days = (last - first).days
            print(f"  {p.name:<20} {len(df):>8,} bars | {first} to {last} ({days}d)")
        except Exception as e:
            print(f"  {p.name:<20} FAILED: {e}")

    results = {}
    for pair_name, (df, pdef) in pair_data.items():
        t0 = time.time()
        r = run_single_pair(df, pdef)
        elapsed = time.time() - t0
        results[pair_name] = r
        ghost_str = f" GHOST: {r['ghost_info']}" if r['ghost'] else ""
        print(f"\n  {pair_name:<20} {r['trades']:>5} trades ({r['trades_per_month']:.0f}/mo)  "
              f"WR={r['wr']:>5.1f}%  PF={r['pf']:>6.2f}  "
              f"P&L=${r['net_pnl']:>12,.2f}  Return={r['return_pct']:>7.2f}%  "
              f"MaxDD={r['max_dd_pct']:>5.2f}%  "
              f"H={r['avg_hurst']:.3f} θ={r['avg_theta']:.2f}  "
              f"HMMhold=[{r['min_hmm_hold']}-{r['max_hmm_hold']}] avg={r['avg_hmm_hold']:.0f}  "
              f"({elapsed:.1f}s){ghost_str}")

    # =========================================================================
    # COMPARISON: Per-Pair Adaptive vs Static HMM=100 vs Best Static
    # =========================================================================
    print(f"\n\n{'='*140}")
    print("COMPARISON: PER-PAIR ADAPTIVE vs STATIC HMM=100 vs BEST STATIC")
    print(f"{'='*140}")
    print(f"\n  {'Pair':<20} {'-- PER-PAIR ADAPTIVE --':^42} {'-- STATIC HMM=100 --':^38} {'-- BEST STATIC --':^38} {'':>8}")
    print(f"  {'':20} {'Hold':>5} {'Trd':>5} {'WR':>6} {'PF':>7} {'Ret':>7} {'DD':>6}  "
          f"{'Trd':>5} {'WR':>6} {'PF':>7} {'Ret':>7} {'DD':>6}  "
          f"{'HMM':>3} {'Trd':>5} {'WR':>6} {'PF':>7} {'Ret':>7} {'DD':>6} {'Winner':>8}")
    print(f"  {'-'*138}")

    for pair_name, r in results.items():
        s100 = prev_100.get(pair_name, {})
        sbest = prev_best.get(pair_name, {})
        
        s_trd = s100.get('trades', 0); s_wr = s100.get('wr', 0)
        s_pf = s100.get('pf', 0); s_ret = s100.get('return_pct', 0); s_dd = s100.get('max_dd_pct', 0)
        
        b_hmm = sbest.get('best_hmm', '?'); b_trd = sbest.get('trades', 0); b_wr = sbest.get('wr', 0)
        b_pf = sbest.get('pf', 0); b_ret = sbest.get('return_pct', 0); b_dd = sbest.get('max_dd_pct', 0)
        
        # Winner by PF (if comparable trade count)
        pfs = [('ADAPT', r['pf']), ('S100', s_pf), ('BEST', b_pf)]
        pfs.sort(key=lambda x: x[1], reverse=True)
        winner = pfs[0][0]
        
        print(f"  {pair_name:<20} "
              f"{r['avg_hmm_hold']:>5.0f} {r['trades']:>5} {r['wr']:>5.1f}% {r['pf']:>7.2f} {r['return_pct']:>6.2f}% {r['max_dd_pct']:>5.2f}%  "
              f"{s_trd:>5} {s_wr:>5.1f}% {s_pf:>7.2f} {s_ret:>6.2f}% {s_dd:>5.2f}%  "
              f"{str(b_hmm):>3} {b_trd:>5} {b_wr:>5.1f}% {b_pf:>7.2f} {b_ret:>6.2f}% {b_dd:>5.2f}% {winner:>8}")

    # =========================================================================
    # 3-PAIR PORTFOLIO
    # =========================================================================
    core_pairs = ["Forex Anchor", "EURJPY/CHFJPY", "Index Spread"]
    print(f"\n\n{'='*110}")
    print("3-PAIR PORTFOLIO COMPARISON (Forex Anchor + EURJPY/CHFJPY + Index Spread)")
    print(f"{'='*110}")
    
    # Per-pair adaptive
    adapt_trades = 0; adapt_pnl = 0; adapt_gp = 0; adapt_gl = 0; adapt_wins = 0
    for cp in core_pairs:
        r = results.get(cp, {})
        adapt_trades += r.get('trades', 0)
        adapt_pnl += r.get('net_pnl', 0)
        adapt_gp += r.get('gross_profit', 0)
        adapt_gl += r.get('gross_loss', 0)
        adapt_wins += int(r.get('trades', 0) * r.get('wr', 0) / 100)
    adapt_wr = adapt_wins / adapt_trades * 100 if adapt_trades > 0 else 0
    adapt_pf = adapt_gp / adapt_gl if adapt_gl > 0 else 0
    
    # Static 100
    s100_trades = 0; s100_pnl = 0; s100_gp = 0; s100_gl = 0; s100_wins = 0
    for cp in core_pairs:
        r = prev_100.get(cp, {})
        s100_trades += r.get('trades', 0)
        s100_pnl += r.get('net_pnl', 0)
        s100_gp += r.get('gross_profit', 0)
        s100_gl += r.get('gross_loss', 0)
        s100_wins += int(r.get('trades', 0) * r.get('wr', 0) / 100)
    s100_wr = s100_wins / s100_trades * 100 if s100_trades > 0 else 0
    s100_pf = s100_gp / s100_gl if s100_gl > 0 else 0
    
    print(f"\n  {'Per-Pair Adaptive':25} {adapt_trades:>5} trades  WR={adapt_wr:.1f}%  PF={adapt_pf:.2f}  "
          f"P&L=${adapt_pnl:>12,.2f}  Return={adapt_pnl/STARTING_BALANCE*100:.2f}%")
    print(f"  {'Static HMM=100':25} {s100_trades:>5} trades  WR={s100_wr:.1f}%  PF={s100_pf:.2f}  "
          f"P&L=${s100_pnl:>12,.2f}  Return={s100_pnl/STARTING_BALANCE*100:.2f}%")
    
    diff = adapt_pnl - s100_pnl
    print(f"\n  DIFFERENCE: ${diff:>+12,.2f} ({diff/STARTING_BALANCE*100:+.2f}%) in favor of "
          f"{'Per-Pair Adaptive' if diff > 0 else 'Static HMM=100'}")

    # =========================================================================
    # 6-PAIR PORTFOLIO
    # =========================================================================
    print(f"\n\n{'='*110}")
    print("6-PAIR PORTFOLIO (ALL PAIRS)")
    print(f"{'='*110}")
    all_adapt_trades = sum(r.get('trades',0) for r in results.values())
    all_adapt_pnl = sum(r.get('net_pnl',0) for r in results.values())
    all_adapt_gp = sum(r.get('gross_profit',0) for r in results.values())
    all_adapt_gl = sum(r.get('gross_loss',0) for r in results.values())
    all_adapt_pf = all_adapt_gp / all_adapt_gl if all_adapt_gl > 0 else 0
    
    all_s100_pnl = sum(r.get('net_pnl',0) for r in prev_100.values())
    
    print(f"\n  Per-Pair Adaptive:  {all_adapt_trades} trades  PF={all_adapt_pf:.2f}  P&L=${all_adapt_pnl:>12,.2f}")
    print(f"  Static HMM=100:    P&L=${all_s100_pnl:>12,.2f}")

    # =========================================================================
    # PER-PAIR CALIBRATION DIAGNOSTICS
    # =========================================================================
    print(f"\n\n{'='*110}")
    print("PER-PAIR CALIBRATION DIAGNOSTICS")
    print(f"{'='*110}")
    for pair_name, r in results.items():
        sbest = prev_best.get(pair_name, {})
        ideal = sbest.get('best_hmm', '?')
        print(f"\n  {pair_name}:")
        print(f"    Avg Hurst:          {r['avg_hurst']:.3f}")
        print(f"    Avg OU θ:           {r['avg_theta']:.4f}")
        print(f"    Adaptive hold:      [{r['min_hmm_hold']} - {r['max_hmm_hold']}] avg={r['avg_hmm_hold']:.1f}")
        print(f"    Ideal static hold:  {ideal}")
        match = "✓ CLOSE" if abs(r['avg_hmm_hold'] - float(ideal if str(ideal).isdigit() else 100)) < 20 else "✗ OFF"
        print(f"    Calibration:        {match}")
        print(f"    Refits:             {r['refits']}")

    # =========================================================================
    # MONTHLY BREAKDOWN
    # =========================================================================
    print(f"\n\n{'='*110}")
    print("MONTHLY BREAKDOWN (Per-Pair Adaptive HMM)")
    print(f"{'='*110}")
    for pair_name, r in results.items():
        monthly = r.get('monthly', {})
        if not monthly: continue
        print(f"\n  {pair_name} (hold={r['avg_hmm_hold']:.0f}, H={r['avg_hurst']:.3f}, θ={r['avg_theta']:.2f}):")
        print(f"    {'Month':<10} {'Trades':>7} {'WR':>7} {'P&L':>12}")
        print(f"    {'-'*40}")
        for month in sorted(monthly.keys()):
            md = monthly[month]
            mwr = md['wins']/md['trades']*100 if md['trades']>0 else 0
            print(f"    {month:<10} {md['trades']:>7} {mwr:>6.1f}% ${md['pnl']:>11,.2f}")

    # Save
    out = Path("Results/perpair_adaptive_hmm_results.json")
    def conv(o):
        if isinstance(o, (np.integer,)): return int(o)
        if isinstance(o, (np.floating,)): return float(o)
        if isinstance(o, (pd.Timestamp, datetime)): return str(o)
        return str(o)
    save_data = {
        'method': 'perpair_adaptive_sigmoid',
        'formula': f'hold = 5 + 95 * sigmoid(-{ADAPT_K} * (H - {ADAPT_H_MIDPOINT})) + theta_discount',
        'parameters': {
            'k': ADAPT_K, 'h_midpoint': ADAPT_H_MIDPOINT,
            'theta_threshold': ADAPT_THETA_THRESHOLD,
            'theta_max_discount': ADAPT_THETA_MAX_DISCOUNT,
            'refit_interval': ADAPT_REFIT_INTERVAL,
        },
        'results': {k: v for k,v in results.items()},
    }
    with open(out, 'w') as f:
        json.dump(save_data, f, indent=2, default=conv)
    print(f"\n  Saved to {out}")


if __name__ == "__main__":
    main()
