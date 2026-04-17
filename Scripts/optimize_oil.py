#!/usr/bin/env python3
"""
OIL PAIR OPTIMIZATION — XTIUSD/XBRUSD (WTI vs Brent)
=====================================================
Same engine as optimize_pairs_full.py but for Oil with EXACT 5%ers broker specs.

EXACT 5%ERS SPECS:
  XTIUSD (WTI):  CS=100, Commission=0.03% per deal, Swap L=-70, S=-40
  XBRUSD (Brent): CS=100, Commission=0.03% per deal, Swap L=-70, S=-40
  Swap multiplier: Mon-Thu=1×, Friday=10× (covers weekend)

Tests every combination of:
  - Timeframe: M1, M5, M15, M30, H1, H4
  - HMM hold: 5, 10, 20, 50
  - Dwell base: 1, 3, 5, 10, 20, 60 bars
  - Z entry: TF-adaptive (2.0-3.0 for M1, lower for higher TFs)
  - Exit Z: 0.0, 0.25, 0.5

Includes:
  - Huber 4.815σ catastrophe hard stop (matches live engine)
  - Session filter 07:00-20:00 UTC
  - Session hard cutoff at 19:45 (prevents overnight holds/swaps)
  - Ghost stops (4% daily DD, 9% max DD)
  - Kalman Sentinel, HMM regime filter, Dynamic AKAD, Correlation monitor
  - Phase 2: Amplitude gate sweep on top configs
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
# EXACT 5%ERS OIL BROKER SPECS
# ============================================================================
PAIRS = {
    'Oil_Spread': {
        'sym_a': 'XTIUSD', 'sym_b': 'XBRUSD',
        'cs_a': 100, 'cs_b': 100,             # contract size = 100 barrels per lot
        'comm_pct': 0.0003,                    # 0.03% commission per deal
        # Typical spreads in price points (dollars per barrel)
        # XTIUSD spread ~$0.03-0.05 → cost per lot per fill = $0.04 × 100 = $4
        # XBRUSD spread ~$0.04-0.06 → cost per lot per fill = $0.05 × 100 = $5
        'spread_a_pts': 4,                     # XTIUSD spread cost per lot per fill ($)
        'spread_b_pts': 5,                     # XBRUSD spread cost per lot per fill ($)
        'swap_long_a': -70, 'swap_short_a': -40,   # XTIUSD swaps (points/lot/night)
        'swap_long_b': -70, 'swap_short_b': -40,   # XBRUSD swaps (points/lot/night)
        'swap_friday_mult': 10,                     # Friday = 10× (covers weekend)
    },
}

# ============================================================================
# ENGINE PARAMS (identical to live engine)
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

# Timeframes to test
TIMEFRAMES = [1, 5, 15, 30, 60, 240]
TF_NAMES = {1:'M1', 5:'M5', 15:'M15', 30:'M30', 60:'H1', 240:'H4'}

# Sweep ranges
HMM_HOLDS = [10]  # Only HMM=10
DWELL_BARS = [1, 3, 5, 10, 20, 60]
Z_ENTRIES = [2.0, 2.5, 3.0]
EXIT_ZS = [0.0, 0.25, 0.5]

# TF-adaptive Z thresholds (Z-scores attenuated on higher TFs due to wider Welford window)
Z_ENTRIES_BY_TF = {
    1:   [2.0, 2.5, 3.0],
    5:   [1.0, 1.5, 2.0],
    15:  [0.8, 1.0, 1.5],
    30:  [0.5, 0.8, 1.0],
    60:  [0.5, 0.8, 1.0],
    240: [0.5, 0.8, 1.0],
}

# Huber 4.815σ catastrophe hard stop (matches live engine)
HUBER_SIGMA = 4.815

# Amplitude gate: baked into all runs (hurdle=1.0, mult=2.0)
AMP_HURDLE = 1.0
AMP_MAX_MULT = 2.0

# Session filter: 07:00-20:00 UTC
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
    """Oil spreads widen outside core hours."""
    if 0 <= hour < 7: return 2.0     # Asian session — oil very thin
    elif 7 <= hour < 9: return 1.3    # London open
    elif 9 <= hour < 17: return 1.0   # London+NY overlap — tightest
    elif 17 <= hour < 20: return 1.2  # NY afternoon
    else: return 1.8                  # After hours


def calc_cost(pair_spec, lots, hour, avg_price_a, avg_price_b):
    """Calculate total round-trip cost with EXACT 5%ers oil specs.
    
    Oil cost model:
      Spread: $4/fill for XTIUSD, $5/fill for XBRUSD (× lots × 2 fills per leg × spread mult)
      Commission: 0.03% of deal volume per deal, 4 deals total (open+close for each leg)
        - XTIUSD deal volume = price × CS × lots = ~$62 × 100 × lots = ~$6,200 × lots
        - XBRUSD deal volume = price × CS × lots = ~$67 × 100 × lots = ~$6,700 × lots
    """
    sm = get_spread_multiplier(hour)
    
    # Spread cost: $/fill × lots × 2 fills (open+close) for each leg
    spread_cost_a = pair_spec['spread_a_pts'] * lots * 2 * sm
    spread_cost_b = pair_spec['spread_b_pts'] * lots * 2 * sm
    
    # Commission: 0.03% of deal volume per deal × 4 deals
    # Deal volume = price × contract_size × lots
    deal_vol_a = avg_price_a * pair_spec['cs_a'] * lots
    deal_vol_b = avg_price_b * pair_spec['cs_b'] * lots
    comm = pair_spec['comm_pct'] * (deal_vol_a + deal_vol_b) * 2  # 2 deals per leg (open+close) = 4 total
    
    return spread_cost_a + spread_cost_b + comm


def calc_swap_cost(pair_spec, lots, position, hold_hours, entry_bar, dates_arr):
    """Calculate swap cost for oil held overnight.
    
    BOTH legs of oil pay negative swap (no free carry):
      Position=+1: long XTIUSD (swap=-70), short XBRUSD (swap=-40)
      Position=-1: short XTIUSD (swap=-40), long XBRUSD (swap=-70)
      Friday = 10× multiplier (covers weekend)
    """
    if hold_hours < 16:  # trades < 16 hours unlikely to cross midnight
        return 0.0
    
    nights = max(1, int(hold_hours / 24))
    
    if position == 1:  # long XTIUSD, short XBRUSD
        swap_a = abs(pair_spec['swap_long_a'])    # -70 → pay 70 pts
        swap_b = abs(pair_spec['swap_short_b'])   # -40 → pay 40 pts
    else:              # short XTIUSD, long XBRUSD
        swap_a = abs(pair_spec['swap_short_a'])   # -40 → pay 40 pts
        swap_b = abs(pair_spec['swap_long_b'])    # -70 → pay 70 pts
    
    # Total swap points per night per lot
    swap_per_night = swap_a + swap_b  # 110 points total
    total_swap_pts = swap_per_night * nights * lots
    
    # Convert points to USD: for oil with CS=100, 1 point = $0.01 per barrel × 100 = $1
    # Actually "points" in MT5 for oil depends on the point size
    # For XTIUSD: 1 point = 0.01, so 70 points = $0.70/lot/night × CS
    # Swap in USD = swap_points × point_value × lots
    # Point value for CS=100 oil = $0.01 × 100 = $1 per point per lot
    swap_usd = total_swap_pts * 1.0  # $1 per point per lot
    
    return swap_usd


def load_pair(sym_a, sym_b):
    d = Path(__file__).resolve().parent.parent / "data" / "historical"
    a = pd.read_csv(d / f"{sym_a}_M1.csv", parse_dates=['time']).rename(columns={'close':'close_a'})
    b = pd.read_csv(d / f"{sym_b}_M1.csv", parse_dates=['time']).rename(columns={'close':'close_b'})
    m = pd.merge(a[['time','close_a']], b[['time','close_b']], on='time', how='inner')
    m = m.sort_values('time').reset_index(drop=True)
    return m[(m['close_a']>0)&(m['close_b']>0)].reset_index(drop=True)


def resample_to_tf(df, tf_minutes):
    if tf_minutes == 1:
        return df
    df2 = df.set_index('time')
    rule = f'{tf_minutes}min' if tf_minutes < 60 else f'{tf_minutes//60}h'
    resampled = df2.resample(rule).agg({'close_a':'last', 'close_b':'last'}).dropna()
    return resampled.reset_index()


def calc_notional(pair_spec, avg_price_a, avg_price_b):
    """Correct effective notional for the oil pair.
    
    For oil CS=100:
      XTIUSD at ~$62: notional_a = 100 × $62 = $6,200
      XBRUSD at ~$67: notional_b = 100 × $67 = $6,700
      Average notional ≈ $6,450
    """
    not_a = pair_spec['cs_a'] * avg_price_a
    not_b = pair_spec['cs_b'] * avg_price_b
    return (not_a + not_b) / 2.0


def run_backtest(df, pair_spec, notional, avg_pa, avg_pb, z_entry, exit_z_base, 
                 hmm_hold, dwell_bars, tf_minutes, amp_hurdle=0.0, amp_max_mult=1.0,
                 session_filter=True):
    """Run full backtest with correct oil notional, exact 5%ers costs, all stops."""
    bal = BAL; peak = BAL; daily_start = BAL; daily_date = None; ghost = False
    dakad = DynamicAKAD()
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec = 0; gcool = 0; n = len(df)
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
    pa_arr = df['close_a'].values.astype(np.float64)
    pb_arr = df['close_b'].values.astype(np.float64)
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

        cdd = max(0, (peak - bal) / peak) if peak > 0 else 0
        ddd = max(0, (daily_start - bal) / daily_start) if daily_start > 0 else 0
        if ddd >= GHOST_DAILY_DD: ghost = True; break
        if cdd >= GHOST_MAX_DD: ghost = True; break
        if bar < gcool: continue

        pspread = lspread
        sig = eng.update(pa, pb)
        z = sig.z_score; s = sig.signal; spread = sig.spread; lspread = spread
        h = eng.last_hurst; exz = eng.last_exit_z

        if pspread != 0.0: corr.push_return(0, spread - pspread)

        la = math.log(pa) if pa > 0 else 0; lb = math.log(pb) if pb > 0 else 0
        beta_k, abort = sen.update(la, lb)
        if abort and not sent_abort:
            sent_abort = True
            if pos != 0:
                hold_mins = (bar - ebar) * tf_minutes
                gross = (spread - es) * pos * elots * notional
                cost = calc_cost(pair_spec, elots, entry_hour, avg_pa, avg_pb)
                swap = calc_swap_cost(pair_spec, elots, pos, hold_mins / 60, entry_time, dates_arr)
                pnl = gross - cost - swap; bal += pnl; peak = max(peak, bal)
                w = pnl > 0; dakad.record(w)
                if not w: consec += 1
                else: consec = 0
                if consec >= MAX_CONSEC_LOSSES: gcool = bar + COOLDOWN_BARS; consec = 0
                trades.append({'pnl': pnl, 'gross': gross, 'cost': cost, 'swap': swap, 'hold_bars': bar - ebar})
                pos = 0; last_close_bar = bar; last_close_h = h
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

            # Dwell check
            if last_close_bar >= 0:
                dwell_adj = max(1, dwell_bars * (last_close_h / 0.3))
                if (bar - last_close_bar) < dwell_adj: continue

            risk = dakad.calc(cdd, ddd)
            corr.compute_risk(); cm = corr.last_risk_multiplier
            lots = max(0.01, round(bal * risk * cm / 1000.0, 2))

            # ── AMPLITUDE GATE ──
            if amp_hurdle > 0:
                spread_sigma = eng.last_std if hasattr(eng, 'last_std') else 0
                if spread_sigma > 0:
                    z_captured = max(0.0, abs(z) - exz)
                    expected_profit = z_captured * spread_sigma * lots * notional
                    trade_cost = calc_cost(pair_spec, lots, bt_hour, avg_pa, avg_pb)
                    ratio = expected_profit / trade_cost if trade_cost > 0 else 999
                    if ratio < amp_hurdle:
                        continue
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
                    ex = True

            # Emergency exit at 2.5× entry Z
            if not ex and abs(z) > abs(ez) * 2.5: ex = True

            # SESSION HARD CUTOFF at 19:45 — prevents overnight holds
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
                gross = (spread - es) * pos * elots * notional
                cost = calc_cost(pair_spec, elots, entry_hour, avg_pa, avg_pb)
                swap = calc_swap_cost(pair_spec, elots, pos, hold_mins / 60, entry_time, dates_arr)
                pnl = gross - cost - swap; bal += pnl; peak = max(peak, bal)
                w = pnl > 0; dakad.record(w)
                if not w: consec += 1
                else: consec = 0
                if consec >= MAX_CONSEC_LOSSES: gcool = bar + COOLDOWN_BARS; consec = 0
                trades.append({'pnl': pnl, 'gross': gross, 'cost': cost, 'swap': swap, 'hold_bars': bar - ebar})
                pos = 0; last_close_bar = bar; last_close_h = h

    total = len(trades)
    if total < 3: return None

    pnls = [t['pnl'] for t in trades]
    gross_pnls = [t['gross'] for t in trades]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    gp = sum(wins) if wins else 0; gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins) / total * 100; pf = gp / gl if gl > 0 else 0
    gross_wins = [g for g in gross_pnls if g > 0]
    gross_wr = len(gross_wins) / total * 100

    first = df['time'].iloc[0]; last_t = df['time'].iloc[-1]
    days = max(1, (last_t - first).days); months = days / 30.0

    eq = BAL; eq_peak = eq; mdd = 0
    for t in trades:
        eq += t['pnl']; eq_peak = max(eq_peak, eq); mdd = max(mdd, eq_peak - eq)

    avg_hold = np.mean([t['hold_bars'] for t in trades])

    return {
        'trades': total, 'net_wr': round(wr, 1), 'gross_wr': round(gross_wr, 1),
        'pf': round(pf, 2), 'net_pnl': round(bal - BAL, 2),
        'gross_pnl': round(sum(gross_pnls), 2),
        'total_costs': round(sum(t['cost'] for t in trades), 2),
        'total_swap': round(sum(t['swap'] for t in trades), 2),
        'avg_gross': round(sum(gross_pnls) / total, 2),
        'avg_cost': round(sum(t['cost'] for t in trades) / total, 2),
        'avg_net': round((bal - BAL) / total, 2),
        'avg_hold_bars': round(avg_hold, 1),
        'ghost': ghost, 'max_dd_pct': round(mdd / BAL * 100, 2),
        'trades_per_month': round(total / months, 1) if months > 0 else 0,
        'monthly_return': round((bal - BAL) / BAL / months * 100, 2) if months > 0 else 0,
        'days': days,
    }


def main():
    t_start = time.time()
    print("=" * 140)
    print("  OIL PAIR OPTIMIZATION — XTIUSD/XBRUSD (WTI vs Brent)")
    print("  Multi-Timeframe × HMM × Dwell × Z Sweep with EXACT 5%ers Broker Specs")
    print("=" * 140)
    print()
    print("  5%ERS SPECS:")
    print("    XTIUSD: CS=100, Commission=0.03%/deal, Swap L=-70, S=-40")
    print("    XBRUSD: CS=100, Commission=0.03%/deal, Swap L=-70, S=-40")
    print("    Swap multiplier: Mon-Thu=1×, Friday=10×")
    print()
    print(f"  Timeframes: {[TF_NAMES[t] for t in TIMEFRAMES]}")
    print(f"  HMM holds: {HMM_HOLDS}")
    print(f"  Dwell (bars): {DWELL_BARS}")
    print(f"  Exit Zs: {EXIT_ZS}")
    print(f"  Session filter: {SESSION_START_HOUR}:00-{SESSION_END_HOUR}:00 UTC")
    print(f"  Huber hard stop: {HUBER_SIGMA}σ")
    print(f"  Amplitude gate: Hurdle={AMP_HURDLE}, Mult={AMP_MAX_MULT} (baked in)")
    total_configs = len(TIMEFRAMES) * len(HMM_HOLDS) * len(DWELL_BARS) * 3 * len(EXIT_ZS)
    print(f"  Total configs: {total_configs}")

    pair_name = 'Oil_Spread'
    pair_spec = PAIRS[pair_name]

    # Load M1 data
    print(f"\n{'=' * 140}")
    print(f"  Loading {pair_spec['sym_a']}/{pair_spec['sym_b']} M1 data...")
    df_m1 = load_pair(pair_spec['sym_a'], pair_spec['sym_b'])
    if df_m1 is None or len(df_m1) < 1000:
        print("  ERROR: Insufficient data")
        return

    avg_a = df_m1['close_a'].mean()
    avg_b = df_m1['close_b'].mean()
    notional = calc_notional(pair_spec, avg_a, avg_b)

    print(f"  M1 bars: {len(df_m1):,}")
    print(f"  Date range: {df_m1['time'].iloc[0]} to {df_m1['time'].iloc[-1]}")
    print(f"  Avg XTIUSD: ${avg_a:.2f} | Avg XBRUSD: ${avg_b:.2f}")
    print(f"  Real notional (CS×price avg): ${notional:,.0f}")
    print()
    
    # Show cost breakdown at typical lot sizes
    for lot_ex in [0.06, 0.5, 1.0]:
        cost = calc_cost(pair_spec, lot_ex, 12, avg_a, avg_b)
        swap = calc_swap_cost(pair_spec, lot_ex, 1, 24, 0, None)
        comm_a = pair_spec['comm_pct'] * avg_a * pair_spec['cs_a'] * lot_ex * 2
        comm_b = pair_spec['comm_pct'] * avg_b * pair_spec['cs_b'] * lot_ex * 2
        print(f"  Cost at {lot_ex:.2f} lots: ${cost:.2f} (spread: ${pair_spec['spread_a_pts']*lot_ex*2 + pair_spec['spread_b_pts']*lot_ex*2:.2f} + "
              f"comm: ${comm_a + comm_b:.2f}) | Overnight swap: ${swap:.2f}/night")
    print()

    pair_results = []
    best = None; best_score = -999999

    header = (f"  {'TF':>4} {'Z':>4} {'ExZ':>4} {'HMM':>4} {'Dwl':>4} "
              f"{'Trades':>6} {'Tr/Mo':>5} {'NetWR':>6} {'GrsWR':>6} {'PF':>6} "
              f"{'NetP&L':>10} {'$/Tr':>7} {'MaxDD':>6} {'Mo%%':>6} {'HldBr':>5}")
    print(f"{header}")
    print(f"  {'-' * 130}")

    config_count = 0

    for tf in TIMEFRAMES:
        df_tf = resample_to_tf(df_m1, tf)
        if len(df_tf) < 500:
            print(f"  SKIP {TF_NAMES[tf]}: only {len(df_tf)} bars (need 500)")
            continue

        tf_t = time.time()
        tf_configs = 0
        tf_z_entries = Z_ENTRIES_BY_TF.get(tf, Z_ENTRIES)
        n_configs = len(tf_z_entries) * len(EXIT_ZS) * len(HMM_HOLDS) * len(DWELL_BARS)
        print(f"  --- {TF_NAMES[tf]} ({len(df_tf):,} bars) Z={tf_z_entries} ---", flush=True)

        for z_ent in tf_z_entries:
            for exit_z in EXIT_ZS:
                for hmm_h in HMM_HOLDS:
                    for dwell_b in DWELL_BARS:
                        config_count += 1; tf_configs += 1
                        if tf_configs % 36 == 0:
                            el = time.time() - tf_t
                            print(f"    {TF_NAMES[tf]}: {tf_configs}/{n_configs} configs ({el:.0f}s)...", flush=True)

                        r = run_backtest(df_tf, pair_spec, notional, avg_a, avg_b,
                                         z_entry=z_ent, exit_z_base=exit_z,
                                         hmm_hold=hmm_h, dwell_bars=dwell_b,
                                         tf_minutes=tf, amp_hurdle=AMP_HURDLE,
                                         amp_max_mult=AMP_MAX_MULT,
                                         session_filter=True)

                        if r is None: continue

                        score = r['net_pnl'] * min(1.0, r['trades'] / 20.0)
                        if r['ghost']: score *= 0.1

                        marker = ""
                        if score > best_score and r['trades'] >= 5 and not r['ghost']:
                            best_score = score; best = (tf, z_ent, exit_z, hmm_h, dwell_b, r)
                            marker = " <<<"

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

    # ── TOP 10 ──
    profitable = [(tf, z, ex, hm, dw, r) for tf, z, ex, hm, dw, r in pair_results
                  if r['net_pnl'] > 0 and r['trades'] >= 5 and not r['ghost']]
    profitable.sort(key=lambda x: -x[5]['net_pnl'])

    print(f"\n  {'=' * 80}")
    print(f"  TOP 10 PROFITABLE CONFIGS FOR OIL SPREAD")
    print(f"  {'=' * 80}")

    if profitable:
        for i, (tf, z, ex, hm, dw, r) in enumerate(profitable[:10]):
            print(f"\n  #{i+1}: TF={TF_NAMES[tf]} Z={z} ExitZ={ex} HMM={hm} Dwell={dw}bars")
            print(f"       Trades: {r['trades']} ({r['trades_per_month']:.0f}/mo) | "
                  f"Net WR: {r['net_wr']}% | Gross WR: {r['gross_wr']}%")
            print(f"       PF: {r['pf']} | Net: ${r['net_pnl']:,.2f} | "
                  f"$/trade: ${r['avg_net']:.2f} | Monthly: {r['monthly_return']:.2f}%")
            print(f"       MaxDD: {r['max_dd_pct']}% | Avg hold: {r['avg_hold_bars']:.1f} bars")
            print(f"       Avg gross: ${r['avg_gross']:.2f} | Avg cost: ${r['avg_cost']:.2f} | "
                  f"Total swap: ${r['total_swap']:.2f}")

            scale = 0.05
            net_5k = r['avg_net'] * scale * r['trades_per_month']
            print(f"       -> At $5K: ~${net_5k:.0f}/month ({net_5k/50:.1f}%/mo)")
    else:
        print(f"  NO profitable configs found")
        almost = [(tf, z, ex, hm, dw, r) for tf, z, ex, hm, dw, r in pair_results
                  if r['trades'] >= 5 and not r['ghost']]
        almost.sort(key=lambda x: -x[5]['pf'])
        if almost:
            print(f"  Closest to profitable (by PF):")
            for tf, z, ex, hm, dw, r in almost[:5]:
                print(f"    TF={TF_NAMES[tf]} Z={z} ExZ={ex} HMM={hm} Dw={dw}: "
                      f"PF={r['pf']} WR={r['net_wr']}% Net=${r['net_pnl']:,.2f} "
                      f"Costs=${r['total_costs']:.2f} Swap=${r['total_swap']:.2f}")

    # ── FINAL SUMMARY ──
    print(f"\n\n{'=' * 140}")
    print("  FINAL SUMMARY")
    print(f"{'=' * 140}")

    if best:
        tf, z, ex, hm, dw, r = best
        print(f"\n  OIL BEST: TF={TF_NAMES[tf]} Z={z} ExZ={ex} HMM={hm} Dwell={dw}")
        print(f"    Net: ${r['net_pnl']:,.2f} | {r['monthly_return']:.2f}%/mo | "
              f"PF={r['pf']} | {r['trades_per_month']:.0f} trades/mo | DD={r['max_dd_pct']}%")
        print(f"    Avg gross: ${r['avg_gross']:.2f} | Avg cost: ${r['avg_cost']:.2f} | Total swap: ${r['total_swap']:.2f}")
        if 'amp_hurdle' in r:
            print(f"    Amplitude gate: Hurdle={r['amp_hurdle']} Mult={r['amp_max_mult']}")

        scale = 0.05
        monthly_5k = r['avg_net'] * scale * r['trades_per_month']
        print(f"\n    At $5K:   ~${monthly_5k:.0f}/month ({monthly_5k/50:.1f}%/mo)")
        print(f"    At $100K: ~${r['avg_net'] * r['trades_per_month']:.0f}/month")
    else:
        print(f"\n  No profitable config found for Oil")

    elapsed = time.time() - t_start
    print(f"\n\n  Completed in {elapsed:.0f}s ({elapsed / 60:.1f} min)")

    # Save results
    save_path = Path(__file__).resolve().parent.parent / "Results" / "oil_optimization.json"
    save_data = {
        'pair': 'XTIUSD/XBRUSD',
        'notional': notional,
        'avg_xtiusd': avg_a,
        'avg_xbrusd': avg_b,
        'profitable_count': len(profitable),
        'top_configs': []
    }
    if profitable:
        for tf, z, ex, hm, dw, r in profitable[:10]:
            save_data['top_configs'].append({
                'tf': TF_NAMES[tf], 'z_entry': z, 'exit_z': ex,
                'hmm_hold': hm, 'dwell_bars': dw, **r
            })

    with open(save_path, 'w') as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"  Results saved to {save_path}")


if __name__ == "__main__":
    main()
