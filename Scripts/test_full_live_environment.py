#!/usr/bin/env python3
"""
SHF v5.6 — FULL LIVE ENVIRONMENT TEST
================================================================
Tests ALL 4 pairs with REAL broker costs and HMM sweep.
This is the definitive test: what works and what doesn't.

Pairs:
  1. US100/DE40      (Index Spread)
  2. AUDUSD/NZDUSD   (Forex Anchor)
  3. EURJPY/CHFJPY   (JPY Cross)
  4. XTIUSD/XBRUSD   (Oil Spread)

Costs per pair (from real broker specs):
  Index:   $1+$1 spread/fill + $4 commission RT = $8/lot
  Forex:   $5+$7 spread/fill + $4 commission RT = $28/lot
  JPY:     $6.2+$9.4 spread/fill + $4 commission RT = $35/lot
  Oil:     $4+$5 spread/fill + 0.03% commission = ~$22/lot

HMM sweep: 100, 20, 10, 5
Time-of-day spread multiplier: Asian 1.8×, LDN open 1.2×, LDN+NY 1.0×, NY PM 1.1×, Late 1.5×
"""

import sys, math, time, json
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime
from collections import deque
from dataclasses import dataclass

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
ROLLOVER_LOCKOUT_MIN = 30
HMM_N_REGIMES = 3; HMM_LOOKBACK = 100
MAX_CONSEC_LOSSES = 5; COOLDOWN_BARS = 60
MIN_WARMUP_BARS = 200; STARTING_BALANCE = 100_000.0

# ============================================================================
# COST MODEL — Real broker specs
# ============================================================================
# Spread cost per fill per lot (entry side + exit side = 2 fills per instrument)
# Commission per round-trip per lot
# For each trade: cost = (spread_a×2 + spread_b×2) × lots × time_mult + commission × lots

@dataclass
class PairCost:
    spread_a_per_fill: float  # $ per fill per lot for instrument A
    spread_b_per_fill: float  # $ per fill per lot for instrument B
    commission_rt: float      # $ per lot round-trip (both legs combined)
    commission_pct: float     # If >0, use percentage instead of flat
    name: str = ""

PAIR_COSTS = {
    "Index Spread":    PairCost(1.0, 1.0, 4.0, 0.0, "US100+DE40: $1+$1 spread, $4 RT"),
    "Forex Anchor":    PairCost(5.0, 7.0, 4.0, 0.0, "AUDUSD+NZDUSD: $5+$7 spread, $4 RT"),
    "EURJPY/CHFJPY":   PairCost(6.2, 9.4, 4.0, 0.0, "EURJPY+CHFJPY: $6.2+$9.4 spread, $4 RT"),
    "Oil Spread":      PairCost(4.0, 5.0, 0.0, 0.0003, "XTIUSD+XBRUSD: $4+$5 spread, 0.03%"),
}

# Oil notional for commission calc: contract_size × approx_price × lots
OIL_APPROX_NOTIONAL_PER_LOT = 6500.0  # 100 barrels × ~$65

def get_spread_multiplier(hour):
    if 0 <= hour < 7: return 1.8      # Asian
    elif 7 <= hour < 9: return 1.2    # LDN open
    elif 9 <= hour < 17: return 1.0   # LDN+NY (tightest)
    elif 17 <= hour < 21: return 1.1  # NY PM
    else: return 1.5                   # Late

def calc_trade_cost(pair_name, lots, hour):
    pc = PAIR_COSTS[pair_name]
    mult = get_spread_multiplier(hour)
    spread_cost = (pc.spread_a_per_fill * 2 + pc.spread_b_per_fill * 2) * lots * mult
    if pc.commission_pct > 0:
        # Percentage-based (oil): 0.03% × notional × lots × 2 sides × 2 legs
        comm = pc.commission_pct * OIL_APPROX_NOTIONAL_PER_LOT * lots * 4
    else:
        comm = pc.commission_rt * lots
    return spread_cost + comm

# ============================================================================
# PAIR DEFINITIONS
# ============================================================================
@dataclass
class PairDef:
    name: str; sym_a: str; sym_b: str; file_a: str; file_b: str
    pair_index: int; notional: float = 100_000.0

ALL_PAIRS = [
    PairDef("Index Spread",    "US100","DE40",    "US100_M1.csv","DE40_M1.csv",     0, notional=150_000.0),
    PairDef("Forex Anchor",    "AUDUSD","NZDUSD", "AUDUSD_M1.csv","NZDUSD_M1.csv",  1, notional=100_000.0),
    PairDef("EURJPY/CHFJPY",   "EURJPY","CHFJPY", "EURJPY_M1.csv","CHFJPY_M1.csv",  2, notional=100_000.0),
    PairDef("Oil Spread",      "XTIUSD","XBRUSD", "XTIUSD_M1.csv","XBRUSD_M1.csv",  0, notional=100_000.0),
]

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
# SINGLE PAIR SIMULATION WITH COSTS
# ============================================================================
def run_pair(df, pdef, hmm_hold):
    balance = STARTING_BALANCE; peak = STARTING_BALANCE; daily_start = STARTING_BALANCE
    daily_date = None; ghost = False; ghost_info = ""
    dakad = DynamicAKAD()
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec = 0; gcool = 0
    trades = []; n = len(df)

    eng = shf_core.CointegrationEngine(span=WELFORD_SPAN, beta=1.0, entry_z=Z_BASE,
        exit_z=EXIT_Z_BASE, z_base=Z_BASE, gamma=GAMMA, hurst_window=HURST_WINDOW,
        dynamic_z=True, exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
    hmm = HMMRegimeDetector(lookback=HMM_LOOKBACK, min_regime_hold=hmm_hold)

    pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0
    lspread = 0.0; pspread = 0.0; sent_abort = False
    last_close_bar = -9999; last_close_h = 0.5
    entry_hour = 0

    for bar in range(n):
        if ghost: break
        row = df.iloc[bar]; bt = row['time']
        pa = float(row['close_a']); pb = float(row['close_b'])

        cd = bt.date() if hasattr(bt,'date') else None
        if cd and cd != daily_date: daily_date = cd; daily_start = balance

        cdd = max(0,(peak-balance)/peak) if peak>0 else 0
        ddd = max(0,(daily_start-balance)/daily_start) if daily_start>0 else 0

        if ddd >= GHOST_DAILY_DD:
            ghost = True; ghost_info = f"Daily DD {ddd*100:.2f}%"; break
        if cdd >= GHOST_MAX_DD:
            ghost = True; ghost_info = f"Max DD {cdd*100:.2f}%"; break
        if bar < gcool: continue

        pspread = lspread
        sig = eng.update(pa, pb)
        z = sig.z_score; s = sig.signal; spread = sig.spread; lspread = spread
        h = eng.last_hurst; exz = eng.last_exit_z

        pidx = min(pdef.pair_index, 2)
        if pspread != 0.0: corr.push_return(pidx, spread - pspread)

        la = math.log(pa) if pa>0 else 0; lb = math.log(pb) if pb>0 else 0
        beta, abort = sen.update(la, lb)
        if abort and not sent_abort:
            sent_abort = True
            if pos != 0:
                gross = (spread-es)*pos*elots*pdef.notional
                cost = calc_trade_cost(pdef.name, elots, entry_hour)
                pnl = gross - cost; balance += pnl; peak = max(peak,balance)
                w = pnl > 0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                trades.append({'pnl':pnl,'gross':gross,'cost':cost,'hold':bar-ebar,'hour':entry_hour})
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
            if hblocked: continue
            if is_rollover(bt): continue
            if last_close_bar >= 0:
                cb = calc_dwell(last_close_h) / 60.0
                if (bar - last_close_bar) < cb: continue

            risk = dakad.calc(cdd, ddd)
            corr.compute_risk(); cm = corr.last_risk_multiplier
            lots = max(0.01, round(balance * risk * cm / 1000.0, 2))
            pos = s; ez = z; es = spread; ebar = bar; elots = lots
            entry_hour = bt.hour

        # EXIT
        elif pos != 0:
            ex = False
            if abs(z) > abs(ez) * 2.5: ex = True
            if not ex:
                hb = bar - ebar; db = calc_dwell(h) / 60.0
                if hb < db: continue
                if pos == 1 and z > -exz: ex = True
                elif pos == -1 and z < exz: ex = True
            if ex:
                gross = (spread-es)*pos*elots*pdef.notional
                cost = calc_trade_cost(pdef.name, elots, entry_hour)
                pnl = gross - cost; balance += pnl; peak = max(peak,balance)
                w = pnl > 0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                trades.append({'pnl':pnl,'gross':gross,'cost':cost,'hold':bar-ebar,'hour':entry_hour})
                pos=0; last_close_bar=bar; last_close_h=h

    total = len(trades)
    if total == 0:
        return {'trades':0,'wr':0,'gross_wr':0,'pf':0,'net_pnl':0,'gross_pnl':0,
                'total_costs':0,'return_pct':0,'max_dd_pct':0,'avg_hold':0,
                'ghost':ghost,'ghost_info':ghost_info,'monthly':{}}

    pnls = [t['pnl'] for t in trades]
    gross_pnls = [t['gross'] for t in trades]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    gp = sum(wins) if wins else 0; gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins)/total*100; pf = gp/gl if gl>0 else 0
    gross_wins = [g for g in gross_pnls if g > 0]
    gross_wr = len(gross_wins)/total*100

    eq = STARTING_BALANCE; eq_peak = eq; mdd = 0
    for t in trades:
        eq += t['pnl']; eq_peak = max(eq_peak, eq); mdd = max(mdd, eq_peak - eq)

    holds = [t['hold'] for t in trades]
    avg_hold = np.mean(holds) if holds else 0

    # Monthly
    monthly = {}
    for t in trades:
        # Get month from bar index
        mk = 'total'
        monthly[mk] = monthly.get(mk, {'trades':0,'pnl':0,'gross':0,'cost':0})
        monthly[mk]['trades'] += 1
        monthly[mk]['pnl'] += t['pnl']
        monthly[mk]['gross'] += t['gross']
        monthly[mk]['cost'] += t['cost']

    first = df['time'].iloc[0]; last_t = df['time'].iloc[-1]
    days = (last_t - first).days
    months = days / 30.0

    return {
        'trades': total, 'wr': round(wr,1), 'gross_wr': round(gross_wr,1),
        'pf': round(pf,2), 'net_pnl': round(balance-STARTING_BALANCE,2),
        'gross_pnl': round(sum(gross_pnls),2), 'total_costs': round(sum(t['cost'] for t in trades),2),
        'return_pct': round((balance-STARTING_BALANCE)/STARTING_BALANCE*100,2),
        'max_dd_pct': round(mdd/STARTING_BALANCE*100,2),
        'avg_hold': round(avg_hold,1), 'ghost': ghost, 'ghost_info': ghost_info,
        'trades_per_month': round(total/months,1) if months > 0 else 0,
        'days': days,
        'avg_win': round(np.mean(wins),2) if wins else 0,
        'avg_loss': round(np.mean(losses),2) if losses else 0,
        'avg_gross_win': round(np.mean([g for g in gross_pnls if g > 0]),2) if gross_wins else 0,
        'avg_cost': round(sum(t['cost'] for t in trades)/total,2),
    }


def main():
    print("="*130)
    print("SHF v5.6 — FULL LIVE ENVIRONMENT TEST (ALL PAIRS + REAL COSTS + HMM SWEEP)")
    print(f"shf_core version: {shf_core.__version__}")
    print("="*130)
    print(f"  Balance: ${STARTING_BALANCE:,.0f} | Rollover lockout: {ROLLOVER_LOCKOUT_MIN} min")
    print(f"\n  COST MODEL (real broker specs):")
    for name, pc in PAIR_COSTS.items():
        base = (pc.spread_a_per_fill * 2 + pc.spread_b_per_fill * 2)
        if pc.commission_pct > 0:
            comm_str = f"{pc.commission_pct*100:.2f}% (~${pc.commission_pct * OIL_APPROX_NOTIONAL_PER_LOT * 4:.1f}/lot)"
        else:
            comm_str = f"${pc.commission_rt:.0f}/lot RT"
        total_approx = base + (pc.commission_pct * OIL_APPROX_NOTIONAL_PER_LOT * 4 if pc.commission_pct > 0 else pc.commission_rt)
        print(f"    {name:<18} Spread: ${pc.spread_a_per_fill}+${pc.spread_b_per_fill}/fill  Comm: {comm_str}  Total: ~${total_approx:.0f}/lot")
    print(f"\n  Spread Multipliers: Asian(00-07)=1.8x  LDN open(07-09)=1.2x  LDN+NY(09-17)=1.0x  NY PM(17-21)=1.1x  Late(21-00)=1.5x")

    # Load all pairs
    pair_data = {}
    print(f"\n  DATA:")
    for p in ALL_PAIRS:
        try:
            df = load_pair(p)
            pair_data[p.name] = (df, p)
            first = df['time'].iloc[0]; last_t = df['time'].iloc[-1]
            days = (last_t - first).days
            print(f"    {p.name:<18} {len(df):>8,} bars | {first} to {last_t} ({days}d)")
        except Exception as e:
            print(f"    {p.name:<18} FAILED: {e}")

    # =========================================================================
    # MAIN GRID: Each pair × Each HMM hold
    # =========================================================================
    hmm_values = [100, 20, 10, 5]
    all_results = {}

    print(f"\n\n{'='*130}")
    print("RESULTS: ALL PAIRS × HMM SWEEP — WITH REAL COSTS")
    print(f"{'='*130}")
    print(f"\n  {'Pair':<18} {'HMM':>4} {'Trades':>7} {'Tr/Mo':>6} {'NetWR':>7} {'GrossWR':>8} {'PF':>7} "
          f"{'GrossP&L':>12} {'Costs':>10} {'NetP&L':>12} {'Return':>8} {'MaxDD':>7} {'AvgHold':>8} {'$/Trade':>9}")
    print(f"  {'-'*140}")

    for pair_name, (df, pdef) in pair_data.items():
        best_pnl = -999999; best_hmm = 0
        for hmm_val in hmm_values:
            r = run_pair(df, pdef, hmm_val)
            key = f"{pair_name}|HMM={hmm_val}"
            all_results[key] = r

            ghost_str = f" GHOST:{r['ghost_info']}" if r['ghost'] else ""
            dollar_per_trade = r['net_pnl'] / r['trades'] if r['trades'] > 0 else 0
            marker = ""
            if r['net_pnl'] > best_pnl and r['trades'] >= 5:
                best_pnl = r['net_pnl']; best_hmm = hmm_val
                marker = " *"

            print(f"  {pair_name:<18} {hmm_val:>4} {r['trades']:>7} {r.get('trades_per_month',0):>5.0f} "
                  f"{r['wr']:>6.1f}% {r['gross_wr']:>7.1f}% {r['pf']:>7.2f} "
                  f"${r['gross_pnl']:>11,.2f} ${r['total_costs']:>9,.2f} ${r['net_pnl']:>11,.2f} "
                  f"{r['return_pct']:>7.2f}% {r['max_dd_pct']:>6.2f}% {r['avg_hold']:>7.1f} "
                  f"${dollar_per_trade:>8.2f}{ghost_str}{marker}")

        print(f"  {'':>18} BEST: HMM={best_hmm} → ${best_pnl:>,.2f}")
        print(f"  {'-'*140}")

    # =========================================================================
    # BEST CONFIG PER PAIR
    # =========================================================================
    print(f"\n\n{'='*130}")
    print("BEST CONFIG PER PAIR (highest Net P&L with 5+ trades)")
    print(f"{'='*130}")
    print(f"\n  {'Pair':<18} {'Best HMM':>9} {'Trades':>7} {'WR':>7} {'PF':>7} {'GrossP&L':>12} {'Costs':>10} {'NetP&L':>12} {'Return':>8} {'$/Trade':>9} {'Verdict'}")
    print(f"  {'-'*120}")

    best_portfolio = {}
    for pair_name in [p.name for p in ALL_PAIRS if p.name in pair_data]:
        best_r = None; best_hmm = 0; best_net = -999999
        for hmm_val in hmm_values:
            key = f"{pair_name}|HMM={hmm_val}"
            if key in all_results:
                r = all_results[key]
                if r['trades'] >= 5 and r['net_pnl'] > best_net:
                    best_net = r['net_pnl']; best_hmm = hmm_val; best_r = r

        if best_r is None: continue
        dpt = best_r['net_pnl'] / best_r['trades'] if best_r['trades'] > 0 else 0
        if best_r['net_pnl'] > 0 and best_r['pf'] >= 1.5:
            verdict = "TRADE LIVE"
        elif best_r['net_pnl'] > 0:
            verdict = "MARGINAL"
        else:
            verdict = "DO NOT TRADE"

        best_portfolio[pair_name] = {'hmm': best_hmm, 'result': best_r}

        marker = ">>>" if verdict == "TRADE LIVE" else ("   " if verdict == "MARGINAL" else "XXX")
        print(f"  {marker} {pair_name:<15} {best_hmm:>9} {best_r['trades']:>7} {best_r['wr']:>6.1f}% {best_r['pf']:>7.2f} "
              f"${best_r['gross_pnl']:>11,.2f} ${best_r['total_costs']:>9,.2f} ${best_r['net_pnl']:>11,.2f} "
              f"{best_r['return_pct']:>7.2f}% ${dpt:>8.2f}  {verdict}")

    # =========================================================================
    # COST IMPACT ANALYSIS
    # =========================================================================
    print(f"\n\n{'='*130}")
    print("COST IMPACT — How much does each pair lose to costs?")
    print(f"{'='*130}")
    print(f"\n  {'Pair':<18} {'HMM':>4} {'Gross':>12} {'Costs':>10} {'Net':>12} {'Cost%':>7} {'AvgGrossWin':>12} {'AvgCost':>9} {'Survives?'}")
    print(f"  {'-'*100}")

    for pair_name in [p.name for p in ALL_PAIRS if p.name in pair_data]:
        if pair_name not in best_portfolio: continue
        bp = best_portfolio[pair_name]
        r = bp['result']
        cost_pct = r['total_costs'] / max(abs(r['gross_pnl']), 0.01) * 100
        survives = "YES" if r['net_pnl'] > 0 else "NO"
        print(f"  {pair_name:<18} {bp['hmm']:>4} ${r['gross_pnl']:>11,.2f} ${r['total_costs']:>9,.2f} "
              f"${r['net_pnl']:>11,.2f} {cost_pct:>6.1f}% ${r.get('avg_gross_win',0):>11.2f} "
              f"${r.get('avg_cost',0):>8.2f}  {survives}")

    # =========================================================================
    # FINAL RECOMMENDATION
    # =========================================================================
    print(f"\n\n{'='*130}")
    print("FINAL RECOMMENDATION")
    print(f"{'='*130}")

    live_pairs = []
    total_net = 0
    for pair_name, bp in best_portfolio.items():
        r = bp['result']
        if r['net_pnl'] > 0 and r['pf'] >= 1.5:
            live_pairs.append((pair_name, bp['hmm'], r))
            total_net += r['net_pnl']

    if live_pairs:
        print(f"\n  TRADE THESE PAIRS LIVE:")
        for pair_name, hmm_val, r in sorted(live_pairs, key=lambda x: -x[2]['net_pnl']):
            print(f"    {pair_name:<18} HMM={hmm_val}  → ${r['net_pnl']:>11,.2f} ({r['return_pct']}%)  "
                  f"PF={r['pf']}  {r['trades']} trades  MaxDD={r['max_dd_pct']}%")
        print(f"\n  COMBINED: ${total_net:>,.2f} ({total_net/STARTING_BALANCE*100:.1f}%) over ~3.5 months")
        print(f"  Monthly rate: ~{total_net/STARTING_BALANCE*100/3.5:.1f}%/month")
    else:
        print(f"\n  NO PAIRS MEET THE CRITERIA (Net>0, PF>=1.5)")

    dead_pairs = [p for p in best_portfolio if best_portfolio[p]['result']['net_pnl'] <= 0 or best_portfolio[p]['result']['pf'] < 1.5]
    if dead_pairs:
        print(f"\n  DO NOT TRADE:")
        for pair_name in dead_pairs:
            r = best_portfolio[pair_name]['result']
            print(f"    {pair_name:<18} Best=${r['net_pnl']:>,.2f}  PF={r['pf']}  (costs eat the edge)")

    # Save
    save_data = {
        'timestamp': datetime.now().isoformat(),
        'config': 'Full live environment test with real costs',
        'cost_model': {k: {'spread_a': v.spread_a_per_fill, 'spread_b': v.spread_b_per_fill,
                           'commission_rt': v.commission_rt, 'commission_pct': v.commission_pct}
                       for k, v in PAIR_COSTS.items()},
        'recommendations': {p: {'hmm': h, 'net_pnl': r['net_pnl'], 'pf': r['pf']}
                           for p, h, r in live_pairs} if live_pairs else {},
    }
    out = Path("Results/full_live_environment_results.json")
    with open(out, 'w') as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"\n  Saved to {out}")


if __name__ == "__main__":
    main()
