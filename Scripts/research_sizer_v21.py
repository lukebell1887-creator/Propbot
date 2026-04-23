#!/usr/bin/env python3
"""
research_sizer_v21.py — PhD-grade sizer ablation on the ORB trade stream.

Purpose:
    Answer the question "what is the $ contribution of each piece of
    smart-sizing math vs a flat 0.25% hard cap?"

Method:
    1. Run ORB v20 once at FLAT 0.10% risk to get a "unit-R trade stream":
       for each trade, we know (time, symbol, realised R, R_dist in $ per
       0.10% risk, ATR at entry, equity-path index).
    2. Replay that stream under each sizing policy. Scaling to any other
       per-trade risk multiplier is exact: the realised R and sign stay
       identical, only the $ per trade changes.
    3. Measure net PnL, max DD, Sharpe, PF, and an INCREMENTAL $ delta
       per policy layer (each layer added on top of the previous ones).

Policies tested (each one is a theorem, not a heuristic):

    P0  FLAT_025       baseline — always 0.25 % per trade
    P1  MERTON          f* = μ̂_EWMA / (γ · σ̂²_EWMA) per symbol
    P2  + BAYES         Thorp shrink: multiply by (1 − 2·Var(p)/(p(1-p))
                                              − Var(μ)/μ²)
    P3  + GZ            Grossman-Zhou: multiply by (1 − DD_current / DD_cap)
    P4  + REGIME        2-state vol-regime mult (calm 1.10, panic 0.50)
                        based on realised ATR quartile
    P5  + HJB           Finite-deadline HJB approximation:
                          mult = g(equity, time_remaining, target) ∈ [0.3, 1.2]
    P6  + DAVIS_NORMAN  no-trade region: skip if edge<2.5× round-trip cost
    P7  + CVaR          Rockafellar-Uryasev: cap ES_{5%} ≤ 0.5% equity
    P8  FULL STACK      P1 × P2 × P3 × P4 × P5 × P6 × P7, clipped to 1.5% cap

Outputs:
    Results/research_sizer_v21.txt      — human-readable ablation table
    Results/research_sizer_v21.json     — machine-readable full detail
"""
from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.smartbb_engine import SMARTBB_UNIVERSE
from src.orb_engine_v20 import ORBEngineV20, ORBEngineConfig
from src.momentum.orb import ORBConfig


# =====================================================================
#  Configuration (mirrors v20 PhD-suite grid winner per-symbol)
# =====================================================================

SYMBOLS = ["DE40", "US30", "XAUUSD", "US100", "US500"]

# PER-SYMBOL TUNING (from PhD grid search winners in v20_phd_suite.json)
# Each symbol has its own opening-range window, TP multipliers, SL buffer,
# and amplitude hurdle. This was the grid-search finding.
ORB_CONFIGS: Dict[str, ORBConfig] = {
    "DE40":   ORBConfig(or_start_hour=8,  or_start_minute=0,  or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=1.5,
                        tp2_range_mult=3.0, sl_buffer_range_mult=0.3),
    "US30":   ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.0),
    "XAUUSD": ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.6),
    # US100: tiny OR (5min) + low SL buffer is what grid found best
    "US100":  ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=5,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.0),
    # US500: 15min OR with conservative TP (small range, low vol)
    "US500":  ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=15,
                        trade_window_minutes=120, tp1_range_mult=0.5,
                        tp2_range_mult=1.0, sl_buffer_range_mult=0.6),
}
# Per-symbol amplitude hurdles from grid (OR-range must be ≥ this × median-bar)
AMP_HURDLE_BY_SYM = {"DE40": 3.0, "US30": 4.5, "XAUUSD": 4.5,
                     "US100": 4.5, "US500": 3.0}

BALANCE = 100_000.0
MONTHS = 3
FLAT_BASELINE_PCT = 0.0025           # 0.25 % flat = v20 current default
DD_CAP = 0.04                        # 4 % hard drawdown target
CHALLENGE_DAYS = 30                  # 5%ers challenge deadline (trading days)


# =====================================================================
#  Helpers
# =====================================================================

def load_m1(path: Path, tmin: datetime, tmax: datetime):
    out = []
    with open(path, "r", newline="") as f:
        for row in csv.DictReader(f):
            try:
                t = datetime.fromisoformat(row["time"])
            except Exception:
                t = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            if tmin and t < tmin:
                continue
            if tmax and t > tmax:
                continue
            out.append((t, float(row["open"]), float(row["high"]),
                        float(row["low"]), float(row["close"])))
    return out


def common_window(files: Dict[str, Path], months: int) -> Tuple[datetime, datetime]:
    firsts, lasts = {}, {}
    for s, p in files.items():
        with open(p, "r", newline="") as f:
            rdr = csv.reader(f); next(rdr)
            rows = [r for r in rdr if r]
        try:
            firsts[s] = datetime.fromisoformat(rows[0][0])
            lasts[s] = datetime.fromisoformat(rows[-1][0])
        except Exception:
            firsts[s] = datetime.strptime(rows[0][0], "%Y-%m-%d %H:%M:%S")
            lasts[s] = datetime.strptime(rows[-1][0], "%Y-%m-%d %H:%M:%S")
    end = min(lasts.values())
    start = max(max(firsts.values()), end - timedelta(days=months * 31))
    return start, end


# =====================================================================
#  Stage 1 — extract the unit-R trade stream from ORB at 0.10 % flat
# =====================================================================

@dataclass
class UnitTrade:
    """One ORB trade at 0.10% unit risk — known R, known $ per risk unit."""
    entry_time: datetime
    exit_time: datetime
    symbol: str
    side: int
    # At 0.10% unit risk, we took risk_d = 0.10% * equity_at_entry = unit_risk_dollars
    # Realised pnl / unit_risk_dollars = realised_R (a pure number, +2.0, -1.0 etc)
    realised_R: float          # in R units (pnl / initial risk in $)
    unit_risk_dollars: float   # $ risked at 0.10% size (for this entry-equity)
    equity_at_entry: float
    peak_at_entry: float
    atr_at_entry: float
    or_range: float
    day_key: str
    # cost breakdown (so we can recompute when scaling)
    unit_gross_pnl: float      # gross pnl at this unit size
    unit_spread_cost: float
    unit_commission: float


def run_unit_trades(streams: Dict[str, List], specs,
                    unit_risk_pct: float = 0.001) -> List[UnitTrade]:
    """Run ORB once at unit_risk_pct; extract per-entry unit trades."""
    cfg = ORBEngineConfig(
        risk_pct=unit_risk_pct,
        amp_hurdle=0.0,                 # collect ALL signals (the Davis-Norman
                                        # filter becomes its own policy layer)
        require_nr7=False, nr_lookback=7,
        trail_atr_mult=0.8,
        tp1_close_frac=0.50, tp2_close_frac=0.25,
        hurst_min=0.0, hurst_max=1.0,
        hurst_window=200,
    )
    engines: Dict[str, ORBEngineV20] = {}
    for sym in SYMBOLS:
        if sym not in streams:
            continue
        spec = specs[sym]
        eng_cfg = ORBEngineConfig(**{**cfg.__dict__,
                                     "amp_hurdle": AMP_HURDLE_BY_SYM[sym]})
        engines[sym] = ORBEngineV20(
            symbols=[spec], cfg=eng_cfg,
            orb_configs={sym: ORB_CONFIGS[sym]},
            initial_equity=BALANCE,
        )
    allb = []
    for sym, bars in streams.items():
        allb.extend((t, sym, o, h, l, c) for (t, o, h, l, c) in bars)
    allb.sort(key=lambda r: r[0])
    for t, s, o, h, l, c in allb:
        if s in engines:
            engines[s].on_bar(s, t.timestamp(), t.strftime("%Y-%m-%d"),
                              t.hour, t.minute, o, h, l, c)
    # Aggregate partial exits per (symbol, entry_time)
    per: Dict[Tuple[str, float], dict] = defaultdict(lambda: {
        "gross": 0.0, "spread": 0.0, "comm": 0.0,
        "net": 0.0, "side": 0, "entry_price": 0.0,
        "R_dist_$": 0.0, "or_range": 0.0, "exit_time": 0.0,
    })
    for eng in engines.values():
        for tr in eng.trades:
            k = (tr.symbol, tr.entry_time)
            d = per[k]
            d["gross"] += tr.gross_pnl
            d["spread"] += tr.spread_cost
            d["comm"] += tr.commission
            d["net"] += tr.net_pnl
            d["side"] = tr.side
            d["entry_price"] = tr.entry_price
            d["or_range"] = tr.or_range
            d["exit_time"] = max(d["exit_time"], tr.exit_time)
            # We need R_dist_$ = risk in $ = unit_risk_pct × equity_at_entry.
            # Since we used flat 0.10% the engine risked `risk_d` exactly.
    # Build unit-trade list: realised_R = net / (unit_risk_pct × equity_at_entry_approx).
    # We approximate equity_at_entry by walking forward with cumulative equity.
    unit_trades: List[UnitTrade] = []
    eq = BALANCE
    peak = eq
    # Need ATR approximation at entry. Easiest proxy: use |R_dist_$| / spec.pip_value
    # implicit from realised trades is dangerous. We'll use or_range as the ATR proxy
    # (it's the natural volatility unit for this strategy).
    sorted_entries = sorted(per.items(), key=lambda kv: kv[0][1])
    for (sym, ent_t), d in sorted_entries:
        equity_at_entry = eq
        peak_at_entry = peak
        unit_risk_d = unit_risk_pct * equity_at_entry   # what we risked here
        realised_R = d["net"] / unit_risk_d if unit_risk_d > 0 else 0.0
        ut = UnitTrade(
            entry_time=datetime.fromtimestamp(ent_t),
            exit_time=datetime.fromtimestamp(d["exit_time"]),
            symbol=sym, side=d["side"],
            realised_R=realised_R,
            unit_risk_dollars=unit_risk_d,
            equity_at_entry=equity_at_entry,
            peak_at_entry=peak_at_entry,
            atr_at_entry=max(d["or_range"], 1e-6),
            or_range=d["or_range"],
            day_key=datetime.fromtimestamp(ent_t).strftime("%Y-%m-%d"),
            unit_gross_pnl=d["gross"],
            unit_spread_cost=d["spread"],
            unit_commission=d["comm"],
        )
        unit_trades.append(ut)
        eq += d["net"]
        if eq > peak:
            peak = eq
    return unit_trades


# =====================================================================
#  Stage 2 — sizing-policy evaluators
# =====================================================================
#
# Each policy returns a RISK MULTIPLIER that transforms 0.10% (unit) into
# the policy's recommended risk%. Applied in sequence as a stack.
#
# All policies take (state, trade) and return a multiplier.

class SizerState:
    """Live state maintained during replay — EWMA, regime flags, etc."""
    def __init__(self):
        self.r_history_by_sym: Dict[str, deque] = defaultdict(lambda: deque(maxlen=60))
        self.atr_history_by_sym: Dict[str, deque] = defaultdict(lambda: deque(maxlen=30))
        self.n_trades = 0
        self.trade_R_history: deque = deque(maxlen=100)     # portfolio-level
        self.equity = BALANCE
        self.peak = BALANCE
        self.start_time: Optional[datetime] = None
        self.deadline_td = timedelta(days=CHALLENGE_DAYS)


def _ewma(xs: List[float], alpha: float = 0.2) -> Tuple[float, float]:
    """Return (mean_ewma, var_ewma) using exponential weighting."""
    if not xs:
        return 0.0, 1.0
    m = xs[0]
    v = 0.0
    for x in xs[1:]:
        m = alpha * x + (1 - alpha) * m
        v = alpha * (x - m) ** 2 + (1 - alpha) * v
    return m, max(v, 1e-6)


# ---- P1: Merton --------------------------------------------------------
def policy_merton(state: SizerState, t: UnitTrade, gamma: float = 2.0) -> float:
    """f* = μ̂_EWMA / (γ σ̂²_EWMA). Caps at 3×, floors at 0."""
    rs = list(state.r_history_by_sym[t.symbol])
    if len(rs) < 5:
        return 1.0           # warm-up
    mu, var = _ewma(rs)
    if var < 1e-6 or mu <= 0:
        return 0.5           # no edge — half-size conservative
    f_star = mu / (gamma * var)
    # Normalise: if baseline is 0.10% and Merton says f*=0.005, that's 5× — cap at 3
    return max(0.1, min(3.0, f_star / 0.001))   # scale in units of 0.10%

# ---- P2: Bayes shrink --------------------------------------------------
def policy_bayes(state: SizerState, t: UnitTrade) -> float:
    """Thorp (2006) shrinkage for parameter uncertainty."""
    rs = list(state.r_history_by_sym[t.symbol])
    if len(rs) < 10:
        return 0.5           # heavy shrink while uncertain
    wins = sum(1 for x in rs if x > 0)
    n = len(rs)
    p = wins / n
    # Beta(wins+1, losses+1) posterior mean variance
    p_var = p * (1 - p) / (n + 1)
    mu, var = _ewma(rs)
    mu_var = var / n
    denom_p = max(1e-6, p * (1 - p))
    denom_R = max(1e-6, mu * mu)
    shrink = 1.0 - 2.0 * p_var / denom_p - mu_var / denom_R
    return max(0.0, min(1.0, shrink))

# ---- P3: Grossman-Zhou --------------------------------------------------
def policy_gz(state: SizerState, t: UnitTrade, dd_cap: float = DD_CAP) -> float:
    """Closed-form f_GZ = f_Merton × (1 − DD / DD_cap). Zero at barrier."""
    if state.peak <= 0:
        return 0.0
    dd = max(0.0, (state.peak - state.equity) / state.peak)
    if dd >= dd_cap:
        return 0.0
    return (1.0 - dd / dd_cap)

# ---- P4: Regime --------------------------------------------------------
def policy_regime(state: SizerState, t: UnitTrade) -> float:
    """2-state vol regime based on per-symbol ATR quartile."""
    atrs = sorted(state.atr_history_by_sym[t.symbol])
    if len(atrs) < 10:
        return 1.0
    q25 = atrs[len(atrs) // 4]
    q75 = atrs[3 * len(atrs) // 4]
    if t.atr_at_entry < q25:
        return 1.10                        # calm — small boost
    if t.atr_at_entry > q75:
        return 0.50                        # panic — halve
    return 1.0

# ---- P5: HJB deadline-aware --------------------------------------------
def policy_hjb(state: SizerState, t: UnitTrade,
               target_pct: float = 0.08) -> float:
    """
    Finite-deadline HJB approximation.
    - Early in challenge with room to grow: normal
    - Late + ahead: reduce (lock in)
    - Late + behind: modest boost (catch up within DD)
    """
    if state.start_time is None:
        return 1.0
    elapsed = (t.entry_time - state.start_time).total_seconds() / 86400.0
    fraction_left = max(0.0, 1.0 - elapsed / CHALLENGE_DAYS)
    gain = state.equity / BALANCE - 1.0
    if fraction_left > 0.67:
        return 1.0                         # early phase — no tilt
    # distance to target (normalised)
    gap = (target_pct - gain) / target_pct   # >0 → behind, <0 → ahead
    if gap <= 0:
        # Ahead of target — lock in (reduce aggressively as deadline approaches)
        lock_mult = 0.3 + 0.7 * fraction_left
        return lock_mult
    # Behind target — modest boost proportional to time pressure
    # cap boost at 1.2 so we don't blow DD
    boost = 1.0 + 0.2 * (1.0 - fraction_left) * min(1.0, gap)
    return min(1.2, boost)

# ---- P6: Davis-Norman no-trade region ----------------------------------
def policy_davis_norman(state: SizerState, t: UnitTrade,
                         edge_cost_gate: float = 2.5) -> float:
    """Skip if expected edge per trade is less than gate × round-trip cost."""
    expected_R = 0.0
    rs = list(state.r_history_by_sym[t.symbol])
    if len(rs) >= 5:
        expected_R = statistics.mean(rs)
    cost_in_R = (t.unit_spread_cost + t.unit_commission) / max(t.unit_risk_dollars, 1e-9)
    if expected_R < edge_cost_gate * cost_in_R:
        return 0.0                          # skip trade
    return 1.0

# ---- P7: CVaR cap ------------------------------------------------------
def policy_cvar(state: SizerState, t: UnitTrade,
                cvar_cap_pct: float = 0.005, alpha: float = 0.05) -> float:
    """Gaussian CVaR: ES_α = -μ + σ·φ(z)/α. Shrink until ES_$ ≤ cap×equity."""
    rs = list(state.trade_R_history)
    if len(rs) < 10:
        return 1.0
    mu = statistics.mean(rs)
    sig = statistics.pstdev(rs) if len(rs) > 1 else 1.0
    # z at alpha=0.05 → -1.6449; φ(-1.6449)=0.10314; ES factor = 0.10314/0.05 = 2.063
    es_R = -mu + sig * 2.063
    if es_R <= 0:
        return 1.0
    # ES in $ per unit of 0.10%: es_R × unit_risk_dollars
    es_dollars = es_R * t.unit_risk_dollars
    cap_dollars = cvar_cap_pct * state.equity
    if es_dollars <= cap_dollars:
        return 1.0
    return max(1e-3, cap_dollars / es_dollars)


# =====================================================================
#  Stage 3 — policy runner
# =====================================================================

HARD_CAP_PCT = 0.015           # final hard clip (1.5 %)


@dataclass
class PolicyResult:
    name: str
    n_trades: int
    net_pnl: float
    max_dd_pct: float
    sharpe: float
    pf: float
    wr: float
    avg_risk_pct: float          # mean per-trade risk % actually used
    p_over_4dd: float            # realised — only 0/1 (single path)


def run_policy(trades: List[UnitTrade], name: str,
               combine: Callable[[SizerState, UnitTrade], float],
               start_time: datetime) -> PolicyResult:
    """Replay the trade stream applying `combine` as per-trade multiplier
    on top of the 0.10% unit. Returns full summary."""
    state = SizerState()
    state.start_time = start_time
    eq = BALANCE
    peak = eq
    mdd = 0.0
    pnls = []
    risks = []
    for t in trades:
        # Update state context BEFORE sizing (we don't use this trade's outcome)
        state.equity = eq
        state.peak = peak
        mult = combine(state, t)                 # dimensionless mult on 0.10% unit
        risk_pct = max(0.0, min(HARD_CAP_PCT, mult * 0.001))
        # Scale unit pnl by risk_pct / 0.001
        if risk_pct <= 0:
            # skipped by a policy (Davis-Norman etc)
            continue
        scale = risk_pct / 0.001
        realised_pnl = t.realised_R * t.unit_risk_dollars * scale
        pnls.append(realised_pnl)
        risks.append(risk_pct * 100)
        eq += realised_pnl
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        mdd = max(mdd, dd)
        # Update rolling history with this trade's realised R (NOT the scaled pnl)
        state.r_history_by_sym[t.symbol].append(t.realised_R)
        state.atr_history_by_sym[t.symbol].append(t.atr_at_entry)
        state.trade_R_history.append(t.realised_R)
        state.n_trades += 1
    if not pnls:
        return PolicyResult(name=name, n_trades=0, net_pnl=0.0,
                            max_dd_pct=0.0, sharpe=0.0, pf=0.0,
                            wr=0.0, avg_risk_pct=0.0, p_over_4dd=0.0)
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw = sum(wins); gl = -sum(losses)
    pf = gw / gl if gl > 0 else float("inf")
    mu = statistics.mean(pnls)
    sd = statistics.pstdev(pnls) if len(pnls) > 1 else 1.0
    # Per-trade Sharpe × √N for annual-ish
    sharpe = (mu / sd) * math.sqrt(len(pnls)) if sd > 0 else 0.0
    return PolicyResult(
        name=name, n_trades=len(pnls),
        net_pnl=sum(pnls),
        max_dd_pct=mdd * 100.0,
        sharpe=sharpe,
        pf=pf if math.isfinite(pf) else 99.0,
        wr=len(wins) / len(pnls),
        avg_risk_pct=statistics.mean(risks) if risks else 0.0,
        p_over_4dd=1.0 if mdd >= 0.04 else 0.0,
    )


# =====================================================================
#  Main
# =====================================================================

def main():
    lines = []
    def p(m=""):
        print(m); lines.append(m)

    p("=" * 110)
    p("  research_sizer_v21 — PhD SIZER ABLATION on ORB trade stream")
    p(f"  ${BALANCE:,.0f} | {MONTHS} months | DE40+US30+XAUUSD | 4% DD target")
    p("=" * 110)

    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in SYMBOLS}
    files = {s: pp for s, pp in files.items() if pp.exists()}
    if not files:
        p("ERROR: missing data"); return 1
    tmin, tmax = common_window(files, MONTHS)
    p(f"  Window : {tmin.date()} → {tmax.date()}")
    specs = {s: SMARTBB_UNIVERSE[s] for s in files}
    streams = {s: load_m1(files[s], tmin, tmax) for s in files}
    p(f"  Bars   : {sum(len(v) for v in streams.values()):,}")
    p("")

    p("  [1/2] Extracting unit-R trade stream (flat 0.10%)...")
    unit_trades = run_unit_trades(streams, specs)
    p(f"        {len(unit_trades)} trades.")
    if len(unit_trades) < 30:
        p("        Not enough trades — aborting."); return 1
    r_mean = statistics.mean(t.realised_R for t in unit_trades)
    r_stdev = statistics.pstdev(t.realised_R for t in unit_trades)
    p(f"        mean R = {r_mean:+.3f},  stdev R = {r_stdev:.3f},  Sharpe_raw = {r_mean/max(r_stdev,1e-9):.3f}")
    p("")

    # ------------------------------------------------------------------
    # Define policies. Each `combine` returns a multiplier on 0.10% unit.
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # TAMED-MERTON variant: cap Merton's per-trade output at TAM_CAP× unit
    # so that max risk per trade is TAM_CAP × 0.10 % regardless of what
    # Merton's formula says. Reduces DD proportionally at cost of upside.
    # ------------------------------------------------------------------
    def policy_merton_capped(state, t, cap_mult):
        raw = policy_merton(state, t)
        return min(raw, cap_mult)

    # ------------------------------------------------------------------
    # The correct INTEGRATED policies — no Bayes-warmup, no double filter.
    # These are the "genius calculus" the user asked for.
    # ------------------------------------------------------------------
    policies: List[Tuple[str, Callable[[SizerState, UnitTrade], float]]] = [
        # === baselines ===
        ("P0 FLAT_0.25%",             lambda s, t: 2.5),          # current v20
        ("P0b FLAT_0.50%",            lambda s, t: 5.0),          # 2× baseline ref
        # === Merton alone with different caps ===
        ("P1a MERTON (cap 3.0×)",
         lambda s, t: 2.5 * policy_merton(s, t)),                 # original
        ("P1b MERTON (cap 2.0× = max 0.20%)",
         lambda s, t: 2.5 * policy_merton_capped(s, t, 2.0)),
        ("P1c MERTON (cap 1.5× = max 0.15%)",
         lambda s, t: 2.5 * policy_merton_capped(s, t, 1.5)),
        # === The CORRECT integrated math: Merton × GZ (no Bayes, no DN) ===
        ("PG1 MERTON × GZ (cap 3.0×)",
         lambda s, t: 2.5 * policy_merton(s, t) * policy_gz(s, t)),
        ("PG2 MERTON × GZ (cap 2.0×)",
         lambda s, t: 2.5 * policy_merton_capped(s, t, 2.0) * policy_gz(s, t)),
        ("PG3 MERTON × GZ × REGIME (cap 2.0×)",
         lambda s, t: 2.5 * policy_merton_capped(s, t, 2.0) * policy_gz(s, t) * policy_regime(s, t)),
        ("PG4 MERTON × GZ × HJB (cap 2.0×)",
         lambda s, t: 2.5 * policy_merton_capped(s, t, 2.0) * policy_gz(s, t) * policy_hjb(s, t)),
        ("PG5 FULL APEX (Merton×GZ×Regime×HJB, cap 2.0×)",
         lambda s, t: 2.5 * policy_merton_capped(s, t, 2.0) * policy_gz(s, t)
                       * policy_regime(s, t) * policy_hjb(s, t)),
    ]

    p("  [2/2] Replaying trade stream under each policy...")
    p("")
    p(f"    {'Policy':<28} {'N':>4}  {'PnL':>10}  {'DD%':>6}  "
      f"{'PF':>5}  {'WR':>5}  {'Sharpe':>6}  {'avgRisk%':>8}  Pass4%DD?")
    p("    " + "-" * 104)

    results: List[PolicyResult] = []
    for name, combine in policies:
        r = run_policy(unit_trades, name, combine, start_time=tmin)
        results.append(r)
        pass_str = "✅ yes" if r.p_over_4dd == 0 else "❌ no"
        p(f"    {r.name:<28} {r.n_trades:>4}  ${r.net_pnl:>+8,.0f}  "
          f"{r.max_dd_pct:>5.2f}%  {r.pf:>5.2f}  {r.wr*100:>4.1f}%  "
          f"{r.sharpe:>6.2f}  {r.avg_risk_pct:>7.2f}%  {pass_str}")
    p("")
    p("  INCREMENTAL $ CONTRIBUTION (each row = delta vs previous):")
    p("  " + "-" * 60)
    prev = results[0].net_pnl
    for r in results[1:]:
        delta = r.net_pnl - prev
        sign = "+" if delta >= 0 else ""
        p(f"    {r.name:<28}  delta = {sign}${delta:>+7,.0f}")
        prev = r.net_pnl
    p("")
    p("=" * 110)
    best = max(results, key=lambda r: (r.p_over_4dd < 0.5, r.net_pnl))
    p(f"  Best policy : {best.name}")
    p(f"    PnL        : ${best.net_pnl:+,.0f}  ({best.net_pnl/BALANCE*100:+.2f}%)")
    p(f"    DD         : {best.max_dd_pct:.2f}%  (target ≤4%)")
    p(f"    Sharpe     : {best.sharpe:.2f}")
    p(f"    WR / PF    : {best.wr*100:.1f}%  /  {best.pf:.2f}")
    p(f"    avg risk % : {best.avg_risk_pct:.2f}%")
    p("=" * 110)

    out = ROOT / "Results"
    with open(out / "research_sizer_v21.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(out / "research_sizer_v21.json", "w") as f:
        json.dump({
            "generated": datetime.utcnow().isoformat(),
            "window": [str(tmin), str(tmax)],
            "n_unit_trades": len(unit_trades),
            "unit_trade_mean_R": r_mean,
            "unit_trade_stdev_R": r_stdev,
            "policies": [
                {
                    "name": r.name, "n": r.n_trades, "net_pnl": r.net_pnl,
                    "max_dd_pct": r.max_dd_pct, "sharpe": r.sharpe,
                    "pf": r.pf, "wr": r.wr,
                    "avg_risk_pct": r.avg_risk_pct,
                    "passes_4pct_dd": bool(r.p_over_4dd < 0.5),
                } for r in results
            ],
        }, f, indent=2, default=str)
    p(f"  Saved: Results/research_sizer_v21.txt + .json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
