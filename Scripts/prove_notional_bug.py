#!/usr/bin/env python3
"""
PROOF: The Oil backtest notional (100,000) is ~16x too large.
5%ers contract size = 100 barrels/lot.
Real notional = contract_size × price ≈ 6,200.

This script runs the EXACT same backtest logic as test_oil_index_live.py
but with BOTH notionals side by side, proving the 79% WR is fake.
"""

import sys, math, time
import numpy as np
import pandas as pd
from pathlib import Path
from collections import deque

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shf_core

# ============================================================================
# EXACT SAME PARAMS AS test_oil_index_live.py
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

OIL_DWELL_BASE = 1800.0; OIL_DWELL_ANCHOR = 0.3
OIL_DWELL_MIN = 900.0;   OIL_DWELL_MAX = 9000.0
OIL_NOTIONAL_PER_LOT = 6500.0

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

def get_spread_multiplier(hour):
    if 0 <= hour < 7: return 1.8
    elif 7 <= hour < 9: return 1.2
    elif 9 <= hour < 17: return 1.0
    elif 17 <= hour < 21: return 1.1
    else: return 1.5

def calc_trade_cost(lots, hour):
    mult = get_spread_multiplier(hour)
    spread_cost = (4.0 * 2 + 5.0 * 2) * lots * mult  # $18/lot base
    comm = 0.0003 * OIL_NOTIONAL_PER_LOT * lots * 4   # 0.03% commission
    return spread_cost + comm

def calc_dwell(h):
    return max(OIL_DWELL_MIN, min(OIL_DWELL_MAX, OIL_DWELL_BASE * (h / OIL_DWELL_ANCHOR)))

def is_rollover(t):
    m = t.hour * 60 + t.minute
    return m < ROLLOVER_LOCKOUT_MIN or (1440 - m) < ROLLOVER_LOCKOUT_MIN

def load_oil():
    d = Path(__file__).resolve().parent.parent / "data" / "historical"
    a = pd.read_csv(d / "XTIUSD_M1.csv", parse_dates=['time']).rename(columns={'close':'close_a'})
    b = pd.read_csv(d / "XBRUSD_M1.csv", parse_dates=['time']).rename(columns={'close':'close_b'})
    m = pd.merge(a[['time','close_a']], b[['time','close_b']], on='time', how='inner').sort_values('time').reset_index(drop=True)
    return m[(m['close_a']>0)&(m['close_b']>0)].reset_index(drop=True)


def run_with_notional(df, notional, hmm_hold=5, label=""):
    """Run EXACT backtest logic with specified notional."""
    balance = STARTING_BALANCE; peak = STARTING_BALANCE; daily_start = STARTING_BALANCE
    daily_date = None; ghost = False
    dakad = DynamicAKAD()
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec = 0; gcool = 0; n = len(df)

    eng = shf_core.CointegrationEngine(span=WELFORD_SPAN, beta=1.0, entry_z=Z_BASE,
        exit_z=EXIT_Z_BASE, z_base=Z_BASE, gamma=GAMMA, hurst_window=HURST_WINDOW,
        dynamic_z=True, exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
    hmm = HMMRegimeDetector(lookback=HMM_LOOKBACK, min_regime_hold=hmm_hold)

    pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0
    lspread = 0.0; pspread = 0.0; sent_abort = False
    last_close_bar = -9999; last_close_h = 0.5
    entry_hour = 0
    trades = []

    for bar in range(n):
        if ghost: break
        row = df.iloc[bar]; bt = row['time']
        pa = float(row['close_a']); pb = float(row['close_b'])

        cd = bt.date() if hasattr(bt,'date') else None
        if cd and cd != daily_date: daily_date = cd; daily_start = balance

        cdd = max(0,(peak-balance)/peak) if peak>0 else 0
        ddd = max(0,(daily_start-balance)/daily_start) if daily_start>0 else 0
        if ddd >= GHOST_DAILY_DD: ghost = True; break
        if cdd >= GHOST_MAX_DD: ghost = True; break
        if bar < gcool: continue

        pspread = lspread
        sig = eng.update(pa, pb)
        z = sig.z_score; s = sig.signal; spread = sig.spread; lspread = spread
        h = eng.last_hurst; exz = eng.last_exit_z

        pidx = 1
        if pspread != 0.0: corr.push_return(pidx, spread - pspread)

        la = math.log(pa) if pa>0 else 0; lb = math.log(pb) if pb>0 else 0
        beta, abort = sen.update(la, lb)
        if abort and not sent_abort:
            sent_abort = True
            if pos != 0:
                gross = (spread-es)*pos*elots*notional
                cost = calc_trade_cost(elots, entry_hour)
                pnl = gross - cost; balance += pnl; peak = max(peak,balance)
                w = pnl > 0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                trades.append({'pnl':pnl,'gross':gross,'cost':cost,'hold':bar-ebar})
                pos=0; last_close_bar=bar; last_close_h=h
            continue
        if sent_abort and not abort: sent_abort = False
        if sent_abort: continue

        hblocked = False
        if pspread != 0.0:
            hmm.update(spread - pspread); hblocked = hmm.is_blocked

        if bar < MIN_WARMUP_BARS: continue

        if pos == 0 and s != 0:
            if hblocked: continue
            if is_rollover(bt): continue
            if last_close_bar >= 0:
                cb = calc_dwell(last_close_h) / 60.0
                if (bar - last_close_bar) < cb: continue

            risk = dakad.calc(cdd, ddd)
            corr.compute_risk(); cm = corr.last_risk_multiplier
            lots = max(0.01, round(balance * risk * cm / 1000.0, 2))

            pos = s; ez = z; es = spread; ebar = bar; elots = lots
            entry_hour = bt.hour

        elif pos != 0:
            ex = False; reason = ""
            if abs(z) > abs(ez) * 2.5: ex = True; reason = "EMERGENCY"
            if not ex:
                hb = bar - ebar
                db = calc_dwell(h) / 60.0
                if hb < db: continue
                if pos == 1 and z > -exz: ex = True; reason = "EXIT"
                elif pos == -1 and z < exz: ex = True; reason = "EXIT"
            if ex:
                gross = (spread-es)*pos*elots*notional
                cost = calc_trade_cost(elots, entry_hour)
                pnl = gross - cost; balance += pnl; peak = max(peak,balance)
                w = pnl > 0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                trades.append({'pnl':pnl,'gross':gross,'cost':cost,'hold':bar-ebar})
                pos=0; last_close_bar=bar; last_close_h=h

    total = len(trades)
    if total == 0:
        return None

    pnls = [t['pnl'] for t in trades]
    gross_pnls = [t['gross'] for t in trades]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    gp = sum(wins) if wins else 0; gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins)/total*100
    gross_wins = [g for g in gross_pnls if g > 0]
    gross_wr = len(gross_wins)/total*100
    pf = gp/gl if gl>0 else 0

    return {
        'label': label,
        'notional': notional,
        'trades': total,
        'net_wr': round(wr,1),
        'gross_wr': round(gross_wr,1),
        'pf': round(pf, 2),
        'gross_pnl': round(sum(gross_pnls),2),
        'total_costs': round(sum(t['cost'] for t in trades),2),
        'net_pnl': round(balance-STARTING_BALANCE,2),
        'avg_gross': round(sum(gross_pnls)/total,2),
        'avg_cost': round(sum(t['cost'] for t in trades)/total,2),
        'avg_net': round((balance-STARTING_BALANCE)/total,2),
        'ghost': ghost,
    }


def main():
    print("="*90)
    print("  PROOF: Oil Backtest Notional Bug — 100,000 vs REAL 6,200")
    print("="*90)

    print("\nLoading Oil data...")
    df = load_oil()
    print(f"  {len(df):,} bars loaded")

    # Get actual average price for dynamic notional
    avg_price_a = df['close_a'].mean()
    avg_price_b = df['close_b'].mean()
    contract_size = 100  # 5%ers: 100 barrels per lot
    real_notional = round(contract_size * avg_price_a)

    print(f"  Avg XTIUSD price: ${avg_price_a:.2f}")
    print(f"  Avg XBRUSD price: ${avg_price_b:.2f}")
    print(f"  Contract size: {contract_size} barrels/lot")
    print(f"  Real notional (100 x ${avg_price_a:.2f}): ${real_notional:,}")
    print(f"  Backtest notional: $100,000")
    print(f"  Inflation factor: {100_000/real_notional:.1f}x")

    # Run with HMM=5 (the one that completed successfully before)
    print(f"\n{'='*90}")
    print("  Running IDENTICAL backtest with TWO notional values (HMM=5)...")
    print(f"{'='*90}")

    t0 = time.time()
    r_fake = run_with_notional(df, notional=100_000, hmm_hold=5, label="BACKTEST (notional=100K)")
    t1 = time.time()
    print(f"  Run 1 (notional=100K): {t1-t0:.1f}s — {r_fake['trades']} trades")

    t0 = time.time()
    r_real = run_with_notional(df, notional=real_notional, hmm_hold=5, label=f"REALITY (notional={real_notional:,})")
    t1 = time.time()
    print(f"  Run 2 (notional={real_notional:,}): {t1-t0:.1f}s — {r_real['trades']} trades")

    # Also run at $5K balance to show it's the same problem
    print(f"\n{'='*90}")
    print("  SIDE BY SIDE COMPARISON")
    print(f"{'='*90}")

    for r in [r_fake, r_real]:
        print(f"\n  --- {r['label']} ---")
        print(f"  Notional:      ${r['notional']:>10,}")
        print(f"  Trades:        {r['trades']}")
        print(f"  Net Win Rate:  {r['net_wr']}%")
        print(f"  Gross Win Rate:{r['gross_wr']}%")
        print(f"  Profit Factor: {r['pf']}")
        print(f"  Gross P&L:     ${r['gross_pnl']:>12,.2f}")
        print(f"  Total Costs:   ${r['total_costs']:>12,.2f}")
        print(f"  Net P&L:       ${r['net_pnl']:>12,.2f}")
        print(f"  Avg gross/trade: ${r['avg_gross']:>8.2f}")
        print(f"  Avg cost/trade:  ${r['avg_cost']:>8.2f}")
        print(f"  Avg net/trade:   ${r['avg_net']:>8.2f}")
        if r['ghost']:
            print(f"  *** GHOST STOP HIT ***")

    print(f"\n{'='*90}")
    print("  VERDICT")
    print(f"{'='*90}")
    inflation = 100_000 / real_notional
    print(f"""
  The backtest uses notional = 100,000 for Oil P&L calculation.
  
  5%ers contract spec: 100 barrels per lot.
  Real notional = contract_size x price = 100 x ${avg_price_a:.2f} = ${real_notional:,}
  
  The backtest INFLATES gross P&L by {inflation:.1f}x.
  Trading costs are modeled correctly in real dollars.
  
  With REAL notional ({real_notional:,}):
    - Net WR drops from {r_fake['net_wr']}% to {r_real['net_wr']}%
    - Net P&L drops from ${r_fake['net_pnl']:,.2f} to ${r_real['net_pnl']:,.2f}
    - Avg $/trade drops from ${r_fake['avg_net']:.2f} to ${r_real['avg_net']:.2f}
  
  The live engine (engine.py) gets P&L from MT5, which uses the REAL 
  contract size of 100 barrels. So live trades generate ~{inflation:.0f}x LESS 
  gross profit than the backtest predicts, but pay the same costs.
  
  THIS IS WHY EVERY LIVE TRADE LOSES.
  
  The amplitude gate in engine.py ALSO uses notional=100,000:
    expected_profit = z_captured * sigma * lots * cfg.notional
  This makes it think trades are {inflation:.0f}x more profitable than reality,
  so it never blocks and always scales lots UP — making losses bigger.
""")


if __name__ == "__main__":
    main()
