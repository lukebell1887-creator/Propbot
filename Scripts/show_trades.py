#!/usr/bin/env python3
"""Show every trade with entry/exit dates using optimal Gold/Silver params."""
import sys, math, numpy as np, pandas as pd
from pathlib import Path
from collections import deque
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shf_core

# Optimal params from Optuna
Z_ENTRY = 2.393; EXIT_Z = 0.432; DWELL = 9.3; HMM_HOLD = 36
HURDLE = 2.86; MULT = 2.49

# Load data
d = Path(__file__).resolve().parent.parent / "data" / "historical"
a = pd.read_csv(d / "XAUUSD_M1.csv", parse_dates=['time']).rename(columns={'close': 'close_a'})
b = pd.read_csv(d / "XAGUSD_M1.csv", parse_dates=['time']).rename(columns={'close': 'close_b'})
df = pd.merge(a[['time','close_a']], b[['time','close_b']], on='time', how='inner').sort_values('time').reset_index(drop=True)
df = df[(df['close_a'] > 0) & (df['close_b'] > 0)].reset_index(drop=True)

avg_a = df['close_a'].mean(); avg_b = df['close_b'].mean()
cs_a, cs_b = 100, 5000
notional = (cs_a * avg_a + cs_b * avg_b) / 2.0

print(f"Gold/Silver Trade Log — Optimal Params")
print(f"Z={Z_ENTRY} ExitZ={EXIT_Z} Dwell={DWELL} HMM={HMM_HOLD} Hurdle={HURDLE} Mult={MULT}")
print(f"Notional: ${notional:,.0f} | Data: {df['time'].iloc[0]} to {df['time'].iloc[-1]}")
print()

class DAKAD:
    def __init__(self):
        self._r = deque(maxlen=50)
        for _ in range(10): self._r.append(1)
        for _ in range(5): self._r.append(0)
    def record(self, w): self._r.append(1 if w else 0)
    def calc(self, tdd, ddd):
        ddr = max(0.001, 0.04 - ddd)
        wr = max(0.5, min(0.85, sum(self._r) / max(len(self._r), 1)))
        ns = math.log(1e-4) / math.log(1 - wr)
        base = max(0.003, min(0.03, (math.exp(40 * ddr) - 1) / (40 * ns)))
        return max(0.0005, base * math.exp(-40 * tdd))

class HMM:
    def __init__(self, lb=100, mh=20):
        self._lb = lb; self._cr = 0; self._buf = []; self._hc = 0; self._mh = mh
    def update(self, sr):
        self._buf.append(sr)
        if len(self._buf) > self._lb * 3: self._buf = self._buf[-self._lb * 2:]
        if len(self._buf) < 50: return 0
        r = np.array(self._buf[-self._lb:]); n = len(r); ws = min(20, n // 3)
        if ws < 5: return 0
        vols = [np.std(r[i:i+ws]) for i in range(0, n - ws + 1, ws)]
        if len(vols) < 3: return 0
        v40 = np.percentile(vols, 40); v80 = np.percentile(vols, 80)
        nr = 0 if vols[-1] <= v40 else (1 if vols[-1] <= v80 else 2)
        self._hc += 1
        if nr != self._cr and self._hc >= self._mh: self._cr = nr; self._hc = 0
        return self._cr
    @property
    def blocked(self): return self._cr >= 2

def sm(h):
    if 0 <= h < 7: return 2.0
    elif 7 <= h < 9: return 1.3
    elif 9 <= h < 17: return 1.0
    elif 17 <= h < 20: return 1.2
    else: return 1.8

def calc_cost(lots, h):
    s = (30 + 3) * lots * 2 * sm(h)
    c = 0.000009 * (avg_a * 100 + avg_b * 5000) * lots * 2
    return s + c

BAL = 100000; bal = BAL; peak = BAL; ds = BAL; dd = None; ghost = False
dakad = DAKAD(); corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=200)
consec = 0; gcool = 0
eng = shf_core.CointegrationEngine(span=100, beta=1.0, entry_z=Z_ENTRY, exit_z=EXIT_Z,
    z_base=Z_ENTRY, gamma=6.0, hurst_window=512, dynamic_z=True,
    exit_z_base=EXIT_Z, exit_gamma=2.0, dynamic_exit=True)
sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=0.15)
hmm = HMM(100, HMM_HOLD)

pos = 0; ez = 0; es = 0; ebar = 0; elots = 0
lsp = 0; psp = 0; sa = False; lcb = -9999; lch = 0.5; eh = 0
entry_time = None; trades = []

pa_arr = df['close_a'].values.astype(np.float64)
pb_arr = df['close_b'].values.astype(np.float64)
tp = pd.DatetimeIndex(df['time'])
hrs = tp.hour.values; mins = tp.minute.values; dates = tp.date

for bar in range(len(df)):
    if ghost: break
    p1 = pa_arr[bar]; p2 = pb_arr[bar]
    bh = int(hrs[bar]); bm = int(mins[bar]); cd = dates[bar]
    if cd != dd: dd = cd; ds = bal
    cdd = max(0, (peak - bal) / peak) if peak > 0 else 0
    ddd = max(0, (ds - bal) / ds) if ds > 0 else 0
    if ddd >= 0.04: ghost = True; break
    if cdd >= 0.09: ghost = True; break
    if bar < gcool: continue

    psp = lsp
    sig = eng.update(p1, p2)
    z = sig.z_score; s = sig.signal; sp = sig.spread; lsp = sp
    h = eng.last_hurst; exz = eng.last_exit_z
    if psp != 0: corr.push_return(0, sp - psp)
    la = math.log(p1) if p1 > 0 else 0
    lb = math.log(p2) if p2 > 0 else 0
    bk, abt = sen.update(la, lb)

    if abt and not sa:
        sa = True
        if pos != 0:
            gr = (sp - es) * pos * elots * notional
            c = calc_cost(elots, eh); pnl = gr - c
            bal += pnl; peak = max(peak, bal)
            w = pnl > 0; dakad.record(w)
            if not w: consec += 1
            else: consec = 0
            if consec >= 5: gcool = bar + 60; consec = 0
            trades.append({
                'entry': entry_time, 'exit': df['time'].iloc[bar],
                'pnl': pnl, 'gross': gr, 'cost': c, 'lots': elots,
                'z_entry': ez, 'bars': bar - ebar,
                'gold_entry': entry_gold, 'silver_entry': entry_silver,
                'gold_exit': p1, 'silver_exit': p2, 'reason': 'SENTINEL'
            })
            pos = 0; lcb = bar; lch = h
        continue
    if sa and not abt: sa = False
    if sa: continue

    hb2 = False
    if psp != 0: hmm.update(sp - psp); hb2 = hmm.blocked
    if bar < 200: continue

    if pos == 0 and s != 0:
        if hb2: continue
        bms = bh * 60 + bm
        if bms < 30 or (1440 - bms) < 30: continue
        if not (7 <= bh < 20): continue
        if lcb >= 0:
            da = max(1, DWELL * (lch / 0.3))
            if (bar - lcb) < da: continue
        risk = dakad.calc(cdd, ddd)
        corr.compute_risk(); cm = corr.last_risk_multiplier
        lots = max(0.01, round(bal * risk * cm / 1000, 2))
        if HURDLE > 0:
            ss = eng.last_std if hasattr(eng, 'last_std') else 0
            if ss > 0:
                zc = max(0, abs(z) - exz)
                ep = zc * ss * lots * notional
                tc = calc_cost(lots, bh)
                ratio = ep / tc if tc > 0 else 999
                if ratio < HURDLE: continue
                if MULT > 1 and ratio > HURDLE:
                    exc = (ratio - HURDLE) / HURDLE
                    ml = min(MULT, 1 + 0.5 * exc)
                    lots = max(0.01, round(lots * ml, 2))
        pos = s; ez = z; es = sp; ebar = bar; elots = lots; eh = bh
        entry_time = df['time'].iloc[bar]
        entry_gold = p1; entry_silver = p2

    elif pos != 0:
        ex = False; reason = ""
        ss = eng.last_std if hasattr(eng, 'last_std') else 0
        if ss > 0:
            uz = (sp - es) * pos / ss
            if uz < -4.815: ex = True; reason = "HUBER_STOP"
        if not ex and abs(z) > abs(ez) * 2.5: ex = True; reason = "EMERGENCY"
        if not ex and bh >= 19 and bm >= 45: ex = True; reason = "SESSION_END"
        if not ex:
            hbs = bar - ebar
            da = max(1, DWELL * (h / 0.3))
            if hbs < da: continue
            if pos == 1 and z > -exz: ex = True; reason = "REVERT"
            elif pos == -1 and z < exz: ex = True; reason = "REVERT"
        if ex:
            gr = (sp - es) * pos * elots * notional
            c = calc_cost(elots, eh); pnl = gr - c
            bal += pnl; peak = max(peak, bal)
            w = pnl > 0; dakad.record(w)
            if not w: consec += 1
            else: consec = 0
            if consec >= 5: gcool = bar + 60; consec = 0
            trades.append({
                'entry': entry_time, 'exit': df['time'].iloc[bar],
                'pnl': pnl, 'gross': gr, 'cost': c, 'lots': elots,
                'z_entry': ez, 'bars': bar - ebar,
                'gold_entry': entry_gold, 'silver_entry': entry_silver,
                'gold_exit': p1, 'silver_exit': p2, 'reason': reason
            })
            pos = 0; lcb = bar; lch = h

# Print results
print(f"{'#':>3}  {'Entry Date':>20}  {'Exit Date':>20}  {'Mins':>5}  {'Lots':>5}  {'|Z|':>6}  "
      f"{'Gross':>10}  {'Cost':>7}  {'Net P&L':>10}  {'Gold$':>8}  {'Silver$':>7}  {'Reason':>12}  {'W/L':>4}")
print("-" * 140)

for i, t in enumerate(trades):
    wl = "WIN" if t['pnl'] > 0 else "LOSS"
    entry_str = str(t['entry'])[:19]
    exit_str = str(t['exit'])[:19]
    print(f"{i+1:>3}  {entry_str:>20}  {exit_str:>20}  {t['bars']:>5}  {t['lots']:>5}  "
          f"{abs(t['z_entry']):>6.2f}  ${t['gross']:>9,.2f}  ${t['cost']:>6,.2f}  "
          f"${t['pnl']:>9,.2f}  ${t['gold_entry']:>7,.0f}  ${t['silver_entry']:>6,.1f}  "
          f"{t['reason']:>12}  {wl:>4}")

wins = sum(1 for t in trades if t['pnl'] > 0)
total = len(trades)
print(f"\nTotal: {total} trades | Wins: {wins} | WR: {wins/total*100:.1f}% | Net: ${bal - BAL:,.2f}")

# Show distribution by month
print("\nTrades by month:")
for t in trades:
    month = str(t['entry'])[:7]
    print(f"  {month}: Trade #{trades.index(t)+1}")
