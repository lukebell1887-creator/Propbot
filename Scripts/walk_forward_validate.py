#!/usr/bin/env python3
"""
WALK-FORWARD VALIDATION — Train 70% / Test 30%
================================================
The gold standard for proving a trading strategy isn't overfit.

Method:
  1. Split data chronologically: first 70% = TRAIN, last 30% = TEST
  2. On TRAIN: sweep Z, ExitZ, Dwell with FINE grid to find optimal config
  3. On TEST: run the EXACT winning config — this is OUT-OF-SAMPLE validation
  4. Compare train vs test metrics (WR, PF, $/trade, MaxDD)

If test results are similar to train → strategy is ROBUST
If test falls apart → strategy was OVERFIT to historical data

Known best params (locked in):
  Gold/Silver: HMM=10, Hurdle=1.0, Mult=2.0
  Oil:         HMM=10, Hurdle=1.0, Mult=2.0
  NAS/DAX:     HMM=20, Hurdle=1.0, Mult=2.0

Swept params (optimized on 70% train):
  Z entry:  fine grid 1.5 to 3.5 step 0.25
  Exit Z:   fine grid 0.0 to 0.75 step 0.125
  Dwell:    [1, 3, 5, 10, 15, 20, 30, 60] bars
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
# EXACT 5%ERS BROKER SPECS — ALL PAIRS
# ============================================================================
PAIRS = {
    'Gold_Silver': {
        'sym_a': 'XAUUSD', 'sym_b': 'XAGUSD',
        'cs_a': 100, 'cs_b': 5000,
        'comm_pct': 0.000009,       # 0.0009%
        'spread_a_pts': 30, 'spread_b_pts': 3,
        'swap_long_a': -91, 'swap_short_a': -68,
        'swap_long_b': -20, 'swap_short_b': -17,
        'swap_friday_mult': 3,
        'hmm_hold': 10,   # LOCKED — proven best
    },
    'Oil_Spread': {
        'sym_a': 'XTIUSD', 'sym_b': 'XBRUSD',
        'cs_a': 100, 'cs_b': 100,
        'comm_pct': 0.0003,         # 0.03%
        'spread_a_pts': 4, 'spread_b_pts': 5,
        'swap_long_a': -70, 'swap_short_a': -40,
        'swap_long_b': -70, 'swap_short_b': -40,
        'swap_friday_mult': 10,
        'hmm_hold': 10,   # LOCKED — proven best
    },
    'NAS_DAX': {
        'sym_a': 'US100', 'sym_b': 'DE40',
        'cs_a': 1, 'cs_b': 1,
        'comm_pct': 0,
        'spread_a_pts': 2, 'spread_b_pts': 2,
        'swap_long_a': -300, 'swap_short_a': -300,
        'swap_long_b': -500, 'swap_short_b': -500,
        'swap_friday_mult': 3,
        'hmm_hold': 20,   # LOCKED — proven best
    },
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
HUBER_SIGMA = 4.815
AMP_HURDLE = 1.0    # LOCKED
AMP_MAX_MULT = 2.0  # LOCKED
SESSION_START_HOUR = 7; SESSION_END_HOUR = 20

# Fine-grid sweep for Z, ExitZ, Dwell (optimized on TRAIN only)
Z_GRID = [1.5, 1.75, 2.0, 2.25, 2.5, 2.75, 3.0, 3.25, 3.5]
EXIT_Z_GRID = [0.0, 0.125, 0.25, 0.375, 0.5, 0.625, 0.75]
DWELL_GRID = [1, 3, 5, 10, 15, 20, 30, 60]

TRAIN_PCT = 0.70  # 70% train, 30% test


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
    if 0 <= hour < 7: return 2.0
    elif 7 <= hour < 9: return 1.3
    elif 9 <= hour < 17: return 1.0
    elif 17 <= hour < 20: return 1.2
    else: return 1.8


def calc_cost(pair_spec, lots, hour, avg_pa, avg_pb):
    sm = get_spread_multiplier(hour)
    spread_cost_a = pair_spec['spread_a_pts'] * lots * 2 * sm
    spread_cost_b = pair_spec['spread_b_pts'] * lots * 2 * sm
    comm = 0
    if pair_spec['comm_pct'] > 0:
        deal_vol_a = avg_pa * pair_spec['cs_a'] * lots
        deal_vol_b = avg_pb * pair_spec['cs_b'] * lots
        comm = pair_spec['comm_pct'] * (deal_vol_a + deal_vol_b) * 2
    return spread_cost_a + spread_cost_b + comm


def calc_swap_cost(pair_spec, lots, position, hold_hours):
    if hold_hours < 16: return 0.0
    nights = max(1, int(hold_hours / 24))
    if position == 1:
        swap_a = abs(pair_spec['swap_long_a'])
        swap_b = abs(pair_spec['swap_short_b'])
    else:
        swap_a = abs(pair_spec['swap_short_a'])
        swap_b = abs(pair_spec['swap_long_b'])
    total_swap_pts = (swap_a + swap_b) * nights * lots
    return total_swap_pts * 0.01 if pair_spec['sym_a'] == 'XAUUSD' else total_swap_pts * 1.0


def load_pair(sym_a, sym_b):
    d = Path(__file__).resolve().parent.parent / "data" / "historical"
    a = pd.read_csv(d / f"{sym_a}_M1.csv", parse_dates=['time']).rename(columns={'close': 'close_a'})
    b = pd.read_csv(d / f"{sym_b}_M1.csv", parse_dates=['time']).rename(columns={'close': 'close_b'})
    m = pd.merge(a[['time', 'close_a']], b[['time', 'close_b']], on='time', how='inner')
    m = m.sort_values('time').reset_index(drop=True)
    return m[(m['close_a'] > 0) & (m['close_b'] > 0)].reset_index(drop=True)


def calc_notional(pair_spec, avg_pa, avg_pb):
    return (pair_spec['cs_a'] * avg_pa + pair_spec['cs_b'] * avg_pb) / 2.0


def run_backtest(df, pair_spec, notional, avg_pa, avg_pb, z_entry, exit_z_base,
                 hmm_hold, dwell_bars, amp_hurdle=1.0, amp_max_mult=2.0):
    """Run full backtest — M1 only."""
    bal = BAL; peak = BAL; daily_start = BAL; daily_date = None; ghost = False
    dakad = DynamicAKAD()
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    consec = 0; gcool = 0; n = len(df)

    eng = shf_core.CointegrationEngine(span=WELFORD_SPAN, beta=1.0, entry_z=z_entry,
        exit_z=exit_z_base, z_base=z_entry, gamma=GAMMA, hurst_window=HURST_WINDOW,
        dynamic_z=True, exit_z_base=exit_z_base, exit_gamma=EXIT_GAMMA, dynamic_exit=True)
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
    hmm = HMMRegimeDetector(lookback=HMM_LOOKBACK, min_regime_hold=hmm_hold)

    pos = 0; ez = 0.0; es = 0.0; ebar = 0; elots = 0.0
    lspread = 0.0; pspread = 0.0; sent_abort = False
    last_close_bar = -9999; last_close_h = 0.5; entry_hour = 0
    trades = []

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
                hold_mins = bar - ebar
                gross = (spread - es) * pos * elots * notional
                cost = calc_cost(pair_spec, elots, entry_hour, avg_pa, avg_pb)
                swap = calc_swap_cost(pair_spec, elots, pos, hold_mins / 60)
                pnl = gross - cost - swap; bal += pnl; peak = max(peak, bal)
                w = pnl > 0; dakad.record(w)
                if not w: consec += 1
                else: consec = 0
                if consec >= MAX_CONSEC_LOSSES: gcool = bar + COOLDOWN_BARS; consec = 0
                trades.append({'pnl': pnl, 'gross': gross, 'cost': cost, 'swap': swap,
                               'hold_bars': bar - ebar, 'bar': bar})
                pos = 0; last_close_bar = bar; last_close_h = h
            continue
        if sent_abort and not abort: sent_abort = False
        if sent_abort: continue

        hblocked = False
        if pspread != 0.0:
            hmm.update(spread - pspread); hblocked = hmm.is_blocked

        if bar < MIN_WARMUP_BARS: continue

        # ── ENTRY ──
        if pos == 0 and s != 0:
            if hblocked: continue
            bt_mins = bt_hour * 60 + bt_min
            if bt_mins < ROLLOVER_LOCKOUT_MIN or (1440 - bt_mins) < ROLLOVER_LOCKOUT_MIN: continue
            if not (SESSION_START_HOUR <= bt_hour < SESSION_END_HOUR): continue

            if last_close_bar >= 0:
                dwell_adj = max(1, dwell_bars * (last_close_h / 0.3))
                if (bar - last_close_bar) < dwell_adj: continue

            risk = dakad.calc(cdd, ddd)
            corr.compute_risk(); cm = corr.last_risk_multiplier
            lots = max(0.01, round(bal * risk * cm / 1000.0, 2))

            if amp_hurdle > 0:
                spread_sigma = eng.last_std if hasattr(eng, 'last_std') else 0
                if spread_sigma > 0:
                    z_captured = max(0.0, abs(z) - exz)
                    expected_profit = z_captured * spread_sigma * lots * notional
                    trade_cost = calc_cost(pair_spec, lots, bt_hour, avg_pa, avg_pb)
                    ratio = expected_profit / trade_cost if trade_cost > 0 else 999
                    if ratio < amp_hurdle: continue
                    if amp_max_mult > 1.0 and ratio > amp_hurdle:
                        excess = (ratio - amp_hurdle) / amp_hurdle
                        mult = min(amp_max_mult, 1.0 + 0.5 * excess)
                        lots = max(0.01, round(lots * mult, 2))

            pos = s; ez = z; es = spread; ebar = bar; elots = lots; entry_hour = bt_hour

        # ── EXIT ──
        elif pos != 0:
            ex = False
            spread_sigma = eng.last_std if hasattr(eng, 'last_std') else 0
            if spread_sigma > 0:
                unrealized_z = (spread - es) * pos / spread_sigma
                if unrealized_z < -HUBER_SIGMA: ex = True
            if not ex and abs(z) > abs(ez) * 2.5: ex = True
            if not ex and bt_hour >= SESSION_END_HOUR - 1 and bt_min >= 45: ex = True

            if not ex:
                hb = bar - ebar
                dwell_adj = max(1, dwell_bars * (h / 0.3))
                if hb < dwell_adj: continue
                if pos == 1 and z > -exz: ex = True
                elif pos == -1 and z < exz: ex = True

            if ex:
                hold_mins = bar - ebar
                gross = (spread - es) * pos * elots * notional
                cost = calc_cost(pair_spec, elots, entry_hour, avg_pa, avg_pb)
                swap = calc_swap_cost(pair_spec, elots, pos, hold_mins / 60)
                pnl = gross - cost - swap; bal += pnl; peak = max(peak, bal)
                w = pnl > 0; dakad.record(w)
                if not w: consec += 1
                else: consec = 0
                if consec >= MAX_CONSEC_LOSSES: gcool = bar + COOLDOWN_BARS; consec = 0
                trades.append({'pnl': pnl, 'gross': gross, 'cost': cost, 'swap': swap,
                               'hold_bars': bar - ebar, 'bar': bar})
                pos = 0; last_close_bar = bar; last_close_h = h

    total = len(trades)
    if total < 1: return None

    pnls = [t['pnl'] for t in trades]
    gross_pnls = [t['gross'] for t in trades]
    wins = [p for p in pnls if p > 0]; losses = [p for p in pnls if p <= 0]
    gp = sum(wins) if wins else 0; gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins) / total * 100; pf = gp / gl if gl > 0 else 0
    gross_wr = len([g for g in gross_pnls if g > 0]) / total * 100

    first = df['time'].iloc[0]; last_t = df['time'].iloc[-1]
    days = max(1, (last_t - first).days); months = max(0.1, days / 30.0)

    eq = BAL; eq_peak = eq; mdd = 0
    for t in trades:
        eq += t['pnl']; eq_peak = max(eq_peak, eq); mdd = max(mdd, eq_peak - eq)

    return {
        'trades': total, 'net_wr': round(wr, 1), 'gross_wr': round(gross_wr, 1),
        'pf': round(pf, 2), 'net_pnl': round(bal - BAL, 2),
        'gross_pnl': round(sum(gross_pnls), 2),
        'total_costs': round(sum(t['cost'] for t in trades), 2),
        'total_swap': round(sum(t['swap'] for t in trades), 2),
        'avg_gross': round(sum(gross_pnls) / total, 2),
        'avg_cost': round(sum(t['cost'] for t in trades) / total, 2),
        'avg_net': round((bal - BAL) / total, 2),
        'avg_hold': round(np.mean([t['hold_bars'] for t in trades]), 1),
        'ghost': ghost, 'max_dd_pct': round(mdd / BAL * 100, 2),
        'trades_per_month': round(total / months, 1),
        'monthly_pct': round((bal - BAL) / BAL / months * 100, 2),
        'days': days, 'months': round(months, 1),
    }


def format_result(r, label=""):
    """Pretty-print a result dict."""
    if r is None:
        return f"  {label}: NO TRADES"
    ghost = " [GHOST STOP]" if r['ghost'] else ""
    return (f"  {label}{ghost}\n"
            f"    Trades: {r['trades']} ({r['trades_per_month']:.0f}/mo over {r['months']:.1f} months)\n"
            f"    Net WR: {r['net_wr']}% | Gross WR: {r['gross_wr']}%\n"
            f"    PF: {r['pf']} | Net P&L: ${r['net_pnl']:,.2f} | Monthly: {r['monthly_pct']:.2f}%\n"
            f"    $/trade: ${r['avg_net']:.2f} | Avg gross: ${r['avg_gross']:.2f} | Avg cost: ${r['avg_cost']:.2f}\n"
            f"    MaxDD: {r['max_dd_pct']}% | Avg hold: {r['avg_hold']:.0f} bars\n"
            f"    Total swap: ${r['total_swap']:.2f}")


def main():
    t_start = time.time()
    print("=" * 140)
    print("  WALK-FORWARD VALIDATION — Train 70% / Test 30%")
    print("  Proving the strategy works on UNSEEN data")
    print("=" * 140)
    print()
    print(f"  Train/Test split: {TRAIN_PCT*100:.0f}% / {(1-TRAIN_PCT)*100:.0f}%")
    print(f"  Locked: Hurdle={AMP_HURDLE}, Mult={AMP_MAX_MULT}")
    print(f"  Z grid: {Z_GRID}")
    print(f"  ExitZ grid: {EXIT_Z_GRID}")
    print(f"  Dwell grid: {DWELL_GRID}")
    total_per_pair = len(Z_GRID) * len(EXIT_Z_GRID) * len(DWELL_GRID)
    print(f"  Configs per pair: {total_per_pair}")
    print()

    all_results = {}

    for pair_name, pair_spec in PAIRS.items():
        print(f"\n{'=' * 140}")
        print(f"  PAIR: {pair_name} ({pair_spec['sym_a']}/{pair_spec['sym_b']})")
        print(f"  Locked HMM hold: {pair_spec['hmm_hold']}")
        print(f"{'=' * 140}")

        # Load data
        df = load_pair(pair_spec['sym_a'], pair_spec['sym_b'])
        if df is None or len(df) < 2000:
            print(f"  ERROR: Insufficient data for {pair_name}")
            continue

        avg_a = df['close_a'].mean(); avg_b = df['close_b'].mean()
        notional = calc_notional(pair_spec, avg_a, avg_b)

        # ── SPLIT 70/30 ──
        split_idx = int(len(df) * TRAIN_PCT)
        df_train = df.iloc[:split_idx].reset_index(drop=True)
        df_test = df.iloc[split_idx:].reset_index(drop=True)

        train_start = df_train['time'].iloc[0]
        train_end = df_train['time'].iloc[-1]
        test_start = df_test['time'].iloc[0]
        test_end = df_test['time'].iloc[-1]
        train_days = (train_end - train_start).days
        test_days = (test_end - test_start).days

        print(f"\n  Total M1 bars: {len(df):,}")
        print(f"  Avg {pair_spec['sym_a']}: ${avg_a:.2f} | Avg {pair_spec['sym_b']}: ${avg_b:.2f}")
        print(f"  Notional: ${notional:,.0f}")
        print(f"\n  TRAIN: {len(df_train):,} bars | {train_start} to {train_end} ({train_days}d)")
        print(f"  TEST:  {len(df_test):,} bars | {test_start} to {test_end} ({test_days}d)")

        hmm_hold = pair_spec['hmm_hold']

        # ══════════════════════════════════════════════════════
        # PHASE 1: OPTIMIZE ON TRAIN SET
        # ══════════════════════════════════════════════════════
        print(f"\n  {'─' * 60}")
        print(f"  PHASE 1: Optimizing on TRAIN ({TRAIN_PCT*100:.0f}%)")
        print(f"  {'─' * 60}")

        best_train = None; best_score = -999999
        best_params = None
        config_count = 0
        pair_t = time.time()

        for z_ent in Z_GRID:
            for exit_z in EXIT_Z_GRID:
                for dwell_b in DWELL_GRID:
                    config_count += 1
                    if config_count % 50 == 0:
                        el = time.time() - pair_t
                        print(f"    {config_count}/{total_per_pair} ({el:.0f}s)...", flush=True)

                    r = run_backtest(df_train, pair_spec, notional, avg_a, avg_b,
                                     z_entry=z_ent, exit_z_base=exit_z,
                                     hmm_hold=hmm_hold, dwell_bars=dwell_b,
                                     amp_hurdle=AMP_HURDLE, amp_max_mult=AMP_MAX_MULT)

                    if r is None: continue
                    if r['trades'] < 3: continue

                    # Score: PF-weighted net P&L, penalize ghost stops
                    score = r['net_pnl'] * min(1.0, r['trades'] / 10.0)
                    if r['ghost']: score *= 0.1

                    if score > best_score and not r['ghost']:
                        best_score = score
                        best_train = r
                        best_params = {'z': z_ent, 'exit_z': exit_z, 'dwell': dwell_b}

        el = time.time() - pair_t
        print(f"    Completed {config_count} configs in {el:.0f}s")

        if best_params is None:
            print(f"\n  NO profitable config found on TRAIN for {pair_name}")
            all_results[pair_name] = {'status': 'NO_TRAIN_PROFIT'}
            continue

        print(f"\n  BEST TRAIN CONFIG: Z={best_params['z']} ExitZ={best_params['exit_z']} "
              f"Dwell={best_params['dwell']}")
        print(format_result(best_train, "TRAIN RESULT"))

        # ══════════════════════════════════════════════════════
        # PHASE 2: VALIDATE ON TEST SET (OUT-OF-SAMPLE)
        # ══════════════════════════════════════════════════════
        print(f"\n  {'─' * 60}")
        print(f"  PHASE 2: Validating on TEST ({(1-TRAIN_PCT)*100:.0f}%) — UNSEEN DATA")
        print(f"  Using EXACT config from train: Z={best_params['z']} ExitZ={best_params['exit_z']} "
              f"Dwell={best_params['dwell']}")
        print(f"  {'─' * 60}")

        test_result = run_backtest(df_test, pair_spec, notional, avg_a, avg_b,
                                    z_entry=best_params['z'],
                                    exit_z_base=best_params['exit_z'],
                                    hmm_hold=hmm_hold,
                                    dwell_bars=best_params['dwell'],
                                    amp_hurdle=AMP_HURDLE, amp_max_mult=AMP_MAX_MULT)

        print(format_result(test_result, "TEST RESULT (OUT-OF-SAMPLE)"))

        # ══════════════════════════════════════════════════════
        # PHASE 3: COMPARISON — THE VERDICT
        # ══════════════════════════════════════════════════════
        print(f"\n  {'─' * 60}")
        print(f"  TRAIN vs TEST COMPARISON")
        print(f"  {'─' * 60}")

        if test_result is not None and best_train is not None:
            print(f"\n  {'Metric':<20} {'TRAIN':>12} {'TEST':>12} {'Delta':>12} {'Verdict':>12}")
            print(f"  {'─'*68}")

            metrics = [
                ('Net WR', f"{best_train['net_wr']}%", f"{test_result['net_wr']}%",
                 test_result['net_wr'] - best_train['net_wr']),
                ('Gross WR', f"{best_train['gross_wr']}%", f"{test_result['gross_wr']}%",
                 test_result['gross_wr'] - best_train['gross_wr']),
                ('PF', f"{best_train['pf']}", f"{test_result['pf']}",
                 test_result['pf'] - best_train['pf']),
                ('$/trade', f"${best_train['avg_net']:.2f}", f"${test_result['avg_net']:.2f}",
                 test_result['avg_net'] - best_train['avg_net']),
                ('Monthly %', f"{best_train['monthly_pct']:.2f}%", f"{test_result['monthly_pct']:.2f}%",
                 test_result['monthly_pct'] - best_train['monthly_pct']),
                ('MaxDD', f"{best_train['max_dd_pct']:.2f}%", f"{test_result['max_dd_pct']:.2f}%",
                 test_result['max_dd_pct'] - best_train['max_dd_pct']),
                ('Trades/mo', f"{best_train['trades_per_month']:.0f}", f"{test_result['trades_per_month']:.0f}",
                 test_result['trades_per_month'] - best_train['trades_per_month']),
            ]

            robust = True
            for name, train_val, test_val, delta in metrics:
                if name in ('MaxDD',):
                    verdict = "OK" if delta <= 2.0 else "WORSE"
                    if delta > 5.0: robust = False
                elif name == 'PF':
                    verdict = "OK" if delta > -2.0 else "WORSE"
                    if test_result['pf'] < 0.8: robust = False
                elif name in ('Net WR', 'Gross WR'):
                    verdict = "OK" if delta > -15 else "WORSE"
                    if test_result['net_wr'] < 40: robust = False
                else:
                    verdict = "OK" if delta > -50 else "CAUTION"
                    if name == '$/trade' and test_result['avg_net'] < 0: robust = False

                color = "✅" if verdict == "OK" else "⚠️" if verdict == "CAUTION" else "❌"
                print(f"  {name:<20} {train_val:>12} {test_val:>12} {delta:>+12.2f} {color} {verdict}")

            # Final verdict
            print(f"\n  {'═' * 68}")
            if test_result['net_pnl'] > 0 and robust:
                print(f"  ✅ VERDICT: {pair_name} PASSES WALK-FORWARD VALIDATION")
                print(f"     Strategy is ROBUST — profits on unseen data!")
                # At $5K projection
                scale = 0.05
                monthly_5k = test_result['avg_net'] * scale * test_result['trades_per_month']
                print(f"     At $5K (test period): ~${monthly_5k:.0f}/month")
            elif test_result['net_pnl'] > 0:
                print(f"  ⚠️  VERDICT: {pair_name} MARGINALLY PASSES — some degradation detected")
                print(f"     Strategy works but with reduced edge on unseen data")
            else:
                print(f"  ❌ VERDICT: {pair_name} FAILS WALK-FORWARD VALIDATION")
                print(f"     Strategy was OVERFIT to training data — loses on unseen data")
        else:
            print(f"  Cannot compare — test produced no trades")
            robust = False

        all_results[pair_name] = {
            'best_params': best_params,
            'hmm_hold': hmm_hold,
            'train': best_train,
            'test': test_result if test_result else None,
            'robust': robust if test_result else False,
        }

    # ══════════════════════════════════════════════════════
    # FINAL PORTFOLIO SUMMARY
    # ══════════════════════════════════════════════════════
    print(f"\n\n{'=' * 140}")
    print("  FINAL WALK-FORWARD PORTFOLIO SUMMARY")
    print(f"{'=' * 140}")

    for pname, pdata in all_results.items():
        if 'best_params' not in pdata:
            print(f"\n  {pname}: FAILED — no profitable config on train")
            continue
        p = pdata['best_params']
        status = "✅ PASS" if pdata['robust'] else "❌ FAIL"
        print(f"\n  {pname} [{status}]: Z={p['z']} ExZ={p['exit_z']} Dwell={p['dwell']} HMM={pdata['hmm_hold']}")
        if pdata['train']:
            print(f"    TRAIN: PF={pdata['train']['pf']} WR={pdata['train']['net_wr']}% "
                  f"${pdata['train']['avg_net']:.2f}/trade DD={pdata['train']['max_dd_pct']}%")
        if pdata['test']:
            print(f"    TEST:  PF={pdata['test']['pf']} WR={pdata['test']['net_wr']}% "
                  f"${pdata['test']['avg_net']:.2f}/trade DD={pdata['test']['max_dd_pct']}%")

    elapsed = time.time() - t_start
    print(f"\n\n  Completed in {elapsed:.0f}s ({elapsed / 60:.1f} min)")

    # Save
    save_path = Path(__file__).resolve().parent.parent / "Results" / "walk_forward_validation.json"
    save_data = {}
    for pname, pdata in all_results.items():
        save_data[pname] = {
            'params': pdata.get('best_params'),
            'hmm_hold': pdata.get('hmm_hold'),
            'robust': pdata.get('robust', False),
            'train': pdata.get('train'),
            'test': pdata.get('test'),
        }
    with open(save_path, 'w') as f:
        json.dump(save_data, f, indent=2, default=str)
    print(f"  Results saved to {save_path}")


if __name__ == "__main__":
    main()
