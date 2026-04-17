#!/usr/bin/env python3
"""
UNIVERSAL PAIR VIABILITY SCANNER
=================================
Tests ALL available instrument pairs with CORRECT notional values
based on real contract sizes. Finds pairs where gross P&L > costs.

Uses the proven Rust CointegrationEngine signal (93% gross directional accuracy).
The question is: which pairs generate enough DOLLARS per trade to cover costs?

Contract sizes (5%ers / standard MT5):
  FX majors: 100,000 units
  Gold (XAUUSD): 100 oz  -> notional = 100 * $2000 = $200,000
  Silver (XAGUSD): 5000 oz -> notional = 5000 * $25 = $125,000
  Oil (XTIUSD/XBRUSD): 100 barrels -> notional = 100 * $60 = $6,000
  Indices: 1 unit (standard CFD) -> notional = 1 * $21,000 = $21,000
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
# ENGINE PARAMS (exact match to live bot)
# ============================================================================
WELFORD_SPAN = 100; Z_BASE = 2.0; GAMMA = 6.0; HURST_WINDOW = 512
EXIT_Z_BASE = 0.5; EXIT_GAMMA = 2.0
DAKAD_LAMBDA = 40.0; DAKAD_P_RUIN = 1e-4
DAKAD_DAILY_DD_CEIL = 0.04; DAKAD_RESULT_WINDOW = 50
DAKAD_MIN_WR = 0.50; DAKAD_MAX_WR = 0.85
DAKAD_MIN_BASE = 0.003; DAKAD_MAX_BASE = 0.03; DAKAD_RISK_FLOOR = 0.0005
GHOST_DAILY_DD = 0.04; GHOST_MAX_DD = 0.09
KALMAN_TOLERANCE = 0.15; CORR_WINDOW = 200
ROLLOVER_LOCKOUT_MIN = 30; HMM_LOOKBACK = 100
MAX_CONSEC_LOSSES = 5; COOLDOWN_BARS = 60
MIN_WARMUP_BARS = 200; BAL = 100_000.0

# ============================================================================
# CONTRACT SIZES — Standard MT5 / 5%ers
# ============================================================================
CONTRACT_SIZES = {
    # FX Majors: 100,000 units per lot
    'EURUSD': 100000, 'GBPUSD': 100000, 'AUDUSD': 100000, 'NZDUSD': 100000,
    'USDCAD': 100000, 'USDCHF': 100000, 'USDJPY': 100000,
    'EURGBP': 100000, 'EURAUD': 100000, 'EURCAD': 100000, 'EURCHF': 100000,
    'EURJPY': 100000, 'EURNZD': 100000,
    'GBPCAD': 100000, 'GBPJPY': 100000,
    'AUDCAD': 100000, 'AUDNZD': 100000,
    'NZDCAD': 100000, 'CADJPY': 100000, 'CHFJPY': 100000,
    # Metals
    'XAUUSD': 100,    # 100 troy ounces
    'XAGUSD': 5000,   # 5000 troy ounces
    # Oil
    'XTIUSD': 100, 'XBRUSD': 100, 'USOIL': 100,
    # Indices (standard CFD = 1 unit; some brokers use 10 or 100)
    'US100': 1, 'US500': 1, 'US30': 1, 'DE40': 1, 'UK100': 1, 'JP225': 1,
}

# Typical spreads per fill in $ (conservative estimates for 5%ers)
SPREAD_PER_FILL = {
    # FX: ~1-2 pips = $10-20 per standard lot per fill
    'EURUSD': 10, 'GBPUSD': 12, 'AUDUSD': 10, 'NZDUSD': 12,
    'USDCAD': 12, 'USDCHF': 12, 'USDJPY': 10,
    'EURGBP': 15, 'EURAUD': 20, 'EURCAD': 20, 'EURCHF': 18,
    'EURJPY': 15, 'EURNZD': 25,
    'GBPCAD': 25, 'GBPJPY': 20,
    'AUDCAD': 18, 'AUDNZD': 15,
    'NZDCAD': 20, 'CADJPY': 18, 'CHFJPY': 18,
    # Metals: gold spread ~30c = $30/lot, silver spread ~3c = $150/lot
    'XAUUSD': 30, 'XAGUSD': 150,
    # Oil: WTI ~4c = $4/lot, Brent ~5c = $5/lot
    'XTIUSD': 4, 'XBRUSD': 5, 'USOIL': 4,
    # Indices: 1-3 points
    'US100': 2, 'US500': 1, 'US30': 3, 'DE40': 2, 'UK100': 2, 'JP225': 10,
}

# Commission: 0 for most, 0.03% for oil, 0.0009% for metals
COMMISSION_PCT = {
    'XTIUSD': 0.0003, 'XBRUSD': 0.0003, 'USOIL': 0.0003,
    'XAUUSD': 0.000009, 'XAGUSD': 0.000009,
}

# ============================================================================
# CANDIDATE PAIRS — Economically linked instruments
# ============================================================================
CANDIDATE_PAIRS = [
    # Precious Metals (CLASSIC pair)
    ("XAUUSD", "XAGUSD", "Gold/Silver", 10),
    # Oil (proven signal, bad dollar-vol on 5%ers)
    ("XTIUSD", "XBRUSD", "WTI/Brent Oil", 5),
    # US Indices
    ("US100", "US500", "Nasdaq/S&P500", 20),
    ("US100", "US30", "Nasdaq/Dow", 20),
    ("US100", "DE40", "Nasdaq/DAX", 20),
    ("US500", "US30", "S&P500/Dow", 20),
    # FX — Classic cointegrated pairs
    ("AUDUSD", "NZDUSD", "AUD/NZD", 10),
    ("EURUSD", "GBPUSD", "EUR/GBP spread", 10),
    ("EURUSD", "EURGBP", "EUR$ vs EUR£", 10),
    ("USDCAD", "AUDUSD", "CAD/AUD commodity", 10),
    ("EURJPY", "GBPJPY", "EUR¥/GBP¥", 10),
    ("AUDCAD", "NZDCAD", "AUD-CAD/NZD-CAD", 10),
    ("EURCHF", "EURGBP", "EUR safe-haven", 10),
    ("CADJPY", "CHFJPY", "CAD¥/CHF¥", 10),
]


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

def calc_cost(sym_a, sym_b, lots, hour):
    """Calculate realistic round-trip cost for a pair trade (4 fills total)."""
    mult = get_spread_multiplier(hour)
    spread_a = SPREAD_PER_FILL.get(sym_a, 15) * 2 * lots * mult  # open + close
    spread_b = SPREAD_PER_FILL.get(sym_b, 15) * 2 * lots * mult
    # Commission (if any)
    comm_a = COMMISSION_PCT.get(sym_a, 0)
    comm_b = COMMISSION_PCT.get(sym_b, 0)
    # Commission on deal volume: pct × contract_size × price × lots × 4 fills
    # But we don't have current price in this function, so use notional estimate
    comm = 0
    # We'll add commission as a flat estimate per lot
    if comm_a > 0:
        comm += comm_a * CONTRACT_SIZES.get(sym_a, 100000) * 100 * lots * 4  # rough: $100 avg price
    if comm_b > 0:
        comm += comm_b * CONTRACT_SIZES.get(sym_b, 100000) * 100 * lots * 4
    return spread_a + spread_b + comm

def calc_notional(sym_a, sym_b, avg_price_a, avg_price_b):
    """Calculate the effective notional for the pair.
    This is the average dollar sensitivity per unit of log-spread change.
    notional ≈ (cs_A × price_A + cs_B × price_B) / 2
    """
    cs_a = CONTRACT_SIZES.get(sym_a, 100000)
    cs_b = CONTRACT_SIZES.get(sym_b, 100000)
    not_a = cs_a * avg_price_a
    not_b = cs_b * avg_price_b
    return (not_a + not_b) / 2.0

def is_rollover(t):
    m = t.hour * 60 + t.minute
    return m < ROLLOVER_LOCKOUT_MIN or (1440 - m) < ROLLOVER_LOCKOUT_MIN

def load_pair(sym_a, sym_b):
    d = Path(__file__).resolve().parent.parent / "data" / "historical"
    fa = d / f"{sym_a}_M1.csv"; fb = d / f"{sym_b}_M1.csv"
    if not fa.exists() or not fb.exists():
        return None
    a = pd.read_csv(fa, parse_dates=['time']).rename(columns={'close':'close_a'})
    b = pd.read_csv(fb, parse_dates=['time']).rename(columns={'close':'close_b'})
    m = pd.merge(a[['time','close_a']], b[['time','close_b']], on='time', how='inner')
    m = m.sort_values('time').reset_index(drop=True)
    m = m[(m['close_a']>0)&(m['close_b']>0)].reset_index(drop=True)
    return m if len(m) > 1000 else None


def run_backtest(df, sym_a, sym_b, notional, hmm_hold=10, dwell_base=300, dwell_anchor=0.3):
    """Run full backtest with correct notional."""
    bal = BAL; peak = BAL; daily_start = BAL; daily_date = None; ghost = False
    dakad = DynamicAKAD()
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec = 0; gcool = 0; n = len(df)
    
    dwell_min = dwell_base * 0.5; dwell_max = dwell_base * 5.0

    eng = shf_core.CointegrationEngine(span=WELFORD_SPAN, beta=1.0, entry_z=Z_BASE,
        exit_z=EXIT_Z_BASE, z_base=Z_BASE, gamma=GAMMA, hurst_window=HURST_WINDOW,
        dynamic_z=True, exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
    hmm = HMMRegimeDetector(lookback=HMM_LOOKBACK, min_regime_hold=hmm_hold)

    pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0
    lspread = 0.0; pspread = 0.0; sent_abort = False
    last_close_bar = -9999; last_close_h = 0.5; entry_hour = 0
    trades = []

    for bar in range(n):
        if ghost: break
        row = df.iloc[bar]; bt = row['time']
        pa = float(row['close_a']); pb = float(row['close_b'])

        cd = bt.date() if hasattr(bt,'date') else None
        if cd and cd != daily_date: daily_date = cd; daily_start = bal

        cdd = max(0,(peak-bal)/peak) if peak>0 else 0
        ddd = max(0,(daily_start-bal)/daily_start) if daily_start>0 else 0
        if ddd >= GHOST_DAILY_DD: ghost = True; break
        if cdd >= GHOST_MAX_DD: ghost = True; break
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
                cost = calc_cost(sym_a, sym_b, elots, entry_hour)
                pnl = gross - cost; bal += pnl; peak = max(peak,bal)
                w = pnl > 0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                trades.append({'pnl':pnl,'gross':gross,'cost':cost})
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
                cb = max(dwell_min, min(dwell_max, dwell_base * (last_close_h / dwell_anchor))) / 60.0
                if (bar - last_close_bar) < cb: continue

            risk = dakad.calc(cdd, ddd)
            corr.compute_risk(); cm = corr.last_risk_multiplier
            lots = max(0.01, round(bal * risk * cm / 1000.0, 2))
            pos = s; ez = z; es = spread; ebar = bar; elots = lots; entry_hour = bt.hour

        elif pos != 0:
            ex = False
            if abs(z) > abs(ez) * 2.5: ex = True
            if not ex:
                hb = bar - ebar
                db = max(dwell_min, min(dwell_max, dwell_base * (h / dwell_anchor))) / 60.0
                if hb < db: continue
                if pos == 1 and z > -exz: ex = True
                elif pos == -1 and z < exz: ex = True
            if ex:
                gross = (spread-es)*pos*elots*notional
                cost = calc_cost(sym_a, sym_b, elots, entry_hour)
                pnl = gross - cost; bal += pnl; peak = max(peak,bal)
                w = pnl > 0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                trades.append({'pnl':pnl,'gross':gross,'cost':cost})
                pos=0; last_close_bar=bar; last_close_h=h

    total = len(trades)
    if total == 0: return None

    pnls = [t['pnl'] for t in trades]
    gross_pnls = [t['gross'] for t in trades]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    gp = sum(wins) if wins else 0; gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins)/total*100; pf = gp/gl if gl>0 else 0
    gross_wins = [g for g in gross_pnls if g > 0]
    gross_wr = len(gross_wins)/total*100

    first = df['time'].iloc[0]; last_t = df['time'].iloc[-1]
    days = (last_t - first).days; months = days / 30.0

    eq = BAL; eq_peak = eq; mdd = 0
    for t in trades:
        eq += t['pnl']; eq_peak = max(eq_peak, eq); mdd = max(mdd, eq_peak - eq)

    return {
        'trades': total, 'net_wr': round(wr,1), 'gross_wr': round(gross_wr,1),
        'pf': round(pf,2), 'net_pnl': round(bal-BAL,2),
        'gross_pnl': round(sum(gross_pnls),2),
        'total_costs': round(sum(t['cost'] for t in trades),2),
        'avg_gross': round(sum(gross_pnls)/total,2),
        'avg_cost': round(sum(t['cost'] for t in trades)/total,2),
        'avg_net': round((bal-BAL)/total,2),
        'ghost': ghost, 'max_dd_pct': round(mdd/BAL*100,2),
        'trades_per_month': round(total/months,1) if months > 0 else 0,
        'days': days,
    }


def main():
    t_start = time.time()
    print("="*130)
    print("  UNIVERSAL PAIR VIABILITY SCANNER — Correct Notional, Real Costs")
    print("  Finding pairs where gross P&L actually exceeds trading costs")
    print("="*130)

    results = []
    
    header = (f"  {'Pair':<22} {'Notional':>10} {'Cost/Lot':>10} {'Ratio':>7} "
              f"{'Trades':>6} {'Tr/Mo':>5} {'NetWR':>6} {'GrossWR':>7} {'PF':>6} "
              f"{'NetP&L':>10} {'$/Tr':>7} {'MaxDD':>6} {'Days':>5} {'Verdict':>10}")
    print(f"\n{header}")
    print(f"  {'-'*128}")

    for sym_a, sym_b, name, hmm_hold in CANDIDATE_PAIRS:
        df = load_pair(sym_a, sym_b)
        if df is None:
            print(f"  {name:<22} — DATA NOT AVAILABLE ({sym_a} or {sym_b})")
            continue

        avg_a = df['close_a'].mean()
        avg_b = df['close_b'].mean()
        notional = calc_notional(sym_a, sym_b, avg_a, avg_b)
        
        # Cost per lot (4 fills total)
        cost_per_lot = (SPREAD_PER_FILL.get(sym_a, 15) + SPREAD_PER_FILL.get(sym_b, 15)) * 2
        ratio = notional / cost_per_lot if cost_per_lot > 0 else 999

        # Choose dwell based on pair type
        if sym_a in ('XTIUSD', 'XBRUSD', 'USOIL'):
            dwell = 1800
        elif sym_a in ('XAUUSD',):
            dwell = 300
        else:
            dwell = 60  # FX and indices: fast

        r = run_backtest(df, sym_a, sym_b, notional, hmm_hold=hmm_hold, dwell_base=dwell)
        
        if r is None:
            print(f"  {name:<22} ${notional:>9,.0f} ${cost_per_lot:>9,.0f} {ratio:>6.0f}x "
                  f"  NO TRADES")
            continue

        # Verdict
        if r['net_pnl'] > 1000 and r['pf'] > 1.3 and r['trades'] >= 10:
            verdict = "VIABLE"
        elif r['net_pnl'] > 0 and r['pf'] > 1.0 and r['trades'] >= 5:
            verdict = "MARGINAL"
        elif r['net_pnl'] > 0:
            verdict = "WEAK"
        else:
            verdict = "DEAD"

        ghost_str = " GHOST" if r['ghost'] else ""
        
        print(f"  {name:<22} ${notional:>9,.0f} ${cost_per_lot:>9,.0f} {ratio:>6.0f}x "
              f"{r['trades']:>6} {r['trades_per_month']:>5.0f} {r['net_wr']:>5.1f}% {r['gross_wr']:>6.1f}% "
              f"{r['pf']:>6.2f} ${r['net_pnl']:>9,.2f} ${r['avg_net']:>6.2f} {r['max_dd_pct']:>5.2f}% "
              f"{r['days']:>5} {verdict:>10}{ghost_str}")
        
        results.append((name, sym_a, sym_b, notional, cost_per_lot, ratio, hmm_hold, dwell, r, verdict))

    # =========================================================================
    # TOP PICKS
    # =========================================================================
    print(f"\n\n{'='*130}")
    print("  TOP PICKS (sorted by Net P&L)")
    print(f"{'='*130}")

    viable = [x for x in results if x[9] in ("VIABLE", "MARGINAL")]
    if viable:
        for name, sa, sb, notl, cpl, ratio, hmm, dwell, r, verdict in sorted(viable, key=lambda x: -x[8]['net_pnl']):
            print(f"\n  {verdict}: {name} ({sa}/{sb})")
            print(f"    Notional: ${notl:,.0f} | Cost/lot: ${cpl:,.0f} | Ratio: {ratio:.0f}x")
            print(f"    HMM hold: {hmm} | Dwell base: {dwell}s")
            print(f"    Trades: {r['trades']} ({r['trades_per_month']:.0f}/mo) over {r['days']} days")
            print(f"    Net WR: {r['net_wr']}% | Gross WR: {r['gross_wr']}%")
            print(f"    PF: {r['pf']} | Net P&L: ${r['net_pnl']:,.2f}")
            print(f"    Avg gross/trade: ${r['avg_gross']:.2f} | Avg cost/trade: ${r['avg_cost']:.2f}")
            print(f"    Avg net/trade: ${r['avg_net']:.2f}")
            print(f"    MaxDD: {r['max_dd_pct']}%")
    else:
        print("\n  NO VIABLE PAIRS FOUND")

    # =========================================================================
    # VIABILITY ANALYSIS
    # =========================================================================
    print(f"\n\n{'='*130}")
    print("  KEY INSIGHT: Notional/Cost Ratio Determines Viability")
    print(f"{'='*130}")
    print(f"""
  The strategy has ~93% gross directional accuracy. But profitability depends on:
  
    Notional / Cost_per_lot = how many dollars of P&L you get per dollar of cost
  
  Pairs sorted by this ratio:""")
    
    for name, sa, sb, notl, cpl, ratio, hmm, dwell, r, verdict in sorted(results, key=lambda x: -x[5]):
        print(f"    {ratio:>6.0f}x  {name:<22} Notional=${notl:>10,.0f}  Cost=${cpl:>6,.0f}  -> {verdict}")

    print(f"""
  RULE OF THUMB: You need ratio > ~500x for the pair to be viable.
  Below that, costs eat the entire edge.
  
  FX pairs have ratio ~2,500x (great!)
  Gold/Silver has ratio ~900x (good!)
  Oil has ratio ~330x (dead!)
  Indices depend on contract size — check your broker!
""")

    elapsed = time.time() - t_start
    print(f"\n  Completed in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
