#!/usr/bin/env python3
"""
SHF v5.6 — FAITHFUL LIVE BOT BACKTEST (Exact Live Config)
================================================================
Tests the EXACT 3 pairs running on the live bot with per-pair HMM holds.
No sweep, no extra pairs — this IS the live bot in backtest form.

Holy Trio (live config):
  1. US100/DE40      (Index Spread)   — HMM hold=10  (H=0.585, trending)
  2. AUDUSD/NZDUSD   (Forex Anchor)   — HMM hold=100 (H=0.512, MR)
  3. EURJPY/CHFJPY   (EURJPY/CHFJPY)  — HMM hold=100 (H=0.528, MR)

INCLUDES ALL LIVE BOT FEATURES:
  Dynamic AKAD, HMM Filter, Dynamic Dwell, Rollover Lockout,
  Kalman Sentinel, Correlation Monitor, Ghost Stop, Emergency Exit,
  Consecutive Loss Cooldown, 200-bar warmup.
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

# ============================================================================
# HOLY TRIO — EXACT MATCH TO engine.py HOLY_TRIO config
# Per-pair HMM hold calibrated from Hurst exponent (physics-based):
#   Index (H=0.585, trending): hmm_min_hold=10
#   Forex (H=0.512, MR):      hmm_min_hold=100
#   EURJPY (H=0.528, MR):     hmm_min_hold=100
# ============================================================================

@dataclass
class PairDef:
    name: str; sym_a: str; sym_b: str; file_a: str; file_b: str
    pair_index: int; hmm_hold: int; notional: float = 100_000.0

HOLY_TRIO = [
    PairDef("Index Spread",    "US100","DE40",    "US100_M1.csv","DE40_M1.csv",     0, hmm_hold=10,  notional=150_000.0),
    PairDef("Forex Anchor",    "AUDUSD","NZDUSD", "AUDUSD_M1.csv","NZDUSD_M1.csv",  1, hmm_hold=100, notional=100_000.0),
    PairDef("EURJPY/CHFJPY",   "EURJPY","CHFJPY", "EURJPY_M1.csv","CHFJPY_M1.csv",  2, hmm_hold=100, notional=100_000.0),
]

# ============================================================================
# COMPONENTS (exact copies from engine.py / src/)
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
    def __init__(self, lookback=100, min_regime_hold=100):
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

# ============================================================================
# SINGLE PAIR SIMULATION (faithful to engine.py)
# ============================================================================

def run_single_pair(df, pdef):
    """Run full live-bot simulation on a single pair with its fixed HMM hold."""
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
    hmm = HMMRegimeDetector(lookback=HMM_LOOKBACK, min_regime_hold=pdef.hmm_hold)

    pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0; erisk = 0.0
    lspread = 0.0; pspread = 0.0; sent_abort = False
    last_close_bar = -9999; last_close_h = 0.5
    hurst_sum = 0.0; hurst_count = 0
    equity = [STARTING_BALANCE]

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

        if bar > MIN_WARMUP_BARS:
            hurst_sum += h; hurst_count += 1

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

        if bar % 5000 == 0: equity.append(balance)
    equity.append(balance)

    # Compute results
    total = len(trades)
    if total == 0:
        return {'trades':0,'wr':0,'pf':0,'net_pnl':0,'return_pct':0,'max_dd_pct':0,
                'avg_hurst':0,'hmm_blocks':hmm_blocks,'ghost':ghost,'ghost_info':ghost_info,
                'avg_win':0,'avg_loss':0,'avg_hold':0,'monthly':{},'rollover_blocks':rollover_blocks,
                'dwell_blocks':dwell_blocks,'sentinel_exits':0,'exit_reasons':{},'trades_per_month':0}

    pnls = [t['pnl'] for t in trades]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    gp = sum(wins) if wins else 0; gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins)/total*100; pf = gp/gl if gl>0 else 0

    # Max DD from equity
    eq_peak = STARTING_BALANCE; mdd = 0
    eq = STARTING_BALANCE
    for t in trades:
        eq += t['pnl']; eq_peak = max(eq_peak, eq); mdd = max(mdd, eq_peak - eq)

    # Avg hold
    holds = [t.get('hold', 0) for t in trades if 'hold' in t]
    avg_hold = np.mean(holds) if holds else 0

    # Exit reasons
    exit_reasons = {}
    for t in trades:
        r = t['reason']; exit_reasons[r] = exit_reasons.get(r, 0) + 1

    # Monthly
    monthly_data = {}
    for t in trades:
        mk = t.get('month', 'Unknown')
        if mk not in monthly_data: monthly_data[mk] = {'trades':0,'wins':0,'pnl':0.0}
        monthly_data[mk]['trades'] += 1
        if t['pnl'] > 0: monthly_data[mk]['wins'] += 1
        monthly_data[mk]['pnl'] += t['pnl']

    # Data span in days
    first_time = df['time'].iloc[0]; last_time = df['time'].iloc[-1]
    days = (last_time - first_time).days
    months_span = days / 30.0
    trades_per_month = total / months_span if months_span > 0 else 0

    return {
        'pair': pdef.name, 'hmm_hold': pdef.hmm_hold,
        'trades': total, 'wr': round(wr,1), 'pf': round(pf,2),
        'net_pnl': round(balance-STARTING_BALANCE,2),
        'return_pct': round((balance-STARTING_BALANCE)/STARTING_BALANCE*100,2),
        'max_dd_pct': round(mdd/STARTING_BALANCE*100,2),
        'avg_hurst': round(hurst_sum/max(hurst_count,1),3),
        'hmm_blocks': hmm_blocks, 'ghost': ghost, 'ghost_info': ghost_info,
        'avg_win': round(np.mean(wins),2) if wins else 0,
        'avg_loss': round(np.mean(losses),2) if losses else 0,
        'avg_hold': round(avg_hold,1),
        'gross_profit': round(gp,2), 'gross_loss': round(gl,2),
        'exit_reasons': exit_reasons, 'sentinel_exits': sentinel_exits,
        'rollover_blocks': rollover_blocks, 'dwell_blocks': dwell_blocks,
        'monthly': monthly_data, 'trades_per_month': round(trades_per_month,1),
        'days': days, 'dakad_wr': round(dakad.current_wr*100,1),
    }


def main():
    print("="*110)
    print("SHF v5.6 — FAITHFUL LIVE BOT BACKTEST (Exact Live Config)")
    print(f"shf_core version: {shf_core.__version__}")
    print("="*110)
    print(f"  All features active: Dynamic AKAD, HMM, Dwell, Rollover, Sentinel, Corr, Ghost Stop")
    print(f"  Starting balance: ${STARTING_BALANCE:,.0f}")
    print(f"\n  Holy Trio (live config):")
    for p in HOLY_TRIO:
        print(f"    {p.name:<20} HMM hold={p.hmm_hold:<4} notional=${p.notional:,.0f}")

    # Load pair data
    pair_data = {}
    for p in HOLY_TRIO:
        try:
            df = load_pair(p)
            pair_data[p.name] = (df, p)
            first = df['time'].iloc[0]; last = df['time'].iloc[-1]
            days = (last - first).days
            print(f"  {p.name:<20} {len(df):>8,} bars | {first} to {last} ({days}d)")
        except Exception as e:
            print(f"  {p.name:<20} FAILED: {e}")

    # =========================================================================
    # RUN EACH PAIR WITH ITS FIXED LIVE HMM HOLD
    # =========================================================================
    results = {}
    combined_pnl = 0.0
    combined_trades = 0

    for pair_name, (df, pdef) in pair_data.items():
        print(f"\n{'='*80}")
        print(f"  {pair_name} ({pdef.sym_a}/{pdef.sym_b}) — HMM hold={pdef.hmm_hold}")
        print(f"{'='*80}")

        t0 = time.time()
        r = run_single_pair(df, pdef)
        elapsed = time.time() - t0

        results[pair_name] = r
        combined_pnl += r['net_pnl']
        combined_trades += r['trades']

        ghost_str = f" GHOST: {r['ghost_info']}" if r['ghost'] else ""
        print(f"    {r['trades']:>5} trades ({r['trades_per_month']:.0f}/mo)  "
              f"WR={r['wr']:>5.1f}%  PF={r['pf']:>6.2f}  "
              f"P&L=${r['net_pnl']:>12,.2f}  Return={r['return_pct']:>7.2f}%  "
              f"MaxDD={r['max_dd_pct']:>5.2f}%  H={r['avg_hurst']:.3f}  "
              f"({elapsed:.1f}s){ghost_str}")

    # =========================================================================
    # HOLY TRIO SUMMARY
    # =========================================================================
    print(f"\n\n{'='*110}")
    print("HOLY TRIO — LIVE BOT CONFIG RESULTS")
    print(f"{'='*110}")
    print(f"\n  {'Pair':<20} {'HMM':>4} {'Trades':>7} {'Tr/Mo':>6} {'WR':>7} {'PF':>7} {'P&L':>13} {'Return':>8} {'MaxDD':>7} {'AvgWin':>9} {'AvgLoss':>9} {'Hurst':>6}")
    print(f"  {'-'*115}")

    for pair_name, r in results.items():
        print(f"  {pair_name:<20} {r['hmm_hold']:>4} {r['trades']:>7} {r['trades_per_month']:>5.0f} "
              f"{r['wr']:>6.1f}% {r['pf']:>7.2f} ${r['net_pnl']:>12,.2f} {r['return_pct']:>7.2f}% {r['max_dd_pct']:>6.2f}% "
              f"${r['avg_win']:>8.2f} ${r['avg_loss']:>8.2f} {r['avg_hurst']:>6.3f}")

    print(f"  {'-'*115}")
    print(f"  {'COMBINED':<20} {'':>4} {combined_trades:>7} {'':>6} {'':>7} {'':>7} ${combined_pnl:>12,.2f} "
          f"{combined_pnl/STARTING_BALANCE*100:>7.2f}%")

    # =========================================================================
    # MONTHLY BREAKDOWN PER PAIR
    # =========================================================================
    print(f"\n\n{'='*110}")
    print("MONTHLY BREAKDOWN")
    print(f"{'='*110}")
    for pair_name, r in results.items():
        monthly = r.get('monthly', {})
        if not monthly: continue
        print(f"\n  {pair_name} (HMM={r['hmm_hold']}):")
        print(f"    {'Month':<10} {'Trades':>7} {'WR':>7} {'P&L':>12}")
        print(f"    {'-'*40}")
        for month in sorted(monthly.keys()):
            md = monthly[month]
            mwr = md['wins']/md['trades']*100 if md['trades']>0 else 0
            print(f"    {month:<10} {md['trades']:>7} {mwr:>6.1f}% ${md['pnl']:>11,.2f}")

    # Combined monthly
    print(f"\n  COMBINED MONTHLY:")
    print(f"    {'Month':<10} {'Trades':>7} {'P&L':>12}")
    print(f"    {'-'*35}")
    all_months = {}
    for r in results.values():
        for mk, md in r.get('monthly', {}).items():
            if mk not in all_months: all_months[mk] = {'trades':0,'pnl':0.0}
            all_months[mk]['trades'] += md['trades']
            all_months[mk]['pnl'] += md['pnl']
    for month in sorted(all_months.keys()):
        md = all_months[month]
        print(f"    {month:<10} {md['trades']:>7} ${md['pnl']:>11,.2f}")

    # =========================================================================
    # EXIT REASON BREAKDOWN
    # =========================================================================
    print(f"\n\n{'='*110}")
    print("EXIT REASON BREAKDOWN")
    print(f"{'='*110}")
    for pair_name, r in results.items():
        er = r.get('exit_reasons', {})
        if not er: continue
        print(f"\n  {pair_name} (HMM={r['hmm_hold']}):")
        for reason, count in sorted(er.items(), key=lambda x: -x[1]):
            pct = count / r['trades'] * 100 if r['trades'] > 0 else 0
            print(f"    {reason:<25} {count:>6} ({pct:>5.1f}%)")

    # =========================================================================
    # FILTER STATS
    # =========================================================================
    print(f"\n\n{'='*110}")
    print("FILTER STATISTICS")
    print(f"{'='*110}")
    print(f"\n  {'Pair':<20} {'HMM Blocks':>11} {'Rollover':>10} {'Dwell':>8} {'Sentinel':>10} {'DAKAD WR':>9}")
    print(f"  {'-'*75}")
    for pair_name, r in results.items():
        print(f"  {pair_name:<20} {r['hmm_blocks']:>11} {r['rollover_blocks']:>10} "
              f"{r['dwell_blocks']:>8} {r['sentinel_exits']:>10} {r['dakad_wr']:>8.1f}%")

    # Save
    out = Path("Results/faithful_live_backtest_results.json")
    def conv(o):
        if isinstance(o, (np.integer,)): return int(o)
        if isinstance(o, (np.floating,)): return float(o)
        if isinstance(o, (pd.Timestamp, datetime)): return str(o)
        return str(o)
    save_data = {
        'config': 'Exact live bot config — Holy Trio with per-pair HMM holds',
        'pairs': {p.name: {'hmm_hold': p.hmm_hold, 'sym_a': p.sym_a, 'sym_b': p.sym_b} for p in HOLY_TRIO},
        'results': results,
        'combined_pnl': round(combined_pnl, 2),
        'combined_trades': combined_trades,
        'combined_return_pct': round(combined_pnl / STARTING_BALANCE * 100, 2),
    }
    with open(out, 'w') as f:
        json.dump(save_data, f, indent=2, default=conv)
    print(f"\n  Saved to {out}")


if __name__ == "__main__":
    main()
