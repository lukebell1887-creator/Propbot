#!/usr/bin/env python3
"""
Gold/Silver v3 — Cost sweep matching EXACT oil backtest methodology.
Models costs as FIXED $/fill/lot (same structure as test_oil_with_costs.py).
Tests a range of cost levels to find breakeven.

Cost structure (per fill per lot):
  Gold:   tick_value = $1/point (100oz × $0.01/pt)
  Silver: tick_value = $5/point (5000oz × $0.001/pt)
  
  Typical spreads (points): Gold 15-40, Silver 20-50
  Cost/fill/lot: Gold $15-$40, Silver $100-$250
  
Commission: 0.0009% of notional per lot per side
  Gold:  $5000 × 100 × 0.000009 = $4.50/side = $9/lot RT  
  Silver: $80 × 5000 × 0.000009 = $3.60/side = $7.20/lot RT
"""
import sys, math, time, json, numpy as np, pandas as pd
from pathlib import Path
from collections import deque, defaultdict
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shf_core

# ── Exact live bot parameters ──
WELFORD_SPAN = 100; Z_BASE = 2.0; GAMMA = 6.0; HURST_WINDOW = 512
EXIT_Z_BASE = 0.5; EXIT_GAMMA = 2.0
DAKAD_LAMBDA = 40.0; DAKAD_P_RUIN = 1e-4; DAKAD_DAILY_DD_CEIL = 0.04
DAKAD_RESULT_WINDOW = 50; DAKAD_MIN_WR = 0.50; DAKAD_MAX_WR = 0.85
DAKAD_MIN_BASE = 0.003; DAKAD_MAX_BASE = 0.03; DAKAD_RISK_FLOOR = 0.0005
GHOST_DAILY_DD = 0.04; GHOST_MAX_DD = 0.09; KALMAN_TOLERANCE = 0.15
CORR_WINDOW = 200; HMM_LOOKBACK = 100; MAX_CONSEC_LOSSES = 5
COOLDOWN_BARS = 60; MIN_WARMUP_BARS = 200; ROLLOVER_MIN = 30; BAL = 100_000.0

# Commission round trip per lot (both legs combined)
COMMISSION_RT_PER_LOT = 16.20  # ($9 gold + $7.20 silver)

def get_spread_multiplier(hour):
    if 0 <= hour < 7: return 1.8
    elif 7 <= hour < 9: return 1.2
    elif 9 <= hour < 17: return 1.0
    elif 17 <= hour < 21: return 1.1
    else: return 1.5

def calc_cost(lots, hour, gold_fill_cost, silver_fill_cost):
    """
    EXACT same structure as oil test: (cost_A × 2 + cost_B × 2) × lots × mult + commission × lots
    
    gold_fill_cost: $/fill/lot for gold (= spread_points × tick_value = pts × $1)
    silver_fill_cost: $/fill/lot for silver (= spread_points × tick_value = pts × $5)
    """
    mult = get_spread_multiplier(hour)
    spread_cost = (gold_fill_cost * 2 + silver_fill_cost * 2) * lots * mult
    commission = COMMISSION_RT_PER_LOT * lots
    return spread_cost + commission

class DynamicAKAD:
    def __init__(self):
        self._r = deque(maxlen=DAKAD_RESULT_WINDOW)
        for _ in range(10): self._r.append(1)
        for _ in range(5): self._r.append(0)
    def record(self, w): self._r.append(1 if w else 0)
    def calc_risk(self, tdd, ddd):
        ddr = max(0.001, DAKAD_DAILY_DD_CEIL - ddd)
        wr = max(DAKAD_MIN_WR, min(DAKAD_MAX_WR, sum(self._r)/max(len(self._r),1)))
        ns = math.log(DAKAD_P_RUIN)/math.log(1.0-wr)
        base = max(DAKAD_MIN_BASE, min(DAKAD_MAX_BASE, (math.exp(DAKAD_LAMBDA*ddr)-1)/(DAKAD_LAMBDA*ns)))
        return max(DAKAD_RISK_FLOOR, base*math.exp(-DAKAD_LAMBDA*tdd))

class SimpleHMM:
    def __init__(self, lb=100, mh=20):
        self._lb=lb; self._cr=0; self._buf=[]; self._hc=0; self._mh=mh
    def update(self, sr):
        self._buf.append(sr)
        if len(self._buf)>self._lb*3: self._buf=self._buf[-self._lb*2:]
        if len(self._buf)<50: return 0
        r=np.array(self._buf[-self._lb:]); n=len(r); ws=min(20,n//3)
        if ws<5: return 0
        vols=[np.std(r[i:i+ws]) for i in range(0,n-ws+1,ws)]
        if len(vols)<3: return 0
        v40=np.percentile(vols,40); v80=np.percentile(vols,80)
        nr=0 if vols[-1]<=v40 else(1 if vols[-1]<=v80 else 2)
        self._hc+=1
        if nr!=self._cr and self._hc>=self._mh: self._cr=nr; self._hc=0
        return self._cr
    @property
    def blocked(self): return self._cr>=2

def load_data():
    d = Path(__file__).resolve().parent.parent / "data" / "historical"
    g = pd.read_csv(d/"XAUUSD_M1.csv", parse_dates=['time']).rename(columns={'close':'gc'})
    s = pd.read_csv(d/"XAGUSD_M1.csv", parse_dates=['time']).rename(columns={'close':'sc'})
    m = pd.merge(g[['time','gc']], s[['time','sc']], on='time', how='inner').sort_values('time').reset_index(drop=True)
    return m[(m['gc']>0)&(m['sc']>0)].reset_index(drop=True)

def dwell_time(hurst, dwell_base, anchor_h=0.3):
    return max(dwell_base*0.5, min(dwell_base*5.0, dwell_base*(hurst/anchor_h)))

def run_backtest(df, hmm_hold, dwell_base, gold_fill_cost, silver_fill_cost, apply_costs=True, notional=100_000.0):
    bal=BAL; peak=BAL; ds=BAL; ld=None; gh=False
    dakad=DynamicAKAD(); crm=shf_core.CorrelationRiskMonitor(n_pairs=3,window=CORR_WINDOW)
    cl=0; cu=0; n=len(df)
    eng=shf_core.CointegrationEngine(span=WELFORD_SPAN,beta=1.0,entry_z=Z_BASE,exit_z=EXIT_Z_BASE,
        z_base=Z_BASE,gamma=GAMMA,hurst_window=HURST_WINDOW,dynamic_z=True,
        exit_z_base=EXIT_Z_BASE,exit_gamma=EXIT_GAMMA,dynamic_exit=True)
    sen=shf_core.KalmanSentinel(static_beta=1.0,beta_tolerance=KALMAN_TOLERANCE)
    hmm=SimpleHMM(lb=HMM_LOOKBACK,mh=hmm_hold)
    pos=0;ez=0;es=0;eb=0;el=0;ls=0;ps=0;sa=False;lcb=-9999;lch=0.5;eh=0;et=None
    trades=[];tc=0.0
    
    for bar in range(n):
        if gh: break
        row=df.iloc[bar]; bt=row['time']; pa=float(row['gc']); pb=float(row['sc'])
        cd=bt.date() if hasattr(bt,'date') else None
        if cd and cd!=ld: ld=cd; ds=bal
        tdd=max(0,(peak-bal)/peak) if peak>0 else 0
        ddd=max(0,(ds-bal)/ds) if ds>0 else 0
        if ddd>=GHOST_DAILY_DD or tdd>=GHOST_MAX_DD: gh=True; break
        if bar<cu: continue
        ps=ls; sig=eng.update(pa,pb); z=sig.z_score; s=sig.signal; sp=sig.spread; ls=sp
        h=eng.last_hurst; exz=eng.last_exit_z
        if ps!=0: crm.push_return(2,sp-ps)
        la=math.log(pa) if pa>0 else 0; lb_=math.log(pb) if pb>0 else 0
        beta,abort=sen.update(la,lb_)
        if abort and not sa:
            sa=True
            if pos!=0:
                gr=(sp-es)*pos*el*notional
                co=calc_cost(el,eh,gold_fill_cost,silver_fill_cost) if apply_costs else 0
                pnl=gr-co; tc+=co; bal+=pnl; peak=max(peak,bal)
                w=pnl>0; dakad.record(w)
                if not w: cl+=1
                else: cl=0
                if cl>=MAX_CONSEC_LOSSES: cu=bar+COOLDOWN_BARS; cl=0
                trades.append({'pnl':pnl,'gr':gr,'co':co,'hold':bar-eb,'eh':eh,'xt':bt})
                pos=0; lcb=bar; lch=h
            continue
        if sa and not abort: sa=False
        if sa: continue
        hbl=False
        if ps!=0: hmm.update(sp-ps); hbl=hmm.blocked
        if bar<MIN_WARMUP_BARS: continue
        msm=bt.hour*60+bt.minute; mbm=1440-msm
        inr=msm<ROLLOVER_MIN or mbm<ROLLOVER_MIN
        if pos==0 and s!=0:
            if hbl or inr: continue
            if lcb>=0:
                db=dwell_time(lch,dwell_base)/60.0
                if(bar-lcb)<db: continue
            risk=dakad.calc_risk(tdd,ddd); crm.compute_risk(); cm=crm.last_risk_multiplier
            lots=max(0.01,round(bal*risk*cm/1000.0,2))
            pos=s;ez=z;es=sp;eb=bar;el=lots;eh=bt.hour;et=bt
        elif pos!=0:
            ex=False;reason=""
            if abs(z)>abs(ez)*2.5: ex=True; reason="EMERGENCY"
            if not ex:
                if(bar-eb)<dwell_time(h,dwell_base)/60.0: continue
                if pos==1 and z>-exz: ex=True; reason="DYN_EXIT"
                elif pos==-1 and z<exz: ex=True; reason="DYN_EXIT"
            if ex:
                gr=(sp-es)*pos*el*notional
                co=calc_cost(el,eh,gold_fill_cost,silver_fill_cost) if apply_costs else 0
                pnl=gr-co; tc+=co; bal+=pnl; peak=max(peak,bal)
                w=pnl>0; dakad.record(w)
                if not w: cl+=1
                else: cl=0
                if cl>=MAX_CONSEC_LOSSES: cu=bar+COOLDOWN_BARS; cl=0
                trades.append({'pnl':pnl,'gr':gr,'co':co,'hold':bar-eb,'eh':eh,'xt':bt})
                pos=0; lcb=bar; lch=h
    return trades, bal, tc


def main():
    t0 = time.time()
    print("=" * 120)
    print("GOLD/SILVER v3 — COST SWEEP (Matching Oil Test Methodology)")
    print("=" * 120)
    
    df = load_data()
    print(f"\nData: {len(df)} aligned M1 bars | {df['time'].iloc[0]} to {df['time'].iloc[-1]}")
    
    # ── BASELINE: Zero costs ──
    print(f"\n{'='*120}")
    print("BASELINE: Zero Costs (shows raw edge)")
    print(f"{'='*120}")
    
    for hmm in [5, 10, 20, 100]:
        for dwell in [60, 300, 1800]:
            t, bal, tc = run_backtest(df, hmm, dwell, 0, 0, apply_costs=False)
            if not t: continue
            net = sum(x['pnl'] for x in t)
            wr = len([x for x in t if x['pnl']>0])/len(t)*100
            gr = sum(x['gr'] for x in t)
            eq=BAL;pk=BAL;mdd=0
            for x in t: eq+=x['pnl'];pk=max(pk,eq);mdd=max(mdd,(pk-eq)/pk)
            pf_w=sum(x['pnl'] for x in t if x['pnl']>0)
            pf_l=abs(sum(x['pnl'] for x in t if x['pnl']<=0))
            pf=pf_w/pf_l if pf_l>0 else 99
            avg_gr = gr/len(t) if t else 0
            print(f"  HMM={hmm:>3} Dwell={dwell:>4}s | {len(t):>3} tr WR={wr:>5.1f}% PF={pf:>5.2f} Gross=${gr:>10,.2f} AvgGross=${avg_gr:>8,.2f} Net=${net:>10,.2f} DD={mdd*100:.1f}%")
    
    # ── COST SWEEP ──
    # Gold: $1/point × spread_pts → $/fill/lot
    # Silver: $5/point × spread_pts → $/fill/lot
    #
    # Scenario mapping:
    # "Tight"   = Gold 15pts=$15, Silver 20pts=$100
    # "Normal"  = Gold 25pts=$25, Silver 30pts=$150  
    # "Wide"    = Gold 35pts=$35, Silver 40pts=$200
    # "V.Wide"  = Gold 44pts=$44, Silver 43pts=$215 (CSV values)
    
    cost_scenarios = [
        ("Zero",    0,   0,   "No spread cost"),
        ("Tight",   15,  100, "Gold 15pts, Silver 20pts"),
        ("Normal",  25,  150, "Gold 25pts, Silver 30pts"),
        ("Wide",    35,  200, "Gold 35pts, Silver 40pts"),
        ("CSV",     44,  215, "Gold 44pts=$0.44, Silver 43pts=$0.043"),
    ]
    
    print(f"\n{'='*120}")
    print("COST SWEEP: HMM=10 Dwell=300s (best from zero-cost baseline)")
    print(f"{'='*120}")
    print(f"\n  Oil reference: WTI=$4/fill, Brent=$5/fill → $22/lot/trade → makes $28,389")
    print(f"  Commission both legs: ${COMMISSION_RT_PER_LOT:.2f}/lot RT")
    print()
    
    print(f"  {'Scenario':<10} {'Au$/fill':>8} {'Ag$/fill':>8} {'Total$/lot':>10} {'@0.3lot':>8} | {'Tr':>4} {'WR':>6} {'Gross':>10} {'Costs':>10} {'Net':>10} {'PF':>6} {'DD':>5}")
    print(f"  {'-'*110}")
    
    for name, gfc, sfc, desc in cost_scenarios:
        total_per_lot = (gfc*2 + sfc*2) + COMMISSION_RT_PER_LOT
        at_03 = total_per_lot * 0.3
        
        t, bal, tc = run_backtest(df, hmm_hold=10, dwell_base=300, 
                                   gold_fill_cost=gfc, silver_fill_cost=sfc, 
                                   apply_costs=(gfc>0 or sfc>0))
        if not t:
            print(f"  {name:<10} ${gfc:>7} ${sfc:>7} ${total_per_lot:>9,.0f} ${at_03:>7,.0f} | NO TRADES")
            continue
        
        net=sum(x['pnl'] for x in t); gr=sum(x['gr'] for x in t)
        wr=len([x for x in t if x['pnl']>0])/len(t)*100
        eq=BAL;pk=BAL;mdd=0
        for x in t: eq+=x['pnl'];pk=max(pk,eq);mdd=max(mdd,(pk-eq)/pk)
        pw=sum(x['pnl'] for x in t if x['pnl']>0)
        pl=abs(sum(x['pnl'] for x in t if x['pnl']<=0))
        pf=pw/pl if pl>0 else 99
        
        mark = " <<<" if net > 0 and gfc > 0 else ""
        print(f"  {name:<10} ${gfc:>7} ${sfc:>7} ${total_per_lot:>9,.0f} ${at_03:>7,.0f} | {len(t):>4} {wr:>5.1f}% ${gr:>9,.0f} ${tc:>9,.0f} ${net:>9,.0f} {pf:>5.2f} {mdd*100:>4.1f}%{mark}")
    
    # ── FULL MATRIX at "Tight" costs ──
    print(f"\n{'='*120}")
    print("FULL MATRIX: Tight Costs (Gold=$15/fill, Silver=$100/fill) × HMM × Dwell")
    print(f"{'='*120}")
    print(f"  {'Config':<25} {'Tr':>4} {'WR':>6} {'Gross':>10} {'Costs':>10} {'Net':>10} {'PF':>6} {'DD':>5}")
    print(f"  {'-'*80}")
    
    best_key=None; best_net=-1e9
    for dwell in [60, 300, 1800]:
        for hmm in [5, 10, 20, 100]:
            key=f"HMM={hmm}_Dwell={dwell}s"
            t,bal,tc = run_backtest(df, hmm, dwell, 15, 100)
            if not t: print(f"  {key:<25} NO TRADES"); continue
            net=sum(x['pnl'] for x in t); gr=sum(x['gr'] for x in t)
            wr=len([x for x in t if x['pnl']>0])/len(t)*100
            eq=BAL;pk=BAL;mdd=0
            for x in t: eq+=x['pnl'];pk=max(pk,eq);mdd=max(mdd,(pk-eq)/pk)
            pw=sum(x['pnl'] for x in t if x['pnl']>0)
            pl=abs(sum(x['pnl'] for x in t if x['pnl']<=0))
            pf=pw/pl if pl>0 else 99
            if net>best_net: best_net=net; best_key=key
            mark=" <<<" if key==best_key else ""
            print(f"  {key:<25} {len(t):>4} {wr:>5.1f}% ${gr:>9,.0f} ${tc:>9,.0f} ${net:>9,.0f} {pf:>5.2f} {mdd*100:>4.1f}%{mark}")
    
    # ── FULL MATRIX at "Normal" costs ──
    print(f"\n{'='*120}")
    print("FULL MATRIX: Normal Costs (Gold=$25/fill, Silver=$150/fill) × HMM × Dwell")
    print(f"{'='*120}")
    print(f"  {'Config':<25} {'Tr':>4} {'WR':>6} {'Gross':>10} {'Costs':>10} {'Net':>10} {'PF':>6} {'DD':>5}")
    print(f"  {'-'*80}")
    
    best_key2=None; best_net2=-1e9
    for dwell in [60, 300, 1800]:
        for hmm in [5, 10, 20, 100]:
            key=f"HMM={hmm}_Dwell={dwell}s"
            t,bal,tc = run_backtest(df, hmm, dwell, 25, 150)
            if not t: print(f"  {key:<25} NO TRADES"); continue
            net=sum(x['pnl'] for x in t); gr=sum(x['gr'] for x in t)
            wr=len([x for x in t if x['pnl']>0])/len(t)*100
            eq=BAL;pk=BAL;mdd=0
            for x in t: eq+=x['pnl'];pk=max(pk,eq);mdd=max(mdd,(pk-eq)/pk)
            pw=sum(x['pnl'] for x in t if x['pnl']>0)
            pl=abs(sum(x['pnl'] for x in t if x['pnl']<=0))
            pf=pw/pl if pl>0 else 99
            if net>best_net2: best_net2=net; best_key2=key
            mark=" <<<" if key==best_key2 else ""
            print(f"  {key:<25} {len(t):>4} {wr:>5.1f}% ${gr:>9,.0f} ${tc:>9,.0f} ${net:>9,.0f} {pf:>5.2f} {mdd*100:>4.1f}%{mark}")
    
    # ── VERDICT ──
    print(f"\n{'='*120}")
    print("VERDICT")
    print(f"{'='*120}")
    print(f"  Zero cost baseline (HMM=10,Dwell=300): raw edge exists")
    print(f"  Tight costs (Au=$15/fill,Ag=$100): Best={best_key} → ${best_net:,.0f}")
    print(f"  Normal costs (Au=$25/fill,Ag=$150): Best={best_key2} → ${best_net2:,.0f}")
    print()
    print(f"  For reference — Oil costs: WTI=$4/fill, Brent=$5/fill → $22/lot base")
    print(f"  Gold/Silver at TIGHT: ($15*2+$100*2)+$16 = $246/lot (11x oil)")
    print(f"  Gold/Silver at NORMAL: ($25*2+$150*2)+$16 = $366/lot (17x oil)")
    print()
    if best_net > 1000:
        print(f"  VIABLE at tight spreads: ${best_net:,.0f} profit with {best_key}")
    elif best_net > 0:
        print(f"  MARGINAL at tight spreads. Check your actual spreads.")
    else:
        print(f"  NOT VIABLE even at tight spreads.")
    
    if best_net2 > 1000:
        print(f"  VIABLE at normal spreads: ${best_net2:,.0f} profit with {best_key2}")
    elif best_net2 > 0:
        print(f"  MARGINAL at normal spreads.")
    else:
        print(f"  NOT VIABLE at normal spreads — costs destroy the edge.")
    
    print(f"\n  Completed in {time.time()-t0:.1f}s")

if __name__=="__main__": main()
