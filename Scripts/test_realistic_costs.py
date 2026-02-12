#!/usr/bin/env python3
"""
SHF v5.6 — REALISTIC COST COMPARISON TEST
================================================================
Runs the EXACT faithful backtest 3 ways:
  A) Original (no costs) — baseline
  B) With realistic commissions + spread costs (24/5)
  C) With realistic costs + European/US session only (07:00-21:00 broker)

Also produces hourly P&L breakdown to identify profitable vs losing hours.

Costs modeled:
  - Bid-ask spread: 4 fills per trade (enter A, enter B, exit A, exit B)
  - Commission: $4/lot round-trip (standard 5%ers)
  - Time-of-day spread multiplier: Asian = 1.8x wider, London/NY = 1.0x
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
ROLLOVER_LOCKOUT_MIN = 30  # Updated to match live bot (was 5)
HMM_N_REGIMES = 3; HMM_LOOKBACK = 100
MAX_CONSEC_LOSSES = 5; COOLDOWN_BARS = 60
MIN_WARMUP_BARS = 200; STARTING_BALANCE = 100_000.0

# ============================================================================
# COST MODEL — Realistic 5%ers costs
# ============================================================================
# Per-pair spread cost per lot per FILL (crossing the bid-ask once)
# Total trade = 4 fills (enter A + enter B + exit A + exit B)
# We test THREE cost levels to find breakeven
COST_LEVELS = {
    "TIGHT (Prop Firm Typical)": {
        "Index Spread":  (1.00, 1.00),    # NAS100 ~1.0pt=$1.00, DAX40 ~1.0pt=$1.00
        "Forex Anchor":  (5.00, 7.00),    # AUDUSD ~0.5pip=$5, NZDUSD ~0.7pip=$7
        "EURJPY/CHFJPY": (6.25, 9.38),    # EURJPY ~1.0pip=$6.25, CHFJPY ~1.5pip=$9.38
    },
    "MEDIUM (Conservative)": {
        "Index Spread":  (1.50, 1.50),    # NAS100 ~1.5pt=$1.50, DAX40 ~1.5pt=$1.50
        "Forex Anchor":  (8.00, 10.0),    # AUDUSD ~0.8pip=$8, NZDUSD ~1.0pip=$10
        "EURJPY/CHFJPY": (9.38, 12.5),    # EURJPY ~1.5pip=$9.38, CHFJPY ~2.0pip=$12.50
    },
    "WIDE (Worst Case)": {
        "Index Spread":  (1.50, 1.50),    # NAS100 ~1.5pt=$1.50, DAX40 ~1.5pt=$1.50
        "Forex Anchor":  (12.0, 15.0),    # AUDUSD ~1.2pip=$12, NZDUSD ~1.5pip=$15
        "EURJPY/CHFJPY": (13.0, 16.0),    # EURJPY ~2.0pip=$13, CHFJPY ~2.5pip=$16
    },
}
SPREAD_COSTS = COST_LEVELS["TIGHT (Prop Firm Typical)"]  # Default for functions
COMMISSION_PER_LOT_RT = 4.0  # $4/lot round-trip (confirmed from 5%ers trades)

def get_spread_multiplier(hour_broker):
    """
    Time-of-day spread multiplier (broker time).
    Asian session has wider spreads due to low liquidity.
    """
    if 0 <= hour_broker < 7:      # 00:00-07:00 Asian/late night
        return 1.8
    elif 7 <= hour_broker < 9:    # 07:00-09:00 London open (transitional)
        return 1.2
    elif 9 <= hour_broker < 17:   # 09:00-17:00 London + NY overlap (tightest)
        return 1.0
    elif 17 <= hour_broker < 21:  # 17:00-21:00 NY afternoon
        return 1.1
    else:                          # 21:00-00:00 Late session
        return 1.5

def calc_trade_cost(pair_name, lots, hour_broker):
    """
    Calculate total transaction cost for a spread trade.
    Returns cost in USD (always positive = money you lose).
    """
    cost_a, cost_b = SPREAD_COSTS.get(pair_name, (10.0, 10.0))
    spread_mult = get_spread_multiplier(hour_broker)
    
    # 4 fills: enter A + exit A + enter B + exit B
    spread_cost = (cost_a * 2 + cost_b * 2) * lots * spread_mult
    commission = COMMISSION_PER_LOT_RT * lots
    
    return spread_cost + commission

# ============================================================================
# PAIR DEFINITIONS
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

def is_session_blocked(t, session_start=7, session_end=21):
    """Block entries outside trading session (broker time hours)."""
    return t.hour < session_start or t.hour >= session_end

def load_pair(p):
    d = Path(__file__).resolve().parent.parent / "data" / "historical"
    a = pd.read_csv(d / p.file_a, parse_dates=['time']).rename(columns={'close':'close_a'})
    b = pd.read_csv(d / p.file_b, parse_dates=['time']).rename(columns={'close':'close_b'})
    m = pd.merge(a[['time','close_a']], b[['time','close_b']], on='time', how='inner').sort_values('time').reset_index(drop=True)
    return m[(m['close_a']>0)&(m['close_b']>0)].reset_index(drop=True)

# ============================================================================
# SINGLE PAIR SIMULATION
# ============================================================================

def run_single_pair(df, pdef, apply_costs=False, session_filter=False):
    """
    Run full live-bot simulation on a single pair.
    
    apply_costs: if True, subtract realistic spread + commission from each trade
    session_filter: if True, block entries outside 07:00-21:00 broker time
    """
    balance = STARTING_BALANCE; peak = STARTING_BALANCE; daily_start = STARTING_BALANCE
    daily_date = None; ghost = False; ghost_info = ""
    dakad = DynamicAKAD()
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec = 0; gcool = 0
    trades = []; hmm_blocks = 0; rollover_blocks = 0; dwell_blocks = 0
    sentinel_exits = 0; session_blocks = 0; total_costs = 0.0
    n = len(df)

    eng = shf_core.CointegrationEngine(span=WELFORD_SPAN, beta=1.0, entry_z=Z_BASE,
        exit_z=EXIT_Z_BASE, z_base=Z_BASE, gamma=GAMMA, hurst_window=HURST_WINDOW,
        dynamic_z=True, exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
    hmm = HMMRegimeDetector(lookback=HMM_LOOKBACK, min_regime_hold=pdef.hmm_hold)

    pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0; erisk = 0.0
    lspread = 0.0; pspread = 0.0; sent_abort = False
    last_close_bar = -9999; last_close_h = 0.5
    entry_hour = 0  # Track entry hour for cost calculation

    # Hourly tracking
    hourly_pnl = {h: 0.0 for h in range(24)}
    hourly_trades = {h: 0 for h in range(24)}
    hourly_wins = {h: 0 for h in range(24)}

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

        pidx = min(pdef.pair_index, 2)
        if pspread != 0.0: corr.push_return(pidx, spread - pspread)

        la = math.log(pa) if pa>0 else 0; lb = math.log(pb) if pb>0 else 0
        beta, abort = sen.update(la, lb)
        if abort and not sent_abort:
            sent_abort = True
            if pos != 0:
                gross_pnl = (spread-es)*pos*elots*pdef.notional
                cost = calc_trade_cost(pdef.name, elots, entry_hour) if apply_costs else 0.0
                pnl = gross_pnl - cost; total_costs += cost
                balance += pnl; peak = max(peak,balance)
                w = pnl>0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                month_key = bt.strftime('%Y-%m') if hasattr(bt,'strftime') else 'Unknown'
                trades.append({'pnl':pnl,'gross':gross_pnl,'cost':cost,'reason':'SENTINEL',
                              'bar':bar,'time':bt,'month':month_key,'hour':entry_hour})
                hourly_pnl[entry_hour] += pnl; hourly_trades[entry_hour] += 1
                if w: hourly_wins[entry_hour] += 1
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
            if session_filter and is_session_blocked(bt):
                session_blocks += 1; continue
            if last_close_bar >= 0:
                cb = calc_dwell(last_close_h) / 60.0
                if (bar - last_close_bar) < cb: dwell_blocks += 1; continue

            risk = dakad.calc(cdd, ddd)
            corr.compute_risk(); cm = corr.last_risk_multiplier
            final_risk = risk * cm
            lots = max(0.01, round(balance * final_risk / 1000.0, 2))
            pos = s; ez = z; es = spread; ebar = bar; elots = lots; erisk = final_risk
            entry_hour = bt.hour

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
                gross_pnl = (spread-es)*pos*elots*pdef.notional
                cost = calc_trade_cost(pdef.name, elots, entry_hour) if apply_costs else 0.0
                pnl = gross_pnl - cost; total_costs += cost
                balance += pnl; peak = max(peak,balance)
                w = pnl>0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                month_key = bt.strftime('%Y-%m') if hasattr(bt,'strftime') else 'Unknown'
                trades.append({'pnl':pnl,'gross':gross_pnl,'cost':cost,'reason':reason,
                              'bar':bar,'hold':bar-ebar,'time':bt,'month':month_key,'hour':entry_hour})
                hourly_pnl[entry_hour] += pnl; hourly_trades[entry_hour] += 1
                if w: hourly_wins[entry_hour] += 1
                pos=0; last_close_bar=bar; last_close_h=h

    # Compute results
    total = len(trades)
    if total == 0:
        return {'trades':0,'wr':0,'pf':0,'net_pnl':0,'return_pct':0,'max_dd_pct':0,
                'total_costs':0,'hourly_pnl':hourly_pnl,'hourly_trades':hourly_trades,
                'hourly_wins':hourly_wins,'session_blocks':session_blocks,
                'ghost':ghost,'ghost_info':ghost_info,'monthly':{}}

    pnls = [t['pnl'] for t in trades]
    gross_pnls = [t['gross'] for t in trades]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    gp = sum(wins) if wins else 0; gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins)/total*100; pf = gp/gl if gl>0 else 0

    gross_wins = [p for p in gross_pnls if p > 0]
    gross_wr = len(gross_wins)/total*100

    eq_peak = STARTING_BALANCE; mdd = 0; eq = STARTING_BALANCE
    for t in trades:
        eq += t['pnl']; eq_peak = max(eq_peak, eq); mdd = max(mdd, eq_peak - eq)

    holds = [t.get('hold', 0) for t in trades if 'hold' in t]
    avg_hold = np.mean(holds) if holds else 0

    monthly_data = {}
    for t in trades:
        mk = t.get('month', 'Unknown')
        if mk not in monthly_data: monthly_data[mk] = {'trades':0,'wins':0,'pnl':0.0,'costs':0.0,'gross':0.0}
        monthly_data[mk]['trades'] += 1
        if t['pnl'] > 0: monthly_data[mk]['wins'] += 1
        monthly_data[mk]['pnl'] += t['pnl']
        monthly_data[mk]['costs'] += t.get('cost', 0.0)
        monthly_data[mk]['gross'] += t.get('gross', 0.0)

    first_time = df['time'].iloc[0]; last_time = df['time'].iloc[-1]
    days = (last_time - first_time).days
    months_span = days / 30.0
    trades_per_month = total / months_span if months_span > 0 else 0

    return {
        'pair': pdef.name, 'hmm_hold': pdef.hmm_hold,
        'trades': total, 'wr': round(wr,1), 'gross_wr': round(gross_wr,1),
        'pf': round(pf,2),
        'net_pnl': round(balance-STARTING_BALANCE,2),
        'gross_pnl': round(sum(gross_pnls),2),
        'total_costs': round(total_costs,2),
        'return_pct': round((balance-STARTING_BALANCE)/STARTING_BALANCE*100,2),
        'max_dd_pct': round(mdd/STARTING_BALANCE*100,2),
        'avg_win': round(np.mean(wins),2) if wins else 0,
        'avg_loss': round(np.mean(losses),2) if losses else 0,
        'avg_hold': round(avg_hold,1),
        'hmm_blocks': hmm_blocks, 'ghost': ghost, 'ghost_info': ghost_info,
        'rollover_blocks': rollover_blocks, 'dwell_blocks': dwell_blocks,
        'session_blocks': session_blocks, 'sentinel_exits': sentinel_exits,
        'monthly': monthly_data, 'trades_per_month': round(trades_per_month,1),
        'hourly_pnl': hourly_pnl, 'hourly_trades': hourly_trades,
        'hourly_wins': hourly_wins,
        'days': days,
    }


def print_scenario_results(scenario_name, results, combined_pnl, combined_trades, combined_costs):
    """Print formatted results for a scenario."""
    print(f"\n  {'Pair':<20} {'Trades':>7} {'Tr/Mo':>6} {'WR':>7} {'GrossWR':>8} {'PF':>7} {'GrossP&L':>12} {'Costs':>10} {'NetP&L':>12} {'Return':>8} {'MaxDD':>7}")
    print(f"  {'-'*120}")
    for pair_name, r in results.items():
        print(f"  {pair_name:<20} {r['trades']:>7} {r['trades_per_month']:>5.0f} "
              f"{r['wr']:>6.1f}% {r['gross_wr']:>7.1f}% {r['pf']:>7.2f} "
              f"${r['gross_pnl']:>11,.2f} ${r['total_costs']:>9,.2f} "
              f"${r['net_pnl']:>11,.2f} {r['return_pct']:>7.2f}% {r['max_dd_pct']:>6.2f}%")
    print(f"  {'-'*120}")
    print(f"  {'COMBINED':<20} {combined_trades:>7} {'':>6} {'':>7} {'':>8} {'':>7} "
          f"{'':>12} ${combined_costs:>9,.2f} ${combined_pnl:>11,.2f} "
          f"{combined_pnl/STARTING_BALANCE*100:>7.2f}%")


def main():
    print("="*120)
    print("SHF v5.6 — REALISTIC COST COMPARISON TEST")
    print(f"shf_core version: {shf_core.__version__}")
    print("="*120)
    print(f"  Starting balance: ${STARTING_BALANCE:,.0f}")
    print(f"  Rollover lockout: {ROLLOVER_LOCKOUT_MIN} min (matches live bot)")
    print(f"\n  Cost Model:")
    for name, (ca, cb) in SPREAD_COSTS.items():
        rt = (ca*2 + cb*2) + COMMISSION_PER_LOT_RT
        print(f"    {name:<20} Spread: ${ca:.1f}+${cb:.1f}/fill  Commission: ${COMMISSION_PER_LOT_RT:.0f}/lot RT  Total: ${rt:.0f}/lot/trade")
    print(f"\n  Spread Multipliers (broker time):")
    print(f"    00:00-07:00 (Asian):     1.8x")
    print(f"    07:00-09:00 (LDN open):  1.2x")
    print(f"    09:00-17:00 (LDN+NY):    1.0x")
    print(f"    17:00-21:00 (NY PM):     1.1x")
    print(f"    21:00-00:00 (Late):      1.5x")

    # Load data
    pair_data = {}
    for p in HOLY_TRIO:
        try:
            df = load_pair(p)
            pair_data[p.name] = (df, p)
            first = df['time'].iloc[0]; last = df['time'].iloc[-1]
            days = (last - first).days
            print(f"\n  {p.name:<20} {len(df):>8,} bars | {first} to {last} ({days}d)")
        except Exception as e:
            print(f"\n  {p.name:<20} FAILED: {e}")

    # =========================================================================
    # SCENARIO A: No costs (baseline — original backtest)
    # =========================================================================
    print(f"\n\n{'='*120}")
    print("SCENARIO A: NO COSTS (Original Baseline)")
    print(f"{'='*120}")

    results_a = {}; pnl_a = 0; trades_a = 0; costs_a = 0
    for pair_name, (df, pdef) in pair_data.items():
        r = run_single_pair(df, pdef, apply_costs=False, session_filter=False)
        results_a[pair_name] = r
        pnl_a += r['net_pnl']; trades_a += r['trades']
    print_scenario_results("A: No Costs", results_a, pnl_a, trades_a, 0)

    # =========================================================================
    # SCENARIO B: Realistic costs, 24/5 trading
    # =========================================================================
    print(f"\n\n{'='*120}")
    print("SCENARIO B: REALISTIC COSTS — 24/5 (All Hours)")
    print(f"{'='*120}")

    results_b = {}; pnl_b = 0; trades_b = 0; costs_b = 0
    for pair_name, (df, pdef) in pair_data.items():
        r = run_single_pair(df, pdef, apply_costs=True, session_filter=False)
        results_b[pair_name] = r
        pnl_b += r['net_pnl']; trades_b += r['trades']; costs_b += r['total_costs']
    print_scenario_results("B: Costs 24/5", results_b, pnl_b, trades_b, costs_b)

    # =========================================================================
    # SCENARIO C: Realistic costs + European/US session only (07:00-21:00)
    # =========================================================================
    print(f"\n\n{'='*120}")
    print("SCENARIO C: REALISTIC COSTS — SESSION FILTER (07:00-21:00 Broker Time Only)")
    print(f"{'='*120}")

    results_c = {}; pnl_c = 0; trades_c = 0; costs_c = 0
    for pair_name, (df, pdef) in pair_data.items():
        r = run_single_pair(df, pdef, apply_costs=True, session_filter=True)
        results_c[pair_name] = r
        pnl_c += r['net_pnl']; trades_c += r['trades']; costs_c += r['total_costs']
        sb = r['session_blocks']
        print(f"  {pair_name}: {sb} entries blocked by session filter")
    print_scenario_results("C: Costs + Session", results_c, pnl_c, trades_c, costs_c)

    # =========================================================================
    # COMPARISON TABLE
    # =========================================================================
    print(f"\n\n{'='*120}")
    print("SCENARIO COMPARISON")
    print(f"{'='*120}")
    print(f"\n  {'Scenario':<45} {'Trades':>7} {'Gross P&L':>12} {'Costs':>10} {'Net P&L':>12} {'Return':>8} {'Net/Trade':>10}")
    print(f"  {'-'*110}")

    net_per_a = pnl_a / trades_a if trades_a > 0 else 0
    net_per_b = pnl_b / trades_b if trades_b > 0 else 0
    net_per_c = pnl_c / trades_c if trades_c > 0 else 0

    gross_a = sum(r['gross_pnl'] for r in results_a.values())
    gross_b = sum(r['gross_pnl'] for r in results_b.values())
    gross_c = sum(r['gross_pnl'] for r in results_c.values())

    print(f"  {'A: No Costs (baseline)':<45} {trades_a:>7} ${gross_a:>11,.2f} ${'0':>9} ${pnl_a:>11,.2f} {pnl_a/STARTING_BALANCE*100:>7.2f}% ${net_per_a:>9.2f}")
    print(f"  {'B: Realistic Costs (24/5)':<45} {trades_b:>7} ${gross_b:>11,.2f} ${costs_b:>9,.2f} ${pnl_b:>11,.2f} {pnl_b/STARTING_BALANCE*100:>7.2f}% ${net_per_b:>9.2f}")
    print(f"  {'C: Realistic Costs + Session (07-21)':<45} {trades_c:>7} ${gross_c:>11,.2f} ${costs_c:>9,.2f} ${pnl_c:>11,.2f} {pnl_c/STARTING_BALANCE*100:>7.2f}% ${net_per_c:>9.2f}")

    cost_drag = pnl_a - pnl_b if trades_b > 0 else 0
    session_gain = pnl_c - pnl_b if trades_c > 0 else 0
    print(f"\n  Cost drag (A vs B):           ${cost_drag:>11,.2f} ({cost_drag/max(abs(pnl_a),1)*100:.1f}% of gross)")
    print(f"  Session filter benefit (C vs B): ${session_gain:>11,.2f}")

    # =========================================================================
    # HOURLY P&L BREAKDOWN (from Scenario B — costs, all hours)
    # =========================================================================
    print(f"\n\n{'='*120}")
    print("HOURLY BREAKDOWN — Scenario B (Costs, All Hours)")
    print("(Broker time — what hour trades were ENTERED)")
    print(f"{'='*120}")
    
    # Aggregate hourly data across all pairs
    total_hourly_pnl = {h: 0.0 for h in range(24)}
    total_hourly_trades = {h: 0 for h in range(24)}
    total_hourly_wins = {h: 0 for h in range(24)}
    
    for r in results_b.values():
        for h in range(24):
            total_hourly_pnl[h] += r['hourly_pnl'].get(h, 0)
            total_hourly_trades[h] += r['hourly_trades'].get(h, 0)
            total_hourly_wins[h] += r['hourly_wins'].get(h, 0)
    
    print(f"\n  {'Hour':>6} {'Session':<12} {'Trades':>7} {'WR':>7} {'Net P&L':>12} {'$/Trade':>10} {'Bar'}")
    print(f"  {'-'*80}")
    
    for h in range(24):
        t = total_hourly_trades[h]
        if t == 0: continue
        p = total_hourly_pnl[h]
        w = total_hourly_wins[h]
        wr = w/t*100 if t > 0 else 0
        per = p/t if t > 0 else 0
        
        if 0 <= h < 7: sess = "Asian"
        elif 7 <= h < 9: sess = "LDN Open"
        elif 9 <= h < 17: sess = "LDN+NY"
        elif 17 <= h < 21: sess = "NY PM"
        else: sess = "Late"
        
        bar_len = int(abs(per) / 5)  # Scale: $5 per character
        bar_char = "+" if per > 0 else "-"
        bar = bar_char * min(bar_len, 40)
        
        color_indicator = ">>>" if per > 0 else "<<<" if per < -10 else "   "
        print(f"  {h:02d}:00  {sess:<12} {t:>7} {wr:>6.1f}% ${p:>11,.2f} ${per:>9.2f} {color_indicator} {bar}")

    # Summarize sessions
    asian_pnl = sum(total_hourly_pnl[h] for h in range(0, 7))
    asian_trades = sum(total_hourly_trades[h] for h in range(0, 7))
    euro_pnl = sum(total_hourly_pnl[h] for h in range(7, 21))
    euro_trades = sum(total_hourly_trades[h] for h in range(7, 21))
    late_pnl = sum(total_hourly_pnl[h] for h in range(21, 24))
    late_trades = sum(total_hourly_trades[h] for h in range(21, 24))

    print(f"\n  SESSION SUMMARY:")
    print(f"    Asian  (00-07): {asian_trades:>5} trades  ${asian_pnl:>11,.2f}  (${asian_pnl/max(asian_trades,1):>7.2f}/trade)")
    print(f"    Euro/NY(07-21): {euro_trades:>5} trades  ${euro_pnl:>11,.2f}  (${euro_pnl/max(euro_trades,1):>7.2f}/trade)")
    print(f"    Late   (21-24): {late_trades:>5} trades  ${late_pnl:>11,.2f}  (${late_pnl/max(late_trades,1):>7.2f}/trade)")

    # =========================================================================
    # MONTHLY COMPARISON
    # =========================================================================
    print(f"\n\n{'='*120}")
    print("MONTHLY P&L COMPARISON")
    print(f"{'='*120}")

    all_months_a = {}; all_months_b = {}; all_months_c = {}
    for r in results_a.values():
        for mk, md in r.get('monthly', {}).items():
            if mk not in all_months_a: all_months_a[mk] = 0.0
            all_months_a[mk] += md['pnl']
    for r in results_b.values():
        for mk, md in r.get('monthly', {}).items():
            if mk not in all_months_b: all_months_b[mk] = 0.0
            all_months_b[mk] += md['pnl']
    for r in results_c.values():
        for mk, md in r.get('monthly', {}).items():
            if mk not in all_months_c: all_months_c[mk] = 0.0
            all_months_c[mk] += md['pnl']

    all_keys = sorted(set(list(all_months_a.keys()) + list(all_months_b.keys()) + list(all_months_c.keys())))
    print(f"\n  {'Month':<10} {'A: No Cost':>12} {'B: With Cost':>12} {'C: Session':>12} {'B-A (drag)':>12} {'C-B (gain)':>12}")
    print(f"  {'-'*75}")
    for mk in all_keys:
        va = all_months_a.get(mk, 0); vb = all_months_b.get(mk, 0); vc = all_months_c.get(mk, 0)
        print(f"  {mk:<10} ${va:>11,.2f} ${vb:>11,.2f} ${vc:>11,.2f} ${vb-va:>11,.2f} ${vc-vb:>11,.2f}")

    # =========================================================================
    # VERDICT
    # =========================================================================
    print(f"\n\n{'='*120}")
    print("VERDICT")
    print(f"{'='*120}")
    
    if pnl_c > pnl_b and pnl_c > 0:
        print(f"\n  >>> SESSION FILTER RECOMMENDED: +${pnl_c - pnl_b:,.2f} improvement over 24/5")
        print(f"      {trades_b - trades_c} fewer trades, but ${pnl_c/max(trades_c,1):.2f}/trade vs ${pnl_b/max(trades_b,1):.2f}/trade")
    elif pnl_b > 0 and pnl_b > pnl_c:
        print(f"\n  >>> 24/5 TRADING IS BETTER: Asian session trades are net positive")
        print(f"      Session filter would LOSE ${pnl_b - pnl_c:,.2f}")
    elif pnl_b <= 0 and pnl_c > 0:
        print(f"\n  >>> SESSION FILTER CRITICAL: Without it, costs eat ALL profit")
        print(f"      24/5 = ${pnl_b:,.2f} | Session filter = ${pnl_c:,.2f}")
    elif pnl_b <= 0 and pnl_c <= 0:
        print(f"\n  >>> WARNING: Costs eat all profit in BOTH scenarios")
        print(f"      Need to reduce costs or increase edge")
    
    # Save
    save_data = {
        'config': 'Realistic cost comparison — 3 scenarios',
        'scenarios': {
            'A_no_costs': {'trades': trades_a, 'net_pnl': pnl_a, 'costs': 0},
            'B_with_costs_24_5': {'trades': trades_b, 'net_pnl': pnl_b, 'costs': costs_b},
            'C_costs_session_filter': {'trades': trades_c, 'net_pnl': pnl_c, 'costs': costs_c},
        },
        'hourly_pnl': {str(h): round(total_hourly_pnl[h], 2) for h in range(24)},
        'hourly_trades': {str(h): total_hourly_trades[h] for h in range(24)},
    }
    out = Path("Results/realistic_cost_comparison.json")
    with open(out, 'w') as f:
        json.dump(save_data, f, indent=2)
    print(f"\n  Saved to {out}")


if __name__ == "__main__":
    main()
