#!/usr/bin/env python3
"""
FULL PAIR OPTIMIZATION — Gold/Silver + NAS/DAX
===============================================
Tests every combination of:
  - Timeframe: M1, M5, M15, M30, H1, H4
  - HMM hold: 5, 10, 20, 50
  - Dwell base: 1, 3, 5, 10, 20, 60 bars (scaled per timeframe)
  - Z entry: 2.0, 2.5, 3.0
  - Exit Z: 0.0, 0.25, 0.5

Uses EXACT 5%ers broker specs:
  Gold:   CS=100oz,  Commission=0.0009%, Swap L=-91, S=-68
  Silver: CS=5000oz, Commission=0.0009%, Swap L=-20, S=-17
  NAS100: CS=1,      Commission=$0,      Swap L=-300, S=-300
  DAX40:  CS=1,      Commission=$0,      Swap L=-500, S=-500

Multi-timeframe theory:
  On M1:  sigma is tiny → small $ moves → costs eat the edge
  On M15: sigma is ~3.9x larger → 4x more $/trade → costs become trivial
  On H1:  sigma is ~7.7x larger → 8x more $/trade → huge edge
  Trade count drops but profit per trade rises dramatically.
"""

import sys, math, time, json
import numpy as np
import pandas as pd
from pathlib import Path
from collections import deque

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shf_core

# ============================================================================
# EXACT 5%ERS BROKER SPECS
# ============================================================================
PAIRS = {
    'Gold_Silver': {
        'sym_a': 'XAUUSD', 'sym_b': 'XAGUSD',
        'cs_a': 100, 'cs_b': 5000,       # contract sizes
        'comm_pct': 0.000009,             # 0.0009% commission
        'spread_a_pts': 30,              # gold spread ~30 cents = $30/lot
        'spread_b_pts': 3,               # silver spread ~3 cents = $15/lot (5000*0.003)
        'swap_long_a': -91, 'swap_short_a': -68,   # gold swaps (points/lot/night)
        'swap_long_b': -20, 'swap_short_b': -17,   # silver swaps
        'swap_friday_mult': 3,            # friday triple swap
    },
    'NAS_DAX': {
        'sym_a': 'US100', 'sym_b': 'DE40',
        'cs_a': 1, 'cs_b': 1,            # contract size = 1
        'comm_pct': 0,                    # zero commission
        'spread_a_pts': 2,               # NAS spread ~2 points = $2/lot
        'spread_b_pts': 2,               # DAX spread ~2 points = $2/lot
        'swap_long_a': -300, 'swap_short_a': -300,  # NAS swaps (both sides!)
        'swap_long_b': -500, 'swap_short_b': -500,  # DAX swaps (both sides!)
        'swap_friday_mult': 3,
    }
}

# ============================================================================
# ENGINE PARAMS
# ============================================================================
WELFORD_SPAN = 100; GAMMA = 6.0; HURST_WINDOW = 512; EXIT_GAMMA = 2.0
DAKAD_LAMBDA = 40.0; DAKAD_P_RUIN = 1e-4
DAKAD_DAILY_DD_CEIL = 0.04; DAKAD_RESULT_WINDOW = 50
DAKAD_MIN_WR = 0.50; DAKAD_MAX_WR = 0.85
DAKAD_MIN_BASE = 0.003; DAKAD_MAX_BASE = 0.03; DAKAD_RISK_FLOOR = 0.0005
GHOST_DAILY_DD = 0.04; GHOST_MAX_DD = 0.09
KALMAN_TOLERANCE = 0.15; CORR_WINDOW = 200
ROLLOVER_LOCKOUT_MIN = 30; HMM_LOOKBACK = 100
MAX_CONSEC_LOSSES = 5; COOLDOWN_BARS = 60
MIN_WARMUP_BARS = 200; BAL = 100_000.0

# Timeframes to test (in minutes)
TIMEFRAMES = [1, 5, 15, 30, 60, 240]
TF_NAMES = {1:'M1', 5:'M5', 15:'M15', 30:'M30', 60:'H1', 240:'H4'}
# Per-pair TF override (skip M1 for Gold since we already have those results)
PAIR_TF_OVERRIDE = {
    'Gold_Silver': [1, 5, 15, 30, 60, 240],  # Full sweep — re-test M1 with Huber stop
    'NAS_DAX': [1, 5, 15, 30, 60, 240],      # Full sweep
}

# Sweep ranges
HMM_HOLDS = [5, 10, 20, 50]
DWELL_BARS = [1, 3, 5, 10, 20, 60]  # in bars (auto-scaled per TF)
Z_ENTRIES = [2.0, 2.5, 3.0]
EXIT_ZS = [0.0, 0.25, 0.5]
# For higher TFs, Z-scores are attenuated by ~1/√(TF) due to wider Welford window
# So we need lower Z thresholds for M5+ to ever trigger entries
Z_ENTRIES_BY_TF = {
    1:   [2.0, 2.5, 3.0],         # M1: standard
    5:   [1.0, 1.5, 2.0],         # M5: Z attenuated ~2.2x
    15:  [0.8, 1.0, 1.5],         # M15: Z attenuated ~3.9x
    30:  [0.5, 0.8, 1.0],         # M30: Z attenuated ~5.5x
    60:  [0.5, 0.8, 1.0],         # H1: Z attenuated ~7.7x
    240: [0.5, 0.8, 1.0],         # H4: Z attenuated ~15.5x
}
# Hard stop: Huber 4.815σ catastrophe net (matches live engine)
HUBER_SIGMA = 4.815
# Also test account-% hard stops to control max DD
HARD_STOP_PCTS = [0.0, 0.01, 0.02, 0.03]  # 0=Huber only, 1%/2%/3% of account

# Amplitude gate: blocks trades where expected_profit < hurdle × cost
# Opportunity multiplier: scales lots up when ratio is high
AMP_HURDLES = [0.0, 1.0, 1.5, 2.0, 3.0]  # 0 = disabled
AMP_MAX_MULTS = [1.0, 1.5, 2.0]           # 1.0 = no scaling

# Session filter: only trade 07:00-20:00 UTC to avoid worst spreads/swaps
SESSION_START_HOUR = 7
SESSION_END_HOUR = 20

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

def calc_cost(pair_spec, lots, hour):
    """Calculate total round-trip cost with EXACT broker specs."""
    sm = get_spread_multiplier(hour)
    # Spread cost: pts × contract_size × lots × 2 (round trip) for each leg
    # For gold: 30 pts = $0.30, so $0.30 × 100oz × lots × 2 = $60/lot RT
    # For silver: 3 pts = $0.03, so $0.03 × 5000oz × lots × 2 = $300/lot RT
    # Wait — need to be careful about what "pts" means per instrument
    # Gold spread ~30 = $0.30 → cost per lot per fill = $0.30 × 100 = $30
    # Silver spread ~3 = $0.03 → cost per lot per fill = $0.03 × 5000 = $150
    # NAS spread ~2 = 2 points → cost per lot per fill = 2 × 1 = $2
    # DAX spread ~2 = 2 points → cost per lot per fill = 2 × 1 = $2
    spread_a = pair_spec['spread_a_pts'] * lots * 2 * sm  # 2 fills (open+close) for leg A
    spread_b = pair_spec['spread_b_pts'] * lots * 2 * sm
    
    # Commission (metals: 0.0009% of notional × 4 deals)
    comm = 0
    if pair_spec['comm_pct'] > 0:
        # Approximate notional at current prices (will be close enough)
        avg_price_a = 2700  # gold approximate
        avg_price_b = 30    # silver approximate
        if pair_spec['sym_a'] == 'XAUUSD':
            not_a = avg_price_a * pair_spec['cs_a'] * lots
            not_b = avg_price_b * pair_spec['cs_b'] * lots
        else:
            not_a = 21000 * pair_spec['cs_a'] * lots
            not_b = 22000 * pair_spec['cs_b'] * lots
        comm = (not_a + not_b) * pair_spec['comm_pct'] * 4  # 4 deals total
    
    return spread_a + spread_b + comm

def calc_swap_cost(pair_spec, lots, position, hold_hours, entry_time):
    """Calculate swap cost if held overnight."""
    if hold_hours < 20:  # trades less than 20 hours don't cross midnight
        return 0.0
    nights = max(1, int(hold_hours / 24))
    # Pair trade: long one, short other
    # Position=1: long A (gold/NAS), short B (silver/DAX)
    # Position=-1: short A, long B
    if position == 1:
        swap_a = pair_spec['swap_long_a']  # long gold/NAS
        swap_b = pair_spec['swap_short_b']  # short silver/DAX
    else:
        swap_a = pair_spec['swap_short_a']  # short gold/NAS
        swap_b = pair_spec['swap_long_b']   # long silver/DAX
    
    # Check if any night is a Friday (3x swap)
    total_swap_pts = (abs(swap_a) + abs(swap_b)) * nights * lots
    # Convert to dollars (approximate)
    if pair_spec['sym_a'] in ('XAUUSD',):
        swap_usd = total_swap_pts * 0.01  # rough conversion
    else:
        swap_usd = total_swap_pts * 0.01
    return swap_usd

def load_pair(sym_a, sym_b):
    d = Path(__file__).resolve().parent.parent / "data" / "historical"
    a = pd.read_csv(d / f"{sym_a}_M1.csv", parse_dates=['time']).rename(columns={'close':'close_a'})
    b = pd.read_csv(d / f"{sym_b}_M1.csv", parse_dates=['time']).rename(columns={'close':'close_b'})
    m = pd.merge(a[['time','close_a']], b[['time','close_b']], on='time', how='inner')
    m = m.sort_values('time').reset_index(drop=True)
    return m[(m['close_a']>0)&(m['close_b']>0)].reset_index(drop=True)

def resample_to_tf(df, tf_minutes):
    """Resample M1 data to higher timeframe."""
    if tf_minutes == 1:
        return df
    df2 = df.set_index('time')
    rule = f'{tf_minutes}min' if tf_minutes < 60 else f'{tf_minutes//60}h'
    resampled = df2.resample(rule).agg({'close_a':'last', 'close_b':'last'}).dropna()
    resampled = resampled.reset_index()
    return resampled

def is_rollover(t):
    m = t.hour * 60 + t.minute
    return m < ROLLOVER_LOCKOUT_MIN or (1440 - m) < ROLLOVER_LOCKOUT_MIN

def in_session(t):
    """Only trade during liquid hours to avoid bad spreads and overnight swaps."""
    return SESSION_START_HOUR <= t.hour < SESSION_END_HOUR

def calc_notional(pair_spec, avg_price_a, avg_price_b):
    """Correct effective notional for the pair."""
    not_a = pair_spec['cs_a'] * avg_price_a
    not_b = pair_spec['cs_b'] * avg_price_b
    return (not_a + not_b) / 2.0


def run_backtest(df, pair_spec, notional, z_entry, exit_z_base, hmm_hold, 
                 dwell_bars, tf_minutes, amp_hurdle=0.0, amp_max_mult=1.0,
                 session_filter=True):
    """Run full backtest with correct notional, exact broker costs, and amplitude gate."""
    bal = BAL; peak = BAL; daily_start = BAL; daily_date = None; ghost = False
    dakad = DynamicAKAD()
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec = 0; gcool = 0; n = len(df)
    
    # Scale warmup for timeframe
    warmup = max(200, MIN_WARMUP_BARS)

    eng = shf_core.CointegrationEngine(span=WELFORD_SPAN, beta=1.0, entry_z=z_entry,
        exit_z=exit_z_base, z_base=z_entry, gamma=GAMMA, hurst_window=HURST_WINDOW,
        dynamic_z=True, exit_z_base=exit_z_base, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
    hmm = HMMRegimeDetector(lookback=HMM_LOOKBACK, min_regime_hold=hmm_hold)

    pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0
    lspread = 0.0; pspread = 0.0; sent_abort = False
    last_close_bar = -9999; last_close_h = 0.5; entry_hour = 0
    entry_time = None
    trades = []

    # PRE-EXTRACT to numpy arrays (10-50x faster than df.iloc[bar])
    times_arr = df['time'].values   # numpy datetime64 array
    pa_arr = df['close_a'].values.astype(np.float64)
    pb_arr = df['close_b'].values.astype(np.float64)
    # Pre-compute hours, minutes, dates for session/rollover checks
    times_pd = pd.DatetimeIndex(df['time'])
    hours_arr = times_pd.hour.values
    minutes_arr = times_pd.minute.values
    dates_arr = times_pd.date

    for bar in range(n):
        if ghost: break
        pa = pa_arr[bar]; pb = pb_arr[bar]
        bt_hour = int(hours_arr[bar]); bt_min = int(minutes_arr[bar])
        cd = dates_arr[bar]
        if cd != daily_date: daily_date = cd; daily_start = bal

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
        beta_k, abort = sen.update(la, lb)
        if abort and not sent_abort:
            sent_abort = True
            if pos != 0:
                hold_mins = (bar - ebar) * tf_minutes
                gross = (spread-es)*pos*elots*notional
                cost = calc_cost(pair_spec, elots, entry_hour)
                swap = calc_swap_cost(pair_spec, elots, pos, hold_mins/60, entry_time)
                pnl = gross - cost - swap; bal += pnl; peak = max(peak,bal)
                w = pnl > 0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                trades.append({'pnl':pnl,'gross':gross,'cost':cost,'swap':swap,'hold_bars':bar-ebar})
                pos=0; last_close_bar=bar; last_close_h=h
            continue
        if sent_abort and not abort: sent_abort = False
        if sent_abort: continue

        hblocked = False
        if pspread != 0.0:
            hmm.update(spread - pspread); hblocked = hmm.is_blocked

        if bar < warmup: continue

        # ── ENTRY ──
        if pos == 0 and s != 0:
            if hblocked: continue
            bt_mins = bt_hour * 60 + bt_min
            if bt_mins < ROLLOVER_LOCKOUT_MIN or (1440 - bt_mins) < ROLLOVER_LOCKOUT_MIN: continue
            if session_filter and not (SESSION_START_HOUR <= bt_hour < SESSION_END_HOUR): continue
            
            # Dwell check (in bars)
            if last_close_bar >= 0:
                dwell_adj = max(1, dwell_bars * (last_close_h / 0.3))
                if (bar - last_close_bar) < dwell_adj: continue

            risk = dakad.calc(cdd, ddd)
            corr.compute_risk(); cm = corr.last_risk_multiplier
            lots = max(0.01, round(bal * risk * cm / 1000.0, 2))

            # ── AMPLITUDE GATE (blocks garbage, scales up strong setups) ──
            if amp_hurdle > 0:
                spread_sigma = eng.last_std if hasattr(eng, 'last_std') else 0
                if spread_sigma > 0:
                    z_captured = max(0.0, abs(z) - exz)
                    expected_profit = z_captured * spread_sigma * lots * notional
                    trade_cost = calc_cost(pair_spec, lots, bt_hour)
                    ratio = expected_profit / trade_cost if trade_cost > 0 else 999
                    if ratio < amp_hurdle:
                        continue  # BLOCKED — expected profit doesn't justify cost
                    # Opportunity multiplier: scale lots up for strong setups
                    if amp_max_mult > 1.0 and ratio > amp_hurdle:
                        excess = (ratio - amp_hurdle) / amp_hurdle
                        mult = min(amp_max_mult, 1.0 + 0.5 * excess)
                        lots = max(0.01, round(lots * mult, 2))

            pos = s; ez = z; es = spread; ebar = bar; elots = lots
            entry_hour = bt_hour; entry_time = bar

        # ── EXIT ──
        elif pos != 0:
            ex = False
            
            # HUBER 4.815σ HARD STOP (matches live engine catastrophe net)
            spread_sigma = eng.last_std if hasattr(eng, 'last_std') else 0
            if spread_sigma > 0:
                unrealized_z = (spread - es) * pos / spread_sigma
                if unrealized_z < -HUBER_SIGMA:
                    ex = True  # Catastrophe stop hit
            
            # Emergency exit at 2.5x entry Z
            if not ex and abs(z) > abs(ez) * 2.5: ex = True
            
            # SESSION HARD CUTOFF: force close before end of session
            if not ex and bt_hour >= SESSION_END_HOUR - 1 and bt_min >= 45:
                ex = True
            
            if not ex:
                hb = bar - ebar
                dwell_adj = max(1, dwell_bars * (h / 0.3))
                if hb < dwell_adj: continue
                if pos == 1 and z > -exz: ex = True
                elif pos == -1 and z < exz: ex = True
            
            if ex:
                hold_mins = (bar - ebar) * tf_minutes
                gross = (spread-es)*pos*elots*notional
                cost = calc_cost(pair_spec, elots, entry_hour)
                swap = calc_swap_cost(pair_spec, elots, pos, hold_mins/60, entry_time)
                pnl = gross - cost - swap; bal += pnl; peak = max(peak,bal)
                w = pnl > 0; dakad.record(w)
                if not w: consec+=1
                else: consec=0
                if consec>=MAX_CONSEC_LOSSES: gcool=bar+COOLDOWN_BARS; consec=0
                trades.append({'pnl':pnl,'gross':gross,'cost':cost,'swap':swap,'hold_bars':bar-ebar})
                pos=0; last_close_bar=bar; last_close_h=h

    total = len(trades)
    if total < 3: return None

    pnls = [t['pnl'] for t in trades]
    gross_pnls = [t['gross'] for t in trades]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    gp = sum(wins) if wins else 0; gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins)/total*100; pf = gp/gl if gl>0 else 0
    gross_wins = [g for g in gross_pnls if g > 0]
    gross_wr = len(gross_wins)/total*100

    first = df['time'].iloc[0]; last_t = df['time'].iloc[-1]
    days = max(1, (last_t - first).days); months = days / 30.0

    eq = BAL; eq_peak = eq; mdd = 0
    for t in trades:
        eq += t['pnl']; eq_peak = max(eq_peak, eq); mdd = max(mdd, eq_peak - eq)

    avg_hold = np.mean([t['hold_bars'] for t in trades])

    return {
        'trades': total, 'net_wr': round(wr,1), 'gross_wr': round(gross_wr,1),
        'pf': round(pf,2), 'net_pnl': round(bal-BAL,2),
        'gross_pnl': round(sum(gross_pnls),2),
        'total_costs': round(sum(t['cost'] for t in trades),2),
        'total_swap': round(sum(t['swap'] for t in trades),2),
        'avg_gross': round(sum(gross_pnls)/total,2),
        'avg_cost': round(sum(t['cost'] for t in trades)/total,2),
        'avg_net': round((bal-BAL)/total,2),
        'avg_hold_bars': round(avg_hold,1),
        'ghost': ghost, 'max_dd_pct': round(mdd/BAL*100,2),
        'trades_per_month': round(total/months,1) if months > 0 else 0,
        'monthly_return': round((bal-BAL)/BAL/months*100, 2) if months > 0 else 0,
        'days': days,
    }


def main():
    t_start = time.time()
    print("="*140)
    print("  FULL PAIR OPTIMIZATION — Multi-Timeframe × HMM × Dwell × Z Sweep")
    print("  Gold/Silver + NAS/DAX with EXACT 5%ers Broker Specs")
    print("="*140)
    print(f"\n  Timeframes: {[TF_NAMES[t] for t in TIMEFRAMES]}")
    print(f"  HMM holds: {HMM_HOLDS}")
    print(f"  Dwell (bars): {DWELL_BARS}")
    print(f"  Z entries: {Z_ENTRIES}")
    print(f"  Exit Zs: {EXIT_ZS}")
    print(f"  Session filter: {SESSION_START_HOUR}:00-{SESSION_END_HOUR}:00 UTC")
    total_configs = len(TIMEFRAMES) * len(HMM_HOLDS) * len(DWELL_BARS) * len(Z_ENTRIES) * len(EXIT_ZS)
    print(f"  Total configs per pair: {total_configs}")
    print(f"  Total tests: {total_configs * 2}")

    all_results = {}

    for pair_name, pair_spec in PAIRS.items():
        print(f"\n\n{'='*140}")
        print(f"  PAIR: {pair_name} ({pair_spec['sym_a']}/{pair_spec['sym_b']})")
        print(f"{'='*140}")

        # Load M1 data
        df_m1 = load_pair(pair_spec['sym_a'], pair_spec['sym_b'])
        if df_m1 is None or len(df_m1) < 1000:
            print(f"  ERROR: Insufficient data for {pair_name}")
            continue

        avg_a = df_m1['close_a'].mean()
        avg_b = df_m1['close_b'].mean()
        notional = calc_notional(pair_spec, avg_a, avg_b)
        
        print(f"  M1 bars: {len(df_m1):,}")
        print(f"  Avg {pair_spec['sym_a']}: ${avg_a:.2f} | Avg {pair_spec['sym_b']}: ${avg_b:.2f}")
        print(f"  Real notional: ${notional:,.0f}")

        pair_results = []
        best = None; best_score = -999999

        header = (f"  {'TF':>4} {'Z':>4} {'ExZ':>4} {'HMM':>4} {'Dwl':>4} "
                  f"{'Trades':>6} {'Tr/Mo':>5} {'NetWR':>6} {'GrsWR':>6} {'PF':>6} "
                  f"{'NetP&L':>10} {'$/Tr':>7} {'MaxDD':>6} {'Mo%%':>6} {'HldBr':>5}")
        print(f"\n{header}")
        print(f"  {'-'*130}")

        config_count = 0
        tf_start = time.time()
        pair_tfs = PAIR_TF_OVERRIDE.get(pair_name, TIMEFRAMES)
        print(f"  Timeframes for {pair_name}: {[TF_NAMES[t] for t in pair_tfs]}")
        for tf in pair_tfs:
            # Resample
            df_tf = resample_to_tf(df_m1, tf)
            if len(df_tf) < 500:
                print(f"  SKIP {TF_NAMES[tf]}: only {len(df_tf)} bars (need 500)")
                continue

            tf_t = time.time()
            tf_configs = 0
            print(f"  --- {TF_NAMES[tf]} ({len(df_tf):,} bars) ---", flush=True)

            # Use TF-adaptive Z entries (lower for higher TFs where Z is attenuated)
            tf_z_entries = Z_ENTRIES_BY_TF.get(tf, Z_ENTRIES)
            for z_ent in tf_z_entries:
                for exit_z in EXIT_ZS:
                    for hmm_h in HMM_HOLDS:
                        for dwell_b in DWELL_BARS:
                            config_count += 1; tf_configs += 1
                            if tf_configs % 36 == 0:
                                el = time.time() - tf_t
                                print(f"    {TF_NAMES[tf]}: {tf_configs}/216 configs ({el:.0f}s)...", flush=True)
                            
                            r = run_backtest(df_tf, pair_spec, notional,
                                            z_entry=z_ent, exit_z_base=exit_z,
                                            hmm_hold=hmm_h, dwell_bars=dwell_b,
                                            tf_minutes=tf, session_filter=True)
                            
                            if r is None: continue
                            
                            # Score: prefer high PF with reasonable trade count
                            score = r['net_pnl'] * min(1.0, r['trades'] / 20.0)
                            if r['ghost']: score *= 0.1
                            
                            marker = ""
                            if score > best_score and r['trades'] >= 5 and not r['ghost']:
                                best_score = score; best = (tf, z_ent, exit_z, hmm_h, dwell_b, r)
                                marker = " <<<"
                            
                            # Only print profitable or near-profitable configs
                            if r['pf'] >= 0.8 and r['trades'] >= 5:
                                ghost_str = " GH" if r['ghost'] else ""
                                print(f"  {TF_NAMES[tf]:>4} {z_ent:>4.1f} {exit_z:>4.2f} {hmm_h:>4} {dwell_b:>4} "
                                      f"{r['trades']:>6} {r['trades_per_month']:>5.0f} "
                                      f"{r['net_wr']:>5.1f}% {r['gross_wr']:>5.1f}% {r['pf']:>6.2f} "
                                      f"${r['net_pnl']:>9,.2f} ${r['avg_net']:>6.2f} {r['max_dd_pct']:>5.2f}% "
                                      f"{r['monthly_return']:>5.2f}% {r['avg_hold_bars']:>5.1f}"
                                      f"{ghost_str}{marker}")
                            
                            pair_results.append((tf, z_ent, exit_z, hmm_h, dwell_b, r))
        
        print(f"\n  Tested {config_count} configs, {len(pair_results)} produced trades")

        # Top 10 for this pair
        profitable = [(tf, z, ex, hm, dw, r) for tf, z, ex, hm, dw, r in pair_results
                      if r['net_pnl'] > 0 and r['trades'] >= 5 and not r['ghost']]
        
        profitable.sort(key=lambda x: -x[5]['net_pnl'])

        print(f"\n  {'='*80}")
        print(f"  TOP 10 PROFITABLE CONFIGS FOR {pair_name}")
        print(f"  {'='*80}")
        
        if profitable:
            for i, (tf, z, ex, hm, dw, r) in enumerate(profitable[:10]):
                print(f"\n  #{i+1}: TF={TF_NAMES[tf]} Z={z} ExitZ={ex} HMM={hm} Dwell={dw}bars")
                print(f"       Trades: {r['trades']} ({r['trades_per_month']:.0f}/mo) | "
                      f"Net WR: {r['net_wr']}% | Gross WR: {r['gross_wr']}%")
                print(f"       PF: {r['pf']} | Net: ${r['net_pnl']:,.2f} | "
                      f"$/trade: ${r['avg_net']:.2f} | Monthly: {r['monthly_return']:.2f}%")
                print(f"       MaxDD: {r['max_dd_pct']}% | Avg hold: {r['avg_hold_bars']:.1f} bars")
                print(f"       Avg gross: ${r['avg_gross']:.2f} | Avg cost: ${r['avg_cost']:.2f}")
                
                # Scale to $5K
                scale = 0.05  # approximate lots at $5K vs $100K
                net_5k = r['avg_net'] * scale * r['trades_per_month']
                print(f"       → At $5K: ~${net_5k:.0f}/month ({net_5k/50:.1f}%/mo)")
        else:
            print(f"  NO profitable configs found for {pair_name}")
            # Show the closest to profitable
            almost = [(tf, z, ex, hm, dw, r) for tf, z, ex, hm, dw, r in pair_results
                      if r['trades'] >= 5 and not r['ghost']]
            almost.sort(key=lambda x: -x[5]['pf'])
            if almost:
                print(f"  Closest to profitable (by PF):")
                for tf, z, ex, hm, dw, r in almost[:5]:
                    print(f"    TF={TF_NAMES[tf]} Z={z} ExZ={ex} HMM={hm} Dw={dw}: "
                          f"PF={r['pf']} WR={r['net_wr']}% Net=${r['net_pnl']:,.2f}")

        # =================================================================
        # PHASE 2: Amplitude Gate Sweep on Top 20 Configs
        # =================================================================
        # Take best configs from Phase 1, then try blocking garbage + scaling good
        top_for_amp = sorted(pair_results, key=lambda x: -x[5]['net_pnl'])[:20]
        
        if top_for_amp:
            print(f"\n  {'='*80}")
            print(f"  PHASE 2: AMPLITUDE GATE SWEEP on Top 20 Configs")
            print(f"  Hurdles: {AMP_HURDLES[1:]} | Multipliers: {AMP_MAX_MULTS}")
            print(f"  {'='*80}")
            
            amp_results = []
            for tf, z_ent, exit_z, hmm_h, dwell_b, base_r in top_for_amp:
                df_tf = resample_to_tf(df_m1, tf)
                if len(df_tf) < 500: continue
                
                for ah in AMP_HURDLES[1:]:  # Skip 0.0 (already tested in Phase 1)
                    for am in AMP_MAX_MULTS:
                        r = run_backtest(df_tf, pair_spec, notional,
                                        z_entry=z_ent, exit_z_base=exit_z,
                                        hmm_hold=hmm_h, dwell_bars=dwell_b,
                                        tf_minutes=tf, amp_hurdle=ah, amp_max_mult=am,
                                        session_filter=True)
                        if r is None: continue
                        if r['trades'] >= 3 and r['pf'] >= 0.8:
                            amp_results.append((tf, z_ent, exit_z, hmm_h, dwell_b, ah, am, r))
                            
                            # Check if this is better than Phase 1 best
                            score = r['net_pnl'] * min(1.0, r['trades'] / 20.0)
                            if not r['ghost'] and score > best_score and r['trades'] >= 5:
                                best_score = score
                                best = (tf, z_ent, exit_z, hmm_h, dwell_b, r)
                                # Store amp params in result
                                r['amp_hurdle'] = ah; r['amp_max_mult'] = am
            
            # Show top amplitude gate results
            amp_profitable = [x for x in amp_results if x[7]['net_pnl'] > 0 and x[7]['trades'] >= 5 and not x[7]['ghost']]
            amp_profitable.sort(key=lambda x: -x[7]['net_pnl'])
            
            if amp_profitable:
                print(f"\n  Top 5 Amplitude Gate configs (improvement over Phase 1):")
                for i, (tf, z, ex, hm, dw, ah, am, r) in enumerate(amp_profitable[:5]):
                    print(f"    #{i+1}: TF={TF_NAMES[tf]} Z={z} ExZ={ex} HMM={hm} Dw={dw} "
                          f"Hurdle={ah} Mult={am}")
                    print(f"         {r['trades']} trades | WR={r['net_wr']}% | PF={r['pf']} | "
                          f"Net=${r['net_pnl']:,.2f} | $/trade=${r['avg_net']:.2f} | DD={r['max_dd_pct']}%")
                    # Find matching Phase 1 baseline
                    base = [x for x in pair_results if x[0]==tf and x[1]==z and x[2]==ex and x[3]==hm and x[4]==dw]
                    if base:
                        bp = base[0][5]['net_pnl']
                        improvement = r['net_pnl'] - bp
                        print(f"         vs Phase 1: ${bp:,.2f} → ${r['net_pnl']:,.2f} ({'+' if improvement > 0 else ''}{improvement:,.2f})")
            else:
                print(f"  No amplitude gate configs improved on Phase 1")

        all_results[pair_name] = {
            'profitable_count': len(profitable),
            'best': best,
            'all': [(tf, z, ex, hm, dw, {k:v for k,v in r.items()}) 
                    for tf, z, ex, hm, dw, r in profitable[:10]] if profitable else []
        }

    # =========================================================================
    # COMBINED PORTFOLIO
    # =========================================================================
    print(f"\n\n{'='*140}")
    print("  COMBINED PORTFOLIO ANALYSIS")
    print(f"{'='*140}")
    
    for pname, pdata in all_results.items():
        if pdata['best']:
            tf, z, ex, hm, dw, r = pdata['best']
            print(f"\n  {pname} BEST: TF={TF_NAMES[tf]} Z={z} ExZ={ex} HMM={hm} Dwell={dw}")
            print(f"    Net: ${r['net_pnl']:,.2f} | {r['monthly_return']:.2f}%/mo | "
                  f"PF={r['pf']} | {r['trades_per_month']:.0f} trades/mo | DD={r['max_dd_pct']}%")
            
            scale = 0.05
            monthly_5k = r['avg_net'] * scale * r['trades_per_month']
            print(f"    At $5K:  ~${monthly_5k:.0f}/month")
            print(f"    At $100K: ~${r['avg_net'] * r['trades_per_month']:.0f}/month")
        else:
            print(f"\n  {pname}: No profitable config found")

    elapsed = time.time() - t_start
    print(f"\n\n  Completed in {elapsed:.0f}s ({elapsed/60:.1f} min)")
    
    # Save results
    save_path = Path(__file__).resolve().parent.parent / "Results" / "pair_optimization_full.json"
    save_data = {}
    for pname, pdata in all_results.items():
        save_data[pname] = {
            'profitable_count': pdata['profitable_count'],
            'top_configs': []
        }
        if pdata['all']:
            for tf, z, ex, hm, dw, r in pdata['all']:
                save_data[pname]['top_configs'].append({
                    'tf': TF_NAMES[tf], 'z_entry': z, 'exit_z': ex,
                    'hmm_hold': hm, 'dwell_bars': dw, **r
                })
    
    with open(save_path, 'w') as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"  Results saved to {save_path}")


if __name__ == "__main__":
    main()
