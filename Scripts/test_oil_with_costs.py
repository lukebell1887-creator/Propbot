#!/usr/bin/env python3
"""
SHF v5.6 — OIL PAIR REALISTIC COST TEST
================================================================
Tests XTIUSD/XBRUSD (WTI/Brent) with:
  1. Realistic spread costs (oil has WIDE spreads)
  2. Multiple minimum hold times (30, 60, 120, 240 bars)
  3. Multiple Z_crit floors (2.0, 3.0, 4.0, 5.0)
  4. Session filter option

Goal: Find if there's a parameter combination where oil works AFTER costs.

Oil Cost Model:
  - WTI: ~4 cent spread = $4/fill/lot
  - Brent: ~5 cent spread = $5/fill/lot
  - 4 fills per trade: ($4×2 + $5×2) = $18/lot spread + $4 commission = $22/lot
  - Asian: 1.8× wider
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
# v5.6 PARAMETERS
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

# Oil cost per fill per lot
OIL_COST_A_PER_FILL = 4.0    # WTI ~4 cent spread × $100 tick value = $4
OIL_COST_B_PER_FILL = 5.0    # Brent ~5 cent spread × $100 tick value = $5
OIL_COMMISSION_RT = 4.0       # $4/lot round-trip

def get_spread_multiplier(hour_broker):
    if 0 <= hour_broker < 7: return 1.8
    elif 7 <= hour_broker < 9: return 1.2
    elif 9 <= hour_broker < 17: return 1.0
    elif 17 <= hour_broker < 21: return 1.1
    else: return 1.5

def calc_oil_cost(lots, hour):
    mult = get_spread_multiplier(hour)
    spread = (OIL_COST_A_PER_FILL * 2 + OIL_COST_B_PER_FILL * 2) * lots * mult
    return spread + OIL_COMMISSION_RT * lots

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

def is_rollover(t):
    m = t.hour * 60 + t.minute
    return m < ROLLOVER_LOCKOUT_MIN or (1440 - m) < ROLLOVER_LOCKOUT_MIN

def is_session_blocked(t):
    return t.hour < 7 or t.hour >= 21

def load_oil():
    d = Path(__file__).resolve().parent.parent / "data" / "historical"
    a = pd.read_csv(d / "XTIUSD_M1.csv", parse_dates=['time']).rename(columns={'close':'close_a'})
    b = pd.read_csv(d / "XBRUSD_M1.csv", parse_dates=['time']).rename(columns={'close':'close_b'})
    m = pd.merge(a[['time','close_a']], b[['time','close_b']], on='time', how='inner').sort_values('time').reset_index(drop=True)
    return m[(m['close_a']>0)&(m['close_b']>0)].reset_index(drop=True)

# ============================================================================
# SINGLE RUN
# ============================================================================
def run_oil(df, z_floor=2.0, min_hold_bars=1, hmm_hold=100, apply_costs=True, session_filter=False, notional=100_000.0):
    balance = STARTING_BALANCE; peak = STARTING_BALANCE; daily_start = STARTING_BALANCE
    daily_date = None; ghost = False; ghost_info = ""
    dakad = DynamicAKAD()
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec = 0; gcool = 0
    trades = []; total_costs = 0.0
    n = len(df)

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

        if pspread != 0.0: corr.push_return(0, spread - pspread)

        la = math.log(pa) if pa>0 else 0; lb = math.log(pb) if pb>0 else 0
        beta, abort = sen.update(la, lb)
        if abort and not sent_abort:
            sent_abort = True
            if pos != 0:
                gross = (spread-es)*pos*elots*notional
                cost = calc_oil_cost(elots, entry_hour) if apply_costs else 0
                pnl = gross - cost; total_costs += cost
                balance += pnl; peak = max(peak,balance)
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

        # Apply Z_crit floor
        z_crit_actual = max(z_floor, eng.last_entry_z if hasattr(eng, 'last_entry_z') else Z_BASE)
        
        # ENTRY
        if pos == 0 and abs(z) >= z_crit_actual and s != 0:
            if hblocked: continue
            if is_rollover(bt): continue
            if session_filter and is_session_blocked(bt): continue
            
            # Enforce minimum hold since last trade
            if last_close_bar >= 0:
                dwell = max(min_hold_bars, int(DWELL_BASE * (h / DWELL_ANCHOR) / 60.0))
                if (bar - last_close_bar) < dwell: continue

            risk = dakad.calc(cdd, ddd)
            corr.compute_risk(); cm = corr.last_risk_multiplier
            lots = max(0.01, round(balance * risk * cm / 1000.0, 2))
            pos = s; ez = z; es = spread; ebar = bar; elots = lots
            entry_hour = bt.hour

        # EXIT — enforce minimum hold time
        elif pos != 0:
            hb = bar - ebar
            if hb < min_hold_bars: continue  # Force minimum hold
            
            ex = False; reason = ""
            if abs(z) > abs(ez) * 2.5: ex = True; reason = "EMERGENCY"
            if not ex:
                if pos == 1 and z > -exz: ex = True; reason = "DYNAMIC_EXIT"
                elif pos == -1 and z < exz: ex = True; reason = "DYNAMIC_EXIT"
            if ex:
                gross = (spread-es)*pos*elots*notional
                cost = calc_oil_cost(elots, entry_hour) if apply_costs else 0
                pnl = gross - cost; total_costs += cost
                balance += pnl; peak = max(peak,balance)
                w = pnl > 0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                trades.append({'pnl':pnl,'gross':gross,'cost':cost,'hold':hb,'hour':entry_hour})
                pos=0; last_close_bar=bar; last_close_h=h

    total = len(trades)
    if total == 0:
        return {'trades':0,'wr':0,'pf':0,'net_pnl':0,'gross_pnl':0,'total_costs':0,
                'return_pct':0,'max_dd_pct':0,'avg_hold':0,'ghost':ghost,'ghost_info':ghost_info}

    pnls = [t['pnl'] for t in trades]
    gross_pnls = [t['gross'] for t in trades]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    gp = sum(wins) if wins else 0; gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins)/total*100; pf = gp/gl if gl > 0 else 0
    gross_wr = len([g for g in gross_pnls if g > 0])/total*100

    eq = STARTING_BALANCE; eq_peak = eq; mdd = 0
    for t in trades:
        eq += t['pnl']; eq_peak = max(eq_peak, eq); mdd = max(mdd, eq_peak - eq)

    holds = [t['hold'] for t in trades]
    avg_hold = np.mean(holds)

    # Hourly breakdown
    hourly_pnl = {}; hourly_count = {}
    for t in trades:
        h = t['hour']
        hourly_pnl[h] = hourly_pnl.get(h, 0) + t['pnl']
        hourly_count[h] = hourly_count.get(h, 0) + 1

    return {
        'trades': total, 'wr': round(wr,1), 'gross_wr': round(gross_wr,1),
        'pf': round(pf,2), 'net_pnl': round(balance-STARTING_BALANCE,2),
        'gross_pnl': round(sum(gross_pnls),2), 'total_costs': round(total_costs,2),
        'return_pct': round((balance-STARTING_BALANCE)/STARTING_BALANCE*100,2),
        'max_dd_pct': round(mdd/STARTING_BALANCE*100,2),
        'avg_hold': round(avg_hold,1), 'ghost': ghost, 'ghost_info': ghost_info,
        'avg_win': round(np.mean(wins),2) if wins else 0,
        'avg_loss': round(np.mean(losses),2) if losses else 0,
    }


def main():
    print("="*120)
    print("SHF v5.6 — OIL PAIR (XTIUSD/XBRUSD) REALISTIC COST ANALYSIS")
    print(f"shf_core version: {shf_core.__version__}")
    print("="*120)
    
    # Oil cost breakdown
    base_cost = (OIL_COST_A_PER_FILL * 2 + OIL_COST_B_PER_FILL * 2) + OIL_COMMISSION_RT
    print(f"  Oil Cost: WTI ${OIL_COST_A_PER_FILL}/fill + Brent ${OIL_COST_B_PER_FILL}/fill")
    print(f"  4 fills + commission = ${base_cost:.0f}/lot/trade (base)")
    print(f"  Asian: 1.8× = ${base_cost*1.8:.0f}/lot, LDN+NY: 1.0× = ${base_cost:.0f}/lot")
    
    df = load_oil()
    first = df['time'].iloc[0]; last = df['time'].iloc[-1]
    days = (last - first).days
    print(f"\n  Data: {len(df):,} bars | {first} to {last} ({days}d)")
    print(f"  Price range: WTI ${df['close_a'].min():.2f}-${df['close_a'].max():.2f}, Brent ${df['close_b'].min():.2f}-${df['close_b'].max():.2f}")

    # =========================================================================
    # SWEEP: Z_crit floor × Minimum Hold × Session Filter
    # =========================================================================
    z_floors = [2.0, 3.0, 4.0, 5.0]
    min_holds = [1, 30, 60, 120, 240]
    
    print(f"\n\n{'='*120}")
    print("GRID SEARCH: Z_crit Floor × Min Hold (bars) — WITH Costs, 24/5")
    print(f"{'='*120}")
    print(f"\n  {'Z_crit':>6} {'MinHold':>8} {'Trades':>7} {'WR':>7} {'GrossWR':>8} {'PF':>7} {'GrossP&L':>12} {'Costs':>10} {'NetP&L':>12} {'Return':>8} {'MaxDD':>7} {'AvgHold':>8} {'Ghost'}")
    print(f"  {'-'*130}")

    best_net = -999999; best_config = ""
    all_results = []
    
    for zf in z_floors:
        for mh in min_holds:
            r = run_oil(df, z_floor=zf, min_hold_bars=mh, hmm_hold=100, 
                       apply_costs=True, session_filter=False, notional=100_000.0)
            ghost_str = f" {r['ghost_info']}" if r['ghost'] else ""
            
            marker = ""
            if r['net_pnl'] > best_net and r['trades'] >= 10:
                best_net = r['net_pnl']; best_config = f"Z={zf} Hold={mh}"
                marker = " <<<BEST"
            
            print(f"  {zf:>6.1f} {mh:>8} {r['trades']:>7} {r['wr']:>6.1f}% {r['gross_wr']:>7.1f}% {r['pf']:>7.2f} "
                  f"${r['gross_pnl']:>11,.2f} ${r['total_costs']:>9,.2f} ${r['net_pnl']:>11,.2f} "
                  f"{r['return_pct']:>7.2f}% {r['max_dd_pct']:>6.2f}% {r['avg_hold']:>7.1f}{ghost_str}{marker}")
            
            all_results.append({'z_floor':zf, 'min_hold':mh, 'session':False, **r})

    print(f"\n  BEST CONFIG (24/5): {best_config} → ${best_net:,.2f}")

    # =========================================================================
    # SAME BUT WITH SESSION FILTER (07:00-21:00)
    # =========================================================================
    print(f"\n\n{'='*120}")
    print("GRID SEARCH: Z_crit Floor × Min Hold — WITH Costs + SESSION FILTER (07-21)")
    print(f"{'='*120}")
    print(f"\n  {'Z_crit':>6} {'MinHold':>8} {'Trades':>7} {'WR':>7} {'GrossWR':>8} {'PF':>7} {'GrossP&L':>12} {'Costs':>10} {'NetP&L':>12} {'Return':>8} {'MaxDD':>7} {'AvgHold':>8} {'Ghost'}")
    print(f"  {'-'*130}")

    best_net_s = -999999; best_config_s = ""
    
    for zf in z_floors:
        for mh in min_holds:
            r = run_oil(df, z_floor=zf, min_hold_bars=mh, hmm_hold=100,
                       apply_costs=True, session_filter=True, notional=100_000.0)
            ghost_str = f" {r['ghost_info']}" if r['ghost'] else ""
            
            marker = ""
            if r['net_pnl'] > best_net_s and r['trades'] >= 10:
                best_net_s = r['net_pnl']; best_config_s = f"Z={zf} Hold={mh}"
                marker = " <<<BEST"
            
            print(f"  {zf:>6.1f} {mh:>8} {r['trades']:>7} {r['wr']:>6.1f}% {r['gross_wr']:>7.1f}% {r['pf']:>7.2f} "
                  f"${r['gross_pnl']:>11,.2f} ${r['total_costs']:>9,.2f} ${r['net_pnl']:>11,.2f} "
                  f"{r['return_pct']:>7.2f}% {r['max_dd_pct']:>6.2f}% {r['avg_hold']:>7.1f}{ghost_str}{marker}")
            
            all_results.append({'z_floor':zf, 'min_hold':mh, 'session':True, **r})

    print(f"\n  BEST CONFIG (Session): {best_config_s} → ${best_net_s:,.2f}")

    # =========================================================================
    # BASELINE: No costs (show the "fantasy" number)
    # =========================================================================
    print(f"\n\n{'='*120}")
    print("BASELINE: No Costs (Fantasy)")
    print(f"{'='*120}")
    r_base = run_oil(df, z_floor=2.0, min_hold_bars=1, hmm_hold=100, apply_costs=False)
    print(f"  Z=2.0 Hold=1: {r_base['trades']} trades, WR={r_base['wr']}%, PF={r_base['pf']}, "
          f"P&L=${r_base['net_pnl']:,.2f} ({r_base['return_pct']}%), MaxDD={r_base['max_dd_pct']}%")
    print(f"  Avg hold: {r_base['avg_hold']} bars, Avg win: ${r_base.get('avg_win',0):,.2f}, Avg loss: ${r_base.get('avg_loss',0):,.2f}")

    # =========================================================================
    # VERDICT
    # =========================================================================
    print(f"\n\n{'='*120}")
    print("VERDICT")
    print(f"{'='*120}")
    
    profitable = [r for r in all_results if r['net_pnl'] > 0 and r['trades'] >= 10]
    if profitable:
        best = max(profitable, key=lambda x: x['net_pnl'])
        print(f"\n  >>> OIL IS VIABLE with the right parameters!")
        print(f"      Best: Z≥{best['z_floor']}, MinHold≥{best['min_hold']} bars, Session={'Yes' if best['session'] else 'No'}")
        print(f"      {best['trades']} trades, WR={best['wr']}%, PF={best['pf']}, Net=${best['net_pnl']:,.2f} ({best['return_pct']}%)")
        print(f"      Avg hold: {best['avg_hold']:.0f} bars ({best['avg_hold']:.0f} min)")
        print(f"      This would ADD ${best['net_pnl']:,.2f} to the portfolio")
    else:
        print(f"\n  >>> OIL IS NOT VIABLE after costs")
        print(f"      No parameter combination produces positive returns with ≥10 trades")
        print(f"      Best 24/5: {best_config} → ${best_net:,.2f}")
        print(f"      Best Session: {best_config_s} → ${best_net_s:,.2f}")
    
    # Save
    save_data = {
        'config': 'Oil pair cost analysis',
        'baseline_no_costs': {'trades': r_base['trades'], 'pnl': r_base['net_pnl']},
        'best_24_5': best_config,
        'best_24_5_pnl': best_net,
        'best_session': best_config_s,
        'best_session_pnl': best_net_s,
        'viable': len(profitable) > 0,
    }
    out = Path("Results/oil_cost_analysis.json")
    with open(out, 'w') as f:
        json.dump(save_data, f, indent=2)
    print(f"\n  Saved to {out}")


if __name__ == "__main__":
    main()
