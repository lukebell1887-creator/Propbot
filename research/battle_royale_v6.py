#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║         BATTLE ROYALE v6 — Alpha Discovery: 3-Way Contender Test           ║
║                                                                            ║
║  Control : v5.6.3 Current Live Logic (Sniper — high accuracy, low freq)    ║
║  Newton  : Kinetic Scale-In (50% @ 0.8×Zcrit, 50% @ 1.0×Zcrit)           ║
║  Tesla   : OU Resonance Dwell (half-life physics replaces Hurst proxy)     ║
║                                                                            ║
║  Data    : Real M1 bars — Oil (XTIUSD/XBRUSD) + Index (US100/DE40)        ║
║  Costs   : Oil: contract=100, comm=0.03%, spread≈$0.03                     ║
║            Index: contract=1, comm=$0, spread≈$1.0                         ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import sys, math, time, json
import numpy as np
import pandas as pd
from pathlib import Path
from collections import deque
from dataclasses import dataclass, field
from typing import List, Tuple, Optional

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import shf_core

# ============================================================================
# UNIVERSAL CONSTANTS (v5.6.3 production)
# ============================================================================
WELFORD_SPAN     = 100
Z_BASE           = 2.0
GAMMA            = 6.0
HURST_WINDOW     = 512
EXIT_Z_BASE      = 0.5
EXIT_GAMMA       = 2.0
KALMAN_TOL       = 0.15
CORR_WINDOW      = 200
HMM_LOOKBACK     = 100
HMM_N_REGIMES    = 3
MIN_WARMUP_BARS  = 200
STARTING_BALANCE = 100_000.0
GHOST_DAILY_DD   = 0.04
GHOST_MAX_DD     = 0.09
MAX_CONSEC_LOSS  = 5
COOLDOWN_BARS    = 60
ROLLOVER_LOCK    = 30  # minutes

# Dynamic AKAD
DAKAD_LAMBDA       = 40.0
DAKAD_P_RUIN       = 1e-4
DAKAD_DD_CEIL      = 0.04
DAKAD_WIN          = 50
DAKAD_MIN_WR       = 0.50
DAKAD_MAX_WR       = 0.85
DAKAD_MIN_BASE     = 0.003
DAKAD_MAX_BASE     = 0.03
DAKAD_FLOOR        = 0.0005

# ============================================================================
# COST MODELS (verified broker: FivePercentOnline-Real)
# ============================================================================
# Oil: contract=100, commission=0.03%
OIL_SPREAD_A    = 4.0   # WTI ~$0.04 spread × 100 contract = $4/fill
OIL_SPREAD_B    = 5.0   # Brent ~$0.05 spread × 100 = $5/fill
OIL_COMM_PCT    = 0.0003  # 0.03% commission

# Index: contract=1, commission=$0
IDX_SPREAD_A    = 1.0   # NAS100 ~1pt spread × $1/pt = $1/fill
IDX_SPREAD_B    = 1.0   # DAX40 ~1pt spread × $1/pt = $1/fill
IDX_COMM        = 0.0   # Zero commission

def session_mult(hour):
    """Session spread multiplier (wider in Asia, tight in NY)."""
    if 0 <= hour < 7:    return 1.8   # Asia
    elif 7 <= hour < 9:  return 1.2   # London open
    elif 9 <= hour < 17: return 1.0   # London+NY overlap
    elif 17 <= hour < 21: return 1.1  # NY afternoon
    else:                 return 1.5   # Evening

def oil_cost(lots, hour, price_a=63.0, price_b=68.0):
    """Real oil round-trip cost: spread (4 fills) + commission (0.03%)."""
    m = session_mult(hour)
    spread_cost = (OIL_SPREAD_A * 2 + OIL_SPREAD_B * 2) * lots * m
    # Commission: 0.03% × notional × 2 sides × 2 legs
    notional_a = price_a * 100 * lots  # contract=100
    notional_b = price_b * 100 * lots
    comm = (notional_a + notional_b) * OIL_COMM_PCT * 2  # buy+sell for each
    return spread_cost + comm

def idx_cost(lots, hour):
    """Real index round-trip cost: spread only, zero commission."""
    m = session_mult(hour)
    return (IDX_SPREAD_A * 2 + IDX_SPREAD_B * 2) * lots * m

# ============================================================================
# COMPONENTS
# ============================================================================
class DynamicAKAD:
    def __init__(self):
        self._results = deque(maxlen=DAKAD_WIN)
        for _ in range(10): self._results.append(1)
        for _ in range(5):  self._results.append(0)

    def record(self, win: bool):
        self._results.append(1 if win else 0)

    def calc(self, total_dd: float, daily_dd: float) -> float:
        ddr = max(0.001, DAKAD_DD_CEIL - daily_dd)
        wr = max(DAKAD_MIN_WR, min(DAKAD_MAX_WR,
                 sum(self._results) / max(len(self._results), 1)))
        ns = math.log(DAKAD_P_RUIN) / math.log(1.0 - wr)
        base = max(DAKAD_MIN_BASE, min(DAKAD_MAX_BASE,
                   (math.exp(DAKAD_LAMBDA * ddr) - 1) / (DAKAD_LAMBDA * ns)))
        return max(DAKAD_FLOOR, base * math.exp(-DAKAD_LAMBDA * total_dd))


class HMMFilter:
    def __init__(self, lookback=100, min_hold=100):
        self._lb = lookback
        self._regime = 0
        self._buf = []
        self._hold = 0
        self._min_hold = min_hold

    def update(self, spread_return: float) -> int:
        self._buf.append(spread_return)
        if len(self._buf) > self._lb * 3:
            self._buf = self._buf[-self._lb * 2:]
        if len(self._buf) < 50:
            return 0
        recent = np.array(self._buf[-self._lb:])
        n = len(recent)
        ws = min(20, n // 3)
        if ws < 5:
            return 0
        vols = [np.std(recent[i:i+ws]) for i in range(0, n - ws + 1, ws)]
        if len(vols) < 3:
            return 0
        v40 = np.percentile(vols, 40)
        v80 = np.percentile(vols, 80)
        nr = 0 if vols[-1] <= v40 else (1 if vols[-1] <= v80 else 2)
        self._hold += 1
        if nr != self._regime and self._hold >= self._min_hold:
            self._regime = nr
            self._hold = 0
        return self._regime

    @property
    def blocked(self):
        return self._regime >= 2


def is_rollover(t) -> bool:
    m = t.hour * 60 + t.minute
    return m < ROLLOVER_LOCK or (1440 - m) < ROLLOVER_LOCK


def fit_ou_halflife(spread_buffer: np.ndarray) -> float:
    """
    Fit OU process to spread series via AR(1) regression.
    dX = theta*(mu - X)*dt + sigma*dW
    Discrete: X(t+1) - X(t) = a + b*X(t) + eps
    theta = -b (per bar), half_life = -ln(2)/ln(1+b) bars
    Returns half-life in bars (M1 = minutes). Clamped [5, 500] bars.
    """
    if len(spread_buffer) < 30:
        return 60.0  # default
    x = spread_buffer[:-1]
    dx = np.diff(spread_buffer)
    if np.std(x) < 1e-12:
        return 60.0
    # OLS: dx = a + b*x
    X = np.column_stack([np.ones(len(x)), x])
    try:
        beta = np.linalg.lstsq(X, dx, rcond=None)[0]
        b = beta[1]
        if b >= 0:
            return 500.0  # not mean-reverting, return ceiling
        hl = -math.log(2) / math.log(1 + b)
        return max(5.0, min(500.0, hl))
    except:
        return 60.0


# ============================================================================
# DATA LOADING
# ============================================================================
def load_pair(sym_a: str, sym_b: str) -> pd.DataFrame:
    d = Path(__file__).resolve().parent.parent / "data" / "historical"
    a = pd.read_csv(d / f"{sym_a}_M1.csv", parse_dates=['time'])
    b = pd.read_csv(d / f"{sym_b}_M1.csv", parse_dates=['time'])
    m = pd.merge(
        a[['time', 'close']].rename(columns={'close': 'close_a'}),
        b[['time', 'close']].rename(columns={'close': 'close_b'}),
        on='time', how='inner'
    ).sort_values('time').reset_index(drop=True)
    return m[(m['close_a'] > 0) & (m['close_b'] > 0)].reset_index(drop=True)


# ============================================================================
# THE UNIFIED BACKTEST ENGINE
# ============================================================================
def run_backtest(
    df: pd.DataFrame,
    mode: str,          # 'control', 'newton', 'tesla'
    pair_type: str,     # 'oil', 'index'
    hmm_hold: int = 5,
    dwell_base: float = 1800.0,
    dwell_anchor: float = 0.3,
    dwell_min: float = 900.0,
    dwell_max: float = 9000.0,
    notional: float = 100_000.0,
) -> dict:
    """
    Unified backtest engine for all 3 contenders.

    Control: Standard v5.6.3 logic (Hurst-dwell, single entry at Zcrit)
    Newton:  Scale-in (Bullet1 @ 0.80×Zcrit, Bullet2 @ 1.00×Zcrit)
    Tesla:   OU half-life dwell (replaces Hurst-based dwell formula)
    """
    cost_fn = oil_cost if pair_type == 'oil' else idx_cost

    balance = STARTING_BALANCE
    peak = STARTING_BALANCE
    daily_start = STARTING_BALANCE
    daily_date = None
    ghost = False

    dakad = DynamicAKAD()
    corr = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)
    hmm = HMMFilter(lookback=HMM_LOOKBACK, min_hold=hmm_hold)

    eng = shf_core.CointegrationEngine(
        span=WELFORD_SPAN, beta=1.0, entry_z=Z_BASE, exit_z=EXIT_Z_BASE,
        z_base=Z_BASE, gamma=GAMMA, hurst_window=HURST_WINDOW,
        dynamic_z=True, exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA,
        dynamic_exit=True,
    )
    sen = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOL)

    # State
    consec = 0
    gcool = 0
    trades = []
    total_costs = 0.0
    n = len(df)

    # Position state (supports scale-in for Newton)
    pos_dir = 0        # +1 long spread, -1 short spread, 0 flat
    pos_lots = 0.0     # total lots
    pos_entry_spread = 0.0
    pos_entry_bar = 0
    pos_entry_hour = 0
    pos_bullets = 0    # Newton: 0, 1, or 2 bullets fired
    pos_entry_z = 0.0

    last_close_bar = -9999
    last_spread = 0.0
    spread_buffer = []  # for Tesla OU fitting

    # Track dwell blocks for stats
    dwell_blocks = 0
    hmm_blocks = 0

    for bar in range(n):
        if ghost:
            break

        row = df.iloc[bar]
        bt = row['time']
        pa = float(row['close_a'])
        pb = float(row['close_b'])

        # Daily reset
        cd = bt.date() if hasattr(bt, 'date') else None
        if cd and cd != daily_date:
            daily_date = cd
            daily_start = balance

        # DD calc
        cdd = max(0, (peak - balance) / peak) if peak > 0 else 0
        ddd = max(0, (daily_start - balance) / daily_start) if daily_start > 0 else 0

        if ddd >= GHOST_DAILY_DD or cdd >= GHOST_MAX_DD:
            ghost = True
            break
        if bar < gcool:
            continue

        # ── SIGNAL COMPUTATION ──
        prev_spread = last_spread
        sig = eng.update(pa, pb)
        z = sig.z_score
        s = sig.signal
        spread = sig.spread
        last_spread = spread
        h = eng.last_hurst
        exz = eng.last_exit_z
        z_crit = eng.last_z_crit if hasattr(eng, 'last_z_crit') else max(2.0, Z_BASE * (1.0 + GAMMA * max(0, h - 0.5)))

        # Spread buffer for Tesla OU
        spread_buffer.append(spread)
        if len(spread_buffer) > 768:
            spread_buffer = spread_buffer[-768:]

        # Correlation
        if prev_spread != 0.0:
            corr.push_return(0, spread - prev_spread)

        # Kalman
        la = math.log(pa) if pa > 0 else 0
        lb = math.log(pb) if pb > 0 else 0
        _, abort = sen.update(la, lb)

        # Sentinel abort → emergency close
        if abort and pos_dir != 0:
            gross = (spread - pos_entry_spread) * pos_dir * pos_lots * notional
            if pair_type == 'oil':
                cost = oil_cost(pos_lots, pos_entry_hour, pa, pb)
            else:
                cost = idx_cost(pos_lots, pos_entry_hour)
            pnl = gross - cost
            total_costs += cost
            balance += pnl
            peak = max(peak, balance)
            w = pnl > 0
            dakad.record(w)
            if not w: consec += 1
            else: consec = 0
            if consec >= MAX_CONSEC_LOSS:
                gcool = bar + COOLDOWN_BARS
                consec = 0
            trades.append({'pnl': pnl, 'gross': gross, 'cost': cost,
                           'hold': bar - pos_entry_bar, 'hour': pos_entry_hour,
                           'reason': 'SENTINEL'})
            pos_dir = 0
            pos_lots = 0.0
            pos_bullets = 0
            last_close_bar = bar
            continue
        if abort:
            continue

        # HMM
        if prev_spread != 0.0:
            hmm.update(spread - prev_spread)

        if bar < MIN_WARMUP_BARS:
            continue

        # ── DWELL CALCULATION ──
        if mode == 'tesla':
            # OU half-life based dwell
            if len(spread_buffer) >= 60:
                hl_bars = fit_ou_halflife(np.array(spread_buffer[-200:]))
            else:
                hl_bars = 60.0
            dwell_bars = max(5, int(0.5 * hl_bars))  # hold for half a cycle
            dwell_bars = min(dwell_bars, 250)  # cap at ~4 hours
        else:
            # Control & Newton: Hurst-based dwell (v5.6.3 production)
            dwell_secs = dwell_base * (h / dwell_anchor)
            dwell_secs = max(dwell_min, min(dwell_max, dwell_secs))
            dwell_bars = max(1, int(dwell_secs / 60.0))

        # ── EXIT LOGIC ──
        if pos_dir != 0:
            hb = bar - pos_entry_bar

            # Emergency exit always bypasses dwell
            emergency = abs(z) > abs(pos_entry_z) * 2.5

            if not emergency and hb < dwell_bars:
                continue  # dwell not expired

            ex = False
            reason = ""
            if emergency:
                ex = True
                reason = "EMERGENCY"
            elif pos_dir == 1 and z > -exz:
                ex = True
                reason = "REVERT"
            elif pos_dir == -1 and z < exz:
                ex = True
                reason = "REVERT"

            if ex:
                gross = (spread - pos_entry_spread) * pos_dir * pos_lots * notional
                if pair_type == 'oil':
                    cost = oil_cost(pos_lots, pos_entry_hour, pa, pb)
                else:
                    cost = idx_cost(pos_lots, pos_entry_hour)
                pnl = gross - cost
                total_costs += cost
                balance += pnl
                peak = max(peak, balance)
                w = pnl > 0
                dakad.record(w)
                if not w: consec += 1
                else: consec = 0
                if consec >= MAX_CONSEC_LOSS:
                    gcool = bar + COOLDOWN_BARS
                    consec = 0
                trades.append({'pnl': pnl, 'gross': gross, 'cost': cost,
                               'hold': hb, 'hour': pos_entry_hour,
                               'reason': reason})
                pos_dir = 0
                pos_lots = 0.0
                pos_bullets = 0
                last_close_bar = bar
            continue

        # ── ENTRY LOGIC ──
        if pos_dir != 0:
            continue  # already positioned (shouldn't reach here)

        # Gate checks
        if hmm.blocked:
            hmm_blocks += 1
            continue
        if is_rollover(bt):
            continue

        # Re-entry cooldown
        if last_close_bar >= 0 and (bar - last_close_bar) < dwell_bars:
            dwell_blocks += 1
            continue

        # ── MODE-SPECIFIC ENTRY ──
        if mode == 'control' or mode == 'tesla':
            # Standard: enter 100% at Z_crit
            if abs(z) >= z_crit and s != 0:
                risk = dakad.calc(cdd, ddd)
                corr.compute_risk()
                cm = corr.last_risk_multiplier
                lots = max(0.01, round(balance * risk * cm / 1000.0, 2))
                pos_dir = s
                pos_lots = lots
                pos_entry_spread = spread
                pos_entry_bar = bar
                pos_entry_hour = bt.hour
                pos_entry_z = z
                pos_bullets = 1

        elif mode == 'newton':
            # Scale-in: Bullet 1 @ 0.80 × Zcrit, Bullet 2 @ 1.00 × Zcrit
            z_bullet1 = 0.80 * z_crit
            z_bullet2 = 1.00 * z_crit

            if abs(z) >= z_bullet1 and s != 0:
                # How much risk for this bullet?
                risk = dakad.calc(cdd, ddd)
                corr.compute_risk()
                cm = corr.last_risk_multiplier
                full_lots = max(0.01, round(balance * risk * cm / 1000.0, 2))

                if abs(z) >= z_bullet2:
                    # Both bullets at once (Z already past full threshold)
                    pos_dir = s
                    pos_lots = full_lots  # Full risk
                    pos_entry_spread = spread
                    pos_entry_bar = bar
                    pos_entry_hour = bt.hour
                    pos_entry_z = z
                    pos_bullets = 2
                else:
                    # Bullet 1 only (50% risk)
                    pos_dir = s
                    pos_lots = max(0.01, round(full_lots * 0.5, 2))
                    pos_entry_spread = spread
                    pos_entry_bar = bar
                    pos_entry_hour = bt.hour
                    pos_entry_z = z
                    pos_bullets = 1

    # ── RESULTS ──
    total = len(trades)
    if total == 0:
        return {
            'mode': mode, 'pair': pair_type, 'trades': 0, 'wr': 0, 'pf': 0,
            'net': 0, 'gross': 0, 'costs': 0, 'ret': 0, 'mdd': 0,
            'avg_hold': 0, 'ghost': ghost, 'hmm_blocks': hmm_blocks,
            'dwell_blocks': dwell_blocks, 'avg_cost': 0,
        }

    pnls = [t['pnl'] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gp = sum(wins) if wins else 0
    gl = abs(sum(losses)) if losses else 0.001
    wr = len(wins) / total * 100
    pf = gp / gl

    eq = STARTING_BALANCE
    eq_peak = eq
    mdd = 0
    for t in trades:
        eq += t['pnl']
        eq_peak = max(eq_peak, eq)
        mdd = max(mdd, eq_peak - eq)

    holds = [t['hold'] for t in trades]
    gross_total = sum(t['gross'] for t in trades)

    return {
        'mode': mode,
        'pair': pair_type,
        'trades': total,
        'wr': round(wr, 1),
        'pf': round(pf, 2),
        'net': round(balance - STARTING_BALANCE, 2),
        'gross': round(gross_total, 2),
        'costs': round(total_costs, 2),
        'ret': round((balance - STARTING_BALANCE) / STARTING_BALANCE * 100, 2),
        'mdd': round(mdd / STARTING_BALANCE * 100, 2),
        'avg_hold': round(np.mean(holds), 1),
        'med_hold': round(np.median(holds), 1),
        'ghost': ghost,
        'hmm_blocks': hmm_blocks,
        'dwell_blocks': dwell_blocks,
        'avg_win': round(np.mean(wins), 2) if wins else 0,
        'avg_loss': round(np.mean(losses), 2) if losses else 0,
        'avg_cost': round(total_costs / total, 2),
        'trades_mo': round(total / max(1, (df['time'].iloc[-1] - df['time'].iloc[0]).days / 30.44), 1),
    }


# ============================================================================
# MAIN — THE BATTLE ROYALE
# ============================================================================
def main():
    t0 = time.time()

    print("=" * 100)
    print("  BATTLE ROYALE v6 — Three Contenders, Real Data, Real Costs")
    print("  Control : v5.6.3 Sniper (current live logic)")
    print("  Newton  : Kinetic Scale-In (50% @ 0.80*Zcrit + 50% @ 1.00*Zcrit)")
    print("  Tesla   : OU Resonance Dwell (half-life physics, not Hurst proxy)")
    print("=" * 100)

    # ── LOAD DATA ──
    print("\n  Loading data...")
    oil = load_pair("XTIUSD", "XBRUSD")
    idx = load_pair("US100", "DE40")
    oil_days = (oil['time'].iloc[-1] - oil['time'].iloc[0]).days
    idx_days = (idx['time'].iloc[-1] - idx['time'].iloc[0]).days
    print(f"  Oil:   {len(oil):>8,} M1 bars | {oil['time'].iloc[0].date()} to {oil['time'].iloc[-1].date()} ({oil_days}d)")
    print(f"  Index: {len(idx):>8,} M1 bars | {idx['time'].iloc[0].date()} to {idx['time'].iloc[-1].date()} ({idx_days}d)")

    # ── COST MODEL ──
    print(f"\n  Cost Model (verified FivePercentOnline-Real):")
    print(f"    Oil:   WTI ${OIL_SPREAD_A}/fill + Brent ${OIL_SPREAD_B}/fill + 0.03% comm")
    print(f"           @ 0.30 lots NY session: ~${oil_cost(0.30, 14, 63, 68):.2f}/trade")
    print(f"    Index: NAS ${IDX_SPREAD_A}/fill + DAX ${IDX_SPREAD_B}/fill + $0 comm")
    print(f"           @ 0.30 lots NY session: ~${idx_cost(0.30, 14):.2f}/trade")

    # ── RUN ALL CONTENDERS ──
    results = {}
    configs = [
        # (label, mode, pair_type, df, hmm_hold, dwell_base, dwell_anchor, dwell_min, dwell_max)
        ("OIL_CONTROL", "control", "oil", oil, 5,  1800.0, 0.3, 900.0,  9000.0),
        ("OIL_NEWTON",  "newton",  "oil", oil, 5,  1800.0, 0.3, 900.0,  9000.0),
        ("OIL_TESLA",   "tesla",   "oil", oil, 5,  1800.0, 0.3, 900.0,  9000.0),
        ("IDX_CONTROL", "control", "index", idx, 20, 60.0,  0.3, 30.0,   300.0),
        ("IDX_NEWTON",  "newton",  "index", idx, 20, 60.0,  0.3, 30.0,   300.0),
        ("IDX_TESLA",   "tesla",   "index", idx, 20, 60.0,  0.3, 30.0,   300.0),
    ]

    for label, mode, ptype, df, hmm_h, db, da, dmin, dmax in configs:
        print(f"\n  Running {label}...", end="", flush=True)
        t1 = time.time()
        r = run_backtest(df, mode=mode, pair_type=ptype, hmm_hold=hmm_h,
                         dwell_base=db, dwell_anchor=da, dwell_min=dmin, dwell_max=dmax)
        dt = time.time() - t1
        results[label] = r
        print(f" {dt:.1f}s | {r['trades']} trades, ${r['net']:+,.0f}")

    # ══════════════════════════════════════════════════════════════════════
    #  RESULTS TABLE — OIL
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 100)
    print("  OIL SPREAD (XTIUSD / XBRUSD) — BATTLE RESULTS")
    print("=" * 100)

    oil_keys = ["OIL_CONTROL", "OIL_NEWTON", "OIL_TESLA"]
    oil_labels = ["Control (v5.6.3)", "Newton (Scale-In)", "Tesla (OU Dwell)"]

    print(f"\n  {'Contender':<22} {'Trades':>7} {'Tr/Mo':>6} {'WR':>7} {'PF':>7} "
          f"{'Gross':>12} {'Costs':>10} {'Net P&L':>12} {'Return':>8} {'MaxDD':>7} {'AvgHold':>8} {'$/Trade':>9}")
    print(f"  {'-'*118}")

    best_oil = max(oil_keys, key=lambda k: results[k]['net'])
    for key, label in zip(oil_keys, oil_labels):
        r = results[key]
        marker = " <-- WINNER" if key == best_oil else ""
        print(f"  {label:<22} {r['trades']:>7} {r['trades_mo']:>6.0f} {r['wr']:>6.1f}% {r['pf']:>7.2f} "
              f"${r['gross']:>11,.0f} ${r['costs']:>9,.0f} ${r['net']:>11,.0f} "
              f"{r['ret']:>7.2f}% {r['mdd']:>6.2f}% {r['avg_hold']:>7.1f}b{marker}")

    # ══════════════════════════════════════════════════════════════════════
    #  RESULTS TABLE — INDEX
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 100)
    print("  INDEX SPREAD (US100 / DE40) — BATTLE RESULTS")
    print("=" * 100)

    idx_keys = ["IDX_CONTROL", "IDX_NEWTON", "IDX_TESLA"]
    idx_labels = ["Control (v5.6.3)", "Newton (Scale-In)", "Tesla (OU Dwell)"]

    print(f"\n  {'Contender':<22} {'Trades':>7} {'Tr/Mo':>6} {'WR':>7} {'PF':>7} "
          f"{'Gross':>12} {'Costs':>10} {'Net P&L':>12} {'Return':>8} {'MaxDD':>7} {'AvgHold':>8} {'$/Trade':>9}")
    print(f"  {'-'*118}")

    best_idx = max(idx_keys, key=lambda k: results[k]['net'])
    for key, label in zip(idx_keys, idx_labels):
        r = results[key]
        marker = " <-- WINNER" if key == best_idx else ""
        print(f"  {label:<22} {r['trades']:>7} {r['trades_mo']:>6.0f} {r['wr']:>6.1f}% {r['pf']:>7.2f} "
              f"${r['gross']:>11,.0f} ${r['costs']:>9,.0f} ${r['net']:>11,.0f} "
              f"{r['ret']:>7.2f}% {r['mdd']:>6.2f}% {r['avg_hold']:>7.1f}b{marker}")

    # ══════════════════════════════════════════════════════════════════════
    #  COMBINED PORTFOLIO
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 100)
    print("  COMBINED PORTFOLIO (Oil + Index)")
    print("=" * 100)

    combos = [
        ("Control + Control", "OIL_CONTROL", "IDX_CONTROL"),
        ("Newton + Newton",   "OIL_NEWTON",  "IDX_NEWTON"),
        ("Tesla + Tesla",     "OIL_TESLA",   "IDX_TESLA"),
        ("Best Oil + Best Idx", best_oil, best_idx),
    ]

    print(f"\n  {'Portfolio':<24} {'Oil Net':>12} {'Idx Net':>12} {'Combined':>12} {'Oil Tr':>7} {'Idx Tr':>7} {'Total':>7}")
    print(f"  {'-'*85}")

    for label, ok, ik in combos:
        ro = results[ok]
        ri = results[ik]
        comb = ro['net'] + ri['net']
        marker = ""
        print(f"  {label:<24} ${ro['net']:>11,.0f} ${ri['net']:>11,.0f} ${comb:>11,.0f} "
              f"{ro['trades']:>7} {ri['trades']:>7} {ro['trades']+ri['trades']:>7}")

    # ══════════════════════════════════════════════════════════════════════
    #  DEEP DIAGNOSTICS
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 100)
    print("  DEEP DIAGNOSTICS")
    print("=" * 100)

    for key in oil_keys + idx_keys:
        r = results[key]
        print(f"\n  {key}:")
        print(f"    Avg Win: ${r['avg_win']:>8,.2f}  |  Avg Loss: ${r['avg_loss']:>8,.2f}  |  Avg Cost: ${r['avg_cost']:>8,.2f}")
        print(f"    HMM Blocks: {r['hmm_blocks']:>6,}  |  Dwell Blocks: {r['dwell_blocks']:>6,}  |  Ghost: {r['ghost']}")
        print(f"    Med Hold: {r.get('med_hold', 0):.0f}b  |  Avg Hold: {r['avg_hold']:.0f}b")

    # ══════════════════════════════════════════════════════════════════════
    #  VERDICT
    # ══════════════════════════════════════════════════════════════════════
    print("\n\n" + "=" * 100)
    print("  VERDICT — WHO WINS?")
    print("=" * 100)

    # Determine winners
    oil_winner = results[best_oil]
    idx_winner = results[best_idx]

    for pair_name, keys, labels, best_key in [
        ("OIL", oil_keys, oil_labels, best_oil),
        ("INDEX", idx_keys, idx_labels, best_idx),
    ]:
        winner_mode = results[best_key]['mode']
        ctrl_key = keys[0]
        ctrl_net = results[ctrl_key]['net']
        best_net = results[best_key]['net']
        delta = best_net - ctrl_net

        if winner_mode == 'control':
            verdict = "CONTROL WINS — You are already at the local maximum."
            emoji = "crown"
        elif winner_mode == 'newton':
            verdict = f"NEWTON WINS — Scale-in found +${delta:,.0f} more ({delta/max(1,abs(ctrl_net))*100:+.0f}%)"
            emoji = "apple"
        elif winner_mode == 'tesla':
            verdict = f"TESLA WINS — OU physics dwell found +${delta:,.0f} more ({delta/max(1,abs(ctrl_net))*100:+.0f}%)"
            emoji = "lightning"

        print(f"\n  {pair_name}: {verdict}")
        print(f"    Control: ${ctrl_net:>+11,.0f}  |  Newton: ${results[keys[1]]['net']:>+11,.0f}  |  Tesla: ${results[keys[2]]['net']:>+11,.0f}")

    # Overall
    ctrl_total = results['OIL_CONTROL']['net'] + results['IDX_CONTROL']['net']
    newt_total = results['OIL_NEWTON']['net'] + results['IDX_NEWTON']['net']
    tesl_total = results['OIL_TESLA']['net'] + results['IDX_TESLA']['net']
    best_total = max(ctrl_total, newt_total, tesl_total)

    print(f"\n  OVERALL PORTFOLIO:")
    print(f"    Control: ${ctrl_total:>+12,.0f}")
    print(f"    Newton:  ${newt_total:>+12,.0f}  ({(newt_total/max(1,ctrl_total)-1)*100:+.1f}% vs control)")
    print(f"    Tesla:   ${tesl_total:>+12,.0f}  ({(tesl_total/max(1,ctrl_total)-1)*100:+.1f}% vs control)")

    if best_total == ctrl_total:
        print(f"\n  >>> YOUR v5.6.3 IS THE LOCAL MAXIMUM. No fancy math beats your simple Hurst rule.")
    elif best_total == newt_total:
        print(f"\n  >>> NEWTON KINETIC WINS. Your bot was too shy — scaling in captures missed alpha.")
    else:
        print(f"\n  >>> TESLA RESONANCE WINS. The market has a frequency your Hurst dwell couldn't see.")

    elapsed = time.time() - t0
    print(f"\n  Completed in {elapsed:.1f}s")

    # Save
    out = Path(__file__).resolve().parent.parent / "Results" / "battle_royale_v6.json"
    out.parent.mkdir(exist_ok=True)
    save = {k: {kk: vv for kk, vv in v.items()} for k, v in results.items()}
    with open(out, 'w') as f:
        json.dump(save, f, indent=2)
    print(f"  Saved to {out}")


if __name__ == "__main__":
    main()
