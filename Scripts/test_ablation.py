#!/usr/bin/env python3
"""
SHF v5.6 — ABLATION TEST: Which live-bot feature is killing performance?
=========================================================================

Original v5.6 on same data: 1,040 trades, 79.0% WR, 2.30 PF
Current live bot on same data: 286 trades, 74.8% WR, 1.66 PF

This script runs 5 configurations to identify the culprit:
  A) BASELINE: Original v5.6 (no HMM, no DynAKAD, no Rollover, no Dwell)
  B) + HMM only
  C) + Rollover Lockout only
  D) + Dynamic AKAD only (lot sizing change, shouldn't affect trade count)
  E) ALL FEATURES (current live bot)
"""

import sys, math, time, json
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from collections import deque
from dataclasses import dataclass
from typing import List, Dict, Optional

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shf_core

# ---- Parameters (same as engine.py) ----
WELFORD_SPAN = 100; Z_BASE = 2.0; GAMMA = 6.0; HURST_WINDOW = 512
EXIT_Z_BASE = 0.5; EXIT_GAMMA = 2.0
AKAD_BASE_RISK = 0.0075; AKAD_DD_LAMBDA = 40.0
GHOST_DAILY_DD = 0.04; GHOST_MAX_DD = 0.09
KALMAN_TOLERANCE = 0.15; CORR_WINDOW = 200
DWELL_BASE = 60.0; DWELL_ANCHOR = 0.3; DWELL_MIN = 30.0; DWELL_MAX = 300.0
ROLLOVER_LOCKOUT_MIN = 5
STARTING_BALANCE = 100_000.0

@dataclass
class PairDef:
    name: str; sym_a: str; sym_b: str; file_a: str; file_b: str
    pair_index: int; notional: float = 100_000.0

HOLY_TRIO = [
    PairDef("Index Spread","US100","DE40","US100_M1.csv","DE40_M1.csv",0,150_000.0),
    PairDef("Forex Anchor","AUDUSD","NZDUSD","AUDUSD_M1.csv","NZDUSD_M1.csv",1,100_000.0),
    PairDef("EUR/GBP Spread","EURUSD","GBPUSD","EURUSD_M1.csv","GBPUSD_M1.csv",2,100_000.0),
]

# ---- HMM (exact copy) ----
class HMMRegimeDetector:
    def __init__(self, lookback=100):
        self._lookback = lookback; self._current_regime = 0
        self._return_buffer = []; self._regime_hold_count = 0; self._min_regime_hold = 100
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

# ---- Dynamic AKAD (exact copy) ----
class DynamicAKAD:
    def __init__(self):
        self._results = deque(maxlen=50)
        for _ in range(10): self._results.append(1)
        for _ in range(5): self._results.append(0)
    def record(self, win): self._results.append(1 if win else 0)
    def calc(self, tdd, ddd):
        ddr = max(0.001, 0.04 - ddd)
        wr = max(0.50, min(0.85, sum(self._results)/max(len(self._results),1)))
        ns = math.log(1e-4) / math.log(1-wr)
        base = max(0.003, min(0.03, (math.exp(40*ddr)-1)/(40*ns)))
        return max(0.0005, base * math.exp(-40*tdd))

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

# ---- Simulation with feature flags ----
def run_sim(pair_data, use_hmm=False, use_rollover=False, use_dyn_akad=False, use_dwell=False, label=""):
    balance = STARTING_BALANCE; peak = STARTING_BALANCE; daily_start = STARTING_BALANCE
    daily_date = None; ghost = False
    dakad = DynamicAKAD() if use_dyn_akad else None
    akad = shf_core.AKADRiskCalculator(base_risk=AKAD_BASE_RISK, dd_lambda=AKAD_DD_LAMBDA)
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec = 0; gcool = 0
    trades_list = []; hmm_blocks = 0; roll_blocks = 0; dwell_holds = 0; dwell_reentry_blocks = 0

    for pdef in HOLY_TRIO:
        if pdef.name not in pair_data: continue
        df = pair_data[pdef.name]; n = len(df)
        eng = shf_core.CointegrationEngine(span=WELFORD_SPAN, beta=1.0, entry_z=Z_BASE,
            exit_z=EXIT_Z_BASE, z_base=Z_BASE, gamma=GAMMA, hurst_window=HURST_WINDOW,
            dynamic_z=True, exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
        sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
        hmm = HMMRegimeDetector() if use_hmm else None
        pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0; eh = 0.5
        lspread = 0.0; pspread = 0.0; sent_abort = False
        last_close_bar = -9999; last_close_h = 0.5

        for bar in range(n):
            if ghost: break
            row = df.iloc[bar]; bt = row['time']; pa = float(row['close_a']); pb = float(row['close_b'])
            cd = bt.date() if hasattr(bt,'date') else None
            if cd and cd != daily_date: daily_date = cd; daily_start = balance
            cdd = max(0,(peak-balance)/peak) if peak>0 else 0
            ddd = max(0,(daily_start-balance)/daily_start) if daily_start>0 else 0
            if ddd >= GHOST_DAILY_DD: ghost = True; break
            if cdd >= GHOST_MAX_DD: ghost = True; break
            if bar < gcool: continue

            pspread = lspread
            sig = eng.update(pa, pb)
            z = sig.z_score; s = sig.signal; spread = sig.spread; lspread = spread
            h = eng.last_hurst; zc = eng.last_z_crit; exz = eng.last_exit_z

            if pspread != 0.0: corr.push_return(pdef.pair_index, spread - pspread)
            la = math.log(pa) if pa>0 else 0; lb = math.log(pb) if pb>0 else 0
            beta, abort = sen.update(la, lb)
            if abort and not sent_abort:
                sent_abort = True
                if pos != 0:
                    pnl = (spread-es)*pos*elots*pdef.notional; balance += pnl; peak = max(peak,balance)
                    w = pnl>0; akad.record_trade(0.49 if w else -1.0)
                    if dakad: dakad.record(w)
                    if not w: consec+=1
                    else: consec=0
                    if consec>=5: gcool=bar+60; consec=0
                    trades_list.append({'pair':pdef.name,'pnl':pnl,'reason':'SENTINEL'})
                    pos=0; last_close_bar=bar; last_close_h=h
                continue
            if sent_abort and not abort: sent_abort = False
            if sent_abort: continue

            # HMM
            hblocked = False
            if use_hmm and hmm and pspread != 0.0:
                hmm.update(spread - pspread); hblocked = hmm.is_blocked

            if bar < 200: continue

            # ENTRY
            if pos == 0 and s != 0:
                if use_hmm and hblocked: hmm_blocks += 1; continue
                if use_rollover and is_rollover(bt): roll_blocks += 1; continue
                if use_dwell and last_close_bar >= 0:
                    cb = calc_dwell(last_close_h) / 60.0
                    if (bar - last_close_bar) < cb: dwell_reentry_blocks += 1; continue

                if use_dyn_akad and dakad:
                    risk = dakad.calc(cdd, ddd)
                else:
                    risk, _, _, _ = akad.calculate_risk(cdd)
                corr.compute_risk(); cm = corr.last_risk_multiplier
                lots = max(0.01, round(balance * risk * cm / 1000.0, 2))
                pos = s; ez = z; es = spread; ebar = bar; elots = lots; eh = h

            # EXIT
            elif pos != 0:
                ex = False; reason = ""
                if abs(z) > abs(ez) * 2.5: ex = True; reason = "EMERGENCY"
                if not ex:
                    if use_dwell:
                        hb = bar - ebar; db = calc_dwell(h) / 60.0
                        if hb < db: dwell_holds += 1; continue
                    if pos == 1 and z > -exz: ex = True; reason = "DYNAMIC_EXIT"
                    elif pos == -1 and z < exz: ex = True; reason = "DYNAMIC_EXIT"
                if ex:
                    pnl = (spread-es)*pos*elots*pdef.notional; balance += pnl; peak = max(peak,balance)
                    w = pnl>0; akad.record_trade(0.49 if w else -1.0)
                    if dakad: dakad.record(w)
                    if not w: consec+=1
                    else: consec=0
                    if consec>=5: gcool=bar+60; consec=0
                    trades_list.append({'pair':pdef.name,'pnl':pnl,'reason':reason})
                    pos=0; last_close_bar=bar; last_close_h=h

    total = len(trades_list)
    pnls = [t['pnl'] for t in trades_list]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    gp = sum(wins) if wins else 0; gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins)/total*100 if total>0 else 0
    pf = gp/gl if gl>0 else 0

    # Per-pair
    pp = {}
    for pdef in HOLY_TRIO:
        pt = [t for t in trades_list if t['pair']==pdef.name]
        if not pt: pp[pdef.name] = {'trades':0,'wr':0,'pf':0}; continue
        ppnl = [t['pnl'] for t in pt]; pw = [p for p in ppnl if p>0]; pl = [p for p in ppnl if p<=0]
        pgp = sum(pw) if pw else 0; pgl = abs(sum(pl)) if pl else 0.001
        pp[pdef.name] = {'trades':len(pt),'wr':round(len(pw)/len(pt)*100,1),'pf':round(pgp/pgl,2),'pnl':round(sum(ppnl),2)}

    return {
        'label': label, 'trades': total, 'wr': round(wr,1), 'pf': round(pf,2),
        'net_pnl': round(balance-STARTING_BALANCE,2), 'ghost': ghost,
        'hmm_blocks': hmm_blocks, 'roll_blocks': roll_blocks,
        'dwell_holds': dwell_holds, 'dwell_reentry_blocks': dwell_reentry_blocks,
        'per_pair': pp,
    }


def main():
    print("="*90)
    print("SHF v5.6 — ABLATION TEST: Finding the Performance Killer")
    print("="*90)

    # Load data
    pair_data = {}
    for p in HOLY_TRIO:
        df = load_pair(p)
        pair_data[p.name] = df
        print(f"  {p.name}: {len(df)} bars")

    configs = [
        ("A) BASELINE (v5.6 original)",   dict(use_hmm=False, use_rollover=False, use_dyn_akad=False, use_dwell=False)),
        ("B) + HMM Filter ONLY",          dict(use_hmm=True,  use_rollover=False, use_dyn_akad=False, use_dwell=False)),
        ("C) + Rollover Lockout ONLY",     dict(use_hmm=False, use_rollover=True,  use_dyn_akad=False, use_dwell=False)),
        ("D) + Dynamic AKAD ONLY",         dict(use_hmm=False, use_rollover=False, use_dyn_akad=True,  use_dwell=False)),
        ("E) + Dwell ONLY",               dict(use_hmm=False, use_rollover=False, use_dyn_akad=False, use_dwell=True)),
        ("F) ALL FEATURES (live bot)",     dict(use_hmm=True,  use_rollover=True,  use_dyn_akad=True,  use_dwell=True)),
    ]

    results = []
    for label, kwargs in configs:
        print(f"\n  Running: {label}...")
        t0 = time.time()
        r = run_sim(pair_data, label=label, **kwargs)
        elapsed = time.time() - t0
        results.append(r)
        print(f"    Done in {elapsed:.1f}s | Trades={r['trades']} WR={r['wr']}% PF={r['pf']} "
              f"P&L=${r['net_pnl']:,.2f} | HMM_blocks={r['hmm_blocks']} Roll_blocks={r['roll_blocks']} "
              f"Dwell_holds={r['dwell_holds']} Dwell_reentry={r['dwell_reentry_blocks']}")

    # Summary
    print(f"\n\n{'='*90}")
    print("ABLATION RESULTS SUMMARY")
    print(f"{'='*90}")
    print(f"\n  {'Config':<35} {'Trades':>7} {'WR':>7} {'PF':>7} {'Net P&L':>12} {'HMM Blk':>8} {'Roll Blk':>9} {'Dwell Blk':>10}")
    print(f"  {'-'*100}")
    baseline_trades = results[0]['trades']
    for r in results:
        delta_t = r['trades'] - baseline_trades
        dt_str = f"({delta_t:+d})" if delta_t != 0 else ""
        print(f"  {r['label']:<35} {r['trades']:>7}{dt_str:>6} {r['wr']:>6.1f}% {r['pf']:>7.2f} "
              f"${r['net_pnl']:>11,.2f} {r['hmm_blocks']:>8} {r['roll_blocks']:>9} "
              f"{r['dwell_reentry_blocks']:>10}")

    # Per-pair detail
    print(f"\n  PER-PAIR TRADE COUNTS:")
    print(f"  {'Config':<35} {'Index':>8} {'Forex':>8} {'EUR/GBP':>8} {'Total':>8}")
    print(f"  {'-'*72}")
    for r in results:
        pp = r['per_pair']
        idx = pp.get('Index Spread',{}).get('trades',0)
        fx = pp.get('Forex Anchor',{}).get('trades',0)
        eg = pp.get('EUR/GBP Spread',{}).get('trades',0)
        print(f"  {r['label']:<35} {idx:>8} {fx:>8} {eg:>8} {r['trades']:>8}")

    print(f"\n  PER-PAIR WIN RATES:")
    print(f"  {'Config':<35} {'Index':>8} {'Forex':>8} {'EUR/GBP':>8}")
    print(f"  {'-'*65}")
    for r in results:
        pp = r['per_pair']
        print(f"  {r['label']:<35} "
              f"{pp.get('Index Spread',{}).get('wr',0):>7.1f}% "
              f"{pp.get('Forex Anchor',{}).get('wr',0):>7.1f}% "
              f"{pp.get('EUR/GBP Spread',{}).get('wr',0):>7.1f}%")

    print(f"\n  PER-PAIR PROFIT FACTORS:")
    print(f"  {'Config':<35} {'Index':>8} {'Forex':>8} {'EUR/GBP':>8}")
    print(f"  {'-'*65}")
    for r in results:
        pp = r['per_pair']
        print(f"  {r['label']:<35} "
              f"{pp.get('Index Spread',{}).get('pf',0):>8.2f} "
              f"{pp.get('Forex Anchor',{}).get('pf',0):>8.2f} "
              f"{pp.get('EUR/GBP Spread',{}).get('pf',0):>8.2f}")

    # Save
    out = Path("Results/ablation_test_results.json")
    def conv(o):
        if isinstance(o, (np.integer,)): return int(o)
        if isinstance(o, (np.floating,)): return float(o)
        return str(o)
    with open(out, 'w') as f:
        json.dump(results, f, indent=2, default=conv)
    print(f"\n  Saved to {out}")

if __name__ == "__main__":
    main()
