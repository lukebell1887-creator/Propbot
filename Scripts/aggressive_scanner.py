#!/usr/bin/env python3
"""
AGGRESSIVE MULTI-PAIR SCANNER — "PhD Mode"
=============================================
Instead of fixed Z thresholds, uses PURE EXPECTED PROFIT gate:
- Computes E[profit] = Z_captured × sigma × lots × notional - costs
- Trades whenever E[profit] > min_profit_threshold
- Lower Z = more trades, safety nets handle risk
- Tests Z from 1.5 to 3.0 across all 3 pairs
- Shows the EXACT tradeoff: trades/month vs profitability

Also tests "PhD Mode": Z=0 (engine decides internally), ONLY gate is E[profit] > $X
"""

import sys, math, time, numpy as np, pandas as pd
from pathlib import Path
from collections import deque

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shf_core

# 5%ERS BROKER SPECS
PAIRS = {
    'Gold_Silver': {
        'sym_a': 'XAUUSD', 'sym_b': 'XAGUSD',
        'cs_a': 100, 'cs_b': 5000,
        'comm_pct': 0.000009, 'spread_a_pts': 30, 'spread_b_pts': 3,
    },
    'Oil_Spread': {
        'sym_a': 'XTIUSD', 'sym_b': 'XBRUSD',
        'cs_a': 100, 'cs_b': 100,
        'comm_pct': 0.0003, 'spread_a_pts': 4, 'spread_b_pts': 5,
    },
    'NAS_DAX': {
        'sym_a': 'US100', 'sym_b': 'DE40',
        'cs_a': 1, 'cs_b': 1,
        'comm_pct': 0, 'spread_a_pts': 2, 'spread_b_pts': 2,
    },
}

BAL = 100_000.0
WELFORD_SPAN = 100; GAMMA = 6.0; HURST_WINDOW = 512; EXIT_GAMMA = 2.0
GHOST_DAILY_DD = 0.04; GHOST_MAX_DD = 0.09
CORR_WINDOW = 200; COOLDOWN_BARS = 60; MAX_CONSEC_LOSSES = 5
ROLLOVER_LOCKOUT_MIN = 30; HMM_LOOKBACK = 100
HUBER_SIGMA = 4.815; MIN_WARMUP = 200
SESSION_START = 7; SESSION_END = 20

DAKAD_LAMBDA = 40.0; DAKAD_P_RUIN = 1e-4
DAKAD_DAILY_DD_CEIL = 0.04; DAKAD_RESULT_WINDOW = 50
DAKAD_MIN_WR = 0.50; DAKAD_MAX_WR = 0.85
DAKAD_MIN_BASE = 0.003; DAKAD_MAX_BASE = 0.03; DAKAD_RISK_FLOOR = 0.0005


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
    def __init__(self, lookback=100, min_regime_hold=20):
        self._lb = lookback; self._cr = 0; self._buf = []; self._hc = 0; self._mh = min_regime_hold
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


def get_spread_mult(hour):
    if 0 <= hour < 7: return 2.0
    elif 7 <= hour < 9: return 1.3
    elif 9 <= hour < 17: return 1.0
    elif 17 <= hour < 20: return 1.2
    else: return 1.8

def calc_cost(ps, lots, hour, pa, pb):
    sm = get_spread_mult(hour)
    sc = (ps['spread_a_pts'] + ps['spread_b_pts']) * lots * 2 * sm
    comm = ps['comm_pct'] * (pa * ps['cs_a'] + pb * ps['cs_b']) * lots * 2 if ps['comm_pct'] > 0 else 0
    return sc + comm

def load_pair(sa, sb):
    d = Path(__file__).resolve().parent.parent / "data" / "historical"
    a = pd.read_csv(d / f"{sa}_M1.csv", parse_dates=['time']).rename(columns={'close': 'close_a'})
    b = pd.read_csv(d / f"{sb}_M1.csv", parse_dates=['time']).rename(columns={'close': 'close_b'})
    m = pd.merge(a[['time','close_a']], b[['time','close_b']], on='time', how='inner').sort_values('time').reset_index(drop=True)
    return m[(m['close_a']>0)&(m['close_b']>0)].reset_index(drop=True)

def calc_notional(ps, pa, pb):
    return (ps['cs_a'] * pa + ps['cs_b'] * pb) / 2.0


def run_aggressive(df, ps, notional, avg_pa, avg_pb,
                    z_entry, exit_z, dwell_bars, hmm_hold,
                    min_profit=0.0, amp_hurdle=0.0, amp_max_mult=1.0):
    """
    Aggressive backtest — uses EXPECTED PROFIT gate instead of high Z.
    min_profit: minimum expected $ profit to enter (0 = any positive)
    """
    bal = BAL; peak = BAL; ds = BAL; dd = None; ghost = False
    dakad = DynamicAKAD()
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec = 0; gcool = 0; n = len(df)

    eng = shf_core.CointegrationEngine(
        span=WELFORD_SPAN, beta=1.0, entry_z=z_entry,
        exit_z=exit_z, z_base=z_entry, gamma=GAMMA, hurst_window=HURST_WINDOW,
        dynamic_z=True, exit_z_base=exit_z, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=0.15)
    hmm = HMMRegimeDetector(lookback=HMM_LOOKBACK, min_regime_hold=hmm_hold)

    pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0
    lsp = 0.0; psp = 0.0; sa = False; lcb = -9999; lch = 0.5; eh = 0
    trades = []; entry_time = None; entry_prices = (0, 0)

    pa_arr = df['close_a'].values.astype(np.float64)
    pb_arr = df['close_b'].values.astype(np.float64)
    tp = pd.DatetimeIndex(df['time'])
    hrs = tp.hour.values; mins = tp.minute.values; dates = tp.date

    blocked_reasons = {'hmm': 0, 'session': 0, 'rollover': 0, 'dwell': 0,
                       'amp': 0, 'profit': 0, 'cooldown': 0, 'warmup': 0}

    for bar in range(n):
        if ghost: break
        p1 = pa_arr[bar]; p2 = pb_arr[bar]
        bh = int(hrs[bar]); bm = int(mins[bar]); cd = dates[bar]
        if cd != dd: dd = cd; ds = bal
        cdd = max(0, (peak-bal)/peak) if peak > 0 else 0
        ddd = max(0, (ds-bal)/ds) if ds > 0 else 0
        if ddd >= GHOST_DAILY_DD: ghost = True; break
        if cdd >= GHOST_MAX_DD: ghost = True; break
        if bar < gcool:
            psp = lsp; sig = eng.update(p1, p2); lsp = sig.spread
            if psp != 0: corr.push_return(0, sig.spread - psp)
            la = math.log(p1) if p1 > 0 else 0; lb = math.log(p2) if p2 > 0 else 0
            sen.update(la, lb)
            if psp != 0: hmm.update(sig.spread - psp)
            continue

        psp = lsp
        sig = eng.update(p1, p2)
        z = sig.z_score; s = sig.signal; sp = sig.spread; lsp = sp
        h = eng.last_hurst; exz = eng.last_exit_z
        if psp != 0: corr.push_return(0, sp - psp)
        la = math.log(p1) if p1 > 0 else 0; lb = math.log(p2) if p2 > 0 else 0
        bk, abt = sen.update(la, lb)

        if abt and not sa:
            sa = True
            if pos != 0:
                gr = (sp - es) * pos * elots * notional
                c = calc_cost(ps, elots, eh, avg_pa, avg_pb)
                pnl = gr - c; bal += pnl; peak = max(peak, bal)
                w = pnl > 0; dakad.record(w)
                if not w: consec += 1
                else: consec = 0
                if consec >= MAX_CONSEC_LOSSES: gcool = bar + COOLDOWN_BARS; consec = 0
                trades.append({'pnl': pnl, 'gross': gr, 'cost': c, 'entry': entry_time,
                               'exit': tp[bar], 'bars': bar - ebar, 'reason': 'SENTINEL'})
                pos = 0; lcb = bar; lch = h
            continue
        if sa and not abt: sa = False
        if sa: continue

        hb2 = False
        if psp != 0: hmm.update(sp - psp); hb2 = hmm.blocked
        if bar < MIN_WARMUP: continue

        if pos == 0 and s != 0:
            if hb2: blocked_reasons['hmm'] += 1; continue
            bms = bh * 60 + bm
            if bms < ROLLOVER_LOCKOUT_MIN or (1440-bms) < ROLLOVER_LOCKOUT_MIN:
                blocked_reasons['rollover'] += 1; continue
            if not (SESSION_START <= bh < SESSION_END):
                blocked_reasons['session'] += 1; continue
            if lcb >= 0:
                da = max(1, dwell_bars * (lch / 0.3))
                if (bar - lcb) < da: blocked_reasons['dwell'] += 1; continue

            risk = dakad.calc(cdd, ddd)
            corr.compute_risk(); cm = corr.last_risk_multiplier
            lots = max(0.01, round(bal * risk * cm / 1000.0, 2))

            # EXPECTED PROFIT GATE — the "PhD" filter
            ss = eng.last_std if hasattr(eng, 'last_std') else 0
            if ss > 0:
                zc = max(0.0, abs(z) - exz)
                expected_profit = zc * ss * lots * notional
                total_cost = calc_cost(ps, lots, bh, avg_pa, avg_pb)
                net_expected = expected_profit - total_cost

                # Only trade if expected profit exceeds minimum
                if net_expected < min_profit:
                    blocked_reasons['profit'] += 1
                    continue

                # Amplitude hurdle (optional, on top of profit gate)
                if amp_hurdle > 0:
                    ratio = expected_profit / total_cost if total_cost > 0 else 999
                    if ratio < amp_hurdle:
                        blocked_reasons['amp'] += 1
                        continue
                    if amp_max_mult > 1.0 and ratio > amp_hurdle:
                        exc = (ratio - amp_hurdle) / amp_hurdle
                        ml = min(amp_max_mult, 1.0 + 0.5 * exc)
                        lots = max(0.01, round(lots * ml, 2))

            pos = s; ez = z; es = sp; ebar = bar; elots = lots; eh = bh
            entry_time = tp[bar]; entry_prices = (p1, p2)

        elif pos != 0:
            ex = False; reason = ""
            ss = eng.last_std if hasattr(eng, 'last_std') else 0
            if ss > 0:
                uz = (sp - es) * pos / ss
                if uz < -HUBER_SIGMA: ex = True; reason = "HUBER"
            if not ex and abs(z) > abs(ez) * 2.5: ex = True; reason = "EMERGENCY"
            if not ex and bh >= SESSION_END - 1 and bm >= 45: ex = True; reason = "SESSION"
            if not ex:
                hbs = bar - ebar
                da = max(1, dwell_bars * (h / 0.3))
                if hbs < da: continue
                if pos == 1 and z > -exz: ex = True; reason = "REVERT"
                elif pos == -1 and z < exz: ex = True; reason = "REVERT"
            if ex:
                gr = (sp - es) * pos * elots * notional
                c = calc_cost(ps, elots, eh, avg_pa, avg_pb)
                pnl = gr - c; bal += pnl; peak = max(peak, bal)
                w = pnl > 0; dakad.record(w)
                if not w: consec += 1
                else: consec = 0
                if consec >= MAX_CONSEC_LOSSES: gcool = bar + COOLDOWN_BARS; consec = 0
                trades.append({'pnl': pnl, 'gross': gr, 'cost': c, 'entry': entry_time,
                               'exit': tp[bar], 'bars': bar - ebar, 'reason': reason})
                pos = 0; lcb = bar; lch = h

    t = len(trades)
    if t < 1:
        return None, blocked_reasons

    wins = [p['pnl'] for p in trades if p['pnl'] > 0]
    losses = [p['pnl'] for p in trades if p['pnl'] <= 0]
    gp = sum(wins) if wins else 0
    gl = abs(sum(p['pnl'] for p in trades if p['pnl'] <= 0)) if losses else 0.001
    net = bal - BAL
    d1 = df['time'].iloc[0]; d2 = df['time'].iloc[-1]
    days = max(1, (d2-d1).days); months = max(0.1, days/30.0)
    eq = BAL; eqp = eq; mdd = 0
    for p in trades: eq += p['pnl']; eqp = max(eqp, eq); mdd = max(mdd, eqp - eq)

    # Distribution by month
    monthly = {}
    for tr in trades:
        m = str(tr['entry'])[:7]
        if m not in monthly: monthly[m] = 0
        monthly[m] += 1

    return {
        'trades': t, 'wr': round(len(wins)/t*100, 1), 'pf': round(gp/gl, 2),
        'net': round(net, 2), 'avg': round(net/t, 2),
        'dd': round(mdd/BAL*100, 2), 'ghost': ghost,
        'trpm': round(t/months, 1), 'months': round(months, 1),
        'monthly': monthly, 'trade_list': trades,
    }, blocked_reasons


def main():
    t0 = time.time()
    print("=" * 130)
    print("  AGGRESSIVE MULTI-PAIR SCANNER — PhD Mode")
    print("  Tests Z from 1.5 to 3.0 | Expected Profit gate | All safety nets active")
    print("=" * 130)

    # Test configs: (z_entry, exit_z, dwell, hmm_hold, min_profit, amp_hurdle, amp_mult, label)
    configs = [
        # PhD Mode: Low Z, no hurdle, just expected profit > $0
        (1.5, 0.3, 5,  10, 0,   0,   1.0, "Z=1.5 PhD (E[P]>$0)"),
        (1.8, 0.3, 5,  10, 0,   0,   1.0, "Z=1.8 PhD"),
        (2.0, 0.4, 8,  10, 0,   0,   1.0, "Z=2.0 PhD"),
        (2.0, 0.4, 8,  10, 0,   1.0, 1.5, "Z=2.0 Hurdle=1x"),
        (2.2, 0.4, 8,  15, 0,   0,   1.0, "Z=2.2 PhD"),
        (2.4, 0.4, 9,  36, 0,   0,   1.0, "Z=2.4 PhD (no hurdle)"),
        (2.4, 0.4, 9,  36, 0,   2.86,2.49,"Z=2.4 CURRENT (Hrdl=2.86)"),
        (2.8, 0.5, 10, 36, 0,   0,   1.0, "Z=2.8 PhD"),
        (3.0, 0.5, 10, 36, 0,   0,   1.0, "Z=3.0 PhD"),
    ]

    for pname, ps in PAIRS.items():
        df = load_pair(ps['sym_a'], ps['sym_b'])
        if df is None or len(df) < 2000: continue

        avg_a = df['close_a'].mean(); avg_b = df['close_b'].mean()
        notional = calc_notional(ps, avg_a, avg_b)
        days = (df['time'].iloc[-1] - df['time'].iloc[0]).days

        print(f"\n{'=' * 130}")
        print(f"  {pname} ({ps['sym_a']}/{ps['sym_b']}) | {len(df):,} bars | {days}d | Notional: ${notional:,.0f}")
        print(f"{'=' * 130}")
        print(f"  {'Config':<30} {'Trades':>6} {'Tr/Mo':>6} {'WR':>6} {'PF':>6} {'Net$':>12} {'$/Trade':>10} {'DD%':>6} {'Ghost':>6}  Monthly Distribution")
        print(f"  {'-'*140}")

        for z, exz, dw, hmm, mp, hrdl, mult, label in configs:
            r, blocked = run_aggressive(df, ps, notional, avg_a, avg_b,
                                         z, exz, dw, hmm, mp, hrdl, mult)
            if r is None:
                total_blocked = sum(blocked.values())
                top_blocker = max(blocked, key=blocked.get)
                print(f"  {label:<30} {'0':>6} {'0':>6} {'--':>6} {'--':>6} {'$0':>12} {'$0':>10} {'--':>6} {'--':>6}  "
                      f"BLOCKED: {top_blocker}={blocked[top_blocker]}")
                continue

            monthly_str = "  ".join(f"{m}:{c}" for m, c in sorted(r['monthly'].items()))
            ghost_str = "YES" if r['ghost'] else "no"
            print(f"  {label:<30} {r['trades']:>6} {r['trpm']:>6.1f} {r['wr']:>5.1f}% {r['pf']:>6.2f} "
                  f"${r['net']:>10,.2f} ${r['avg']:>9,.2f} {r['dd']:>5.2f}% {ghost_str:>6}  {monthly_str}")

    elapsed = time.time() - t0
    print(f"\n  Completed in {elapsed:.0f}s ({elapsed/60:.1f}min)")


if __name__ == "__main__":
    main()
