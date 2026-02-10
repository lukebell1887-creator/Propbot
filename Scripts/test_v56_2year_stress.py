#!/usr/bin/env python3
"""
SHF v5.6 — Comprehensive 2-Year Multi-Regime Stress Test
==========================================================

Faithfully replicates the EXACT v5.6 trading loop from engine.py:
  • Rust CointegrationEngine (Dynamic Z Entry + Dynamic Z Exit, Hurst-adaptive)
  • Rust KalmanSentinel (β drift kill-switch, tolerance=0.15)
  • Rust AKADRiskCalculator (λ=40, base=0.75%, expectancy gate, ATR factor)
  • Rust CorrelationRiskMonitor (window=200, 4-tier risk mult)
  • Ghost Stop (4% daily DD / 9% max DD)
  • Position sizing: lots = max(0.01, round(balance * risk / 1000, 2))
  • Emergency exit: |Z| > 2.5 × |entry_Z|

Account: $100,000 starting balance
Holy Trio: US100/DE40 | AUDUSD/NZDUSD | EURUSD/GBPUSD

12 Scenarios — each ~500K M1 bars (~2 calendar years):
  1.  Normal Conditions (baseline)
  2.  Raging Bull Market
  3.  Severe Bear Market
  4.  Mixed Choppy (up/down swings)
  5.  Flash Crash + Recovery (3 crashes)
  6.  Correlation Breakdown (6 months of correlated spreads)
  7.  Low Volatility Grind
  8.  High Volatility Storm
  9.  Regime Switching (quarterly alternation)
  10. Pandemic Shock (crash → V-recovery → overshoot)
  11. Stagflation Grind (slow bleed, rising vol)
  12. Combined Worst-Case (sequential worst regimes)
"""

import numpy as np
import json
import time
import sys
import io
import math
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Tuple, Optional

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import shf_core

# ============================================================================
# v5.6 PARAMETERS (exact match to engine.py / architecture doc)
# ============================================================================

WELFORD_SPAN     = 100
Z_BASE           = 2.0
GAMMA            = 6.0
HURST_WINDOW     = 512
EXIT_Z_BASE      = 0.5
EXIT_GAMMA       = 2.0
AKAD_BASE_RISK   = 0.0075    # 0.75%
AKAD_DD_LAMBDA   = 40.0
GHOST_DAILY_DD   = 0.04      # 4%
GHOST_MAX_DD     = 0.09      # 9%
KALMAN_TOLERANCE = 0.15
CORR_WINDOW      = 200
STARTING_BALANCE = 100_000.0

# Bars per scenario (~2 calendar years of M1 = ~500K trading bars)
BARS_PER_SCENARIO = 500_000
DT = 1.0 / 1440.0  # 1 minute in days

# ============================================================================
# PAIR DEFINITIONS
# ============================================================================

@dataclass
class PairDef:
    name: str
    sym_a: str
    sym_b: str
    base_price_a: float
    base_price_b: float
    pair_index: int
    # Dollar P&L per lot per log-spread unit (calibrated for realistic account P&L)
    notional: float = 100_000.0


HOLY_TRIO = [
    PairDef("US100/DE40",       "US100",  "DE40",   18000.0, 18200.0, 0, notional=150_000.0),
    PairDef("AUDUSD/NZDUSD",    "AUDUSD", "NZDUSD",  0.6500,  0.6100, 1, notional=100_000.0),
    PairDef("EURUSD/GBPUSD",    "EURUSD", "GBPUSD",  1.0800,  1.2700, 2, notional=100_000.0),
]


# ============================================================================
# SYNTHETIC PRICE GENERATOR — cointegrated pairs with regime control
# ============================================================================

def generate_cointegrated_prices(
    n: int,
    base_a: float,
    base_b: float,
    drift: float = 0.0,            # Market direction (per bar)
    sigma_common: float = 0.0003,  # Common factor volatility
    theta_ou: float = 0.5,         # OU mean-reversion speed
    sigma_ou: float = 0.0008,      # Spread (OU) volatility
    mu_ou: float = 0.0,            # OU long-run mean
    seed_offset: int = 0,
) -> Tuple[np.ndarray, np.ndarray]:
    """Generate cointegrated price paths for a pair (vectorized)."""
    rng = np.random.RandomState(42 + seed_offset)

    # Common factor — vectorized random walk
    increments = drift + sigma_common * rng.randn(n)
    increments[0] = 0.0
    common = np.cumsum(increments)

    # OU process — pre-generate all noise, then loop with scalars
    dt_val = 1.0 / 60.0
    sqrt_dt = np.sqrt(dt_val)
    noise = rng.randn(n)
    ou = np.empty(n)
    ou[0] = mu_ou
    # Exact OU discretization coefficients
    decay = theta_ou * dt_val
    vol = sigma_ou * sqrt_dt
    for i in range(1, n):
        ou[i] = ou[i-1] + decay * (mu_ou - ou[i-1]) + vol * noise[i]

    # Construct log prices so spread = ln(A) - ln(B) ≈ OU
    log_a = np.log(base_a) + common + 0.5 * ou
    log_b = np.log(base_b) + common - 0.5 * ou

    return np.exp(log_a), np.exp(log_b)


def generate_regime_prices(
    n: int,
    base_a: float,
    base_b: float,
    regime_schedule: List[Tuple[int, dict]],
    seed_offset: int = 0,
) -> Tuple[np.ndarray, np.ndarray]:
    """Generate prices with time-varying regimes.
    regime_schedule: list of (n_bars, params_dict) tuples.
    """
    all_a, all_b = [], []
    current_a, current_b = base_a, base_b
    rng = np.random.RandomState(42 + seed_offset)

    for seg_n, params in regime_schedule:
        drift = params.get('drift', 0.0)
        sigma_c = params.get('sigma_common', 0.0003)
        theta = params.get('theta_ou', 0.5)
        sigma_s = params.get('sigma_ou', 0.0008)

        dt_val = 1.0 / 60.0
        sqrt_dt = np.sqrt(dt_val)

        # Vectorized common factor
        c_noise = rng.randn(seg_n)
        c_noise[0] = 0.0
        common = np.cumsum(drift * np.ones(seg_n) + sigma_c * c_noise)
        common[0] = 0.0

        # OU with pre-generated noise
        o_noise = rng.randn(seg_n)
        ou = np.empty(seg_n)
        ou[0] = 0.0
        decay = theta * dt_val
        vol = sigma_s * sqrt_dt
        for i in range(1, seg_n):
            ou[i] = ou[i-1] + decay * (0.0 - ou[i-1]) + vol * o_noise[i]

        log_a = np.log(current_a) + common + 0.5 * ou
        log_b = np.log(current_b) + common - 0.5 * ou

        seg_a = np.exp(log_a)
        seg_b = np.exp(log_b)

        current_a = seg_a[-1]
        current_b = seg_b[-1]

        all_a.append(seg_a)
        all_b.append(seg_b)

    return np.concatenate(all_a), np.concatenate(all_b)


# ============================================================================
# SCENARIO DEFINITIONS
# ============================================================================

def make_scenarios() -> Dict[str, dict]:
    """Define all 12 scenarios with per-pair generation parameters."""
    N = BARS_PER_SCENARIO

    scenarios = {}

    # 1. NORMAL CONDITIONS — baseline mean-reverting, moderate vol
    scenarios["1. Normal Conditions"] = {
        'description': "Typical market: moderate vol, strong mean-reversion, no shocks. The baseline.",
        'type': 'simple',
        'params': {'drift': 0.0000005, 'sigma_common': 0.0003, 'theta_ou': 0.5, 'sigma_ou': 0.0008},
    }

    # 2. RAGING BULL MARKET — steady uptrend, low-moderate vol
    scenarios["2. Raging Bull Market"] = {
        'description': "2-year bull run: steady uptrend, compressed vol, strong MR spreads.",
        'type': 'simple',
        'params': {'drift': 0.000003, 'sigma_common': 0.00025, 'theta_ou': 0.6, 'sigma_ou': 0.0007},
    }

    # 3. SEVERE BEAR MARKET — persistent downtrend, elevated vol, weaker MR
    scenarios["3. Severe Bear Market"] = {
        'description': "2-year bear: sharp downtrend, elevated vol, weakened mean-reversion.",
        'type': 'simple',
        'params': {'drift': -0.000004, 'sigma_common': 0.0005, 'theta_ou': 0.25, 'sigma_ou': 0.0012},
    }

    # 4. MIXED CHOPPY — alternating up/down every ~3 weeks
    n_swing = N // 20  # ~20 swings over 2 years
    schedule = []
    for i in range(20):
        d = 0.000004 if i % 2 == 0 else -0.000004
        schedule.append((n_swing, {'drift': d, 'sigma_common': 0.0004, 'theta_ou': 0.35, 'sigma_ou': 0.001}))
    scenarios["4. Mixed Choppy"] = {
        'description': "Alternating 3-week up/down swings, high uncertainty, whipsaw risk.",
        'type': 'regime', 'schedule': schedule,
    }

    # 5. FLASH CRASH + RECOVERY — normal with 3 embedded crashes
    scenarios["5. Flash Crash Recovery"] = {
        'description': "Normal market with 3 flash crashes (bars 100K, 250K, 400K) + V-recoveries.",
        'type': 'flash_crash',
        'base_params': {'drift': 0.0000005, 'sigma_common': 0.0003, 'theta_ou': 0.5, 'sigma_ou': 0.0008},
        'crashes': [
            {'bar': 100_000, 'magnitude': 0.03, 'recovery_bars': 2000},
            {'bar': 250_000, 'magnitude': 0.05, 'recovery_bars': 3000},
            {'bar': 400_000, 'magnitude': 0.04, 'recovery_bars': 1500},
        ],
    }

    # 6. CORRELATION BREAKDOWN — spreads become correlated for 6 months
    pre = N // 4
    breakdown = N // 2
    post = N - pre - breakdown
    scenarios["6. Correlation Breakdown"] = {
        'description': "Normal → 6 months all spreads suddenly correlate → recovery. Ukraine-style.",
        'type': 'regime',
        'schedule': [
            (pre, {'drift': 0.000001, 'sigma_common': 0.0003, 'theta_ou': 0.5, 'sigma_ou': 0.0008}),
            (breakdown, {'drift': 0.000002, 'sigma_common': 0.0006, 'theta_ou': 0.1, 'sigma_ou': 0.0015}),
            (post, {'drift': 0.0, 'sigma_common': 0.0003, 'theta_ou': 0.5, 'sigma_ou': 0.0008}),
        ],
        'correlated_segment': (pre, pre + breakdown),  # bars where spreads are correlated
    }

    # 7. LOW VOLATILITY GRIND — very calm, tight spreads
    scenarios["7. Low Volatility Grind"] = {
        'description': "2 years of ultra-low vol: tight spreads, strong MR, few signals.",
        'type': 'simple',
        'params': {'drift': 0.0000002, 'sigma_common': 0.00015, 'theta_ou': 0.8, 'sigma_ou': 0.0004},
    }

    # 8. HIGH VOLATILITY STORM — persistent 2-3x normal vol
    scenarios["8. High Volatility Storm"] = {
        'description': "2 years of elevated vol (2-3x normal), frequent whipsaws.",
        'type': 'simple',
        'params': {'drift': 0.0, 'sigma_common': 0.0008, 'theta_ou': 0.3, 'sigma_ou': 0.002},
    }

    # 9. REGIME SWITCHING — quarterly bull/bear/choppy/calm alternation
    q = N // 8  # 8 quarters
    scenarios["9. Regime Switching"] = {
        'description': "Quarterly alternation: bull → bear → choppy → calm → repeat.",
        'type': 'regime',
        'schedule': [
            (q, {'drift':  0.000003, 'sigma_common': 0.00025, 'theta_ou': 0.5, 'sigma_ou': 0.0008}),  # Bull
            (q, {'drift': -0.000004, 'sigma_common': 0.0005,  'theta_ou': 0.2, 'sigma_ou': 0.0012}),  # Bear
            (q, {'drift':  0.0,      'sigma_common': 0.0004,  'theta_ou': 0.3, 'sigma_ou': 0.001}),   # Choppy
            (q, {'drift':  0.0000005,'sigma_common': 0.00015, 'theta_ou': 0.7, 'sigma_ou': 0.0005}),  # Calm
            (q, {'drift':  0.000003, 'sigma_common': 0.00025, 'theta_ou': 0.5, 'sigma_ou': 0.0008}),  # Bull
            (q, {'drift': -0.000005, 'sigma_common': 0.0006,  'theta_ou': 0.15,'sigma_ou': 0.0015}),  # Deep Bear
            (q, {'drift':  0.000002, 'sigma_common': 0.0003,  'theta_ou': 0.4, 'sigma_ou': 0.0009}),  # Recovery
            (q, {'drift':  0.0,      'sigma_common': 0.0002,  'theta_ou': 0.6, 'sigma_ou': 0.0006}),  # Normal
        ],
    }

    # 10. PANDEMIC SHOCK — normal → crash → V-recovery → overshoot
    scenarios["10. Pandemic Shock"] = {
        'description': "12 months normal → 2-month crash → 6-month V-recovery → 4-month overshoot.",
        'type': 'regime',
        'schedule': [
            (N * 12 // 24, {'drift': 0.000001, 'sigma_common': 0.0003, 'theta_ou': 0.5, 'sigma_ou': 0.0008}),
            (N * 2  // 24, {'drift': -0.00002, 'sigma_common': 0.001,  'theta_ou': 0.1, 'sigma_ou': 0.003}),
            (N * 6  // 24, {'drift': 0.000008, 'sigma_common': 0.0005, 'theta_ou': 0.3, 'sigma_ou': 0.0012}),
            (N * 4  // 24, {'drift': 0.000005, 'sigma_common': 0.0004, 'theta_ou': 0.4, 'sigma_ou': 0.001}),
        ],
    }

    # 11. STAGFLATION GRIND — slow bleed, rising vol, weakening MR
    scenarios["11. Stagflation Grind"] = {
        'description': "Slow persistent decline, gradually rising vol, MR weakens over time.",
        'type': 'regime',
        'schedule': [
            (N // 4, {'drift': -0.000001, 'sigma_common': 0.0003, 'theta_ou': 0.45, 'sigma_ou': 0.0009}),
            (N // 4, {'drift': -0.000002, 'sigma_common': 0.0004, 'theta_ou': 0.35, 'sigma_ou': 0.0011}),
            (N // 4, {'drift': -0.000003, 'sigma_common': 0.0005, 'theta_ou': 0.25, 'sigma_ou': 0.0013}),
            (N // 4, {'drift': -0.000004, 'sigma_common': 0.0006, 'theta_ou': 0.15, 'sigma_ou': 0.0016}),
        ],
    }

    # 12. COMBINED WORST-CASE — sequential worst regimes
    seg = N // 6
    scenarios["12. Combined Worst-Case"] = {
        'description': "Sequential: crash → bear → whipsaw → correlation breakdown → stagflation → recovery.",
        'type': 'regime',
        'schedule': [
            (seg, {'drift': -0.00002, 'sigma_common': 0.001,  'theta_ou': 0.1,  'sigma_ou': 0.003}),   # Crash
            (seg, {'drift': -0.000005,'sigma_common': 0.0006, 'theta_ou': 0.15, 'sigma_ou': 0.0015}),  # Bear
            (seg, {'drift': 0.0,      'sigma_common': 0.0008, 'theta_ou': 0.2,  'sigma_ou': 0.002}),   # Whipsaw
            (seg, {'drift': 0.000002, 'sigma_common': 0.0006, 'theta_ou': 0.08, 'sigma_ou': 0.002}),   # Corr breakdown
            (seg, {'drift': -0.000003,'sigma_common': 0.0005, 'theta_ou': 0.2,  'sigma_ou': 0.0014}),  # Stagflation
            (seg, {'drift': 0.000004, 'sigma_common': 0.0004, 'theta_ou': 0.4,  'sigma_ou': 0.001}),   # Recovery
        ],
    }

    return scenarios


# ============================================================================
# PRICE GENERATION
# ============================================================================

def generate_prices_for_scenario(scenario: dict, pair: PairDef, pair_seed: int) -> Tuple[np.ndarray, np.ndarray]:
    N = BARS_PER_SCENARIO
    stype = scenario['type']

    if stype == 'simple':
        return generate_cointegrated_prices(
            N, pair.base_price_a, pair.base_price_b,
            seed_offset=pair_seed, **scenario['params']
        )

    elif stype == 'regime':
        return generate_regime_prices(
            N, pair.base_price_a, pair.base_price_b,
            regime_schedule=scenario['schedule'],
            seed_offset=pair_seed,
        )

    elif stype == 'flash_crash':
        prices_a, prices_b = generate_cointegrated_prices(
            N, pair.base_price_a, pair.base_price_b,
            seed_offset=pair_seed, **scenario['base_params']
        )
        # Inject crashes into the spread (affect price_a)
        for crash in scenario['crashes']:
            bar = crash['bar']
            mag = crash['magnitude']
            rec = crash['recovery_bars']
            if bar < N:
                prices_a[bar] *= np.exp(mag)
                for j in range(1, min(rec, N - bar)):
                    recovery_pct = j / rec
                    prices_a[bar + j] *= np.exp(mag * (1.0 - recovery_pct) * 0.8)
        return prices_a, prices_b

    return generate_cointegrated_prices(N, pair.base_price_a, pair.base_price_b, seed_offset=pair_seed)


# ============================================================================
# v5.6 FULL SIMULATION ENGINE
# ============================================================================

@dataclass
class TradeRecord:
    pair: str
    bar: int
    direction: int  # +1 long spread, -1 short spread
    entry_z: float
    exit_z_threshold: float
    exit_z_actual: float
    hurst_at_entry: float
    hurst_at_exit: float
    entry_spread: float
    exit_spread: float
    lots: float
    pnl: float
    balance_after: float
    exit_reason: str


@dataclass
class PairSimState:
    engine: object  # CointegrationEngine
    sentinel: object  # KalmanSentinel
    position: int = 0
    entry_z: float = 0.0
    entry_spread: float = 0.0
    entry_bar: int = 0
    entry_hurst: float = 0.5
    entry_lots: float = 0.0
    last_spread: float = 0.0
    prev_spread: float = 0.0
    sentinel_aborted: bool = False


def run_v56_simulation(
    scenario_name: str,
    pair_prices: Dict[str, Tuple[np.ndarray, np.ndarray]],
) -> dict:
    """Run the full v5.6 simulation matching engine.py behavior exactly."""

    balance = STARTING_BALANCE
    peak_balance = STARTING_BALANCE
    max_dd_pct = 0.0
    daily_start_balance = STARTING_BALANCE
    ghost_stopped = False
    ghost_stop_bar = -1

    # Initialize Rust components
    akad = shf_core.AKADRiskCalculator(
        base_risk=AKAD_BASE_RISK,
        dd_lambda=AKAD_DD_LAMBDA,
        fast_window=15,
        slow_window=50,
    )
    corr_monitor = shf_core.CorrelationRiskMonitor(n_pairs=3, window=CORR_WINDOW)

    # Per-pair state
    pair_states: Dict[str, PairSimState] = {}
    for pdef in HOLY_TRIO:
        engine = shf_core.CointegrationEngine(
            span=WELFORD_SPAN, beta=1.0, entry_z=Z_BASE, exit_z=EXIT_Z_BASE,
            z_base=Z_BASE, gamma=GAMMA, hurst_window=HURST_WINDOW,
            dynamic_z=True, exit_z_base=EXIT_Z_BASE, exit_gamma=EXIT_GAMMA, dynamic_exit=True,
        )
        sentinel = shf_core.KalmanSentinel(static_beta=1.0, beta_tolerance=KALMAN_TOLERANCE)
        pair_states[pdef.name] = PairSimState(engine=engine, sentinel=sentinel)

    trades: List[TradeRecord] = []
    equity_curve = []
    n_bars = len(next(iter(pair_prices.values()))[0])

    # Daily reset every 1440 bars
    bars_per_day = 1440
    consecutive_losses = 0
    cooldown_until = 0

    for bar in range(n_bars):
        if ghost_stopped:
            break

        # Daily reset
        if bar % bars_per_day == 0 and bar > 0:
            daily_start_balance = balance

        # Current DD
        current_dd = max(0.0, (peak_balance - balance) / peak_balance) if peak_balance > 0 else 0.0
        daily_dd = max(0.0, (daily_start_balance - balance) / daily_start_balance) if daily_start_balance > 0 else 0.0

        # Ghost stop checks
        if daily_dd >= GHOST_DAILY_DD:
            ghost_stopped = True
            ghost_stop_bar = bar
            # Close all open positions
            for pname, pstate in pair_states.items():
                if pstate.position != 0:
                    pdef = next(p for p in HOLY_TRIO if p.name == pname)
                    pa, pb = pair_prices[pname]
                    spread = pstate.engine.last_z_score  # Current Z
                    spread_val = math.log(pa[bar]) - math.log(pb[bar])
                    pnl = (spread_val - pstate.entry_spread) * pstate.position * pstate.entry_lots * pdef.notional
                    balance += pnl
                    trades.append(TradeRecord(
                        pair=pname, bar=bar, direction=pstate.position,
                        entry_z=pstate.entry_z, exit_z_threshold=0, exit_z_actual=spread,
                        hurst_at_entry=pstate.entry_hurst, hurst_at_exit=0.5,
                        entry_spread=pstate.entry_spread, exit_spread=spread_val,
                        lots=pstate.entry_lots, pnl=pnl, balance_after=balance,
                        exit_reason="GHOST_STOP"
                    ))
                    pstate.position = 0
            break

        if current_dd >= GHOST_MAX_DD:
            ghost_stopped = True
            ghost_stop_bar = bar
            break

        # Cooldown check (5 consecutive losses → 60 min pause)
        if bar < cooldown_until:
            # Record equity but skip trading
            if bar % 1000 == 0:
                equity_curve.append((bar, balance))
            continue

        # Process each pair
        for pdef in HOLY_TRIO:
            pname = pdef.name
            pstate = pair_states[pname]
            pa, pb = pair_prices[pname]

            price_a = float(pa[bar])
            price_b = float(pb[bar])

            # Store previous spread
            pstate.prev_spread = pstate.last_spread

            # Run CointegrationEngine
            signal = pstate.engine.update(price_a, price_b)
            z = signal.z_score
            sig = signal.signal
            spread = signal.spread
            pstate.last_spread = spread

            hurst = pstate.engine.last_hurst
            z_crit = pstate.engine.last_z_crit
            exit_z = pstate.engine.last_exit_z

            # Feed spread returns to correlation monitor
            if pstate.prev_spread != 0.0:
                sr = spread - pstate.prev_spread
                corr_monitor.push_return(pdef.pair_index, sr)

            # Kalman sentinel
            log_a = math.log(price_a) if price_a > 0 else 0.0
            log_b = math.log(price_b) if price_b > 0 else 0.0
            beta, should_abort = pstate.sentinel.update(log_a, log_b)

            if should_abort and not pstate.sentinel_aborted:
                pstate.sentinel_aborted = True
                # Close any open position
                if pstate.position != 0:
                    pnl = (spread - pstate.entry_spread) * pstate.position * pstate.entry_lots * pdef.notional
                    balance += pnl
                    peak_balance = max(peak_balance, balance)
                    is_win = pnl > 0
                    akad.record_trade(0.49 if is_win else -1.0)
                    if not is_win:
                        consecutive_losses += 1
                    else:
                        consecutive_losses = 0
                    if consecutive_losses >= 5:
                        cooldown_until = bar + 60
                        consecutive_losses = 0
                    trades.append(TradeRecord(
                        pair=pname, bar=bar, direction=pstate.position,
                        entry_z=pstate.entry_z, exit_z_threshold=exit_z, exit_z_actual=z,
                        hurst_at_entry=pstate.entry_hurst, hurst_at_exit=hurst,
                        entry_spread=pstate.entry_spread, exit_spread=spread,
                        lots=pstate.entry_lots, pnl=pnl, balance_after=balance,
                        exit_reason="SENTINEL_ABORT"
                    ))
                    pstate.position = 0
                continue

            if pstate.sentinel_aborted and not should_abort:
                pstate.sentinel_aborted = False

            if pstate.sentinel_aborted:
                continue

            # Skip first 200 bars (warmup)
            if bar < 200:
                continue

            # ENTRY
            if pstate.position == 0 and sig != 0:
                # AKAD risk
                risk, dd_f, atr_f, exp_g = akad.calculate_risk(current_dd)

                # Correlation risk multiplier
                _, corr_mult = corr_monitor.compute_risk()
                final_risk = risk * corr_mult

                # Position sizing (exact match to engine.py)
                lots = max(0.01, round(balance * final_risk / 1000.0, 2))

                pstate.position = sig
                pstate.entry_z = z
                pstate.entry_spread = spread
                pstate.entry_bar = bar
                pstate.entry_hurst = hurst
                pstate.entry_lots = lots

            # EXIT
            elif pstate.position != 0:
                should_exit = False
                reason = ""

                # Dynamic exit (exact match to engine.py _maybe_exit)
                if pstate.position == 1 and z > -exit_z:
                    should_exit = True
                    reason = "DYNAMIC_EXIT"
                elif pstate.position == -1 and z < exit_z:
                    should_exit = True
                    reason = "DYNAMIC_EXIT"

                # Emergency exit: Z went 2.5x past entry
                if abs(z) > abs(pstate.entry_z) * 2.5:
                    should_exit = True
                    reason = "EMERGENCY_2.5X"

                if should_exit:
                    pnl = (spread - pstate.entry_spread) * pstate.position * pstate.entry_lots * pdef.notional
                    balance += pnl
                    peak_balance = max(peak_balance, balance)

                    is_win = pnl > 0
                    akad.record_trade(0.49 if is_win else -1.0)

                    if not is_win:
                        consecutive_losses += 1
                    else:
                        consecutive_losses = 0
                    if consecutive_losses >= 5:
                        cooldown_until = bar + 60
                        consecutive_losses = 0

                    trades.append(TradeRecord(
                        pair=pname, bar=bar, direction=pstate.position,
                        entry_z=pstate.entry_z, exit_z_threshold=exit_z, exit_z_actual=z,
                        hurst_at_entry=pstate.entry_hurst, hurst_at_exit=hurst,
                        entry_spread=pstate.entry_spread, exit_spread=spread,
                        lots=pstate.entry_lots, pnl=pnl, balance_after=balance,
                        exit_reason=reason
                    ))
                    pstate.position = 0

        # Equity tracking (every 1000 bars to keep output manageable)
        if bar % 1000 == 0:
            equity_curve.append((bar, balance))

    # Final equity point
    equity_curve.append((n_bars - 1, balance))

    # Compute metrics
    return compute_results(scenario_name, trades, equity_curve, balance, ghost_stopped, ghost_stop_bar)


def compute_results(scenario_name, trades, equity_curve, final_balance, ghost_stopped, ghost_stop_bar):
    total_trades = len(trades)
    if total_trades == 0:
        return {
            'scenario': scenario_name, 'total_trades': 0,
            'final_balance': final_balance, 'net_pnl': final_balance - STARTING_BALANCE,
            'return_pct': 0.0, 'win_rate': 0.0, 'profit_factor': 0.0,
            'max_drawdown_pct': 0.0, 'max_drawdown_usd': 0.0,
            'ghost_stopped': ghost_stopped, 'ghost_stop_bar': ghost_stop_bar,
            'avg_trade_pnl': 0.0, 'avg_win': 0.0, 'avg_loss': 0.0,
            'sharpe_approx': 0.0, 'per_pair': {}, 'monthly_returns': [],
        }

    pnls = [t.pnl for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]
    gross_profit = sum(winners) if winners else 0.0
    gross_loss = abs(sum(losers)) if losers else 0.001

    # Equity curve metrics
    balances = [e[1] for e in equity_curve]
    peak = STARTING_BALANCE
    max_dd_usd = 0.0
    for b in balances:
        peak = max(peak, b)
        dd = peak - b
        max_dd_usd = max(max_dd_usd, dd)
    max_dd_pct = max_dd_usd / STARTING_BALANCE * 100

    # Monthly returns (every ~30K bars ≈ 1 month)
    monthly = []
    month_bars = 30_000
    for i in range(0, len(equity_curve) - 1, max(1, len(equity_curve) // 24)):
        if i + 1 < len(equity_curve):
            ret = (equity_curve[min(i + len(equity_curve) // 24, len(equity_curve)-1)][1] -
                   equity_curve[i][1]) / max(equity_curve[i][1], 1.0) * 100
            monthly.append(ret)

    # Per-pair breakdown
    per_pair = {}
    for pdef in HOLY_TRIO:
        pt = [t for t in trades if t.pair == pdef.name]
        if not pt:
            per_pair[pdef.name] = {'trades': 0, 'win_rate': 0, 'pf': 0, 'pnl': 0, 'avg_hurst': 0}
            continue
        ppnl = [t.pnl for t in pt]
        pw = [p for p in ppnl if p > 0]
        pl = [p for p in ppnl if p <= 0]
        gp = sum(pw) if pw else 0.0
        gl = abs(sum(pl)) if pl else 0.001
        hursts = [t.hurst_at_entry for t in pt]
        sentinel_exits = sum(1 for t in pt if t.exit_reason == 'SENTINEL_ABORT')
        emergency_exits = sum(1 for t in pt if t.exit_reason == 'EMERGENCY_2.5X')
        per_pair[pdef.name] = {
            'trades': len(pt),
            'win_rate': len(pw) / len(pt) * 100 if pt else 0,
            'pf': gp / gl,
            'pnl': sum(ppnl),
            'avg_hurst': np.mean(hursts) if hursts else 0.5,
            'sentinel_exits': sentinel_exits,
            'emergency_exits': emergency_exits,
        }

    # Sharpe approximation (annualized from trade P&L)
    pnl_arr = np.array(pnls)
    sharpe = (np.mean(pnl_arr) / np.std(pnl_arr) * np.sqrt(252)) if np.std(pnl_arr) > 0 else 0.0

    return {
        'scenario': scenario_name,
        'total_trades': total_trades,
        'final_balance': round(final_balance, 2),
        'net_pnl': round(final_balance - STARTING_BALANCE, 2),
        'return_pct': round((final_balance - STARTING_BALANCE) / STARTING_BALANCE * 100, 2),
        'win_rate': round(len(winners) / total_trades * 100, 1) if total_trades > 0 else 0,
        'profit_factor': round(gross_profit / gross_loss, 2),
        'max_drawdown_pct': round(max_dd_pct, 2),
        'max_drawdown_usd': round(max_dd_usd, 2),
        'ghost_stopped': ghost_stopped,
        'ghost_stop_bar': ghost_stop_bar,
        'avg_trade_pnl': round(np.mean(pnls), 2) if pnls else 0,
        'avg_win': round(np.mean(winners), 2) if winners else 0,
        'avg_loss': round(np.mean(losers), 2) if losers else 0,
        'sharpe_approx': round(sharpe, 2),
        'per_pair': per_pair,
        'monthly_returns': [round(m, 2) for m in monthly],
    }


# ============================================================================
# MAIN
# ============================================================================

def main():
    print("=" * 90)
    print("SHF v5.6 — COMPREHENSIVE 2-YEAR MULTI-REGIME STRESS TEST")
    print(f"shf_core version: {shf_core.__version__}")
    print("=" * 90)
    print(f"\nStarting Balance: ${STARTING_BALANCE:,.0f}")
    print(f"AKAD: base={AKAD_BASE_RISK*100:.2f}%, lambda={AKAD_DD_LAMBDA}")
    print(f"Dynamic Z Entry: Z_crit = {Z_BASE}*(1+{GAMMA}*max(0,H-0.5))")
    print(f"Dynamic Z Exit:  Z_exit = {EXIT_Z_BASE}*(1+{EXIT_GAMMA}*(H-0.5)) in [0.1, 1.0]")
    print(f"Ghost Stop: {GHOST_DAILY_DD*100}% daily / {GHOST_MAX_DD*100}% max DD")
    print(f"Kalman Sentinel: beta tolerance = {KALMAN_TOLERANCE}")
    print(f"Bars per scenario: {BARS_PER_SCENARIO:,} (~2 years M1)")
    print(f"Holy Trio: {', '.join(p.name for p in HOLY_TRIO)}")

    scenarios = make_scenarios()
    all_results = {}
    total_start = time.time()

    for idx, (sname, sconfig) in enumerate(scenarios.items()):
        print(f"\n\n{'='*90}")
        print(f"SCENARIO {idx+1}/12: {sname}")
        print(f"{'='*90}")
        print(f"  {sconfig['description']}")

        t0 = time.time()

        # Generate prices for all 3 pairs
        pair_prices = {}
        for pidx, pdef in enumerate(HOLY_TRIO):
            pa, pb = generate_prices_for_scenario(sconfig, pdef, pair_seed=pidx * 1000 + idx * 100)
            pair_prices[pdef.name] = (pa, pb)
            print(f"  Generated {pdef.name}: A=[{pa[0]:.2f} to {pa[-1]:.2f}], B=[{pb[0]:.2f} to {pb[-1]:.2f}]")

        gen_time = time.time() - t0
        print(f"  Price generation: {gen_time:.1f}s")

        # Run simulation
        t1 = time.time()
        result = run_v56_simulation(sname, pair_prices)
        sim_time = time.time() - t1
        print(f"  Simulation: {sim_time:.1f}s")

        # Print results
        print(f"\n  {'─'*70}")
        print(f"  RESULTS: {sname}")
        print(f"  {'─'*70}")
        gs = " *** GHOST STOPPED ***" if result['ghost_stopped'] else ""
        print(f"  Final Balance:   ${result['final_balance']:>12,.2f}{gs}")
        print(f"  Net P&L:         ${result['net_pnl']:>12,.2f} ({result['return_pct']:+.2f}%)")
        print(f"  Total Trades:    {result['total_trades']:>12}")
        print(f"  Win Rate:        {result['win_rate']:>11.1f}%")
        print(f"  Profit Factor:   {result['profit_factor']:>12.2f}")
        print(f"  Max Drawdown:    ${result['max_drawdown_usd']:>12,.2f} ({result['max_drawdown_pct']:.2f}%)")
        print(f"  Avg Trade P&L:   ${result['avg_trade_pnl']:>12,.2f}")
        print(f"  Avg Win:         ${result['avg_win']:>12,.2f}")
        print(f"  Avg Loss:        ${result['avg_loss']:>12,.2f}")
        print(f"  Sharpe (approx): {result['sharpe_approx']:>12.2f}")

        print(f"\n  Per-Pair Breakdown:")
        print(f"  {'Pair':<22} {'Trades':>7} {'WR':>7} {'PF':>7} {'P&L':>12} {'Hurst':>7} {'Sentinel':>8} {'Emerg':>6}")
        print(f"  {'─'*80}")
        for pname, pdata in result['per_pair'].items():
            print(f"  {pname:<22} {pdata['trades']:>7} {pdata['win_rate']:>6.1f}% {pdata['pf']:>7.2f} "
                  f"${pdata['pnl']:>11,.2f} {pdata['avg_hurst']:>7.3f} {pdata.get('sentinel_exits',0):>8} "
                  f"{pdata.get('emergency_exits',0):>6}")

        all_results[sname] = result

    total_time = time.time() - total_start

    # ========== FINAL SUMMARY ==========
    print(f"\n\n{'='*90}")
    print(f"FINAL SUMMARY — ALL 12 SCENARIOS")
    print(f"{'='*90}")
    print(f"\nTotal computation time: {total_time:.1f}s")

    print(f"\n  {'Scenario':<30} {'P&L':>12} {'Return':>9} {'WR':>7} {'PF':>7} {'MaxDD%':>8} {'Trades':>7} {'Ghost':>6}")
    print(f"  {'─'*96}")

    profitable = 0
    survived = 0
    for sname, r in all_results.items():
        gs = "YES" if r['ghost_stopped'] else "No"
        print(f"  {sname:<30} ${r['net_pnl']:>11,.2f} {r['return_pct']:>+8.2f}% {r['win_rate']:>6.1f}% "
              f"{r['profit_factor']:>7.2f} {r['max_drawdown_pct']:>7.2f}% {r['total_trades']:>7} {gs:>6}")
        if r['net_pnl'] > 0:
            profitable += 1
        if not r['ghost_stopped']:
            survived += 1

    print(f"\n  Profitable: {profitable}/12 scenarios")
    print(f"  Survived (no ghost stop): {survived}/12 scenarios")
    print(f"  Avg Return: {np.mean([r['return_pct'] for r in all_results.values()]):.2f}%")
    print(f"  Avg Win Rate: {np.mean([r['win_rate'] for r in all_results.values()]):.1f}%")
    print(f"  Avg Profit Factor: {np.mean([r['profit_factor'] for r in all_results.values() if r['profit_factor'] > 0]):.2f}")
    print(f"  Worst Max DD: {max(r['max_drawdown_pct'] for r in all_results.values()):.2f}%")

    # RISK ASSESSMENT
    print(f"\n\n  {'='*70}")
    print(f"  RISK ASSESSMENT")
    print(f"  {'='*70}")
    ghost_scenarios = [s for s, r in all_results.items() if r['ghost_stopped']]
    if ghost_scenarios:
        print(f"  Ghost stop triggered in: {', '.join(ghost_scenarios)}")
    else:
        print(f"  Ghost stop: NEVER TRIGGERED across all 12 scenarios")

    best = max(all_results.items(), key=lambda x: x[1]['return_pct'])
    worst = min(all_results.items(), key=lambda x: x[1]['return_pct'])
    print(f"  Best scenario:  {best[0]} → {best[1]['return_pct']:+.2f}%")
    print(f"  Worst scenario: {worst[0]} → {worst[1]['return_pct']:+.2f}%")

    # AKAD effectiveness
    print(f"\n  AKAD Risk Sizing Effectiveness:")
    for sname, r in all_results.items():
        if r['max_drawdown_pct'] > 0:
            risk_efficiency = r['return_pct'] / r['max_drawdown_pct'] if r['max_drawdown_pct'] > 0 else 0
            print(f"    {sname:<30} Return/DD ratio: {risk_efficiency:>6.2f}")

    # Save results
    output_path = Path("Results/v56_2year_stress_results.json")
    output_path.parent.mkdir(exist_ok=True)

    # Convert for JSON
    def convert(obj):
        if isinstance(obj, (np.integer,)): return int(obj)
        elif isinstance(obj, (np.floating,)): return float(obj)
        elif isinstance(obj, np.ndarray): return obj.tolist()
        elif isinstance(obj, np.bool_): return bool(obj)
        return obj

    with open(output_path, 'w') as f:
        json.dump(all_results, f, indent=2, default=convert)
    print(f"\n  Results saved to: {output_path}")

    return all_results


if __name__ == "__main__":
    results = main()
