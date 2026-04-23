"""
Market-condition stress transforms for PropBot v24.

Each Scenario is a pure function that takes a list of (t, o, h, l, c) bars
(as emitted by `Scripts.backtest_v22_lean_uk5.load_m1`) and returns a new
list of bars with the same length and timestamps but with the price path
mutated to simulate a specific market regime.

MATHEMATICAL INVARIANTS (held by every transform):
  1. len(out) == len(bars)
  2. out[i].t  == bars[i].t           (timestamps never mutated)
  3. high >= max(open, close)          (OHLC integrity)
  4. low  <= min(open, close)
  5. high >= low
  6. all prices > 0                   (no negative-price paradox)

Two primitives are used by every scenario:

  (A)  PATH DRIFT      — scale each bar's intraday-close-return around the
       previous bar's close by `mult`, then add `drift_per_day / n_bars_in_day`.
       This preserves intraday HIGH/LOW topology (spike / gap) while cleanly
       warping the overall path.

  (B)  JUMP INJECTION  — pick a specific (day, bar) and add ±k·σ_daily to
       every close from that point onwards. Preserves continuity within the
       bar by rescaling H/L around the shifted close.

Scenarios range from "very positive" (+1σ bull melt-up) to "catastrophic"
(fat-tail vol explosion + flash crashes + trend inversion).
"""
from __future__ import annotations
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

Bar = Tuple[datetime, float, float, float, float]   # (t, o, h, l, c)


# =============================================================================
#  Primitives
# =============================================================================

def _estimate_daily_sigma(bars: List[Bar]) -> float:
    """Standard deviation of day-over-day close-to-close log-returns."""
    daily_closes: Dict[str, float] = {}
    for t, o, h, l, c in bars:
        d = t.strftime("%Y-%m-%d")
        daily_closes[d] = c   # overwrite → last close of the day survives
    closes = [daily_closes[k] for k in sorted(daily_closes.keys())]
    if len(closes) < 2:
        return 0.01
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes)) if closes[i - 1] > 0]
    if not rets:
        return 0.01
    m = sum(rets) / len(rets)
    v = sum((r - m) ** 2 for r in rets) / max(1, len(rets) - 1)
    return math.sqrt(v)


def _rebuild_bar_from_close(prev_close: float, o: float, h: float, l: float,
                            c: float, new_c: float) -> Bar:
    """Shift a bar's OHLC so that close=new_c, preserving the bar's *shape*.

    We keep the intraday high-close, low-close, open-close distances and
    apply them to the new close. That preserves wick shape / candle body
    proportion while moving the whole bar vertically.
    """
    d_oc = o - c
    d_hc = h - c
    d_lc = l - c
    new_o = new_c + d_oc
    new_h = max(new_c + d_hc, new_o, new_c)
    new_l = min(new_c + d_lc, new_o, new_c)
    # safety floor
    new_l = max(new_l, 1e-6)
    new_o = max(new_o, 1e-6)
    new_h = max(new_h, max(new_o, new_c))
    return (new_c, new_o, new_h, new_l)  # (will be reassembled below)


def _transform_path(
    bars: List[Bar],
    *,
    vol_mult: float = 1.0,
    drift_per_day: float = 0.0,
    jumps: Optional[List[Tuple[int, float]]] = None,
    seed: int = 42,
) -> List[Bar]:
    """
    Warp a bar stream while preserving OHLC integrity.

    Args:
      vol_mult      : multiplicative scaling applied to each bar's LOG RETURN
                      from prev close.  1.0 = no change, 2.0 = 2× bar-level vol.
      drift_per_day : additive log-return applied uniformly across all bars
                      in each calendar day (positive = bull, negative = bear).
      jumps         : list of (day_index, shock_in_sigma_daily) tuples.
                      A jump of (30, -8.0) means "on day 30, drop by 8 daily σ
                      between bars 0 and 1" — i.e. a flash crash gap.
      seed          : reserved for stochastic scenarios (unused in deterministic
                      transforms but kept for signature uniformity).
    """
    if not bars:
        return []

    rng = random.Random(seed)

    # Estimate original daily σ to scale `jumps` into absolute log-return shocks
    daily_sigma = _estimate_daily_sigma(bars)

    # Group bar indices by day so we can spread drift uniformly & inject jumps
    day_indices: Dict[str, List[int]] = {}
    for i, (t, *_rest) in enumerate(bars):
        day_indices.setdefault(t.strftime("%Y-%m-%d"), []).append(i)
    sorted_days = sorted(day_indices.keys())

    # Map day-index → (day-string, bar-offset) for jump resolution
    jump_map: Dict[int, float] = {}   # index → log-return shock
    if jumps:
        for day_idx, sigma_shock in jumps:
            if 0 <= day_idx < len(sorted_days):
                idx0 = day_indices[sorted_days[day_idx]][0]
                jump_map[idx0] = sigma_shock * daily_sigma

    out: List[Bar] = []
    prev_new_close = bars[0][4]  # first bar keeps its close as anchor

    for i, (t, o, h, l, c) in enumerate(bars):
        day = t.strftime("%Y-%m-%d")
        n_bars_today = len(day_indices[day])
        drift_per_bar = drift_per_day / max(1, n_bars_today)

        # Log-return of the *original* bar (close-to-close from previous bar)
        if i == 0:
            orig_logret = 0.0
            orig_prev_c = c
        else:
            orig_prev_c = bars[i - 1][4]
            orig_logret = math.log(c / orig_prev_c) if orig_prev_c > 0 else 0.0

        # Warped log-return
        new_logret = orig_logret * vol_mult + drift_per_bar
        if i in jump_map:
            new_logret += jump_map[i]

        new_c = prev_new_close * math.exp(new_logret) if prev_new_close > 0 \
                else max(c, 1e-6)

        # Rebuild OHLC around the new close, preserving original wick shape
        # but scaling wick SIZE by vol_mult (realistic: higher vol = wider wicks)
        d_oc = (o - c) * vol_mult
        d_hc = max(h - c, 0.0) * vol_mult
        d_lc = min(l - c, 0.0) * vol_mult    # negative or zero

        new_o = new_c + d_oc
        new_h = new_c + d_hc
        new_l = new_c + d_lc

        # Enforce OHLC integrity even after vol scaling
        new_h = max(new_h, new_o, new_c)
        new_l = min(new_l, new_o, new_c)
        # positive floors
        new_l = max(new_l, 1e-6)
        new_o = max(new_o, 1e-6)
        new_h = max(new_h, max(new_o, new_c))

        out.append((t, new_o, new_h, new_l, new_c))
        prev_new_close = new_c

    return out


# =============================================================================
#  Scenario catalogue — 13 regimes, "very positive" → "catastrophic"
# =============================================================================

@dataclass
class Scenario:
    key: str
    label: str
    severity: str                     # one of: V+, +, N, -, V-, X
    description: str
    apply: Callable[[List[Bar]], List[Bar]]


def _baseline(bars: List[Bar]) -> List[Bar]:
    """Pass-through. Baseline sanity check that the harness reproduces the
    known $23,311 / 2.06 % DD number on real data."""
    return list(bars)


def _bull_melt_up(bars: List[Bar]) -> List[Bar]:
    # +0.5 σ/day average drift on top of real returns; vol unchanged
    sigma = _estimate_daily_sigma(bars)
    return _transform_path(bars, vol_mult=1.0, drift_per_day=0.5 * sigma)


def _strong_bull(bars: List[Bar]) -> List[Bar]:
    # +1.0 σ/day drift + 1.2× vol (a trending boom)
    sigma = _estimate_daily_sigma(bars)
    return _transform_path(bars, vol_mult=1.2, drift_per_day=1.0 * sigma)


def _low_vol_grind(bars: List[Bar]) -> List[Bar]:
    # 0.5× vol, no drift — summer doldrums, OR ranges shrink, small wins
    return _transform_path(bars, vol_mult=0.5, drift_per_day=0.0)


def _high_vol(bars: List[Bar]) -> List[Bar]:
    # 2× vol, no drift — October 2018 / March 2020 style
    return _transform_path(bars, vol_mult=2.0, drift_per_day=0.0)


def _vol_explosion(bars: List[Bar]) -> List[Bar]:
    # 3× vol, no drift — extreme vix-spike regime
    return _transform_path(bars, vol_mult=3.0, drift_per_day=0.0)


def _chop_hell(bars: List[Bar]) -> List[Bar]:
    # Zero drift, normal vol, but INVERT sign of every day's realised return —
    # this removes any residual trend and forces pure mean-reversion which is
    # poisonous for a breakout system. Preserves intraday vol profile.
    sigma = _estimate_daily_sigma(bars)
    # Implementation: flip sign of drift each day deterministically
    out: List[Bar] = []
    day_indices: Dict[str, List[int]] = {}
    for i, (t, *_) in enumerate(bars):
        day_indices.setdefault(t.strftime("%Y-%m-%d"), []).append(i)
    sorted_days = sorted(day_indices.keys())
    # Choose an alternating +/- daily drift of size 0.3σ — chop generator
    flip = {d: (1.0 if k % 2 == 0 else -1.0) for k, d in enumerate(sorted_days)}

    prev_new_close = bars[0][4]
    for i, (t, o, h, l, c) in enumerate(bars):
        day = t.strftime("%Y-%m-%d")
        n_bars_today = len(day_indices[day])
        drift_per_bar = (0.3 * sigma * flip[day]) / max(1, n_bars_today)

        orig_prev_c = bars[i - 1][4] if i > 0 else c
        orig_logret = (math.log(c / orig_prev_c) if i > 0 and orig_prev_c > 0
                       else 0.0)
        # Keep vol the same but INVERT sign of intraday move on "down" days
        new_logret = orig_logret * flip[day] + drift_per_bar

        new_c = prev_new_close * math.exp(new_logret) if prev_new_close > 0 else c
        d_oc = (o - c); d_hc = max(h - c, 0.0); d_lc = min(l - c, 0.0)
        new_o = new_c + d_oc
        new_h = max(new_c + d_hc, new_o, new_c)
        new_l = min(new_c + d_lc, new_o, new_c)
        new_l = max(new_l, 1e-6); new_o = max(new_o, 1e-6)
        new_h = max(new_h, max(new_o, new_c))
        out.append((t, new_o, new_h, new_l, new_c))
        prev_new_close = new_c
    return out


def _bear_trend(bars: List[Bar]) -> List[Bar]:
    # -1 σ/day drift — a steady bear market
    sigma = _estimate_daily_sigma(bars)
    return _transform_path(bars, vol_mult=1.0, drift_per_day=-1.0 * sigma)


def _fat_tail_storm(bars: List[Bar]) -> List[Bar]:
    """2.5× vol path PLUS a random extreme shock every ~5 days.
    Simulates a student-t(3) / Taleb-style environment where the system
    is expected to encounter outliers it hasn't seen in sample."""
    sigma = _estimate_daily_sigma(bars)
    rng = random.Random(1729)
    # Identify unique days
    days: List[str] = []
    seen = set()
    for t, *_ in bars:
        d = t.strftime("%Y-%m-%d")
        if d not in seen:
            seen.add(d); days.append(d)
    # ~20 % of days get a ±5σ shock at the start of the day
    jumps: List[Tuple[int, float]] = []
    for i in range(len(days)):
        if rng.random() < 0.20:
            sign = 1.0 if rng.random() < 0.5 else -1.0
            mag = 3.0 + 2.0 * rng.random()   # 3–5 sigma
            jumps.append((i, sign * mag))
    return _transform_path(bars, vol_mult=2.5, drift_per_day=0.0,
                           jumps=jumps, seed=1729)


def _flash_crash(bars: List[Bar]) -> List[Bar]:
    """Single -8σ gap on day-30 (or the middle day if <60 days)."""
    days = sorted(set(t.strftime("%Y-%m-%d") for t, *_ in bars))
    day_30 = min(30, len(days) // 2)
    return _transform_path(bars, vol_mult=1.0, drift_per_day=0.0,
                           jumps=[(day_30, -8.0)])


def _regime_flip(bars: List[Bar]) -> List[Bar]:
    """+1σ/day for first half, -1σ/day for second half.  No warning, a pure
    trend-reversal test for the sizer's learning capacity."""
    sigma = _estimate_daily_sigma(bars)
    days = sorted(set(t.strftime("%Y-%m-%d") for t, *_ in bars))
    if len(days) < 2:
        return list(bars)
    split = len(days) // 2
    split_day = days[split]
    # Build a per-bar drift array
    day_indices: Dict[str, List[int]] = {}
    for i, (t, *_) in enumerate(bars):
        day_indices.setdefault(t.strftime("%Y-%m-%d"), []).append(i)

    out: List[Bar] = []
    prev_new_close = bars[0][4]
    for i, (t, o, h, l, c) in enumerate(bars):
        day = t.strftime("%Y-%m-%d")
        n_bars_today = len(day_indices[day])
        drift = (1.0 if day < split_day else -1.0) * sigma
        drift_per_bar = drift / max(1, n_bars_today)

        orig_prev_c = bars[i - 1][4] if i > 0 else c
        orig_logret = (math.log(c / orig_prev_c) if i > 0 and orig_prev_c > 0
                       else 0.0)
        new_logret = orig_logret + drift_per_bar
        new_c = prev_new_close * math.exp(new_logret) if prev_new_close > 0 else c
        d_oc = (o - c); d_hc = max(h - c, 0.0); d_lc = min(l - c, 0.0)
        new_o = new_c + d_oc
        new_h = max(new_c + d_hc, new_o, new_c)
        new_l = min(new_c + d_lc, new_o, new_c)
        new_l = max(new_l, 1e-6); new_o = max(new_o, 1e-6)
        new_h = max(new_h, max(new_o, new_c))
        out.append((t, new_o, new_h, new_l, new_c))
        prev_new_close = new_c
    return out


def _two_flash_crashes(bars: List[Bar]) -> List[Bar]:
    """Two -6σ gaps, one near day 20 and one near day 50."""
    days = sorted(set(t.strftime("%Y-%m-%d") for t, *_ in bars))
    if len(days) < 4:
        return list(bars)
    d1 = min(20, len(days) // 3)
    d2 = min(50, 2 * len(days) // 3)
    return _transform_path(bars, vol_mult=1.0, drift_per_day=0.0,
                           jumps=[(d1, -6.0), (d2, -6.0)])


def _catastrophe(bars: List[Bar]) -> List[Bar]:
    """The kitchen sink: 3× vol + -1σ/day drift + two -6σ flash crashes.
    This is the worst realistic regime we could face; if DD stays < 4 %
    here, 5ers challenge is mathematically unbreakable."""
    sigma = _estimate_daily_sigma(bars)
    days = sorted(set(t.strftime("%Y-%m-%d") for t, *_ in bars))
    if len(days) < 4:
        return list(bars)
    d1 = min(20, len(days) // 3)
    d2 = min(50, 2 * len(days) // 3)
    return _transform_path(bars, vol_mult=3.0, drift_per_day=-1.0 * sigma,
                           jumps=[(d1, -6.0), (d2, -6.0)])


def _monday_gaps(bars: List[Bar]) -> List[Bar]:
    """Inject ±3σ random gap at every Monday open — simulates weekend news.
    Good test for the weekend-flat rail and broker SL integrity."""
    days = sorted(set(t.strftime("%Y-%m-%d") for t, *_ in bars))
    mondays: List[int] = []
    for i, d in enumerate(days):
        if datetime.fromisoformat(d).weekday() == 0:   # Monday
            mondays.append(i)
    rng = random.Random(2718)
    jumps = [(i, (1 if rng.random() < 0.5 else -1) * (2.5 + rng.random()))
             for i in mondays]
    return _transform_path(bars, vol_mult=1.0, drift_per_day=0.0, jumps=jumps)


# -----------------------------------------------------------------------------
#  Registry — the order here is the order used in the summary table
# -----------------------------------------------------------------------------
SCENARIOS: List[Scenario] = [
    Scenario("baseline",     "Baseline (real data)",           "N",
             "Real 5ers data, no transform. Should reproduce v24d sweep.",
             _baseline),
    Scenario("bull_melt",    "Bull Melt-Up (+0.5σ/day)",       "V+",
             "Persistent +0.5σ/day drift: best case for breakout + trend.",
             _bull_melt_up),
    Scenario("strong_bull",  "Strong Bull (+1σ + 1.2× vol)",   "V+",
             "Aggressive trending boom; 1.2× wider ORs, wider TPs.",
             _strong_bull),
    Scenario("low_vol",      "Low-Vol Grind (0.5× vol)",       "+",
             "Summer doldrums: half normal range. Small trades, small DD.",
             _low_vol_grind),
    Scenario("high_vol",     "High-Vol (2× vol)",              "-",
             "October '18 / COVID style: 2× vol, no direction.",
             _high_vol),
    Scenario("vol_explosion","Vol Explosion (3× vol)",         "V-",
             "VIX-spike: 3× vol, wider whipsaws, likely DD rise.",
             _vol_explosion),
    Scenario("chop_hell",    "Chop-Hell (zero-trend + alt)",   "V-",
             "Alternating +/- daily drift: mean-reversion poison for ORB.",
             _chop_hell),
    Scenario("bear_trend",   "Bear Market (-1σ/day)",          "-",
             "Steady -1σ/day drift: ORB's short side fires a lot.",
             _bear_trend),
    Scenario("fat_tail",     "Fat-Tail Storm (Taleb)",         "V-",
             "2.5× vol + random 3-5σ shocks on ~20% of days.",
             _fat_tail_storm),
    Scenario("flash_crash",  "Flash Crash (single -8σ gap)",   "V-",
             "One big -8σ gap on day 30. Tests SL integrity + sizer cutback.",
             _flash_crash),
    Scenario("regime_flip",  "Regime Flip (+1σ → -1σ)",        "-",
             "Bull first half, bear second. Pure learning-rate stress.",
             _regime_flip),
    Scenario("two_crashes",  "Two Flash Crashes (-6σ × 2)",    "V-",
             "Two big gaps (day 20 + day 50). Clustered tail risk.",
             _two_flash_crashes),
    Scenario("monday_gaps",  "Weekend-News Gaps (±3σ)",        "-",
             "Random ±3σ gap every Monday open. Tests weekend-flat rail.",
             _monday_gaps),
    Scenario("catastrophe",  "CATASTROPHE (3×vol + -1σ + gaps)","X",
             "Kitchen-sink worst case: 3× vol, -1σ/day, two -6σ crashes.",
             _catastrophe),
]


def apply_scenario(bars: List[Bar], scenario_key: str) -> List[Bar]:
    """Apply a named scenario to a bar stream."""
    for s in SCENARIOS:
        if s.key == scenario_key:
            return s.apply(bars)
    raise KeyError(f"unknown scenario: {scenario_key}. "
                   f"Available: {[s.key for s in SCENARIOS]}")
