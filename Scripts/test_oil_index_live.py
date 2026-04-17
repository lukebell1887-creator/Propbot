#!/usr/bin/env python3
"""
SHF v5.6 — OIL + INDEX LIVE ENVIRONMENT TEST
================================================================
Tests the 2 profitable pairs with EXACT live bot logic + REAL costs.
Oil gets per-pair raised dwell (1800 base) to eliminate bid-ask bounce.
Index keeps standard dwell (60 base).

Costs (real broker specs):
  Index (US100/DE40):  $1+$1 spread/fill, ZERO commission
  Oil (XTIUSD/XBRUSD): $4+$5 spread/fill, 0.03% commission

HMM sweep: 100, 20, 10, 5
All dynamic features: AKAD, Dynamic Z, Dynamic Exit Z, HMM, Kalman,
                      Correlation Monitor, Ghost Stop, Rollover Lockout,
                      Dynamic Dwell (per-pair base), Re-entry Cooldown
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
ROLLOVER_LOCKOUT_MIN = 30
HMM_N_REGIMES = 3; HMM_LOOKBACK = 100
MAX_CONSEC_LOSSES = 5; COOLDOWN_BARS = 60
MIN_WARMUP_BARS = 200; STARTING_BALANCE = 100_000.0

# ============================================================================
# PER-PAIR DWELL CONFIG
# ============================================================================
# Index: standard dwell (fast mean-reversion, proven at 2-bar holds)
INDEX_DWELL_BASE = 60.0;   INDEX_DWELL_ANCHOR = 0.3
INDEX_DWELL_MIN = 30.0;    INDEX_DWELL_MAX = 300.0

# Oil: raised dwell (eliminate bid-ask bounce, let real reversion play out)
OIL_DWELL_BASE = 1800.0;   OIL_DWELL_ANCHOR = 0.3
OIL_DWELL_MIN = 900.0;     OIL_DWELL_MAX = 9000.0

# ============================================================================
# COST MODEL — Real broker specs (5%ers)
# ============================================================================
@dataclass
class PairCost:
    spread_a_per_fill: float
    spread_b_per_fill: float
    commission_rt: float
    commission_pct: float

PAIR_COSTS = {
    "Index Spread": PairCost(1.0, 1.0, 0.0, 0.0),      # ZERO commission on NAS/DAX
    "Oil Spread":   PairCost(4.0, 5.0, 0.0, 0.0003),    # 0.03% commission
}

OIL_NOTIONAL_PER_LOT = 6500.0  # 100 barrels × ~$65

# ============================================================================
# SWAP COSTS — 5%ers actual specs
# ============================================================================
# Contract size = 100 barrels, point = 0.001
# Swap in $ per lot per night = swap_points × point_size × contract_size
# XTIUSD: long=-70pts, short=-40pts → long=-$7.00/lot, short=-$4.00/lot
# XBRUSD: long=-70pts, short=-40pts → long=-$7.00/lot, short=-$4.00/lot
# Both legs always negative! Pair always pays swap on BOTH sides.
# For LONG spread (buy XTIUSD + sell XBRUSD): $7.00 + $4.00 = $11.00/lot/night
# For SHORT spread (sell XTIUSD + buy XBRUSD): $4.00 + $7.00 = $11.00/lot/night
# Friday night = 10× (covers Saturday + Sunday)
# Mon-Thu nights = 1×
PAIR_SWAPS = {
    "Oil Spread": {
        'per_lot_per_night': 11.0,   # $11.00 combined both legs
        'friday_mult': 10,            # 10× on Friday night (covers weekend)
    },
    "Index Spread": {
        'per_lot_per_night': 0.0,    # Unknown — set to 0 for now
        'friday_mult': 10,
    },
}

def get_spread_multiplier(hour):
    if 0 <= hour < 7: return 1.8
    elif 7 <= hour < 9: return 1.2
    elif 9 <= hour < 17: return 1.0
    elif 17 <= hour < 21: return 1.1
    else: return 1.5

def calc_trade_cost(pair_name, lots, hour):
    """Spread + commission cost (no swap — swap calculated separately)."""
    pc = PAIR_COSTS[pair_name]
    mult = get_spread_multiplier(hour)
    spread_cost = (pc.spread_a_per_fill * 2 + pc.spread_b_per_fill * 2) * lots * mult
    if pc.commission_pct > 0:
        comm = pc.commission_pct * OIL_NOTIONAL_PER_LOT * lots * 4
    else:
        comm = pc.commission_rt * lots
    return spread_cost + comm

def calc_swap_cost(pair_name, lots, entry_time, exit_time):
    """Calculate swap cost for holding position from entry_time to exit_time.
    Swap is charged once per overnight hold (midnight crossing).
    Friday night = 10× (covers the weekend)."""
    sc = PAIR_SWAPS.get(pair_name)
    if not sc or sc['per_lot_per_night'] <= 0:
        return 0.0

    swap_per_night = sc['per_lot_per_night'] * lots
    friday_mult = sc['friday_mult']

    # Count midnight crossings between entry and exit
    entry_date = entry_time.date() if hasattr(entry_time, 'date') else entry_time
    exit_date = exit_time.date() if hasattr(exit_time, 'date') else exit_time

    if entry_date == exit_date:
        return 0.0  # Same day — no swap

    total_swap = 0.0
    current_date = entry_date
    from datetime import timedelta
    while current_date < exit_date:
        # This night = current_date → next day
        day_of_week = current_date.weekday()  # Mon=0, Fri=4
        if day_of_week == 4:  # Friday night
            total_swap += swap_per_night * friday_mult
        elif day_of_week < 5:  # Mon-Thu night (skip Sat/Sun — no swap charged)
            total_swap += swap_per_night
        # Skip Sat(5) and Sun(6) — swap already covered by Friday's 10×
        current_date += timedelta(days=1)

    return total_swap

# ============================================================================
# PAIR DEFINITIONS
# ============================================================================
@dataclass
class PairDef:
    name: str; sym_a: str; sym_b: str; file_a: str; file_b: str
    pair_index: int; notional: float
    dwell_base: float; dwell_anchor: float; dwell_min: float; dwell_max: float

PAIRS = [
    PairDef("Oil Spread", "XTIUSD","XBRUSD", "XTIUSD_M1.csv","XBRUSD_M1.csv", 1,
            notional=100_000.0,
            dwell_base=OIL_DWELL_BASE, dwell_anchor=OIL_DWELL_ANCHOR,
            dwell_min=OIL_DWELL_MIN, dwell_max=OIL_DWELL_MAX),
    PairDef("Index Spread", "US100","DE40", "US100_M1.csv","DE40_M1.csv", 0,
            notional=150_000.0,
            dwell_base=INDEX_DWELL_BASE, dwell_anchor=INDEX_DWELL_ANCHOR,
            dwell_min=INDEX_DWELL_MIN, dwell_max=INDEX_DWELL_MAX),
]

# ============================================================================
# COMPONENTS — EXACT MATCH TO engine.py
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

def calc_dwell_pair(h, pdef):
    """Per-pair dynamic dwell using pair-specific base/anchor/min/max."""
    return max(pdef.dwell_min, min(pdef.dwell_max, pdef.dwell_base * (h / pdef.dwell_anchor)))

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
# SIMULATION — EXACT LIVE BOT LOGIC + COSTS
# ============================================================================
def run_pair(df, pdef, hmm_hold, amp_hurdle=0.0, amp_max_mult=1.0):
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
    entry_hour = 0; entry_time = None; hmm_blocks = 0; dwell_blocks = 0; rollover_blocks = 0; amp_blocks = 0

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
                swap = calc_swap_cost(pdef.name, elots, entry_time, bt) if entry_time else 0.0
                cost += swap
                pnl = gross - cost; balance += pnl; peak = max(peak,balance)
                w = pnl > 0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                trades.append({'pnl':pnl,'gross':gross,'cost':cost,'swap':swap,'hold':bar-ebar,'hour':entry_hour,'reason':'SENTINEL'})
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
                cb = calc_dwell_pair(last_close_h, pdef) / 60.0  # Per-pair dwell
                if (bar - last_close_bar) < cb: dwell_blocks += 1; continue

            risk = dakad.calc(cdd, ddd)
            corr.compute_risk(); cm = corr.last_risk_multiplier
            lots = max(0.01, round(balance * risk * cm / 1000.0, 2))

            # ── AMPLITUDE GATE + OPPORTUNITY MULTIPLIER ──────────────
            # CORRECTED: use (|Z| - exit_z) = actual captured Z-move (matches live engine.py)
            if amp_hurdle > 0.0:
                sigma = eng.last_std
                z_captured = max(0.0, abs(z) - exz)  # Only the Z-distance from entry to exit
                expected_profit = z_captured * sigma * lots * pdef.notional
                trade_cost = calc_trade_cost(pdef.name, lots, bt.hour)
                if trade_cost > 0:
                    amp_ratio = expected_profit / (trade_cost * amp_hurdle)
                else:
                    amp_ratio = 999.0  # No cost = always pass

                if amp_ratio < 1.0:
                    amp_blocks += 1; continue  # Not enough juice

                # Opportunity multiplier: scale lots up for bigger moves
                if amp_max_mult > 1.0:
                    opp_mult = min(amp_max_mult, 1.0 + 0.5 * (amp_ratio - 1.0))
                    lots = max(0.01, round(lots * opp_mult, 2))
            # ── END AMPLITUDE GATE ───────────────────────────────────

            pos = s; ez = z; es = spread; ebar = bar; elots = lots
            entry_hour = bt.hour; entry_time = bt

        # EXIT
        elif pos != 0:
            ex = False; reason = ""
            # Emergency exit — ALWAYS bypasses dwell
            if abs(z) > abs(ez) * 2.5: ex = True; reason = "EMERGENCY"
            if not ex:
                hb = bar - ebar
                db = calc_dwell_pair(h, pdef) / 60.0  # Per-pair dwell
                if hb < db: continue
                if pos == 1 and z > -exz: ex = True; reason = "DYNAMIC_EXIT"
                elif pos == -1 and z < exz: ex = True; reason = "DYNAMIC_EXIT"
            if ex:
                gross = (spread-es)*pos*elots*pdef.notional
                cost = calc_trade_cost(pdef.name, elots, entry_hour)
                swap = calc_swap_cost(pdef.name, elots, entry_time, bt) if entry_time else 0.0
                cost += swap
                pnl = gross - cost; balance += pnl; peak = max(peak,balance)
                w = pnl > 0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                trades.append({'pnl':pnl,'gross':gross,'cost':cost,'swap':swap,'hold':bar-ebar,'hour':entry_hour,'reason':reason})
                pos=0; last_close_bar=bar; last_close_h=h

    total = len(trades)
    if total == 0:
        return {'trades':0,'wr':0,'gross_wr':0,'pf':0,'net_pnl':0,'gross_pnl':0,
                'total_costs':0,'return_pct':0,'max_dd_pct':0,'avg_hold':0,
                'ghost':ghost,'ghost_info':ghost_info,'hmm_blocks':hmm_blocks,
                'dwell_blocks':dwell_blocks,'rollover_blocks':rollover_blocks,
                'amp_blocks':amp_blocks,
                'exit_reasons':{},'trades_per_month':0,'days':0}

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

    exit_reasons = {}
    for t in trades:
        r = t.get('reason','?'); exit_reasons[r] = exit_reasons.get(r, 0) + 1

    first = df['time'].iloc[0]; last_t = df['time'].iloc[-1]
    days = (last_t - first).days; months = days / 30.0

    return {
        'trades': total, 'wr': round(wr,1), 'gross_wr': round(gross_wr,1),
        'pf': round(pf,2), 'net_pnl': round(balance-STARTING_BALANCE,2),
        'gross_pnl': round(sum(gross_pnls),2),
        'total_costs': round(sum(t['cost'] for t in trades),2),
        'return_pct': round((balance-STARTING_BALANCE)/STARTING_BALANCE*100,2),
        'max_dd_pct': round(mdd/STARTING_BALANCE*100,2),
        'avg_hold': round(avg_hold,1), 'ghost': ghost, 'ghost_info': ghost_info,
        'hmm_blocks': hmm_blocks, 'dwell_blocks': dwell_blocks,
        'rollover_blocks': rollover_blocks, 'amp_blocks': amp_blocks,
        'exit_reasons': exit_reasons,
        'trades_per_month': round(total/months,1) if months > 0 else 0,
        'days': days,
        'avg_win': round(np.mean(wins),2) if wins else 0,
        'avg_loss': round(np.mean(losses),2) if losses else 0,
        'avg_gross_win': round(np.mean([g for g in gross_pnls if g > 0]),2) if gross_wins else 0,
        'avg_cost': round(sum(t['cost'] for t in trades)/total,2),
        'total_swap': round(sum(t.get('swap',0) for t in trades),2),
        'trades_with_swap': sum(1 for t in trades if t.get('swap',0) > 0),
    }


def main():
    t_start = time.time()
    print("="*130)
    print("SHF v5.6 — OIL + INDEX LIVE TEST (Per-Pair Dwell + Real Costs + HMM Sweep)")
    print(f"shf_core version: {shf_core.__version__}")
    print("="*130)

    # Show config
    print(f"\n  DWELL CONFIG (per-pair, Hurst-adaptive):")
    print(f"    Index:  dwell = {INDEX_DWELL_BASE} x (H/{INDEX_DWELL_ANCHOR}), clamped [{INDEX_DWELL_MIN}s, {INDEX_DWELL_MAX}s]")
    print(f"            At H=0.5: {INDEX_DWELL_BASE * (0.5/INDEX_DWELL_ANCHOR):.0f}s = {INDEX_DWELL_BASE * (0.5/INDEX_DWELL_ANCHOR)/60:.1f} bars")
    print(f"    Oil:    dwell = {OIL_DWELL_BASE} x (H/{OIL_DWELL_ANCHOR}), clamped [{OIL_DWELL_MIN}s, {OIL_DWELL_MAX}s]")
    print(f"            At H=0.4: {OIL_DWELL_BASE * (0.4/OIL_DWELL_ANCHOR):.0f}s = {OIL_DWELL_BASE * (0.4/OIL_DWELL_ANCHOR)/60:.0f} bars")
    print(f"            At H=0.5: {OIL_DWELL_BASE * (0.5/OIL_DWELL_ANCHOR):.0f}s = {OIL_DWELL_BASE * (0.5/OIL_DWELL_ANCHOR)/60:.0f} bars")
    print(f"            At H=0.6: {OIL_DWELL_BASE * (0.6/OIL_DWELL_ANCHOR):.0f}s = {OIL_DWELL_BASE * (0.6/OIL_DWELL_ANCHOR)/60:.0f} bars")

    print(f"\n  COST MODEL:")
    print(f"    Index:  Spread $1+$1/fill × 4 fills = $4/lot base, ZERO commission")
    print(f"    Oil:    Spread $4+$5/fill × 4 fills = $18/lot base + 0.03% commission (~$7.80/lot)")

    print(f"\n  OTHER: Dynamic Z, Dynamic Exit Z, Dynamic AKAD, HMM Filter, Kalman Sentinel,")
    print(f"         Correlation Monitor, Ghost Stop (4%/9%), Rollover {ROLLOVER_LOCKOUT_MIN}min, Cooldown")

    # Load data
    pair_data = {}
    print(f"\n  DATA:")
    for p in PAIRS:
        try:
            df = load_pair(p)
            pair_data[p.name] = (df, p)
            first = df['time'].iloc[0]; last_t = df['time'].iloc[-1]
            days = (last_t - first).days
            print(f"    {p.name:<16} {len(df):>8,} bars | {first} to {last_t} ({days}d)")
        except Exception as e:
            print(f"    {p.name:<16} FAILED: {e}")

    # =========================================================================
    # AMPLITUDE GATE + OPPORTUNITY MULTIPLIER SWEEP
    # =========================================================================
    # HMM values to test per pair:
    #   Oil: test BOTH 5 (current live) and 10 (previous test optimum) to compare
    #   Index: 20 (both test and live agree)
    hmm_per_pair = {"Oil Spread": [5, 10], "Index Spread": [20]}

    hurdle_values = [0.0, 1.0, 1.5, 2.0, 2.5, 3.0]
    mult_values = [1.0, 1.5, 2.0]
    amp_results = {}

    print(f"\n\n{'='*130}")
    print("AMPLITUDE GATE + OPPORTUNITY MULTIPLIER SWEEP (HMM 5 & 10 for Oil, 20 for Index)")
    print(f"  NOTE: Amplitude gate now uses CORRECTED formula: (|Z| - exit_z) x sigma")
    print(f"{'='*130}")
    print(f"\n  {'Pair':<16} {'HMM':>4} {'Hrdl':>5} {'Mult':>5} {'Trades':>7} {'Tr/Mo':>6} {'NetWR':>7} {'GrossWR':>8} {'PF':>7} "
          f"{'GrossP&L':>12} {'Costs':>10} {'NetP&L':>12} {'Return':>8} {'MaxDD':>7} {'$/Trade':>9} {'AmpBlk':>7}")
    print(f"  {'-'*155}")

    for pair_name, (df, pdef) in pair_data.items():
        hmm_vals = hmm_per_pair.get(pair_name, [20])
        best_pnl = -999999; best_cfg = ""

        for hmm_val in hmm_vals:
            for hurdle in hurdle_values:
                for mult in mult_values:
                    # Skip redundant: no gate means multiplier is irrelevant
                    if hurdle == 0.0 and mult > 1.0:
                        continue

                    t0 = time.time()
                    r = run_pair(df, pdef, hmm_val, amp_hurdle=hurdle, amp_max_mult=mult)
                    elapsed = time.time() - t0

                    key = f"{pair_name}|H{hmm_val}|A{hurdle}|M{mult}"
                    amp_results[key] = r

                    dpt = r['net_pnl'] / r['trades'] if r['trades'] > 0 else 0
                    marker = ""
                    if r['net_pnl'] > best_pnl and r['trades'] >= 3:
                        best_pnl = r['net_pnl']; best_cfg = f"hmm={hmm_val} hurdle={hurdle} mult={mult}"
                        marker = " <<<"

                    ghost_str = f" {r['ghost_info']}" if r['ghost'] else ""
                    ab = r.get('amp_blocks', 0)

                    print(f"  {pair_name:<16} {hmm_val:>4} {hurdle:>5.1f} {mult:>5.1f} {r['trades']:>7} {r.get('trades_per_month',0):>5.0f} "
                          f"{r['wr']:>6.1f}% {r['gross_wr']:>7.1f}% {r['pf']:>7.2f} "
                          f"${r['gross_pnl']:>11,.2f} ${r['total_costs']:>9,.2f} ${r['net_pnl']:>11,.2f} "
                          f"{r['return_pct']:>7.2f}% {r['max_dd_pct']:>6.2f}% "
                          f"${dpt:>8.2f} {ab:>6}{ghost_str}{marker}")

            print(f"  {'':>16} --- HMM={hmm_val} best so far: {best_cfg} ---")

        print(f"  {'':>16} BEST OVERALL: {best_cfg} -> Net ${best_pnl:>,.2f}")
        print(f"  {'-'*155}")

    # =========================================================================
    # BEST AMPLITUDE CONFIG PER PAIR
    # =========================================================================
    print(f"\n\n{'='*130}")
    print("BEST AMPLITUDE GATE CONFIG PER PAIR")
    print(f"{'='*130}")

    best_combos = {}
    for pair_name in [p.name for p in PAIRS if p.name in pair_data]:
        best_r = None; best_n = -999999; best_hurdle = 0; best_mult = 1.0; best_hmm = 0
        for key, r in amp_results.items():
            if key.startswith(pair_name) and r['trades'] >= 3 and r['net_pnl'] > best_n:
                best_n = r['net_pnl']; best_r = r
                # Parse hmm, hurdle and mult from key
                parts = key.split('|')
                for p in parts:
                    if p.startswith('H'): best_hmm = int(p[1:])
                    if p.startswith('A'): best_hurdle = float(p[1:])
                    if p.startswith('M'): best_mult = float(p[1:])
        if best_r:
            best_combos[pair_name] = (best_hmm, best_hurdle, best_mult, best_r)

    total_net = 0; total_trades = 0
    for pair_name, (hmm_val, hurdle, mult, r) in sorted(best_combos.items(), key=lambda x: -x[1][3]['net_pnl']):
        total_net += r['net_pnl']; total_trades += r['trades']
        dpt = r['net_pnl'] / r['trades'] if r['trades'] > 0 else 0
        pdef = [p for p in PAIRS if p.name == pair_name][0]
        print(f"\n  {pair_name}:")
        print(f"    HMM hold = {hmm_val}")
        print(f"    Amplitude Hurdle = {hurdle}x | Opp Multiplier Cap = {mult}x")
        print(f"    Amp Blocks: {r.get('amp_blocks',0)}")
        print(f"    Trades: {r['trades']} ({r.get('trades_per_month',0):.0f}/month)")
        print(f"    WR: {r['wr']}% net, {r['gross_wr']}% gross")
        print(f"    PF: {r['pf']}")
        print(f"    Gross P&L: ${r['gross_pnl']:,.2f}")
        print(f"    Costs:     ${r['total_costs']:,.2f} (incl swap: ${r.get('total_swap',0):,.2f} on {r.get('trades_with_swap',0)} trades)")
        print(f"    Net P&L:   ${r['net_pnl']:,.2f} ({r['return_pct']}%)")
        print(f"    MaxDD:     {r['max_dd_pct']}%")
        print(f"    Avg hold:  {r['avg_hold']:.0f} bars ({r['avg_hold']:.0f} min)")
        print(f"    $/trade:   ${dpt:.2f}")
        if r['ghost']: print(f"    GHOST: {r['ghost_info']}")

    days_span = 0
    for _, (_, _, _, r) in best_combos.items():
        days_span = max(days_span, r.get('days', 0))
    months_span = days_span / 30.0

    print(f"\n  {'='*60}")
    print(f"  PORTFOLIO TOTAL: ${total_net:>,.2f} ({total_net/STARTING_BALANCE*100:.1f}%)")
    print(f"  Total trades: {total_trades}")
    if months_span > 0:
        print(f"  Monthly rate: ~{total_net/STARTING_BALANCE*100/months_span:.1f}%/month")
    print(f"  Data span: {days_span} days")

    elapsed_total = time.time() - t_start
    print(f"\n  Test completed in {elapsed_total:.1f}s")

    # Save
    save_data = {
        'timestamp': datetime.now().isoformat(),
        'config': {
            'index_dwell': {'base': INDEX_DWELL_BASE, 'min': INDEX_DWELL_MIN, 'max': INDEX_DWELL_MAX},
            'oil_dwell': {'base': OIL_DWELL_BASE, 'min': OIL_DWELL_MIN, 'max': OIL_DWELL_MAX},
            'costs': {k: {'spread_a': v.spread_a_per_fill, 'spread_b': v.spread_b_per_fill,
                          'comm_rt': v.commission_rt, 'comm_pct': v.commission_pct}
                      for k, v in PAIR_COSTS.items()},
        },
        'best': {p: {'hmm': h, 'hurdle': hrd, 'mult': m, 'net': r['net_pnl'], 'pf': r['pf'], 'trades': r['trades']}
                 for p, (h, hrd, m, r) in best_combos.items()},
        'portfolio_net': round(total_net, 2),
    }
    out = Path("Results/oil_index_live_results.json")
    with open(out, 'w') as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"  Saved to {out}")


if __name__ == "__main__":
    main()
