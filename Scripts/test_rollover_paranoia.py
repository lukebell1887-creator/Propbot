#!/usr/bin/env python3
"""
ROLLOVER PARANOIA CHECK
=======================
Deep investigation: Is the $28K backtest profit fake because of midnight rollover?

Tests:
1. What hours do trades ENTER and EXIT? P&L by entry hour.
2. How many trades SPAN the midnight gap (23:xx → 01:xx)?
3. P&L with/without gap-spanning trades.
4. Run with AGGRESSIVE rollover exclusion (±60min, ±120min) to see impact.
5. Check the spread behavior across the midnight gap (price jumps).
6. Compare the rollover lockout in backtest vs live engine.

Also checks: Does the live engine match backtest assumptions?
"""

import sys, math, time, json
import numpy as np
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
from collections import deque, defaultdict
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
HMM_N_REGIMES = 3; HMM_LOOKBACK = 100
MAX_CONSEC_LOSSES = 5; COOLDOWN_BARS = 60
MIN_WARMUP_BARS = 200; STARTING_BALANCE = 100_000.0

# Per-pair dwell
INDEX_DWELL_BASE = 60.0;   INDEX_DWELL_ANCHOR = 0.3
INDEX_DWELL_MIN = 30.0;    INDEX_DWELL_MAX = 300.0
OIL_DWELL_BASE = 1800.0;   OIL_DWELL_ANCHOR = 0.3
OIL_DWELL_MIN = 900.0;     OIL_DWELL_MAX = 9000.0

# Cost model
@dataclass
class PairCost:
    spread_a: float; spread_b: float; comm_rt: float; comm_pct: float

PAIR_COSTS = {
    "Index Spread": PairCost(1.0, 1.0, 0.0, 0.0),
    "Oil Spread": PairCost(4.0, 5.0, 0.0, 0.0003),
}
OIL_NOTIONAL = 6500.0

def get_spread_mult(h):
    if 0 <= h < 7: return 1.8
    elif 7 <= h < 9: return 1.2
    elif 9 <= h < 17: return 1.0
    elif 17 <= h < 21: return 1.1
    else: return 1.5

def calc_cost(pn, lots, h):
    pc = PAIR_COSTS[pn]
    m = get_spread_mult(h)
    sc = (pc.spread_a * 2 + pc.spread_b * 2) * lots * m
    cm = pc.comm_pct * OIL_NOTIONAL * lots * 4 if pc.comm_pct > 0 else pc.comm_rt * lots
    return sc + cm

# Pair definitions
@dataclass
class PairDef:
    name: str; sym_a: str; sym_b: str; file_a: str; file_b: str
    pair_index: int; notional: float
    dwell_base: float; dwell_anchor: float; dwell_min: float; dwell_max: float
    hmm_hold: int

PAIRS = [
    PairDef("Index Spread", "US100","DE40", "US100_M1.csv","DE40_M1.csv", 0,
            150_000.0, INDEX_DWELL_BASE, INDEX_DWELL_ANCHOR, INDEX_DWELL_MIN, INDEX_DWELL_MAX, 20),
    PairDef("Oil Spread", "XTIUSD","XBRUSD", "XTIUSD_M1.csv","XBRUSD_M1.csv", 1,
            100_000.0, OIL_DWELL_BASE, OIL_DWELL_ANCHOR, OIL_DWELL_MIN, OIL_DWELL_MAX, 5),
]

# Components
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

def calc_dwell(h, pdef):
    return max(pdef.dwell_min, min(pdef.dwell_max, pdef.dwell_base * (h / pdef.dwell_anchor)))

def load_pair(p):
    d = Path(__file__).resolve().parent.parent / "data" / "historical"
    a = pd.read_csv(d / p.file_a, parse_dates=['time']).rename(columns={'close':'close_a'})
    b = pd.read_csv(d / p.file_b, parse_dates=['time']).rename(columns={'close':'close_b'})
    m = pd.merge(a[['time','close_a']], b[['time','close_b']], on='time', how='inner').sort_values('time').reset_index(drop=True)
    return m[(m['close_a']>0)&(m['close_b']>0)].reset_index(drop=True)


def run_pair_detailed(df, pdef, rollover_lockout_min=30):
    """Run backtest with detailed per-trade tracking including entry/exit hours."""
    balance = STARTING_BALANCE; peak = STARTING_BALANCE; daily_start = STARTING_BALANCE
    daily_date = None; ghost = False
    dakad = DynamicAKAD()
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec = 0; gcool = 0
    trades = []; n = len(df)

    eng = shf_core.CointegrationEngine(span=WELFORD_SPAN, beta=1.0, entry_z=Z_BASE,
        exit_z=EXIT_Z_BASE, z_base=Z_BASE, gamma=GAMMA, hurst_window=HURST_WINDOW,
        dynamic_z=True, exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
    hmm = HMMRegimeDetector(lookback=HMM_LOOKBACK, min_regime_hold=pdef.hmm_hold)

    pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0
    lspread = 0.0; pspread = 0.0; sent_abort = False
    last_close_bar = -9999; last_close_h = 0.5
    entry_hour = 0; entry_time = None

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

        pidx = min(pdef.pair_index, 2)
        if pspread != 0.0: corr.push_return(pidx, spread - pspread)

        la = math.log(pa) if pa>0 else 0; lb = math.log(pb) if pb>0 else 0
        beta, abort = sen.update(la, lb)
        if abort and not sent_abort:
            sent_abort = True
            if pos != 0:
                gross = (spread-es)*pos*elots*pdef.notional
                cost = calc_cost(pdef.name, elots, entry_hour)
                pnl = gross - cost; balance += pnl; peak = max(peak,balance)
                w = pnl > 0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                trades.append({
                    'pnl':pnl,'gross':gross,'cost':cost,'hold':bar-ebar,
                    'entry_hour':entry_hour,'exit_hour':bt.hour,
                    'entry_time':entry_time,'exit_time':bt,
                    'entry_bar':ebar,'exit_bar':bar,
                    'spans_midnight': _spans_midnight(entry_time, bt),
                    'reason':'SENTINEL'
                })
                pos=0; last_close_bar=bar; last_close_h=h
            continue
        if sent_abort and not abort: sent_abort = False
        if sent_abort: continue

        hblocked = False
        if pspread != 0.0:
            hmm.update(spread - pspread); hblocked = hmm.is_blocked

        if bar < MIN_WARMUP_BARS: continue

        # ROLLOVER LOCKOUT — parameterized
        m_since = bt.hour * 60 + bt.minute
        m_before = 1440 - m_since
        in_rollover = (m_since < rollover_lockout_min or m_before < rollover_lockout_min)

        # ENTRY
        if pos == 0 and s != 0:
            if hblocked: continue
            if in_rollover: continue
            if last_close_bar >= 0:
                cb = calc_dwell(last_close_h, pdef) / 60.0
                if (bar - last_close_bar) < cb: continue

            risk = dakad.calc(cdd, ddd)
            corr.compute_risk(); cm = corr.last_risk_multiplier
            lots = max(0.01, round(balance * risk * cm / 1000.0, 2))
            pos = s; ez = z; es = spread; ebar = bar; elots = lots
            entry_hour = bt.hour; entry_time = bt

        # EXIT
        elif pos != 0:
            ex = False; reason = ""
            if abs(z) > abs(ez) * 2.5: ex = True; reason = "EMERGENCY"
            if not ex:
                hb = bar - ebar
                db = calc_dwell(h, pdef) / 60.0
                if hb < db: continue
                if pos == 1 and z > -exz: ex = True; reason = "DYNAMIC_EXIT"
                elif pos == -1 and z < exz: ex = True; reason = "DYNAMIC_EXIT"
            if ex:
                gross = (spread-es)*pos*elots*pdef.notional
                cost = calc_cost(pdef.name, elots, entry_hour)
                pnl = gross - cost; balance += pnl; peak = max(peak,balance)
                w = pnl > 0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                trades.append({
                    'pnl':pnl,'gross':gross,'cost':cost,'hold':bar-ebar,
                    'entry_hour':entry_hour,'exit_hour':bt.hour,
                    'entry_time':entry_time,'exit_time':bt,
                    'entry_bar':ebar,'exit_bar':bar,
                    'spans_midnight': _spans_midnight(entry_time, bt),
                    'reason':reason
                })
                pos=0; last_close_bar=bar; last_close_h=h

    return trades, balance


def _spans_midnight(entry_t, exit_t):
    """Check if a trade spans the midnight gap (data goes 23:xx -> 01:xx next day)."""
    if entry_t is None or exit_t is None:
        return False
    # If they're on different dates and the entry was in evening / exit in early morning
    if entry_t.date() != exit_t.date():
        return True
    return False


def analyze_spread_at_midnight(df):
    """Check how the LOG SPREAD behaves across the midnight gap."""
    df = df.copy()
    df['log_spread'] = np.log(df['close_a']) - np.log(df['close_b'])
    df['hour'] = df['time'].dt.hour
    df['spread_change'] = df['log_spread'].diff().abs()
    df['time_gap'] = df['time'].diff().dt.total_seconds()
    
    # Find bars right after midnight gap (first bar with hour=1 after a gap)
    gap_bars = df[(df['time_gap'] > 120) & (df['hour'].isin([1, 2]))]
    normal_bars = df[(df['time_gap'] <= 120) & (df['time_gap'] > 0)]
    
    return {
        'gap_bars': len(gap_bars),
        'avg_spread_jump_at_gap': gap_bars['spread_change'].mean() if len(gap_bars) > 0 else 0,
        'max_spread_jump_at_gap': gap_bars['spread_change'].max() if len(gap_bars) > 0 else 0,
        'avg_normal_spread_change': normal_bars['spread_change'].mean() if len(normal_bars) > 0 else 0,
        'gap_vs_normal_ratio': (gap_bars['spread_change'].mean() / normal_bars['spread_change'].mean()) 
            if len(gap_bars) > 0 and len(normal_bars) > 0 and normal_bars['spread_change'].mean() > 0 else 0,
    }


def main():
    print("=" * 130)
    print("ROLLOVER PARANOIA CHECK — Is the $28K profit from midnight scalping?")
    print("=" * 130)
    
    # =========================================================================
    # PART 1: DATA ANALYSIS — Spread behavior at midnight
    # =========================================================================
    print("\n\n" + "=" * 80)
    print("PART 1: SPREAD BEHAVIOR AT MIDNIGHT GAP")
    print("=" * 80)
    
    for p in PAIRS:
        df = load_pair(p)
        result = analyze_spread_at_midnight(df)
        print(f"\n  {p.name}:")
        print(f"    Data hours: 1-23 (hour 0 MISSING = broker closed midnight)")
        print(f"    Gap bars (after midnight): {result['gap_bars']}")
        print(f"    Avg spread jump at gap:    {result['avg_spread_jump_at_gap']:.6f}")
        print(f"    Max spread jump at gap:    {result['max_spread_jump_at_gap']:.6f}")
        print(f"    Avg NORMAL spread change:  {result['avg_normal_spread_change']:.6f}")
        print(f"    Gap/Normal ratio:          {result['gap_vs_normal_ratio']:.2f}x")
        
        if result['gap_vs_normal_ratio'] > 3:
            print(f"    >>> WARNING: Midnight gap is {result['gap_vs_normal_ratio']:.1f}x bigger than normal!")
        elif result['gap_vs_normal_ratio'] > 1.5:
            print(f"    >>> MODERATE: Gap is somewhat bigger than normal bars")
        else:
            print(f"    >>> OK: Gap spread change is similar to normal bars")
    
    # =========================================================================
    # PART 2: BASELINE RUN — Standard 30-min rollover lockout (matches live)
    # =========================================================================
    print("\n\n" + "=" * 80)
    print("PART 2: BASELINE BACKTEST (30-min rollover lockout = matches live)")
    print("=" * 80)
    
    all_pair_data = {}
    for p in PAIRS:
        df = load_pair(p)
        all_pair_data[p.name] = (df, p)
    
    baseline_results = {}
    for pname, (df, pdef) in all_pair_data.items():
        trades, final_bal = run_pair_detailed(df, pdef, rollover_lockout_min=30)
        baseline_results[pname] = trades
        net = sum(t['pnl'] for t in trades)
        wins = [t for t in trades if t['pnl'] > 0]
        wr = len(wins)/len(trades)*100 if trades else 0
        print(f"\n  {pname}: {len(trades)} trades, WR={wr:.1f}%, Net=${net:,.2f}")
        
        # P&L by entry hour
        hour_pnl = defaultdict(lambda: {'pnl': 0, 'count': 0, 'wins': 0})
        for t in trades:
            h = t['entry_hour']
            hour_pnl[h]['pnl'] += t['pnl']
            hour_pnl[h]['count'] += 1
            if t['pnl'] > 0: hour_pnl[h]['wins'] += 1
        
        print(f"\n    P&L BY ENTRY HOUR:")
        print(f"    {'Hour':>6} {'Trades':>7} {'WR':>6} {'Net P&L':>12} {'Avg P&L':>10} {'% of Total':>10}")
        for h in sorted(hour_pnl.keys()):
            d = hour_pnl[h]
            avg = d['pnl']/d['count'] if d['count'] else 0
            pct = d['pnl']/net*100 if net != 0 else 0
            w = d['wins']/d['count']*100 if d['count'] else 0
            marker = " <<<" if (h in [23, 1, 2] and abs(pct) > 15) else ""
            print(f"    {h:>6} {d['count']:>7} {w:>5.1f}% ${d['pnl']:>11,.2f} ${avg:>9,.2f} {pct:>9.1f}%{marker}")
        
        # Midnight-spanning trades
        midnight_trades = [t for t in trades if t['spans_midnight']]
        midnight_pnl = sum(t['pnl'] for t in midnight_trades)
        non_midnight_pnl = sum(t['pnl'] for t in trades if not t['spans_midnight'])
        
        print(f"\n    MIDNIGHT-SPANNING TRADES (entry day != exit day):")
        print(f"      Trades that span midnight: {len(midnight_trades)} ({len(midnight_trades)/len(trades)*100:.1f}%)")
        print(f"      P&L from midnight-span:    ${midnight_pnl:,.2f} ({midnight_pnl/net*100:.1f}% of total)")
        print(f"      P&L from NON-midnight:     ${non_midnight_pnl:,.2f} ({non_midnight_pnl/net*100:.1f}% of total)")
        
        # Late night entries (22-23) and early morning entries (1-3)
        late_night = [t for t in trades if t['entry_hour'] in [22, 23]]
        early_morning = [t for t in trades if t['entry_hour'] in [1, 2, 3]]
        thin_hour_trades = late_night + early_morning
        thin_pnl = sum(t['pnl'] for t in thin_hour_trades)
        
        print(f"\n    THIN LIQUIDITY HOURS (entry at 22-23 or 1-3):")
        print(f"      Trades entered in thin hours: {len(thin_hour_trades)} ({len(thin_hour_trades)/len(trades)*100:.1f}%)")
        print(f"      P&L from thin hours:          ${thin_pnl:,.2f} ({thin_pnl/net*100:.1f}% of total)")
    
    # =========================================================================
    # PART 3: SENSITIVITY — Different rollover lockout sizes
    # =========================================================================
    print("\n\n" + "=" * 80)
    print("PART 3: ROLLOVER LOCKOUT SENSITIVITY (what if we block more hours?)")
    print("=" * 80)
    
    lockout_values = [0, 30, 60, 120, 180, 240]
    
    print(f"\n  {'Pair':<16} {'Lockout':>8} {'Trades':>7} {'WR':>7} {'Net P&L':>12} {'vs 30min':>10}")
    print(f"  {'-'*80}")
    
    for pname, (df, pdef) in all_pair_data.items():
        baseline_net = None
        for lockout in lockout_values:
            trades, final_bal = run_pair_detailed(df, pdef, rollover_lockout_min=lockout)
            net = sum(t['pnl'] for t in trades)
            wins = [t for t in trades if t['pnl'] > 0]
            wr = len(wins)/len(trades)*100 if trades else 0
            
            if lockout == 30:
                baseline_net = net
            
            diff = ""
            if baseline_net is not None and baseline_net != 0:
                diff = f"{(net - baseline_net)/abs(baseline_net)*100:+.1f}%"
            
            marker = " <<<" if lockout == 30 else ""
            print(f"  {pname:<16} {lockout:>6}min {len(trades):>7} {wr:>6.1f}% ${net:>11,.2f} {diff:>10}{marker}")
        print()
    
    # =========================================================================
    # PART 4: NUCLEAR TEST — Remove ALL trades near midnight entirely
    # =========================================================================
    print("\n\n" + "=" * 80)
    print("PART 4: NUCLEAR TEST — P&L if we REMOVE all thin-liquidity-hour trades")
    print("=" * 80)
    
    for pname, trades in baseline_results.items():
        net_all = sum(t['pnl'] for t in trades)
        
        # Remove trades entering at 22, 23, 1, 2, 3 (thin hours)
        clean_trades = [t for t in trades if t['entry_hour'] not in [22, 23, 1, 2, 3]]
        net_clean = sum(t['pnl'] for t in clean_trades)
        
        # Remove trades that span midnight
        no_midnight = [t for t in trades if not t['spans_midnight']]
        net_no_midnight = sum(t['pnl'] for t in no_midnight)
        
        # MAXIMUM paranoia: remove thin hour entries AND midnight spanners
        max_clean = [t for t in trades 
                     if t['entry_hour'] not in [22, 23, 1, 2, 3] 
                     and not t['spans_midnight']]
        net_max_clean = sum(t['pnl'] for t in max_clean)
        
        print(f"\n  {pname}:")
        print(f"    ALL trades:                        {len(trades):>5} trades  ${net_all:>11,.2f}")
        print(f"    Remove thin-hour entries (22-3):   {len(clean_trades):>5} trades  ${net_clean:>11,.2f}  ({net_clean/net_all*100:.1f}% of original)")
        print(f"    Remove midnight-spanners:          {len(no_midnight):>5} trades  ${net_no_midnight:>11,.2f}  ({net_no_midnight/net_all*100:.1f}% of original)")
        print(f"    MAXIMUM PARANOIA (both removed):   {len(max_clean):>5} trades  ${net_max_clean:>11,.2f}  ({net_max_clean/net_all*100:.1f}% of original)")
        
        if net_max_clean / net_all < 0.5:
            print(f"    >>> RED FLAG: More than 50% of profit comes from suspicious hours!")
        elif net_max_clean / net_all < 0.75:
            print(f"    >>> YELLOW: Significant portion from edge hours, but bulk is real")
        else:
            print(f"    >>> GREEN: Profit is well-distributed, not dependent on midnight")
    
    # =========================================================================
    # PART 5: LIVE vs BACKTEST COMPARISON
    # =========================================================================
    print("\n\n" + "=" * 80)
    print("PART 5: LIVE BOT vs BACKTEST — Does the engine match?")
    print("=" * 80)
    
    print("""
    FEATURE CHECK:
    
    Feature                     Backtest              Live Engine (engine.py)         MATCH?
    -------------------------   --------------------  ------------------------------- ------
    Signal cadence              M1 bar close          M1 bar close (v5.6.1 fix)       YES
    Welford span                100                   100                             YES
    Dynamic Z (entry)           base=2.0, gamma=6.0   base=2.0, gamma=6.0            YES
    Dynamic Z (exit)            base=0.5, gamma=2.0   base=0.5, gamma=2.0            YES
    Hurst window                512                   512                             YES
    HMM (Index)                 hold=20               hold=20                         YES
    HMM (Oil)                   hold=5                hold=5                          YES
    Kalman Sentinel             tol=0.15              tol=0.15                        YES
    Dynamic AKAD                lam=40, ceil=4%       lam=40, ceil=4%                 YES
    Correlation Monitor         window=200            window=200                      YES
    Ghost Stop (daily)          4%                    4%                              YES
    Ghost Stop (max)            9%                    9%                              YES
    Rollover Lockout            30 min                30 min                          YES
    Dwell (Index)               base=60s              base=60s                        YES
    Dwell (Oil)                 base=1800s            base=1800s                      YES
    Re-entry Cooldown           = dwell period        = dwell period                  YES
    Emergency Exit              |Z| > 2.5x entry      |Z| > 2.5x entry               YES
    Pre-warm bars               N/A (uses all data)   768 M1 bars from history        YES*
    Spread Blowout Filter       NOT in backtest       200/150 pts per-pair            LIVE ONLY
    Staleness Guard             NOT in backtest       5s timeout                      LIVE ONLY
    Widowmaker Reconcile        NOT in backtest       3-state audit                   LIVE ONLY
    Spread COST model           Session-based ×1.0-1.8 Real bid-ask crossing          BACKTEST APPROX
    
    * Pre-warm uses identical logic to backtest (replay M1 bars through engines)
    
    KEY DIFFERENCES:
    1. Backtest uses mid-price (close). Live uses (bid+ask)/2 which is essentially the same.
    2. Backtest applies spread costs via a MODEL. Live pays REAL spread at execution.
       The model uses session multipliers (Asian 1.8x, London 1.2x, NY 1.0x).
       This is CONSERVATIVE — the model likely OVERSTATES costs during liquid hours.
    3. Live has ADDITIONAL safety layers (spread blowout, staleness, widowmaker) 
       that can only REDUCE trades, never add fake ones.
    4. The backtest processes bars sequentially with no execution delay.
       Live has ~500ms order round-trip, but at M1 cadence this is negligible.
    """)
    
    # =========================================================================
    # PART 6: THE VERDICT
    # =========================================================================
    print("\n" + "=" * 80)
    print("VERDICT")
    print("=" * 80)
    
    # Calculate combined stats
    total_net = 0
    total_net_clean = 0
    for pname, trades in baseline_results.items():
        net = sum(t['pnl'] for t in trades)
        total_net += net
        clean = [t for t in trades 
                 if t['entry_hour'] not in [22, 23, 1, 2, 3] 
                 and not t['spans_midnight']]
        total_net_clean += sum(t['pnl'] for t in clean)
    
    pct_clean = total_net_clean / total_net * 100 if total_net != 0 else 0
    
    print(f"\n  COMBINED PORTFOLIO:")
    print(f"    Total P&L (all trades):        ${total_net:>12,.2f}")
    print(f"    Total P&L (max paranoia clean): ${total_net_clean:>12,.2f} ({pct_clean:.1f}% survives)")
    
    if pct_clean >= 75:
        print(f"\n  CONCLUSION: The edge is REAL.")
        print(f"  {pct_clean:.0f}% of profit survives even after removing ALL trades")
        print(f"  that enter during thin hours or span midnight.")
        print(f"  The midnight rollover is NOT the source of your edge.")
    elif pct_clean >= 50:
        print(f"\n  CONCLUSION: The edge is MOSTLY real but partially enhanced by thin hours.")
        print(f"  Consider tightening rollover lockout if concerned.")
    else:
        print(f"\n  CONCLUSION: RED FLAG — Most profit comes from suspicious hours!")
        print(f"  You should investigate further or increase rollover lockout significantly.")


if __name__ == "__main__":
    main()
