#!/usr/bin/env python3
"""
SHF v5.6 — 2-Year Stress Test: FIXED vs DYNAMIC AKAD Comparison
=================================================================

Runs the EXACT same 12 scenarios from test_v56_2year_stress.py
but with BOTH:
  1. FIXED AKAD (current 0.75% base)
  2. DYNAMIC AKAD (adaptive base from daily DD headroom + rolling WR)

Compares P&L, DD, and safety across all scenarios.
"""

import sys, io, math, time, json
import numpy as np
from pathlib import Path
from collections import deque

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # For importing sibling scripts

import shf_core

# Import everything from the original stress test
from test_v56_2year_stress import (
    HOLY_TRIO, STARTING_BALANCE, BARS_PER_SCENARIO,
    WELFORD_SPAN, Z_BASE, GAMMA, HURST_WINDOW, EXIT_Z_BASE, EXIT_GAMMA,
    AKAD_BASE_RISK, AKAD_DD_LAMBDA, GHOST_DAILY_DD, GHOST_MAX_DD,
    KALMAN_TOLERANCE, CORR_WINDOW,
    PairSimState, TradeRecord,
    make_scenarios, generate_prices_for_scenario,
)

# ============================================================================
# DYNAMIC AKAD
# ============================================================================

LAMBDA = 40.0
P_RUIN = 1e-4
MIN_WR, MAX_WR = 0.50, 0.85
MIN_BASE, MAX_BASE = 0.003, 0.03

class DynamicAKAD:
    def __init__(self):
        self.results = deque(maxlen=50)
        for _ in range(10): self.results.append(1)
        for _ in range(5):  self.results.append(0)

    def record(self, win): self.results.append(1 if win else 0)

    def calc(self, total_dd, daily_dd):
        dd_rem = max(0.001, GHOST_DAILY_DD - daily_dd)
        wr = max(MIN_WR, min(MAX_WR, sum(self.results)/max(len(self.results),1)))
        n_s = math.log(P_RUIN) / math.log(1-wr)
        base = (math.exp(LAMBDA*dd_rem)-1) / (LAMBDA*n_s)
        base = max(MIN_BASE, min(MAX_BASE, base))
        return max(0.0005, base * math.exp(-LAMBDA*total_dd))


# ============================================================================
# SIMULATION (accepts use_dynamic flag)
# ============================================================================

def run_sim(scenario_name, pair_prices, use_dynamic=False):
    balance = STARTING_BALANCE
    peak = STARTING_BALANCE
    daily_start = STARTING_BALANCE
    ghost_stopped = False
    ghost_bar = -1

    akad = shf_core.AKADRiskCalculator(base_risk=AKAD_BASE_RISK, dd_lambda=AKAD_DD_LAMBDA,
                                        fast_window=15, slow_window=50)
    dakad = DynamicAKAD() if use_dynamic else None
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)

    states = {}
    for p in HOLY_TRIO:
        eng = shf_core.CointegrationEngine(span=WELFORD_SPAN, beta=1.0, entry_z=Z_BASE,
            exit_z=EXIT_Z_BASE, z_base=Z_BASE, gamma=GAMMA, hurst_window=HURST_WINDOW,
            dynamic_z=True, exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
        sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
        states[p.name] = PairSimState(engine=eng, sentinel=sen)

    trades = []
    eq = []
    n_bars = len(next(iter(pair_prices.values()))[0])
    consec = 0
    cooldown = 0

    for bar in range(n_bars):
        if ghost_stopped: break
        if bar % 1440 == 0 and bar > 0: daily_start = balance

        cur_dd = max(0.0, (peak-balance)/peak) if peak>0 else 0.0
        day_dd = max(0.0, (daily_start-balance)/daily_start) if daily_start>0 else 0.0

        if day_dd >= GHOST_DAILY_DD:
            ghost_stopped = True; ghost_bar = bar
            for pn, ps in states.items():
                if ps.position != 0:
                    pd_ = next(p for p in HOLY_TRIO if p.name==pn)
                    pa,pb = pair_prices[pn]
                    sv = math.log(pa[bar])-math.log(pb[bar])
                    pnl = (sv-ps.entry_spread)*ps.position*ps.entry_lots*pd_.notional
                    balance += pnl
                    trades.append(TradeRecord(pn,bar,ps.position,ps.entry_z,0,0,ps.entry_hurst,0.5,
                        ps.entry_spread,sv,ps.entry_lots,pnl,balance,"GHOST_STOP"))
                    ps.position = 0
            break

        if cur_dd >= GHOST_MAX_DD: ghost_stopped=True; ghost_bar=bar; break
        if bar < cooldown:
            if bar%1000==0: eq.append((bar,balance))
            continue

        for pd_ in HOLY_TRIO:
            pn = pd_.name; ps = states[pn]
            pa,pb = pair_prices[pn]
            pA,pB = float(pa[bar]),float(pb[bar])
            ps.prev_spread = ps.last_spread

            sig_r = ps.engine.update(pA,pB)
            z,sig,spread = sig_r.z_score, sig_r.signal, sig_r.spread
            ps.last_spread = spread
            h = ps.engine.last_hurst; ez = ps.engine.last_exit_z

            if ps.prev_spread != 0.0: corr.push_return(pd_.pair_index, spread-ps.prev_spread)

            la = math.log(pA) if pA>0 else 0.0; lb = math.log(pB) if pB>0 else 0.0
            beta, abort = ps.sentinel.update(la,lb)

            if abort and not ps.sentinel_aborted:
                ps.sentinel_aborted = True
                if ps.position != 0:
                    pnl = (spread-ps.entry_spread)*ps.position*ps.entry_lots*pd_.notional
                    balance += pnl; peak = max(peak,balance)
                    w = pnl>0
                    akad.record_trade(0.49 if w else -1.0)
                    if dakad: dakad.record(w)
                    if not w: consec+=1
                    else: consec=0
                    if consec>=5: cooldown=bar+60; consec=0
                    trades.append(TradeRecord(pn,bar,ps.position,ps.entry_z,ez,z,ps.entry_hurst,h,
                        ps.entry_spread,spread,ps.entry_lots,pnl,balance,"SENTINEL_ABORT"))
                    ps.position = 0
                continue

            if ps.sentinel_aborted and not abort: ps.sentinel_aborted = False
            if ps.sentinel_aborted: continue
            if bar < 200: continue

            if ps.position==0 and sig!=0:
                if use_dynamic and dakad:
                    risk = dakad.calc(cur_dd, day_dd)
                else:
                    risk,_,_,_ = akad.calculate_risk(cur_dd)
                _,cm = corr.compute_risk()
                lots = max(0.01, round(balance*risk*cm/1000.0, 2))
                ps.position=sig; ps.entry_z=z; ps.entry_spread=spread
                ps.entry_bar=bar; ps.entry_hurst=h; ps.entry_lots=lots

            elif ps.position != 0:
                ex = False; reason=""
                if ps.position==1 and z>-ez: ex=True; reason="DYNAMIC_EXIT"
                elif ps.position==-1 and z<ez: ex=True; reason="DYNAMIC_EXIT"
                if abs(z)>abs(ps.entry_z)*2.5: ex=True; reason="EMERGENCY_2.5X"
                if ex:
                    pnl = (spread-ps.entry_spread)*ps.position*ps.entry_lots*pd_.notional
                    balance += pnl; peak = max(peak,balance)
                    w = pnl>0
                    akad.record_trade(0.49 if w else -1.0)
                    if dakad: dakad.record(w)
                    if not w: consec+=1
                    else: consec=0
                    if consec>=5: cooldown=bar+60; consec=0
                    trades.append(TradeRecord(pn,bar,ps.position,ps.entry_z,ez,z,ps.entry_hurst,h,
                        ps.entry_spread,spread,ps.entry_lots,pnl,balance,reason))
                    ps.position = 0

        if bar%1000==0: eq.append((bar,balance))

    eq.append((n_bars-1, balance))

    # Metrics
    total = len(trades)
    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p>0]
    losses = [p for p in pnls if p<=0]
    gp = sum(wins) if wins else 0.0
    gl = abs(sum(losses)) if losses else 0.001
    bals = [e[1] for e in eq]
    pk=STARTING_BALANCE; mdd=0
    for b in bals: pk=max(pk,b); mdd=max(mdd,pk-b)

    return {
        'scenario': scenario_name,
        'mode': 'DYNAMIC' if use_dynamic else 'FIXED',
        'trades': total,
        'net_pnl': round(balance-STARTING_BALANCE, 2),
        'return_pct': round((balance-STARTING_BALANCE)/STARTING_BALANCE*100, 2),
        'wr': round(len(wins)/total*100, 1) if total>0 else 0,
        'pf': round(gp/gl, 2),
        'max_dd_pct': round(mdd/STARTING_BALANCE*100, 2),
        'max_dd_usd': round(mdd, 2),
        'ghost': ghost_stopped,
        'ghost_bar': ghost_bar,
    }


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("="*90)
    print("SHF v5.6 -- 2-YEAR STRESS TEST: FIXED vs DYNAMIC AKAD")
    print(f"shf_core: {shf_core.__version__} | Bars/scenario: {BARS_PER_SCENARIO:,}")
    print("="*90)
    print(f"\nFixed AKAD:   base=0.75%, lambda=40")
    print(f"Dynamic AKAD: base=(exp(40*dd_remaining)-1)/(40*n_survive), rolling WR, lambda=40")
    print(f"              4% daily DD ceiling GUARANTEED\n")

    scenarios = make_scenarios()
    fixed_results = {}
    dynamic_results = {}
    total_t = time.time()

    for idx, (sn, sc) in enumerate(scenarios.items()):
        print(f"\n{'='*90}")
        print(f"SCENARIO {idx+1}/12: {sn}")
        print(f"{'='*90}")
        print(f"  {sc.get('description','')}")

        # Generate prices once, reuse for both
        t0 = time.time()
        pp = {}
        for pi, pd_ in enumerate(HOLY_TRIO):
            pa,pb = generate_prices_for_scenario(sc, pd_, pair_seed=pi*1000+idx*100)
            pp[pd_.name] = (pa,pb)
        gen = time.time()-t0
        print(f"  Prices: {gen:.1f}s")

        # Fixed
        t0 = time.time()
        rf = run_sim(sn, pp, use_dynamic=False)
        tf = time.time()-t0
        fixed_results[sn] = rf

        # Dynamic
        t0 = time.time()
        rd = run_sim(sn, pp, use_dynamic=True)
        td = time.time()-t0
        dynamic_results[sn] = rd

        diff = rd['net_pnl'] - rf['net_pnl']
        gf = "GHOST!" if rf['ghost'] else "OK"
        gd = "GHOST!" if rd['ghost'] else "OK"

        print(f"\n  {'':>8} {'Trades':>7} {'P&L':>12} {'Return':>9} {'WR':>6} {'PF':>6} {'MaxDD%':>8} {'Ghost':>6} {'Time':>5}")
        print(f"  {'-'*72}")
        print(f"  {'FIXED':>8} {rf['trades']:>7} ${rf['net_pnl']:>11,.2f} {rf['return_pct']:>+8.2f}% "
              f"{rf['wr']:>5.1f}% {rf['pf']:>6.2f} {rf['max_dd_pct']:>7.2f}% {gf:>6} {tf:>4.0f}s")
        print(f"  {'DYNAMIC':>8} {rd['trades']:>7} ${rd['net_pnl']:>11,.2f} {rd['return_pct']:>+8.2f}% "
              f"{rd['wr']:>5.1f}% {rd['pf']:>6.2f} {rd['max_dd_pct']:>7.2f}% {gd:>6} {td:>4.0f}s")
        print(f"  {'DELTA':>8} {'':>7} ${diff:>+11,.2f}")

    total_time = time.time() - total_t

    # ===== FINAL SUMMARY =====
    print(f"\n\n{'='*90}")
    print(f"FINAL COMPARISON — ALL 12 SCENARIOS")
    print(f"{'='*90}")
    print(f"Total time: {total_time:.0f}s\n")

    print(f"  {'Scenario':<28} {'FIXED P&L':>12} {'DYN P&L':>12} {'DIFF':>12} {'F-DD%':>7} {'D-DD%':>7} {'F-Ghost':>8} {'D-Ghost':>8}")
    print(f"  {'-'*100}")

    f_prof = d_prof = f_surv = d_surv = 0
    for sn in scenarios:
        rf = fixed_results[sn]; rd = dynamic_results[sn]
        diff = rd['net_pnl'] - rf['net_pnl']
        gf = "YES" if rf['ghost'] else "No"
        gd = "YES" if rd['ghost'] else "No"
        print(f"  {sn:<28} ${rf['net_pnl']:>11,.2f} ${rd['net_pnl']:>11,.2f} ${diff:>+11,.2f} "
              f"{rf['max_dd_pct']:>6.2f}% {rd['max_dd_pct']:>6.2f}% {gf:>8} {gd:>8}")
        if rf['net_pnl']>0: f_prof+=1
        if rd['net_pnl']>0: d_prof+=1
        if not rf['ghost']: f_surv+=1
        if not rd['ghost']: d_surv+=1

    # Aggregates
    f_avg_ret = np.mean([r['return_pct'] for r in fixed_results.values()])
    d_avg_ret = np.mean([r['return_pct'] for r in dynamic_results.values()])
    f_avg_pf = np.mean([r['pf'] for r in fixed_results.values() if r['pf']>0])
    d_avg_pf = np.mean([r['pf'] for r in dynamic_results.values() if r['pf']>0])
    f_worst = max(r['max_dd_pct'] for r in fixed_results.values())
    d_worst = max(r['max_dd_pct'] for r in dynamic_results.values())
    total_f = sum(r['net_pnl'] for r in fixed_results.values())
    total_d = sum(r['net_pnl'] for r in dynamic_results.values())

    print(f"\n  {'SUMMARY':>28} {'FIXED':>12} {'DYNAMIC':>12} {'DELTA':>12}")
    print(f"  {'-'*66}")
    print(f"  {'Total P&L (all 12)':>28} ${total_f:>11,.2f} ${total_d:>11,.2f} ${total_d-total_f:>+11,.2f}")
    print(f"  {'Avg Return':>28} {f_avg_ret:>11.2f}% {d_avg_ret:>11.2f}% {d_avg_ret-f_avg_ret:>+11.2f}%")
    print(f"  {'Avg PF':>28} {f_avg_pf:>12.2f} {d_avg_pf:>12.2f} {d_avg_pf-f_avg_pf:>+12.2f}")
    print(f"  {'Worst Max DD':>28} {f_worst:>11.2f}% {d_worst:>11.2f}% {d_worst-f_worst:>+11.2f}%")
    print(f"  {'Profitable':>28} {f_prof:>10}/12 {d_prof:>10}/12")
    print(f"  {'Survived (no ghost)':>28} {f_surv:>10}/12 {d_surv:>10}/12")

    # Save
    out = {'fixed': {k: {kk:vv for kk,vv in v.items()} for k,v in fixed_results.items()},
           'dynamic': {k: {kk:vv for kk,vv in v.items()} for k,v in dynamic_results.items()}}

    def conv(o):
        if isinstance(o, (np.integer,)): return int(o)
        elif isinstance(o, (np.floating,)): return float(o)
        elif isinstance(o, np.bool_): return bool(o)
        return o

    with open("Results/v56_2year_fixed_vs_dynamic.json", 'w') as f:
        json.dump(out, f, indent=2, default=conv)
    print(f"\n  Saved to Results/v56_2year_fixed_vs_dynamic.json")


if __name__ == "__main__":
    main()
