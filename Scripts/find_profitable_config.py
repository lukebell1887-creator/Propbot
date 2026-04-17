#!/usr/bin/env python3
"""
With the CORRECT notional (contract_size × price), find configurations
that actually generate enough gross P&L to cover real costs.

Tests:
1. Higher Z entry thresholds (Z > 3, 4, 5) — bigger moves
2. Lower exit Z (exit at 0.0 instead of 0.5) — capture full reversion  
3. High-sigma-only filter — trade only when volatility is high
4. Combinations of all the above
5. Also test Index pair with various notionals
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
# PARAMS
# ============================================================================
WELFORD_SPAN = 100; GAMMA = 6.0; HURST_WINDOW = 512
EXIT_GAMMA = 2.0
DAKAD_LAMBDA = 40.0; DAKAD_P_RUIN = 1e-4
DAKAD_DAILY_DD_CEIL = 0.04; DAKAD_RESULT_WINDOW = 50
DAKAD_MIN_WR = 0.50; DAKAD_MAX_WR = 0.85
DAKAD_MIN_BASE = 0.003; DAKAD_MAX_BASE = 0.03; DAKAD_RISK_FLOOR = 0.0005
GHOST_DAILY_DD = 0.04; GHOST_MAX_DD = 0.09
KALMAN_TOLERANCE = 0.15; CORR_WINDOW = 200
ROLLOVER_LOCKOUT_MIN = 30
HMM_LOOKBACK = 100
MAX_CONSEC_LOSSES = 5; COOLDOWN_BARS = 60
MIN_WARMUP_BARS = 200; STARTING_BALANCE = 100_000.0

OIL_NOTIONAL_PER_LOT = 6500.0  # for commission calc

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

def calc_oil_cost(lots, hour):
    mult = get_spread_multiplier(hour)
    spread_cost = (4.0 * 2 + 5.0 * 2) * lots * mult
    comm = 0.0003 * OIL_NOTIONAL_PER_LOT * lots * 4
    return spread_cost + comm

def calc_index_cost(lots, hour):
    mult = get_spread_multiplier(hour)
    spread_cost = (1.0 * 2 + 1.0 * 2) * lots * mult  # $1+$1 per fill x 4
    return spread_cost  # ZERO commission on indices

def is_rollover(t):
    m = t.hour * 60 + t.minute
    return m < ROLLOVER_LOCKOUT_MIN or (1440 - m) < ROLLOVER_LOCKOUT_MIN

def load_pair(file_a, file_b):
    d = Path(__file__).resolve().parent.parent / "data" / "historical"
    a = pd.read_csv(d / file_a, parse_dates=['time']).rename(columns={'close':'close_a'})
    b = pd.read_csv(d / file_b, parse_dates=['time']).rename(columns={'close':'close_b'})
    m = pd.merge(a[['time','close_a']], b[['time','close_b']], on='time', how='inner').sort_values('time').reset_index(drop=True)
    return m[(m['close_a']>0)&(m['close_b']>0)].reset_index(drop=True)


def run_config(df, notional, cost_fn, z_entry, exit_z_base, hmm_hold, 
               dwell_base, dwell_min, dwell_max, dwell_anchor=0.3,
               min_sigma=0.0, amp_hurdle=0.0):
    """Run backtest with configurable Z entry, exit Z, sigma filter, amplitude gate."""
    balance = STARTING_BALANCE; peak = STARTING_BALANCE; daily_start = STARTING_BALANCE
    daily_date = None; ghost = False
    dakad = DynamicAKAD()
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec = 0; gcool = 0; n = len(df)

    eng = shf_core.CointegrationEngine(span=WELFORD_SPAN, beta=1.0, 
        entry_z=z_entry, exit_z=exit_z_base, z_base=z_entry, gamma=GAMMA, 
        hurst_window=HURST_WINDOW, dynamic_z=True, 
        exit_z_base=exit_z_base, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
    hmm = HMMRegimeDetector(lookback=HMM_LOOKBACK, min_regime_hold=hmm_hold)

    pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0
    lspread = 0.0; pspread = 0.0; sent_abort = False
    last_close_bar = -9999; last_close_h = 0.5
    entry_hour = 0
    trades = []; sigma_blocks = 0; amp_blocks = 0

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
        sigma = eng.last_std

        pidx = 1
        if pspread != 0.0: corr.push_return(pidx, spread - pspread)

        la = math.log(pa) if pa>0 else 0; lb = math.log(pb) if pb>0 else 0
        beta, abort = sen.update(la, lb)
        if abort and not sent_abort:
            sent_abort = True
            if pos != 0:
                gross = (spread-es)*pos*elots*notional
                cost = cost_fn(elots, entry_hour)
                pnl = gross - cost; balance += pnl; peak = max(peak,balance)
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

            # Sigma filter
            if min_sigma > 0 and sigma < min_sigma:
                sigma_blocks += 1; continue

            risk = dakad.calc(cdd, ddd)
            corr.compute_risk(); cm = corr.last_risk_multiplier
            lots = max(0.01, round(balance * risk * cm / 1000.0, 2))

            # Amplitude gate with CORRECT notional
            if amp_hurdle > 0 and sigma > 0:
                z_captured = max(0.0, abs(z) - exz)
                expected_profit = z_captured * sigma * lots * notional
                trade_cost = cost_fn(lots, bt.hour)
                if trade_cost > 0:
                    ratio = expected_profit / trade_cost
                    if ratio < amp_hurdle:
                        amp_blocks += 1; continue

            pos = s; ez = z; es = spread; ebar = bar; elots = lots
            entry_hour = bt.hour

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
                cost = cost_fn(elots, entry_hour)
                pnl = gross - cost; balance += pnl; peak = max(peak,balance)
                w = pnl > 0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                trades.append({'pnl':pnl,'gross':gross,'cost':cost})
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

    first = df['time'].iloc[0]; last_t = df['time'].iloc[-1]
    days = (last_t - first).days; months = days / 30.0

    eq = STARTING_BALANCE; eq_peak = eq; mdd = 0
    for t in trades:
        eq += t['pnl']; eq_peak = max(eq_peak, eq); mdd = max(mdd, eq_peak - eq)

    return {
        'trades': total, 'net_wr': round(wr,1), 'gross_wr': round(gross_wr,1),
        'pf': round(pf,2), 'net_pnl': round(balance-STARTING_BALANCE,2),
        'gross_pnl': round(sum(gross_pnls),2),
        'total_costs': round(sum(t['cost'] for t in trades),2),
        'avg_gross': round(sum(gross_pnls)/total,2) if total else 0,
        'avg_cost': round(sum(t['cost'] for t in trades)/total,2) if total else 0,
        'avg_net': round((balance-STARTING_BALANCE)/total,2) if total else 0,
        'ghost': ghost, 'sigma_blocks': sigma_blocks, 'amp_blocks': amp_blocks,
        'trades_per_month': round(total/months,1) if months > 0 else 0,
        'max_dd_pct': round(mdd/STARTING_BALANCE*100,2),
        'return_pct': round((balance-STARTING_BALANCE)/STARTING_BALANCE*100,2),
    }


def main():
    t_start = time.time()
    print("="*120)
    print("  FINDING PROFITABLE CONFIGS — Correct Notional + Cost Optimization")
    print("="*120)

    # =========================================================================
    # PART 1: OIL (XTIUSD / XBRUSD) — Contract size 100 barrels
    # =========================================================================
    print("\n  Loading Oil data...")
    df_oil = load_pair("XTIUSD_M1.csv", "XBRUSD_M1.csv")
    avg_price = df_oil['close_a'].mean()
    oil_notional = round(100 * avg_price)  # 100 barrels × price
    print(f"  {len(df_oil):,} bars | Avg XTIUSD: ${avg_price:.2f} | Real notional: ${oil_notional:,}")

    print(f"\n{'='*120}")
    print("  OIL SWEEP: Z_entry × Exit_Z × Min_Sigma × Amp_Hurdle (REAL notional)")
    print(f"{'='*120}")
    
    header = (f"  {'Z_ent':>5} {'ExitZ':>5} {'MinSig':>7} {'AmpH':>5} {'HMM':>4} "
              f"{'Trades':>6} {'Tr/Mo':>5} {'NetWR':>6} {'GrossWR':>7} {'PF':>6} "
              f"{'GrossP&L':>10} {'Costs':>10} {'NetP&L':>10} {'$/Tr':>7} {'MaxDD':>6} "
              f"{'SigBlk':>6} {'AmpBlk':>6}")
    print(header)
    print(f"  {'-'*len(header)}")

    oil_results = []
    configs = [
        # (z_entry, exit_z, min_sigma, amp_hurdle, hmm_hold)
        # Baseline (what was running)
        (2.0, 0.5, 0.0, 0.0, 5),
        # Higher Z entry (bigger moves)
        (3.0, 0.5, 0.0, 0.0, 5),
        (4.0, 0.5, 0.0, 0.0, 5),
        (5.0, 0.5, 0.0, 0.0, 5),
        # Lower exit Z (capture full reversion)
        (2.0, 0.0, 0.0, 0.0, 5),
        (3.0, 0.0, 0.0, 0.0, 5),
        (4.0, 0.0, 0.0, 0.0, 5),
        # High sigma filter (only trade when vol is high)
        (2.0, 0.5, 0.003, 0.0, 5),
        (2.0, 0.5, 0.005, 0.0, 5),
        (2.0, 0.5, 0.008, 0.0, 5),
        (3.0, 0.0, 0.005, 0.0, 5),
        (4.0, 0.0, 0.005, 0.0, 5),
        # Amplitude gate with CORRECT notional
        (2.0, 0.5, 0.0, 1.5, 5),
        (2.0, 0.5, 0.0, 3.0, 5),
        (2.0, 0.5, 0.0, 5.0, 5),
        (2.0, 0.0, 0.0, 3.0, 5),
        (3.0, 0.0, 0.0, 3.0, 5),
        # Combined: high Z + low exit + sigma filter
        (3.0, 0.0, 0.003, 0.0, 5),
        (3.0, 0.0, 0.005, 0.0, 5),
        (4.0, 0.0, 0.003, 0.0, 5),
        # HMM=10 variants of best configs
        (3.0, 0.0, 0.0, 0.0, 10),
        (4.0, 0.0, 0.0, 0.0, 10),
        (3.0, 0.0, 0.005, 0.0, 10),
    ]

    best_oil = None; best_oil_pnl = -999999
    for z_ent, exit_z, min_sig, amp_h, hmm_h in configs:
        r = run_config(df_oil, oil_notional, calc_oil_cost,
                       z_entry=z_ent, exit_z_base=exit_z, hmm_hold=hmm_h,
                       dwell_base=1800, dwell_min=900, dwell_max=9000,
                       min_sigma=min_sig, amp_hurdle=amp_h)
        if r is None:
            print(f"  {z_ent:>5.1f} {exit_z:>5.1f} {min_sig:>7.4f} {amp_h:>5.1f} {hmm_h:>4} — NO TRADES")
            continue

        marker = ""
        if r['net_pnl'] > best_oil_pnl and r['trades'] >= 5:
            best_oil_pnl = r['net_pnl']; best_oil = (z_ent, exit_z, min_sig, amp_h, hmm_h, r)
            marker = " <<<"

        print(f"  {z_ent:>5.1f} {exit_z:>5.1f} {min_sig:>7.4f} {amp_h:>5.1f} {hmm_h:>4} "
              f"{r['trades']:>6} {r['trades_per_month']:>5.0f} {r['net_wr']:>5.1f}% {r['gross_wr']:>6.1f}% "
              f"{r['pf']:>6.2f} ${r['gross_pnl']:>9,.2f} ${r['total_costs']:>9,.2f} "
              f"${r['net_pnl']:>9,.2f} ${r['avg_net']:>6.2f} {r['max_dd_pct']:>5.2f}% "
              f"{r.get('sigma_blocks',0):>6} {r.get('amp_blocks',0):>6}"
              f"{'  GHOST' if r['ghost'] else ''}{marker}")
        oil_results.append((z_ent, exit_z, min_sig, amp_h, hmm_h, r))

    if best_oil:
        z_ent, exit_z, min_sig, amp_h, hmm_h, r = best_oil
        print(f"\n  BEST OIL CONFIG: Z_entry={z_ent} Exit_Z={exit_z} MinSigma={min_sig} AmpHurdle={amp_h} HMM={hmm_h}")
        print(f"    Trades: {r['trades']} | Net WR: {r['net_wr']}% | PF: {r['pf']} | Net P&L: ${r['net_pnl']:,.2f} | $/trade: ${r['avg_net']:.2f}")
    else:
        print(f"\n  NO PROFITABLE OIL CONFIG FOUND")

    # =========================================================================
    # PART 2: INDEX (US100 / DE40)
    # =========================================================================
    print(f"\n\n{'='*120}")
    print("  INDEX PAIR: Testing with various contract sizes")
    print(f"{'='*120}")

    try:
        df_idx = load_pair("US100_M1.csv", "DE40_M1.csv")
        avg_us100 = df_idx['close_a'].mean()
        avg_de40 = df_idx['close_b'].mean()
        print(f"  {len(df_idx):,} bars | Avg US100: ${avg_us100:.0f} | Avg DE40: ${avg_de40:.0f}")

        # Test with different contract sizes
        for cs_name, cs_val in [("1 (standard CFD)", 1), ("10", 10), ("100", 100)]:
            idx_notional = round(cs_val * avg_us100)
            r = run_config(df_idx, idx_notional, calc_index_cost,
                           z_entry=2.0, exit_z_base=0.5, hmm_hold=20,
                           dwell_base=60, dwell_min=30, dwell_max=300)
            if r is None:
                print(f"\n  Contract size = {cs_name}: notional=${idx_notional:,} — NO TRADES")
                continue
            print(f"\n  Contract size = {cs_name}: notional=${idx_notional:,}")
            print(f"    Trades: {r['trades']} | Net WR: {r['net_wr']}% | Gross WR: {r['gross_wr']}%")
            print(f"    Gross P&L: ${r['gross_pnl']:,.2f} | Costs: ${r['total_costs']:,.2f} | Net: ${r['net_pnl']:,.2f}")
            print(f"    Avg gross: ${r['avg_gross']:.2f} | Avg cost: ${r['avg_cost']:.2f} | Avg net: ${r['avg_net']:.2f}")
            if r['ghost']: print(f"    *** GHOST STOP HIT ***")
    except Exception as e:
        print(f"  Index data not available: {e}")

    # =========================================================================
    # SUMMARY
    # =========================================================================
    elapsed = time.time() - t_start
    print(f"\n\n{'='*120}")
    print(f"  SUMMARY (completed in {elapsed:.0f}s)")
    print(f"{'='*120}")
    
    profitable = [(z, ex, ms, ah, hm, r) for z, ex, ms, ah, hm, r in oil_results 
                  if r['net_pnl'] > 0 and r['trades'] >= 5]
    
    if profitable:
        print(f"\n  {len(profitable)} PROFITABLE Oil configs found:")
        for z, ex, ms, ah, hm, r in sorted(profitable, key=lambda x: -x[5]['net_pnl']):
            print(f"    Z={z} ExitZ={ex} MinSig={ms} AmpH={ah} HMM={hm}: "
                  f"{r['trades']} trades, {r['net_wr']}% WR, PF={r['pf']}, "
                  f"Net=${r['net_pnl']:,.2f}, $/trade=${r['avg_net']:.2f}")
    else:
        print(f"\n  NO profitable Oil configs found with real notional.")
        print(f"  The Oil spread simply doesn't move enough in dollar terms")
        print(f"  to cover the $18/lot spread + $7.80/lot commission costs.")
        print(f"  Possible solutions:")
        print(f"    1. Find a broker with lower oil spreads/commissions")
        print(f"    2. Switch to higher-volatility pairs (Gold/Silver?)")
        print(f"    3. Use a different strategy for oil (momentum instead of MR?)")
        print(f"    4. Focus exclusively on the Index pair if it works")


if __name__ == "__main__":
    main()
