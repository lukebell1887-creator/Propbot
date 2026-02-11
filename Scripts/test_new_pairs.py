#!/usr/bin/env python3
"""
SHF v5.6 — NEW CANDIDATE PAIRS BACKTEST
=========================================
Tests new pairs with the current live bot engine (all features) at various HMM settings.

Pairs to test (using existing M1 data):
  1. US30 / US100 (NAS100)    — Sector Rotation (Index vs Index)
  2. EURJPY / CHFJPY          — Euro-Swiss Stability
  3. XAUUSD / XAGUSD          — Gold/Silver Ratio
  4. EURUSD / GBPUSD           — Retest with different HMM settings
  
  (USOIL / UKOIL skipped — no UKOIL data available locally. Need VPS download.)

Each pair tested with HMM hold = [5, 10, 20, 100] to find optimal setting.
"""

import sys, math, time, json
import numpy as np
import pandas as pd
from pathlib import Path
from collections import deque
from dataclasses import dataclass

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shf_core

WELFORD_SPAN = 100; Z_BASE = 2.0; GAMMA = 6.0; HURST_WINDOW = 512
EXIT_Z_BASE = 0.5; EXIT_GAMMA = 2.0
GHOST_DAILY_DD = 0.04; GHOST_MAX_DD = 0.09
KALMAN_TOLERANCE = 0.15; CORR_WINDOW = 200
DWELL_BASE = 60.0; DWELL_ANCHOR = 0.3; DWELL_MIN = 30.0; DWELL_MAX = 300.0
ROLLOVER_LOCKOUT_MIN = 5
STARTING_BALANCE = 100_000.0

@dataclass
class PairDef:
    name: str; file_a: str; file_b: str; notional: float = 100_000.0

CANDIDATE_PAIRS = [
    PairDef("XTIUSD/XBRUSD",  "XTIUSD_M1.csv", "XBRUSD_M1.csv", 100_000.0),
]

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
    m = m[(m['close_a']>0)&(m['close_b']>0)].reset_index(drop=True)
    return m

def run_single_pair(df, pdef, hmm_hold=20):
    """Run full live-bot simulation on a single pair."""
    balance = STARTING_BALANCE; peak = STARTING_BALANCE; daily_start = STARTING_BALANCE
    daily_date = None; ghost = False
    dakad = DynamicAKAD()
    consec = 0; gcool = 0
    trades_list = []; hmm_blocks = 0
    n = len(df)
    
    eng = shf_core.CointegrationEngine(span=WELFORD_SPAN, beta=1.0, entry_z=Z_BASE,
        exit_z=EXIT_Z_BASE, z_base=Z_BASE, gamma=GAMMA, hurst_window=HURST_WINDOW,
        dynamic_z=True, exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
    hmm = HMMRegimeDetector(min_regime_hold=hmm_hold)
    
    pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0
    lspread = 0.0; pspread = 0.0; sent_abort = False
    last_close_bar = -9999; last_close_h = 0.5
    hurst_sum = 0.0; hurst_count = 0

    for bar in range(n):
        if ghost: break
        row = df.iloc[bar]; bt = row['time']
        pa = float(row['close_a']); pb = float(row['close_b'])
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
        h = eng.last_hurst; exz = eng.last_exit_z
        
        if bar > 200:
            hurst_sum += h; hurst_count += 1

        la = math.log(pa) if pa>0 else 0; lb = math.log(pb) if pb>0 else 0
        beta, abort = sen.update(la, lb)
        if abort and not sent_abort:
            sent_abort = True
            if pos != 0:
                pnl = (spread-es)*pos*elots*pdef.notional; balance += pnl; peak = max(peak,balance)
                w = pnl>0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=5: gcool=bar+60; consec=0
                trades_list.append({'pnl':pnl,'reason':'SENTINEL','bar':bar})
                pos=0; last_close_bar=bar; last_close_h=h
            continue
        if sent_abort and not abort: sent_abort = False
        if sent_abort: continue

        hblocked = False
        if pspread != 0.0:
            hmm.update(spread - pspread); hblocked = hmm.is_blocked

        if bar < 200: continue

        if pos == 0 and s != 0:
            if hblocked: hmm_blocks += 1; continue
            if is_rollover(bt): continue
            if last_close_bar >= 0:
                cb = calc_dwell(last_close_h) / 60.0
                if (bar - last_close_bar) < cb: continue

            risk = dakad.calc(cdd, ddd)
            lots = max(0.01, round(balance * risk / 1000.0, 2))
            pos = s; ez = z; es = spread; ebar = bar; elots = lots

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
                if consec>=5: gcool=bar+60; consec=0
                trades_list.append({'pnl':pnl,'reason':reason,'bar':bar})
                pos=0; last_close_bar=bar; last_close_h=h

    total = len(trades_list)
    if total == 0:
        return {'trades':0,'wr':0,'pf':0,'net_pnl':0,'return_pct':0,'max_dd_pct':0,
                'avg_hurst':0,'hmm_blocks':hmm_blocks,'ghost':ghost,'avg_win':0,'avg_loss':0,'avg_hold':0}
    
    pnls = [t['pnl'] for t in trades_list]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    gp = sum(wins) if wins else 0; gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins)/total*100
    pf = gp/gl if gl>0 else 0

    # Max DD
    eq = STARTING_BALANCE; eq_peak = eq; mdd = 0
    for t in trades_list:
        eq += t['pnl']; eq_peak = max(eq_peak, eq); mdd = max(mdd, eq_peak - eq)

    # Avg hold
    holds = []
    for i in range(len(trades_list)):
        if i == 0: holds.append(trades_list[0]['bar'])
        else: holds.append(trades_list[i]['bar'] - trades_list[i-1]['bar'])
    avg_hold = np.mean(holds) if holds else 0

    return {
        'trades': total, 'wr': round(wr,1), 'pf': round(pf,2),
        'net_pnl': round(balance-STARTING_BALANCE,2),
        'return_pct': round((balance-STARTING_BALANCE)/STARTING_BALANCE*100,2),
        'max_dd_pct': round(mdd/STARTING_BALANCE*100,2),
        'avg_hurst': round(hurst_sum/max(hurst_count,1),3),
        'hmm_blocks': hmm_blocks, 'ghost': ghost,
        'avg_win': round(np.mean(wins),2) if wins else 0,
        'avg_loss': round(np.mean(losses),2) if losses else 0,
        'avg_hold': round(avg_hold,1),
        'gross_profit': round(gp,2), 'gross_loss': round(gl,2),
    }


def main():
    print("="*90)
    print("SHF v5.6 — NEW CANDIDATE PAIRS BACKTEST")
    print("="*90)
    print("  Testing with FULL live bot features (Dynamic AKAD, HMM, Dwell, etc.)")
    print("  NOTE: USOIL/UKOIL skipped — no UKOIL data locally. Need VPS download.\n")

    # Load all pair data
    pair_data = {}
    for p in CANDIDATE_PAIRS:
        try:
            df = load_pair(p)
            pair_data[p.name] = (df, p)
            first = df['time'].iloc[0]; last = df['time'].iloc[-1]
            days = (last - first).days
            print(f"  {p.name:<20} {len(df):>8,} bars | {first} to {last} ({days}d)")
        except Exception as e:
            print(f"  {p.name:<20} FAILED: {e}")

    hmm_settings = [5, 10, 20, 100]

    # Run each pair with each HMM setting
    all_results = {}
    for pair_name, (df, pdef) in pair_data.items():
        print(f"\n{'='*70}")
        print(f"  PAIR: {pair_name}")
        print(f"{'='*70}")
        pair_results = []
        for hmm_h in hmm_settings:
            t0 = time.time()
            r = run_single_pair(df, pdef, hmm_hold=hmm_h)
            elapsed = time.time() - t0
            r['hmm_hold'] = hmm_h
            pair_results.append(r)
            ghost_str = " GHOST!" if r['ghost'] else ""
            print(f"    HMM={hmm_h:>3}: {r['trades']:>4} trades  WR={r['wr']:>5.1f}%  PF={r['pf']:>5.2f}  "
                  f"P&L=${r['net_pnl']:>10,.2f}  Return={r['return_pct']:>6.2f}%  "
                  f"MaxDD={r['max_dd_pct']:>5.2f}%  H={r['avg_hurst']:.3f}  "
                  f"HMM_blk={r['hmm_blocks']:>4}  ({elapsed:.1f}s){ghost_str}")
        all_results[pair_name] = pair_results

    # =========================================================================
    # GRAND SUMMARY
    # =========================================================================
    print(f"\n\n{'='*90}")
    print("GRAND SUMMARY — ALL PAIRS, BEST HMM SETTING")
    print(f"{'='*90}")
    
    # Find best HMM for each pair (by PF, min 10 trades)
    print(f"\n  {'Pair':<20} {'Best HMM':>9} {'Trades':>7} {'WR':>7} {'PF':>7} {'Return':>8} {'MaxDD':>7} {'Hurst':>7} {'Verdict':<15}")
    print(f"  {'-'*95}")
    
    best_pairs = []
    for pair_name, results in all_results.items():
        valid = [r for r in results if r['trades'] >= 10 and r['pf'] > 0]
        if not valid:
            print(f"  {pair_name:<20} {'N/A':>9} {'--':>7} {'--':>7} {'--':>7} {'--':>8} {'--':>7} {'--':>7} {'TOO FEW TRADES':<15}")
            continue
        best = max(valid, key=lambda x: x['pf'])
        verdict = "STRONG" if best['pf'] >= 2.0 else ("GOOD" if best['pf'] >= 1.5 else ("MARGINAL" if best['pf'] >= 1.0 else "WEAK"))
        emoji = "+++" if best['pf'] >= 2.0 else ("++" if best['pf'] >= 1.5 else ("+" if best['pf'] >= 1.0 else "-"))
        print(f"  {pair_name:<20} {best['hmm_hold']:>9} {best['trades']:>7} {best['wr']:>6.1f}% {best['pf']:>7.2f} "
              f"{best['return_pct']:>7.2f}% {best['max_dd_pct']:>6.2f}% {best['avg_hurst']:>7.3f} {emoji+' '+verdict:<15}")
        best_pairs.append({'pair': pair_name, 'best_hmm': best['hmm_hold'], **best})

    # Comparison with existing Holy Trio
    print(f"\n  EXISTING HOLY TRIO (for reference):")
    print(f"  {'Pair':<20} {'HMM=20':>9} {'Trades':>7} {'WR':>7} {'PF':>7}")
    print(f"  {'-'*55}")
    print(f"  {'US100/DE40':<20} {'20':>9} {'98':>7} {'~76%':>7} {'1.85':>7}")
    print(f"  {'AUDUSD/NZDUSD':<20} {'20':>9} {'134':>7} {'~82%':>7} {'3.12':>7}")

    # Detailed per-HMM table
    print(f"\n\n{'='*90}")
    print("DETAILED RESULTS — ALL PAIRS x ALL HMM SETTINGS")
    print(f"{'='*90}")
    for pair_name, results in all_results.items():
        print(f"\n  {pair_name}:")
        print(f"    {'HMM':>5} {'Trades':>7} {'WR':>7} {'PF':>7} {'P&L':>12} {'Return':>8} {'MaxDD':>7} {'AvgWin':>9} {'AvgLoss':>9} {'Hurst':>7}")
        print(f"    {'-'*90}")
        for r in results:
            print(f"    {r['hmm_hold']:>5} {r['trades']:>7} {r['wr']:>6.1f}% {r['pf']:>7.2f} "
                  f"${r['net_pnl']:>11,.2f} {r['return_pct']:>7.2f}% {r['max_dd_pct']:>6.2f}% "
                  f"${r['avg_win']:>8.2f} ${r['avg_loss']:>8.2f} {r['avg_hurst']:>7.3f}")

    # Save
    out = Path("Results/new_pairs_backtest_results.json")
    def conv(o):
        if isinstance(o, (np.integer,)): return int(o)
        if isinstance(o, (np.floating,)): return float(o)
        return str(o)
    with open(out, 'w') as f:
        json.dump({'all_results': {k: v for k,v in all_results.items()}, 'best_pairs': best_pairs}, f, indent=2, default=conv)
    print(f"\n  Saved to {out}")


if __name__ == "__main__":
    main()
