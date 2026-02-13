#!/usr/bin/env python3
"""FAST rollover check — just baseline + trade-by-hour analysis. No sensitivity sweep."""
import sys, math, time, json, numpy as np, pandas as pd
from pathlib import Path; from collections import deque, defaultdict; from dataclasses import dataclass
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shf_core

WELFORD_SPAN=100;Z_BASE=2.0;GAMMA=6.0;HURST_WINDOW=512;EXIT_Z_BASE=0.5;EXIT_GAMMA=2.0
DAKAD_LAMBDA=40.0;DAKAD_P_RUIN=1e-4;DAKAD_DAILY_DD_CEIL=0.04;DAKAD_RESULT_WINDOW=50
DAKAD_MIN_WR=0.50;DAKAD_MAX_WR=0.85;DAKAD_MIN_BASE=0.003;DAKAD_MAX_BASE=0.03;DAKAD_RISK_FLOOR=0.0005
GHOST_DAILY_DD=0.04;GHOST_MAX_DD=0.09;KALMAN_TOLERANCE=0.15;CORR_WINDOW=200
HMM_LOOKBACK=100;MAX_CONSEC_LOSSES=5;COOLDOWN_BARS=60;MIN_WARMUP_BARS=200;BAL=100_000.0
ROLLOVER_MIN=30

@dataclass
class PC:
    sa:float;sb:float;cr:float;cp:float
COSTS={"Index Spread":PC(1,1,0,0),"Oil Spread":PC(4,5,0,0.0003)}
OIL_NOT=6500.0

def smult(h):
    if 0<=h<7:return 1.8
    elif 7<=h<9:return 1.2
    elif 9<=h<17:return 1.0
    elif 17<=h<21:return 1.1
    else:return 1.5

def ccost(pn,lots,h):
    pc=COSTS[pn];m=smult(h);sc=(pc.sa*2+pc.sb*2)*lots*m
    cm=pc.cp*OIL_NOT*lots*4 if pc.cp>0 else pc.cr*lots
    return sc+cm

class DAKAD:
    def __init__(self):
        self._r=deque(maxlen=DAKAD_RESULT_WINDOW)
        for _ in range(10):self._r.append(1)
        for _ in range(5):self._r.append(0)
    def rec(self,w):self._r.append(1 if w else 0)
    def calc(self,tdd,ddd):
        ddr=max(0.001,DAKAD_DAILY_DD_CEIL-ddd);wr=max(DAKAD_MIN_WR,min(DAKAD_MAX_WR,sum(self._r)/max(len(self._r),1)))
        ns=math.log(DAKAD_P_RUIN)/math.log(1.0-wr)
        base=max(DAKAD_MIN_BASE,min(DAKAD_MAX_BASE,(math.exp(DAKAD_LAMBDA*ddr)-1)/(DAKAD_LAMBDA*ns)))
        return max(DAKAD_RISK_FLOOR,base*math.exp(-DAKAD_LAMBDA*tdd))

class HMM:
    def __init__(self,lb=100,mh=100):
        self._lb=lb;self._cr=0;self._rb=[];self._hc=0;self._mh=mh
    def update(self,sr):
        self._rb.append(sr)
        if len(self._rb)>self._lb*3:self._rb=self._rb[-self._lb*2:]
        if len(self._rb)<50:return 0
        r=np.array(self._rb[-self._lb:]);n=len(r);ws=min(20,n//3)
        if ws<5:return 0
        vols=[np.std(r[i:i+ws]) for i in range(0,n-ws+1,ws)]
        if len(vols)<3:return 0
        v40=np.percentile(vols,40);v80=np.percentile(vols,80)
        nr=0 if vols[-1]<=v40 else(1 if vols[-1]<=v80 else 2)
        self._hc+=1
        if nr!=self._cr and self._hc>=self._mh:self._cr=nr;self._hc=0
        return self._cr
    @property
    def blocked(self):return self._cr>=2

@dataclass
class PD:
    name:str;fa:str;fb:str;pi:int;not_:float;db:float;da:float;dmin:float;dmax:float;hh:int

PAIRS=[
    PD("Index Spread","US100_M1.csv","DE40_M1.csv",0,150000,60,0.3,30,300,20),
    PD("Oil Spread","XTIUSD_M1.csv","XBRUSD_M1.csv",1,100000,1800,0.3,900,9000,5),
]

def dwell(h,p):return max(p.dmin,min(p.dmax,p.db*(h/p.da)))

def load(p):
    d=Path(__file__).resolve().parent.parent/"data"/"historical"
    a=pd.read_csv(d/p.fa,parse_dates=['time']).rename(columns={'close':'ca'})
    b=pd.read_csv(d/p.fb,parse_dates=['time']).rename(columns={'close':'cb'})
    m=pd.merge(a[['time','ca']],b[['time','cb']],on='time',how='inner').sort_values('time').reset_index(drop=True)
    return m[(m['ca']>0)&(m['cb']>0)].reset_index(drop=True)

def run(df,p):
    bal=BAL;pk=BAL;ds=BAL;dd_=None;gh=False
    dk=DAKAD();cr=shf_core.CorrelationRiskMonitor(n_pairs=3,window=CORR_WINDOW);con=0;gc=0
    trades=[];n=len(df)
    eng=shf_core.CointegrationEngine(span=WELFORD_SPAN,beta=1.0,entry_z=Z_BASE,exit_z=EXIT_Z_BASE,
        z_base=Z_BASE,gamma=GAMMA,hurst_window=HURST_WINDOW,dynamic_z=True,
        exit_z_base=EXIT_Z_BASE,exit_gamma=EXIT_GAMMA,dynamic_exit=True)
    sen=shf_core.KalmanSentinel(static_beta=1.0,beta_tolerance=KALMAN_TOLERANCE)
    hmm=HMM(lb=HMM_LOOKBACK,mh=p.hh)
    pos=0;ez=0;es=0;eb=0;el=0;ls=0;ps=0;sa=False;lcb=-9999;lch=0.5;eh=0;et=None
    
    for bar in range(n):
        if gh:break
        row=df.iloc[bar];bt=row['time'];pa=float(row['ca']);pb=float(row['cb'])
        cd=bt.date() if hasattr(bt,'date') else None
        if cd and cd!=dd_:dd_=cd;ds=bal
        cdd=max(0,(pk-bal)/pk) if pk>0 else 0
        ddd=max(0,(ds-bal)/ds) if ds>0 else 0
        if ddd>=GHOST_DAILY_DD:gh=True;break
        if cdd>=GHOST_MAX_DD:gh=True;break
        if bar<gc:continue
        ps=ls;sig=eng.update(pa,pb);z=sig.z_score;s=sig.signal;sp=sig.spread;ls=sp
        h=eng.last_hurst;exz=eng.last_exit_z
        if ps!=0:cr.push_return(min(p.pi,2),sp-ps)
        la=math.log(pa) if pa>0 else 0;lb_=math.log(pb) if pb>0 else 0
        beta,abort=sen.update(la,lb_)
        if abort and not sa:
            sa=True
            if pos!=0:
                gr=(sp-es)*pos*el*p.not_;co=ccost(p.name,el,eh);pnl=gr-co;bal+=pnl;pk=max(pk,bal)
                w=pnl>0;dk.rec(w)
                if not w:con+=1
                else:con=0
                if con>=MAX_CONSEC_LOSSES:gc=bar+COOLDOWN_BARS;con=0
                trades.append({'pnl':pnl,'gr':gr,'co':co,'hold':bar-eb,'eh':eh,'xh':bt.hour,
                    'et':et,'xt':bt,'span':et.date()!=bt.date() if et else False,'r':'SENTINEL'})
                pos=0;lcb=bar;lch=h
            continue
        if sa and not abort:sa=False
        if sa:continue
        hb=False
        if ps!=0:hmm.update(sp-ps);hb=hmm.blocked
        if bar<MIN_WARMUP_BARS:continue
        msm=bt.hour*60+bt.minute;mbm=1440-msm
        inr=msm<ROLLOVER_MIN or mbm<ROLLOVER_MIN
        if pos==0 and s!=0:
            if hb:continue
            if inr:continue
            if lcb>=0:
                cb=dwell(lch,p)/60.0
                if(bar-lcb)<cb:continue
            risk=dk.calc(cdd,ddd);cr.compute_risk();cm=cr.last_risk_multiplier
            lots=max(0.01,round(bal*risk*cm/1000.0,2))
            pos=s;ez=z;es=sp;eb=bar;el=lots;eh=bt.hour;et=bt
        elif pos!=0:
            ex=False;reason=""
            if abs(z)>abs(ez)*2.5:ex=True;reason="EMERGENCY"
            if not ex:
                hb_=bar-eb;db=dwell(h,p)/60.0
                if hb_<db:continue
                if pos==1 and z>-exz:ex=True;reason="DYN_EXIT"
                elif pos==-1 and z<exz:ex=True;reason="DYN_EXIT"
            if ex:
                gr=(sp-es)*pos*el*p.not_;co=ccost(p.name,el,eh);pnl=gr-co;bal+=pnl;pk=max(pk,bal)
                w=pnl>0;dk.rec(w)
                if not w:con+=1
                else:con=0
                if con>=MAX_CONSEC_LOSSES:gc=bar+COOLDOWN_BARS;con=0
                trades.append({'pnl':pnl,'gr':gr,'co':co,'hold':bar-eb,'eh':eh,'xh':bt.hour,
                    'et':et,'xt':bt,'span':et.date()!=bt.date() if et else False,'r':reason})
                pos=0;lcb=bar;lch=h
    return trades,bal

def main():
    t0=time.time()
    print("="*100)
    print("ROLLOVER PARANOIA CHECK — Quick Version")
    print("="*100)
    
    # Part 1: Spread gap analysis
    print("\n--- PART 1: SPREAD GAP AT MIDNIGHT ---")
    for p in PAIRS:
        df=load(p);df2=df.copy()
        df2['ls']=np.log(df2['ca'])-np.log(df2['cb'])
        df2['sc']=df2['ls'].diff().abs()
        df2['tg']=df2['time'].diff().dt.total_seconds()
        df2['h']=df2['time'].dt.hour
        gap=df2[(df2['tg']>120)&(df2['h'].isin([1,2]))]
        norm=df2[(df2['tg']<=120)&(df2['tg']>0)]
        ratio=gap['sc'].mean()/norm['sc'].mean() if len(gap)>0 and norm['sc'].mean()>0 else 0
        print(f"\n  {p.name}: {len(gap)} midnight gaps")
        print(f"    Avg spread jump at gap:   {gap['sc'].mean():.6f}" if len(gap)>0 else "    No gaps")
        print(f"    Avg NORMAL spread change:  {norm['sc'].mean():.6f}")
        print(f"    Gap/Normal ratio:          {ratio:.2f}x")
        print(f"    Verdict: {'WARNING - gap bigger than normal!' if ratio>3 else 'MODERATE' if ratio>1.5 else 'OK - similar to normal'}")
    
    # Part 2: Run backtests and analyze by hour
    print("\n\n--- PART 2: P&L BY ENTRY HOUR ---")
    all_trades={}
    for p in PAIRS:
        df=load(p)
        trades,bal=run(df,p)
        all_trades[p.name]=trades
        net=sum(t['pnl'] for t in trades)
        wins=[t for t in trades if t['pnl']>0]
        wr=len(wins)/len(trades)*100 if trades else 0
        print(f"\n  {p.name}: {len(trades)} trades, WR={wr:.1f}%, Net=${net:,.2f}")
        
        hp=defaultdict(lambda:{'pnl':0,'n':0,'w':0})
        for t in trades:
            h=t['eh'];hp[h]['pnl']+=t['pnl'];hp[h]['n']+=1
            if t['pnl']>0:hp[h]['w']+=1
        
        print(f"    {'Hour':>6} {'Trades':>7} {'WR':>6} {'Net P&L':>12} {'AvgP&L':>10} {'%Total':>8}")
        for h in sorted(hp.keys()):
            d=hp[h];avg=d['pnl']/d['n'] if d['n'] else 0
            pct=d['pnl']/net*100 if net!=0 else 0
            w=d['w']/d['n']*100 if d['n'] else 0
            flag=" <<<" if h in[23,1,2,3] and abs(pct)>15 else ""
            print(f"    {h:>6} {d['n']:>7} {w:>5.1f}% ${d['pnl']:>11,.2f} ${avg:>9,.2f} {pct:>7.1f}%{flag}")
        
        # Midnight spanning
        mid=[t for t in trades if t['span']]
        mid_pnl=sum(t['pnl'] for t in mid)
        nonmid_pnl=sum(t['pnl'] for t in trades if not t['span'])
        print(f"\n    Midnight-spanning: {len(mid)} trades ({len(mid)/len(trades)*100:.1f}%), P&L=${mid_pnl:,.2f} ({mid_pnl/net*100:.1f}%)")
        print(f"    Non-midnight:      {len(trades)-len(mid)} trades, P&L=${nonmid_pnl:,.2f} ({nonmid_pnl/net*100:.1f}%)")
        
        # Thin hours
        thin=[t for t in trades if t['eh'] in[22,23,1,2,3]]
        thin_pnl=sum(t['pnl'] for t in thin)
        print(f"    Thin hours (22-3): {len(thin)} trades ({len(thin)/len(trades)*100:.1f}%), P&L=${thin_pnl:,.2f} ({thin_pnl/net*100:.1f}%)")
    
    # Part 3: Nuclear test
    print("\n\n--- PART 3: NUCLEAR TEST ---")
    total_all=0;total_clean=0
    for pn,trades in all_trades.items():
        net=sum(t['pnl'] for t in trades)
        total_all+=net
        
        clean=[t for t in trades if t['eh'] not in[22,23,1,2,3] and not t['span']]
        net_c=sum(t['pnl'] for t in clean)
        total_clean+=net_c
        
        no_mid=[t for t in trades if not t['span']]
        net_nm=sum(t['pnl'] for t in no_mid)
        
        no_thin=[t for t in trades if t['eh'] not in[22,23,1,2,3]]
        net_nt=sum(t['pnl'] for t in no_thin)
        
        print(f"\n  {pn}:")
        print(f"    ALL trades:                      {len(trades):>5} trades  ${net:>11,.2f}  (100%)")
        print(f"    Remove thin-hour entries (22-3):  {len(no_thin):>5} trades  ${net_nt:>11,.2f}  ({net_nt/net*100:.1f}%)")
        print(f"    Remove midnight-spanners:         {len(no_mid):>5} trades  ${net_nm:>11,.2f}  ({net_nm/net*100:.1f}%)")
        print(f"    MAXIMUM PARANOIA (both):          {len(clean):>5} trades  ${net_c:>11,.2f}  ({net_c/net*100:.1f}%)")
        
        if net_c/net<0.5:print(f"    >>> RED FLAG!")
        elif net_c/net<0.75:print(f"    >>> YELLOW: Some edge-hour dependency")
        else:print(f"    >>> GREEN: Edge is real, not midnight-dependent")
    
    pct=total_clean/total_all*100 if total_all!=0 else 0
    print(f"\n  COMBINED: ${total_all:>,.2f} total, ${total_clean:>,.2f} clean ({pct:.1f}% survives)")
    
    if pct>=75:print(f"\n  VERDICT: EDGE IS REAL. {pct:.0f}% survives max paranoia filter.")
    elif pct>=50:print(f"\n  VERDICT: MOSTLY REAL but edge hours help. Consider wider lockout.")
    else:print(f"\n  VERDICT: RED FLAG — investigate further!")
    
    print(f"\n  Completed in {time.time()-t0:.1f}s")

if __name__=="__main__":
    main()
