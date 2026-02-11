#!/usr/bin/env python3
"""
SHF v5.6 — BINARY HMM HOLD BACKTEST
======================================
Simplest possible per-pair adaptive: TWO settings based on Hurst.

  if H < 0.53: hold = 100  (MR pair — vol spikes are noise, be patient)
  if H >= 0.53: hold = 5   (Trending pair — respect vol regime changes fast)

Re-evaluated every 500 bars. No sigmoid, no OU, no magic.
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

WELFORD_SPAN=100;Z_BASE=2.0;GAMMA=6.0;HURST_WINDOW=512
EXIT_Z_BASE=0.5;EXIT_GAMMA=2.0
DAKAD_LAMBDA=40.0;DAKAD_P_RUIN=1e-4;DAKAD_DAILY_DD_CEIL=0.04;DAKAD_RESULT_WINDOW=50
DAKAD_MIN_WR=0.50;DAKAD_MAX_WR=0.85;DAKAD_MIN_BASE=0.003;DAKAD_MAX_BASE=0.03;DAKAD_RISK_FLOOR=0.0005
GHOST_DAILY_DD=0.04;GHOST_MAX_DD=0.09;KALMAN_TOLERANCE=0.15;CORR_WINDOW=200
DWELL_BASE=60.0;DWELL_ANCHOR=0.3;DWELL_MIN=30.0;DWELL_MAX=300.0
ROLLOVER_LOCKOUT_MIN=5;HMM_LOOKBACK=100;MAX_CONSEC_LOSSES=5;COOLDOWN_BARS=60
MIN_WARMUP_BARS=200;BAL=100_000.0

# THE BINARY SWITCH WITH HYSTERESIS
H_THRESHOLD = 0.55
HOLD_MR = 100    # Mean-reverting pairs
HOLD_TREND = 5   # Trending pairs
REFIT_INTERVAL = 500
HYSTERESIS_REQUIRED = 3  # Must see 3 consecutive refits on same side before flipping

@dataclass
class P:
    name:str;sa:str;sb:str;fa:str;fb:str;pi:int;notional:float=100_000.0

ALL=[
    P("Index Spread","US100","DE40","US100_M1.csv","DE40_M1.csv",0,150_000.0),
    P("Forex Anchor","AUDUSD","NZDUSD","AUDUSD_M1.csv","NZDUSD_M1.csv",1),
    P("EUR/GBP Spread","EURUSD","GBPUSD","EURUSD_M1.csv","GBPUSD_M1.csv",2),
    P("EURJPY/CHFJPY","EURJPY","CHFJPY","EURJPY_M1.csv","CHFJPY_M1.csv",3),
    P("XTIUSD/XBRUSD","XTIUSD","XBRUSD","XTIUSD_M1.csv","XBRUSD_M1.csv",4),
    P("XAUUSD/XAGUSD","XAUUSD","XAGUSD","XAUUSD_M1.csv","XAGUSD_M1.csv",5),
]

class DAKAD:
    def __init__(self):
        self._r=deque(maxlen=DAKAD_RESULT_WINDOW)
        for _ in range(10):self._r.append(1)
        for _ in range(5):self._r.append(0)
    def rec(self,w):self._r.append(1 if w else 0)
    def calc(self,tdd,ddd):
        ddr=max(0.001,DAKAD_DAILY_DD_CEIL-ddd)
        wr=max(DAKAD_MIN_WR,min(DAKAD_MAX_WR,sum(self._r)/max(len(self._r),1)))
        ns=math.log(DAKAD_P_RUIN)/math.log(1.0-wr)
        base=max(DAKAD_MIN_BASE,min(DAKAD_MAX_BASE,(math.exp(DAKAD_LAMBDA*ddr)-1)/(DAKAD_LAMBDA*ns)))
        return max(DAKAD_RISK_FLOOR,base*math.exp(-DAKAD_LAMBDA*tdd))
    @property
    def wr(self):return sum(self._r)/max(len(self._r),1)

class HMM:
    def __init__(self):
        self._lb=100;self._cr=0;self._buf=[];self._hc=0;self._mh=100
    def set_hold(self,h):self._mh=h
    def update(self,sr):
        self._buf.append(sr)
        if len(self._buf)>self._lb*3:self._buf=self._buf[-self._lb*2:]
        if len(self._buf)<50:return 0
        r=np.array(self._buf[-self._lb:]);n=len(r);ws=min(20,n//3)
        if ws<5:return 0
        v=[np.std(r[i:i+ws]) for i in range(0,n-ws+1,ws)]
        if len(v)<3:return 0
        v40=np.percentile(v,40);v80=np.percentile(v,80)
        nr=0 if v[-1]<=v40 else(1 if v[-1]<=v80 else 2)
        self._hc+=1
        if nr!=self._cr and self._hc>=self._mh:self._cr=nr;self._hc=0
        return self._cr
    @property
    def blocked(self):return self._cr>=2

def dwell(h):return max(DWELL_MIN,min(DWELL_MAX,DWELL_BASE*(h/DWELL_ANCHOR)))
def rollover(t):m=t.hour*60+t.minute;return m<ROLLOVER_LOCKOUT_MIN or(1440-m)<ROLLOVER_LOCKOUT_MIN

def load(p):
    d=Path(__file__).resolve().parent.parent/"data"/"historical"
    a=pd.read_csv(d/p.fa,parse_dates=['time']).rename(columns={'close':'ca'})
    b=pd.read_csv(d/p.fb,parse_dates=['time']).rename(columns={'close':'cb'})
    m=pd.merge(a[['time','ca']],b[['time','cb']],on='time',how='inner').sort_values('time').reset_index(drop=True)
    return m[(m['ca']>0)&(m['cb']>0)].reset_index(drop=True)

def run(df,pdef):
    bal=BAL;pk=BAL;ds=BAL;dd=None;gh=False;gi=""
    dakad=DAKAD();corr=shf_core.CorrelationRiskMonitor(n_pairs=3,window=CORR_WINDOW)
    con=0;gc=0;trades=[];hblk=0;rblk=0;dblk=0;sexit=0
    eng=shf_core.CointegrationEngine(span=WELFORD_SPAN,beta=1.0,entry_z=Z_BASE,exit_z=EXIT_Z_BASE,
        z_base=Z_BASE,gamma=GAMMA,hurst_window=HURST_WINDOW,dynamic_z=True,
        exit_z_base=EXIT_Z_BASE,exit_gamma=EXIT_GAMMA,dynamic_exit=True)
    sen=shf_core.KalmanSentinel(static_beta=1.0,beta_tolerance=KALMAN_TOLERANCE)
    hmm=HMM()
    pos=0;ez=0.0;es=0.0;eb=0;el=0.0;ls=0.0;ps=0.0;sa=False
    lcb=-9999;lch=0.5;hs=0.0;hc=0;lrb=-REFIT_INTERVAL
    holds=[];cur_hold=100;mr_streak=0;trend_streak=0

    for bar in range(len(df)):
        if gh:break
        row=df.iloc[bar];bt=row['time'];pa=float(row['ca']);pb=float(row['cb'])
        cd=bt.date() if hasattr(bt,'date') else None
        if cd and cd!=dd:dd=cd;ds=bal
        cdd=max(0,(pk-bal)/pk) if pk>0 else 0
        ddd=max(0,(ds-bal)/ds) if ds>0 else 0
        if ddd>=GHOST_DAILY_DD:gh=True;gi=f"DailyDD {ddd*100:.2f}%";break
        if cdd>=GHOST_MAX_DD:gh=True;gi=f"MaxDD {cdd*100:.2f}%";break
        if bar<gc:continue
        ps=ls
        sig=eng.update(pa,pb);z=sig.z_score;s=sig.signal;sp=sig.spread;ls=sp
        h=eng.last_hurst;exz=eng.last_exit_z
        if bar>MIN_WARMUP_BARS:hs+=h;hc+=1

        # BINARY HMM HOLD CALIBRATION WITH HYSTERESIS
        if bar-lrb>=REFIT_INTERVAL and bar>MIN_WARMUP_BARS:
            lrb=bar
            if h<H_THRESHOLD:
                mr_streak+=1;trend_streak=0
            else:
                trend_streak+=1;mr_streak=0
            # Only flip if we've seen N consecutive readings on the same side
            if mr_streak>=HYSTERESIS_REQUIRED and cur_hold!=HOLD_MR:
                cur_hold=HOLD_MR;hmm.set_hold(cur_hold)
            elif trend_streak>=HYSTERESIS_REQUIRED and cur_hold!=HOLD_TREND:
                cur_hold=HOLD_TREND;hmm.set_hold(cur_hold)
            holds.append(cur_hold)

        pidx=min(pdef.pi,2)
        if ps!=0.0:corr.push_return(pidx,sp-ps)
        la=math.log(pa) if pa>0 else 0;lb=math.log(pb) if pb>0 else 0
        beta,abort=sen.update(la,lb)
        if abort and not sa:
            sa=True
            if pos!=0:
                pnl=(sp-es)*pos*el*pdef.notional;bal+=pnl;pk=max(pk,bal)
                w=pnl>0;dakad.rec(w)
                if not w:con+=1
                else:con=0
                if con>=MAX_CONSEC_LOSSES:gc=bar+COOLDOWN_BARS;con=0
                mk=bt.strftime('%Y-%m')
                trades.append({'pnl':pnl,'r':'SENT','b':bar,'h':bar-eb,'t':bt,'m':mk})
                sexit+=1;pos=0;lcb=bar;lch=h
            continue
        if sa and not abort:sa=False
        if sa:continue
        hb=False
        if ps!=0.0:hmm.update(sp-ps);hb=hmm.blocked
        if bar<MIN_WARMUP_BARS:continue

        if pos==0 and s!=0:
            if hb:hblk+=1;continue
            if rollover(bt):rblk+=1;continue
            if lcb>=0:
                cb=dwell(lch)/60.0
                if(bar-lcb)<cb:dblk+=1;continue
            risk=dakad.calc(cdd,ddd);corr.compute_risk();cm=corr.last_risk_multiplier
            lots=max(0.01,round(bal*risk*cm/1000.0,2))
            pos=s;ez=z;es=sp;eb=bar;el=lots
        elif pos!=0:
            ex=False;reason=""
            if abs(z)>abs(ez)*2.5:ex=True;reason="EMRG"
            if not ex:
                hb2=bar-eb;db=dwell(h)/60.0
                if hb2<db:continue
                if pos==1 and z>-exz:ex=True;reason="DYN"
                elif pos==-1 and z<exz:ex=True;reason="DYN"
            if ex:
                pnl=(sp-es)*pos*el*pdef.notional;bal+=pnl;pk=max(pk,bal)
                w=pnl>0;dakad.rec(w)
                if not w:con+=1
                else:con=0
                if con>=MAX_CONSEC_LOSSES:gc=bar+COOLDOWN_BARS;con=0
                mk=bt.strftime('%Y-%m')
                trades.append({'pnl':pnl,'r':reason,'b':bar,'h':bar-eb,'t':bt,'m':mk})
                pos=0;lcb=bar;lch=h

    total=len(trades)
    if total==0:return{'trades':0,'wr':0,'pf':0,'net_pnl':0,'return_pct':0,'max_dd_pct':0,
        'avg_hurst':0,'hmm_blocks':hblk,'ghost':gh,'ghost_info':gi,'monthly':{},'avg_hmm_hold':0}
    pnls=[t['pnl'] for t in trades]
    wins=[p for p in pnls if p>0];losses=[p for p in pnls if p<=0]
    gp=sum(wins) if wins else 0;gl=abs(sum(losses)) if losses else 0.001
    wr=len(wins)/total*100;pf=gp/gl
    eq=BAL;eqp=BAL;mdd=0
    for t in trades:eq+=t['pnl'];eqp=max(eqp,eq);mdd=max(mdd,eqp-eq)
    hlds=[t.get('h',0) for t in trades if 'h' in t]
    monthly={}
    for t in trades:
        mk=t.get('m','?')
        if mk not in monthly:monthly[mk]={'trades':0,'wins':0,'pnl':0.0}
        monthly[mk]['trades']+=1
        if t['pnl']>0:monthly[mk]['wins']+=1
        monthly[mk]['pnl']+=t['pnl']
    ft=df['time'].iloc[0];lt=df['time'].iloc[-1];days=(lt-ft).days
    ms=days/30.0;tpm=total/ms if ms>0 else 0
    ah=np.mean(holds) if holds else 100
    return{
        'trades':total,'wr':round(wr,1),'pf':round(pf,2),
        'net_pnl':round(bal-BAL,2),'return_pct':round((bal-BAL)/BAL*100,2),
        'max_dd_pct':round(mdd/BAL*100,2),'avg_hurst':round(hs/max(hc,1),3),
        'hmm_blocks':hblk,'ghost':gh,'ghost_info':gi,
        'avg_win':round(np.mean(wins),2) if wins else 0,
        'avg_loss':round(np.mean(losses),2) if losses else 0,
        'avg_hold':round(np.mean(hlds),1) if hlds else 0,
        'gross_profit':round(gp,2),'gross_loss':round(gl,2),
        'monthly':monthly,'trades_per_month':round(tpm,1),'days':days,
        'dakad_wr':round(dakad.wr*100,1),
        'avg_hmm_hold':round(ah,1),'min_hold':min(holds) if holds else 100,
        'max_hold':max(holds) if holds else 100,
        'refits':len(holds),'rblk':rblk,'dblk':dblk,'sexit':sexit,
    }

def main():
    print("="*110)
    print("SHF v5.6 — BINARY HMM HOLD WITH HYSTERESIS (3-refit confirm)")
    print(f"shf_core version: {shf_core.__version__}")
    print("="*110)
    print(f"  Rule: H < {H_THRESHOLD} → hold={HOLD_MR} (MR)  |  H >= {H_THRESHOLD} → hold={HOLD_TREND} (Trending)")
    print(f"  Re-check every {REFIT_INTERVAL} bars\n")

    # Load previous results for comparison
    prev={}
    pp=Path("Results/faithful_live_backtest_results.json")
    if pp.exists():
        with open(pp) as f:pd2=json.load(f)
        for bp in pd2.get('best_pairs',[]):prev[bp['pair']]=bp
        for pn,rl in pd2.get('all_results',{}).items():
            for r in rl:
                if r.get('hmm_hold')==100:prev.setdefault(pn+'_s100',r)

    pair_data={}
    for p in ALL:
        try:
            df=load(p);pair_data[p.name]=(df,p)
            f=df['time'].iloc[0];l=df['time'].iloc[-1];d=(l-f).days
            print(f"  {p.name:<20} {len(df):>8,} bars | {f} to {l} ({d}d)")
        except Exception as e:print(f"  {p.name:<20} FAILED: {e}")

    results={}
    for name,(df,pdef) in pair_data.items():
        t0=time.time();r=run(df,pdef);elapsed=time.time()-t0
        results[name]=r
        gs=f" GHOST:{r['ghost_info']}" if r['ghost'] else ""
        expected=HOLD_MR if r['avg_hurst']<H_THRESHOLD else HOLD_TREND
        print(f"\n  {name:<20} {r['trades']:>5} trades ({r['trades_per_month']:.0f}/mo)  "
              f"WR={r['wr']:>5.1f}%  PF={r['pf']:>6.2f}  "
              f"P&L=${r['net_pnl']:>12,.2f}  Return={r['return_pct']:>7.2f}%  MaxDD={r['max_dd_pct']:>5.2f}%  "
              f"H={r['avg_hurst']:.3f}→hold={r['avg_hmm_hold']:.0f}  ({elapsed:.1f}s){gs}")

    # COMPARISON TABLE
    print(f"\n\n{'='*130}")
    print("COMPARISON: BINARY SWITCH vs STATIC HMM=100 vs BEST STATIC")
    print(f"{'='*130}")
    print(f"\n  {'Pair':<20} {'--- BINARY (H<0.53→100, else→5) ---':^36}  {'--- STATIC HMM=100 ---':^30}  {'--- BEST STATIC ---':^30}  Winner")
    print(f"  {'':20} {'Hold':>5}{'Trd':>5}{'WR':>6}{'PF':>7}{'Ret':>7}{'DD':>6}  "
          f"{'Trd':>5}{'WR':>6}{'PF':>7}{'Ret':>7}{'DD':>6}  "
          f"{'HMM':>4}{'Trd':>5}{'WR':>6}{'PF':>7}{'Ret':>7}{'DD':>6}")
    print(f"  {'-'*128}")

    for name,r in results.items():
        s100key=name+'_s100'
        s100=prev.get(s100key,{})
        best=prev.get(name,{})
        s_t=s100.get('trades',0);s_w=s100.get('wr',0);s_p=s100.get('pf',0)
        s_r=s100.get('return_pct',0);s_d=s100.get('max_dd_pct',0)
        b_h=best.get('best_hmm','?');b_t=best.get('trades',0);b_w=best.get('wr',0)
        b_p=best.get('pf',0);b_r=best.get('return_pct',0);b_d=best.get('max_dd_pct',0)
        pfs=[('BIN',r['pf']),('S100',s_p),('BEST',b_p)]
        pfs.sort(key=lambda x:x[1],reverse=True);win=pfs[0][0]
        print(f"  {name:<20} {r['avg_hmm_hold']:>5.0f}{r['trades']:>5}{r['wr']:>5.1f}%{r['pf']:>7.2f}{r['return_pct']:>6.2f}%{r['max_dd_pct']:>5.2f}%  "
              f"{s_t:>5}{s_w:>5.1f}%{s_p:>7.2f}{s_r:>6.2f}%{s_d:>5.2f}%  "
              f"{str(b_h):>4}{b_t:>5}{b_w:>5.1f}%{b_p:>7.2f}{b_r:>6.2f}%{b_d:>5.2f}%  {win}")

    # 3-PAIR PORTFOLIO
    core=["Forex Anchor","EURJPY/CHFJPY","Index Spread"]
    print(f"\n\n{'='*100}")
    print("3-PAIR PORTFOLIO (Forex Anchor + EURJPY/CHFJPY + Index Spread)")
    print(f"{'='*100}")
    bt=0;bp=0;bgp=0;bgl=0;bw=0
    for c in core:
        r=results.get(c,{});bt+=r.get('trades',0);bp+=r.get('net_pnl',0)
        bgp+=r.get('gross_profit',0);bgl+=r.get('gross_loss',0)
        bw+=int(r.get('trades',0)*r.get('wr',0)/100)
    bwr=bw/bt*100 if bt>0 else 0;bpf=bgp/bgl if bgl>0 else 0
    print(f"\n  Binary Switch:      {bt:>5} trades  WR={bwr:.1f}%  PF={bpf:.2f}  P&L=${bp:>12,.2f}  Return={bp/BAL*100:.2f}%")
    print(f"  Static HMM=100:       594 trades  WR=79.5%  PF=2.64  P&L=$   23,366.54  Return=23.37%")
    diff=bp-23366.54
    print(f"\n  DIFFERENCE: ${diff:>+12,.2f} ({diff/BAL*100:+.2f}%) → {'BINARY WINS' if diff>0 else 'STATIC WINS'}")

    # 6-PAIR
    print(f"\n\n{'='*100}")
    print("6-PAIR PORTFOLIO (ALL)")
    print(f"{'='*100}")
    at=sum(r.get('trades',0) for r in results.values())
    ap=sum(r.get('net_pnl',0) for r in results.values())
    agp=sum(r.get('gross_profit',0) for r in results.values())
    agl=sum(r.get('gross_loss',0) for r in results.values())
    apf=agp/agl if agl>0 else 0
    print(f"\n  Binary Switch:  {at} trades  PF={apf:.2f}  P&L=${ap:>12,.2f}")
    print(f"  Static HMM=100: P&L=$  465,205.55")

    # MONTHLY
    print(f"\n\n{'='*100}")
    print("MONTHLY BREAKDOWN (Binary HMM)")
    print(f"{'='*100}")
    for name,r in results.items():
        monthly=r.get('monthly',{})
        if not monthly:continue
        print(f"\n  {name} (hold={r['avg_hmm_hold']:.0f}, H={r['avg_hurst']:.3f}):")
        print(f"    {'Month':<10}{'Trades':>7}{'WR':>7}{'P&L':>12}")
        print(f"    {'-'*38}")
        for mo in sorted(monthly):
            md=monthly[mo];mwr=md['wins']/md['trades']*100 if md['trades']>0 else 0
            print(f"    {mo:<10}{md['trades']:>7}{mwr:>6.1f}%${md['pnl']:>11,.2f}")

    out=Path("Results/binary_hmm_results.json")
    def cv(o):
        if isinstance(o,(np.integer,)):return int(o)
        if isinstance(o,(np.floating,)):return float(o)
        if isinstance(o,(pd.Timestamp,datetime)):return str(o)
        return str(o)
    with open(out,'w') as f:
        json.dump({'method':'binary','rule':f'H<{H_THRESHOLD}→{HOLD_MR} else→{HOLD_TREND}',
            'threshold':H_THRESHOLD,'hold_mr':HOLD_MR,'hold_trend':HOLD_TREND,
            'results':{k:v for k,v in results.items()}},f,indent=2,default=cv)
    print(f"\n  Saved to {out}")

if __name__=="__main__":main()
