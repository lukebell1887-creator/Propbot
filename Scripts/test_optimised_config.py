#!/usr/bin/env python3
"""
SHF v5.6 — OPTIMISED CONFIG TEST (ALL PAIRS)
==============================================
Tests various portfolio combinations with all available pairs.

Configs tested:
  A) Current live bot (3 original pairs, HMM=100)
  B) Drop EUR/GBP, keep 2 originals (HMM=20)
  C) Replace EUR/GBP with EURJPY/CHFJPY (HMM=20)
  D) Replace EUR/GBP with XTIUSD/XBRUSD Oil (HMM=20)
  E) 4 pairs: Originals + EURJPY/CHFJPY + Oil (HMM=20)
  F) 5 pairs: E + XAUUSD/XAGUSD (HMM=20)
  G) Best 3: AUDUSD/NZDUSD + EURJPY/CHFJPY + XTIUSD/XBRUSD (HMM=20)
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
    name: str; sym_a: str; sym_b: str; file_a: str; file_b: str
    pair_index: int; notional: float = 100_000.0

# ALL available pairs
IDX   = PairDef("Index Spread",    "US100","DE40",    "US100_M1.csv","DE40_M1.csv",     0, 150_000.0)
FX    = PairDef("Forex Anchor",    "AUDUSD","NZDUSD", "AUDUSD_M1.csv","NZDUSD_M1.csv",  1, 100_000.0)
EURGBP= PairDef("EUR/GBP Spread",  "EURUSD","GBPUSD", "EURUSD_M1.csv","GBPUSD_M1.csv",  2, 100_000.0)
EJCJ  = PairDef("EURJPY/CHFJPY",   "EURJPY","CHFJPY", "EURJPY_M1.csv","CHFJPY_M1.csv",  3, 100_000.0)
OIL   = PairDef("XTIUSD/XBRUSD",   "XTIUSD","XBRUSD", "XTIUSD_M1.csv","XBRUSD_M1.csv", 4, 100_000.0)
GOLD  = PairDef("XAUUSD/XAGUSD",   "XAUUSD","XAGUSD", "XAUUSD_M1.csv","XAGUSD_M1.csv", 5, 100_000.0)

ALL_PAIR_DEFS = [IDX, FX, EURGBP, EJCJ, OIL, GOLD]

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

def run_sim(pair_data, active_pairs, hmm_hold=20, label=""):
    balance = STARTING_BALANCE; peak = STARTING_BALANCE; daily_start = STARTING_BALANCE
    daily_date = None; ghost = False
    dakad = DynamicAKAD()
    n_pairs = max(p.pair_index for p in active_pairs) + 1
    corr = shf_core.CorrelationRiskMonitor(n_pairs=max(n_pairs, 3), window=CORR_WINDOW)
    consec = 0; gcool = 0
    trades_list = []; hmm_blocks = 0

    for pdef in active_pairs:
        if pdef.name not in pair_data: continue
        df = pair_data[pdef.name]; n = len(df)
        eng = shf_core.CointegrationEngine(span=WELFORD_SPAN, beta=1.0, entry_z=Z_BASE,
            exit_z=EXIT_Z_BASE, z_base=Z_BASE, gamma=GAMMA, hurst_window=HURST_WINDOW,
            dynamic_z=True, exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
        sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
        hmm = HMMRegimeDetector(min_regime_hold=hmm_hold)
        pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0
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
            h = eng.last_hurst; exz = eng.last_exit_z

            pidx = min(pdef.pair_index, 2)  # Corr monitor only supports up to n_pairs
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
                    if consec>=5: gcool=bar+60; consec=0
                    trades_list.append({'pair':pdef.name,'pnl':pnl,'reason':'SENTINEL'})
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
                corr.compute_risk(); cm = corr.last_risk_multiplier
                lots = max(0.01, round(balance * risk * cm / 1000.0, 2))
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
                    trades_list.append({'pair':pdef.name,'pnl':pnl,'reason':reason})
                    pos=0; last_close_bar=bar; last_close_h=h

    total = len(trades_list)
    pnls = [t['pnl'] for t in trades_list]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    gp = sum(wins) if wins else 0; gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins)/total*100 if total>0 else 0
    pf = gp/gl if gl>0 else 0

    eq = STARTING_BALANCE; eq_peak = eq; mdd = 0
    for t in trades_list:
        eq += t['pnl']; eq_peak = max(eq_peak, eq); mdd = max(mdd, eq_peak - eq)
    mdd_pct = mdd / STARTING_BALANCE * 100

    pp = {}
    for pdef in active_pairs:
        pt = [t for t in trades_list if t['pair']==pdef.name]
        if not pt: pp[pdef.name] = {'trades':0,'wr':0,'pf':0,'pnl':0}; continue
        ppnl = [t['pnl'] for t in pt]; pw = [p for p in ppnl if p>0]; pl = [p for p in ppnl if p<=0]
        pgp = sum(pw) if pw else 0; pgl = abs(sum(pl)) if pl else 0.001
        pp[pdef.name] = {'trades':len(pt),'wr':round(len(pw)/len(pt)*100,1),'pf':round(pgp/pgl,2),'pnl':round(sum(ppnl),2)}

    return {
        'label': label, 'trades': total, 'wr': round(wr,1), 'pf': round(pf,2),
        'net_pnl': round(balance-STARTING_BALANCE,2), 'return_pct': round((balance-STARTING_BALANCE)/STARTING_BALANCE*100,2),
        'max_dd_pct': round(mdd_pct,2), 'ghost': ghost, 'hmm_blocks': hmm_blocks,
        'per_pair': pp, 'avg_win': round(np.mean(wins),2) if wins else 0,
        'avg_loss': round(np.mean(losses),2) if losses else 0,
    }


def main():
    print("="*100)
    print("SHF v5.6 -- FULL PORTFOLIO OPTIMISATION TEST (ALL PAIRS)")
    print("="*100)

    # Load all pairs
    pair_data = {}
    for p in ALL_PAIR_DEFS:
        try:
            df = load_pair(p)
            pair_data[p.name] = df
            first = df['time'].iloc[0]; last = df['time'].iloc[-1]
            days = (last - first).days
            print(f"  {p.name:<20} {len(df):>8,} bars | {first} to {last} ({days}d)")
        except Exception as e:
            print(f"  {p.name:<20} FAILED: {e}")

    # Define portfolio configs to test
    configs = [
        ("A) CURRENT LIVE (3 orig, HMM=100)",        [IDX, FX, EURGBP],           100),
        ("B) 2 orig only (HMM=20)",                   [IDX, FX],                   20),
        ("C) Replace EUR/GBP -> EURJPY/CHFJPY",       [IDX, FX, EJCJ],             20),
        ("D) Replace EUR/GBP -> Oil",                  [IDX, FX, OIL],              20),
        ("E) 4 pairs: Orig2+EJCJ+Oil",                [IDX, FX, EJCJ, OIL],        20),
        ("F) 5 pairs: E + Gold/Silver",                [IDX, FX, EJCJ, OIL, GOLD],  20),
        ("G) Best3: FX+EJCJ+Oil (no Index)",           [FX, EJCJ, OIL],             20),
        ("H) Oil only",                                [OIL],                        20),
        ("I) EJCJ only",                               [EJCJ],                       20),
    ]

    results = []
    for label, pairs, hmm_h in configs:
        missing = [p.name for p in pairs if p.name not in pair_data]
        if missing:
            print(f"\n  SKIP: {label} -- missing data for {missing}")
            continue
        print(f"\n  Running: {label}...")
        t0 = time.time()
        r = run_sim(pair_data, pairs, hmm_hold=hmm_h, label=label)
        elapsed = time.time() - t0
        results.append(r)
        ghost_str = " GHOST!" if r['ghost'] else ""
        print(f"    {elapsed:.1f}s | {r['trades']} trades  WR={r['wr']}%  PF={r['pf']}  "
              f"P&L=${r['net_pnl']:,.2f}  Return={r['return_pct']}%  MaxDD={r['max_dd_pct']}%  "
              f"HMM_blk={r['hmm_blocks']}{ghost_str}")

    # =========================================================================
    # GRAND COMPARISON TABLE
    # =========================================================================
    print(f"\n\n{'='*100}")
    print("GRAND COMPARISON TABLE")
    print(f"{'='*100}")
    print(f"\n  {'Config':<45} {'Trades':>7} {'WR':>7} {'PF':>7} {'Net P&L':>14} {'Return':>8} {'MaxDD':>7} {'Ghost':>6}")
    print(f"  {'-'*110}")
    for r in results:
        g = "YES" if r['ghost'] else "no"
        print(f"  {r['label']:<45} {r['trades']:>7} {r['wr']:>6.1f}% {r['pf']:>7.2f} "
              f"${r['net_pnl']:>13,.2f} {r['return_pct']:>7.2f}% {r['max_dd_pct']:>6.2f}% {g:>6}")

    # Per-pair breakdown for each config
    print(f"\n\n{'='*100}")
    print("PER-PAIR BREAKDOWN")
    print(f"{'='*100}")
    for r in results:
        print(f"\n  {r['label']}:")
        pp = r['per_pair']
        for pname, pstats in pp.items():
            print(f"    {pname:<22} Trades={pstats['trades']:>5}  WR={pstats['wr']:>5.1f}%  PF={pstats['pf']:>6.2f}  P&L=${pstats['pnl']:>12,.2f}")

    # Risk metrics
    print(f"\n\n{'='*100}")
    print("RISK/REWARD METRICS")
    print(f"{'='*100}")
    print(f"\n  {'Config':<45} {'AvgWin':>10} {'AvgLoss':>10} {'W/L Ratio':>10} {'Calmar':>8}")
    print(f"  {'-'*90}")
    for r in results:
        wl = abs(r['avg_win']/r['avg_loss']) if r['avg_loss'] != 0 else 0
        calmar = r['return_pct'] / r['max_dd_pct'] if r['max_dd_pct'] > 0 else 0
        print(f"  {r['label']:<45} ${r['avg_win']:>9.2f} ${r['avg_loss']:>9.2f} {wl:>10.2f} {calmar:>8.2f}")

    # Save
    out = Path("Results/optimised_config_results.json")
    def conv(o):
        if isinstance(o, (np.integer,)): return int(o)
        if isinstance(o, (np.floating,)): return float(o)
        return str(o)
    with open(out, 'w') as f:
        json.dump(results, f, indent=2, default=conv)
    print(f"\n  Saved to {out}")

if __name__ == "__main__":
    main()
