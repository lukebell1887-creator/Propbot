#!/usr/bin/env python3
"""
Gold/Silver (XAUUSD/XAGUSD) Backtest v2 — CORRECTED COSTS
Fixed: spread data is in price units (not MT5 points)
Uses hardcoded realistic spreads + session multiplier + 0.0009% commission

Gold: contract=100oz, typical spread=$0.44 (from broker data)
Silver: contract=5000oz, typical spread=$0.043 (from broker data)  
Commission: 0.0009% of notional per lot per side
"""
import sys, math, time, json, numpy as np, pandas as pd
from pathlib import Path
from collections import deque, defaultdict
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shf_core

# ── Exact live bot engine parameters ──
WELFORD_SPAN = 100
Z_BASE = 2.0; GAMMA = 6.0; HURST_WINDOW = 512
EXIT_Z_BASE = 0.5; EXIT_GAMMA = 2.0

# ── AKAD ──
DAKAD_LAMBDA = 40.0; DAKAD_P_RUIN = 1e-4; DAKAD_DAILY_DD_CEIL = 0.04
DAKAD_RESULT_WINDOW = 50; DAKAD_MIN_WR = 0.50; DAKAD_MAX_WR = 0.85
DAKAD_MIN_BASE = 0.003; DAKAD_MAX_BASE = 0.03; DAKAD_RISK_FLOOR = 0.0005

# ── Risk limits ──
GHOST_DAILY_DD = 0.04; GHOST_MAX_DD = 0.09; KALMAN_TOLERANCE = 0.15
CORR_WINDOW = 200; HMM_LOOKBACK = 100; MAX_CONSEC_LOSSES = 5
COOLDOWN_BARS = 60; MIN_WARMUP_BARS = 200; ROLLOVER_MIN = 30; BAL = 100_000.0

# ── Gold/Silver CORRECTED costs ──
GOLD_CONTRACT = 100        # 100 oz per lot
SILVER_CONTRACT = 5000     # 5000 oz per lot
COMMISSION_PCT = 0.000009  # 0.0009% = 0.000009

# Spreads in PRICE UNITS (dollars per oz) — from broker data + realistic estimates
GOLD_SPREAD = 0.44    # $0.44 per oz bid-ask (from CSV, matches raw ECN typical)
SILVER_SPREAD = 0.043  # $0.043 per oz bid-ask (from CSV, matches raw ECN typical)

def spread_multiplier(hour):
    if 0 <= hour < 7:   return 1.8
    elif 7 <= hour < 9: return 1.2
    elif 9 <= hour < 17: return 1.0
    elif 17 <= hour < 21: return 1.1
    else: return 1.5

def calc_cost(lots, gold_price, silver_price, hour):
    """
    CORRECTED cost model.
    Spread cost: spread_in_dollars × contract_size × lots × session_mult
    (spread is paid on entry and exit, but "full spread" already = ask-bid = cost per cross)
    Round trip for pair = entry spread (both legs) + exit spread (both legs)
    """
    sm = spread_multiplier(hour)
    
    # Round-trip spread cost: full_spread × contract × lots × session_mult
    # You cross the spread on ENTRY and EXIT = 2× per leg
    gold_ba = GOLD_SPREAD * GOLD_CONTRACT * lots * 2 * sm
    silver_ba = SILVER_SPREAD * SILVER_CONTRACT * lots * 2 * sm
    
    # Commission: 0.0009% of notional × 2 sides × 2 legs
    gold_notional = gold_price * GOLD_CONTRACT * lots
    silver_notional = silver_price * SILVER_CONTRACT * lots
    commission = (gold_notional + silver_notional) * COMMISSION_PCT * 4
    
    return gold_ba + silver_ba + commission

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
    dmin = dwell_base * 0.5; dmax = dwell_base * 5.0
    return max(dmin, min(dmax, dwell_base * (hurst / anchor_h)))

def run_backtest(df, hmm_hold, dwell_base, notional_mult=100000.0):
    bal=BAL; peak=BAL; ds=BAL; ld=None; gh=False
    dakad=DynamicAKAD(); crm=shf_core.CorrelationRiskMonitor(n_pairs=3,window=CORR_WINDOW)
    cl=0; cu=0; n=len(df)
    eng=shf_core.CointegrationEngine(span=WELFORD_SPAN,beta=1.0,entry_z=Z_BASE,exit_z=EXIT_Z_BASE,
        z_base=Z_BASE,gamma=GAMMA,hurst_window=HURST_WINDOW,dynamic_z=True,
        exit_z_base=EXIT_Z_BASE,exit_gamma=EXIT_GAMMA,dynamic_exit=True)
    sen=shf_core.KalmanSentinel(static_beta=1.0,beta_tolerance=KALMAN_TOLERANCE)
    hmm=SimpleHMM(lb=HMM_LOOKBACK,mh=hmm_hold)
    pos=0;ez=0;es=0;eb=0;el=0;ls=0;ps=0;sa=False;lcb=-9999;lch=0.5;eh=0;et=None
    trades=[];hb_cnt=0;sb_cnt=0
    
    for bar in range(n):
        if gh: break
        row=df.iloc[bar]; bt=row['time']; pa=float(row['gc']); pb=float(row['sc'])
        cd=bt.date() if hasattr(bt,'date') else None
        if cd and cd!=ld: ld=cd; ds=bal
        tdd=max(0,(peak-bal)/peak) if peak>0 else 0
        ddd=max(0,(ds-bal)/ds) if ds>0 else 0
        if ddd>=GHOST_DAILY_DD: gh=True; break
        if tdd>=GHOST_MAX_DD: gh=True; break
        if bar<cu: continue
        ps=ls; sig=eng.update(pa,pb); z=sig.z_score; s=sig.signal; sp=sig.spread; ls=sp
        h=eng.last_hurst; exz=eng.last_exit_z
        if ps!=0: crm.push_return(2,sp-ps)
        la=math.log(pa) if pa>0 else 0; lb_=math.log(pb) if pb>0 else 0
        beta,abort=sen.update(la,lb_)
        if abort and not sa:
            sa=True; sb_cnt+=1
            if pos!=0:
                gr=(sp-es)*pos*el*notional_mult
                co=calc_cost(el,pa,pb,eh); pnl=gr-co; bal+=pnl; peak=max(peak,bal)
                w=pnl>0; dakad.record(w)
                if not w: cl+=1
                else: cl=0
                if cl>=MAX_CONSEC_LOSSES: cu=bar+COOLDOWN_BARS; cl=0
                trades.append({'pnl':pnl,'gr':gr,'co':co,'hold':bar-eb,'eh':eh,'xh':bt.hour,'et':et,'xt':bt,'r':'SENTINEL'})
                pos=0; lcb=bar; lch=h
            continue
        if sa and not abort: sa=False
        if sa: continue
        hbl=False
        if ps!=0: hmm.update(sp-ps); hbl=hmm.blocked
        if hbl: hb_cnt+=1
        if bar<MIN_WARMUP_BARS: continue
        msm=bt.hour*60+bt.minute; mbm=1440-msm
        inr=msm<ROLLOVER_MIN or mbm<ROLLOVER_MIN
        if pos==0 and s!=0:
            if hbl: continue
            if inr: continue
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
                hb_=bar-eb; mh=dwell_time(h,dwell_base)/60.0
                if hb_<mh: continue
                if pos==1 and z>-exz: ex=True; reason="DYN_EXIT"
                elif pos==-1 and z<exz: ex=True; reason="DYN_EXIT"
            if ex:
                gr=(sp-es)*pos*el*notional_mult
                co=calc_cost(el,pa,pb,eh); pnl=gr-co; bal+=pnl; peak=max(peak,bal)
                w=pnl>0; dakad.record(w)
                if not w: cl+=1
                else: cl=0
                if cl>=MAX_CONSEC_LOSSES: cu=bar+COOLDOWN_BARS; cl=0
                trades.append({'pnl':pnl,'gr':gr,'co':co,'hold':bar-eb,'eh':eh,'xh':bt.hour,'et':et,'xt':bt,'r':reason})
                pos=0; lcb=bar; lch=h
    return trades, bal, hb_cnt, sb_cnt


def main():
    t0 = time.time()
    print("=" * 100)
    print("GOLD/SILVER (XAUUSD/XAGUSD) v2 — CORRECTED REALISTIC COSTS")
    print("=" * 100)
    
    # Show cost breakdown
    print("\n--- COST MODEL BREAKDOWN (at 0.30 lots, London session) ---")
    gold_p = 5000.0; silver_p = 80.0; lots = 0.30
    g_ba = GOLD_SPREAD * GOLD_CONTRACT * lots * 2  # RT no session mult
    s_ba = SILVER_SPREAD * SILVER_CONTRACT * lots * 2
    comm = (gold_p*GOLD_CONTRACT*lots + silver_p*SILVER_CONTRACT*lots) * COMMISSION_PCT * 4
    total = g_ba + s_ba + comm
    print(f"  Gold spread cost (RT):   ${g_ba:.2f}  ({GOLD_SPREAD}$/oz × {GOLD_CONTRACT}oz × {lots} lots × 2)")
    print(f"  Silver spread cost (RT): ${s_ba:.2f}  ({SILVER_SPREAD}$/oz × {SILVER_CONTRACT}oz × {lots} lots × 2)")
    print(f"  Commission (RT both):    ${comm:.2f}  (0.0009% × notional × 4)")
    print(f"  TOTAL (no session mult): ${total:.2f}")
    print(f"  With 1.2x avg session:   ${total*1.2:.2f}")
    print(f"  With 1.8x Asian:         ${total*1.8:.2f}")
    
    # Compare with oil costs
    print("\n--- vs OIL PAIR (for reference) ---")
    oil_spread_a = 4 * 0.01  # 4 points × $0.01
    oil_spread_b = 5 * 0.01
    oil_lots = 0.30
    oil_ba = (oil_spread_a + oil_spread_b) * 2 * oil_lots * 1000  # assuming 1000 barrel notional
    print(f"  Oil typical cost (0.30 lots): ~${oil_ba:.2f} spread + swap")
    print(f"  Gold/Silver is {total/max(0.01,oil_ba):.1f}x more expensive than oil")
    
    df = load_data()
    print(f"\nData: {len(df)} aligned M1 bars")
    print(f"Range: {df['time'].iloc[0]} to {df['time'].iloc[-1]}")
    
    # Quick Hurst
    eng_c = shf_core.CointegrationEngine(span=100,beta=1.0,entry_z=2.0,exit_z=0.5,
        z_base=2.0,gamma=6.0,hurst_window=512,dynamic_z=True,exit_z_base=0.5,exit_gamma=2.0,dynamic_exit=True)
    for i in range(min(2000,len(df))): eng_c.update(float(df.iloc[i]['gc']),float(df.iloc[i]['sc']))
    print(f"Hurst after 2000 bars: {eng_c.last_hurst:.3f} | Z_crit: {eng_c.last_z_crit:.2f}")
    
    # Test matrix
    HMM_HOLDS = [5, 10, 20, 100]
    DWELL_BASES = [60, 300, 1800]
    
    print("\n" + "=" * 100)
    print("BACKTEST MATRIX (CORRECTED COSTS)")
    print("=" * 100)
    
    results = {}
    best_key = None; best_net = -float('inf')
    
    for dwell_base in DWELL_BASES:
        for hmm_hold in HMM_HOLDS:
            key = f"HMM={hmm_hold}_Dwell={dwell_base}s"
            print(f"\n  Running: {key}...", end="", flush=True)
            trades, final_bal, hb, sb = run_backtest(df, hmm_hold=hmm_hold, dwell_base=dwell_base)
            
            net = sum(t['pnl'] for t in trades) if trades else 0
            wr = len([t for t in trades if t['pnl']>0])/len(trades)*100 if trades else 0
            tc = sum(t['co'] for t in trades) if trades else 0
            tg = sum(t['gr'] for t in trades) if trades else 0
            
            eq=BAL; pk=BAL; mdd=0
            for t in trades: eq+=t['pnl']; pk=max(pk,eq); mdd=max(mdd,(pk-eq)/pk)
            
            pf=0
            if trades:
                ws=sum(t['pnl'] for t in trades if t['pnl']>0)
                ls_=abs(sum(t['pnl'] for t in trades if t['pnl']<=0))
                pf=ws/ls_ if ls_>0 else float('inf')
            
            avg_hold = np.mean([t['hold'] for t in trades]) if trades else 0
            cost_pct = tc/tg*100 if tg>0 else 0
            
            results[key] = {
                'trades':len(trades),'wr':wr,'net':net,'pf':pf,'mdd':mdd,
                'costs':tc,'gross':tg,'cost_pct':cost_pct,'avg_hold':avg_hold,
                'dwell':dwell_base,'hmm':hmm_hold,'all_trades':trades
            }
            if net>best_net: best_net=net; best_key=key
            print(f" {len(trades)} tr, WR={wr:.1f}%, Net=${net:,.0f}, Gross=${tg:,.0f}, Costs=${tc:,.0f} ({cost_pct:.0f}%), PF={pf:.2f}, DD={mdd*100:.1f}%")
    
    # Summary
    print("\n" + "=" * 100)
    print("SUMMARY TABLE (CORRECTED COSTS)")
    print("=" * 100)
    print(f"  {'Config':<25} {'Tr':>4} {'WR':>6} {'Gross':>10} {'Costs':>10} {'C%':>5} {'Net':>10} {'PF':>6} {'DD':>6} {'AvgH':>6}")
    print(f"  {'-'*23:<25} {'--':>4} {'----':>6} {'--------':>10} {'--------':>10} {'--':>5} {'--------':>10} {'----':>6} {'----':>6} {'----':>6}")
    
    for key in sorted(results.keys()):
        r=results[key]
        star=" <<<" if key==best_key else ""
        print(f"  {key:<25} {r['trades']:>4} {r['wr']:>5.1f}% ${r['gross']:>9,.0f} ${r['costs']:>9,.0f} {r['cost_pct']:>4.0f}% ${r['net']:>9,.0f} {r['pf']:>5.2f} {r['mdd']*100:>5.1f}% {r['avg_hold']:>5.0f}{star}")
    
    # Best config detail
    if best_key and results[best_key]['all_trades']:
        print(f"\n{'='*100}")
        print(f"BEST: {best_key}")
        print(f"{'='*100}")
        bt = results[best_key]['all_trades']
        net = sum(t['pnl'] for t in bt)
        wins = [t for t in bt if t['pnl']>0]
        losses = [t for t in bt if t['pnl']<=0]
        print(f"  {len(bt)} trades | WR: {len(wins)/len(bt)*100:.1f}% | Net: ${net:,.2f}")
        print(f"  Avg Win: ${np.mean([t['pnl'] for t in wins]):,.2f}" if wins else "")
        print(f"  Avg Loss: ${np.mean([abs(t['pnl']) for t in losses]):,.2f}" if losses else "")
        print(f"  Avg Cost: ${np.mean([t['co'] for t in bt]):,.2f} per trade")
        
        # Hour breakdown
        hp=defaultdict(lambda:{'pnl':0,'n':0,'w':0})
        for t in bt:
            hp[t['eh']]['pnl']+=t['pnl']; hp[t['eh']]['n']+=1
            if t['pnl']>0: hp[t['eh']]['w']+=1
        print(f"\n  {'Hr':>4} {'Tr':>4} {'WR':>6} {'Net':>12} {'Avg':>10}")
        for h in sorted(hp.keys()):
            d=hp[h]; w=d['w']/d['n']*100 if d['n'] else 0
            print(f"  {h:>4} {d['n']:>4} {w:>5.1f}% ${d['pnl']:>11,.2f} ${d['pnl']/d['n']:>9,.2f}")
    
    # Final comparison
    print(f"\n{'='*100}")
    print("COMPARISON WITH LIVE PAIRS")
    print(f"{'='*100}")
    br=results[best_key] if best_key else None
    if br and br['all_trades']:
        dr=(br['all_trades'][-1]['xt']-br['all_trades'][0]['et']).days
        mo=max(1,dr/30.0); pm=br['net']/mo
    else: pm=0
    print(f"  {'Pair':<20} {'Tr':>5} {'WR':>6} {'Net':>12} {'DD':>6} {'$/month':>10} {'CostPct':>8}")
    print(f"  {'Oil (WTI/Brent)':<20} {'659':>5} {'79.7':>5}% ${'28,389':>11} {'1.5':>5}% ${'8,111':>9} {'~15':>7}%")
    print(f"  {'Index (NAS/DAX)':<20} {'98':>5} {'70.4':>5}% ${'4,377':>11} {'2.0':>5}% ${'1,250':>9} {'~10':>7}%")
    if br:
        print(f"  {'Gold/Silver':<20} {br['trades']:>5} {br['wr']:>5.1f}% ${br['net']:>11,.0f} {br['mdd']*100:>5.1f}% ${pm:>9,.0f} {br['cost_pct']:>7.0f}%")
    
    # Verdict
    print(f"\n{'='*100}")
    print("VERDICT")
    print(f"{'='*100}")
    if br and best_net > 2000 and br['pf'] > 1.3 and br['mdd'] < 0.05:
        print(f"  RECOMMENDED: Gold/Silver viable as 3rd pair with {best_key}")
    elif br and best_net > 500 and br['pf'] > 1.1:
        print(f"  MARGINAL: Some edge but modest. Best: {best_key}")
    elif br and best_net > 0:
        print(f"  WEAK: Barely profitable after costs. Not recommended.")
    else:
        print(f"  NOT VIABLE: Negative after realistic costs.")
    
    # Save
    sd={k:{kk:vv for kk,vv in v.items() if kk!='all_trades'} for k,v in results.items()}
    with open(Path(__file__).resolve().parent.parent/"Results"/"gold_silver_v2_backtest.json",'w') as f:
        json.dump(sd,f,indent=2,default=str)
    
    print(f"\n  Completed in {time.time()-t0:.1f}s")

if __name__=="__main__": main()
