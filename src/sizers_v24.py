"""
sizers_v24.py — PhD sizer zoo for the v24 shootout.

All sizers implement the SAME interface:

    class Sizer:
        name: str
        def reset(self) -> None: ...
        def size(self, hist: History, trade: TradeMeta) -> float: ...
        def on_closed(self, trade: TradeMeta, realised_R: float) -> None: ...

Semantics:
    * size() is called BEFORE the trade is booked, returning the risk
      fraction (0.0 - HARD_CAP_F=0.005) to use for THIS trade.
    * on_closed() is called AFTER the trade completes, with the realised
      R-multiple, so the sizer can update internal EWMA / GARCH / etc.
    * hist is a mutable History that already tracks all prior closed trades
      and the current equity/peak/start_equity.

Every sizer is capped at HARD_CAP_F = 0.5 % per trade regardless of its
internal calculation (prop-firm sanity belt). Every sizer is also
floor-capped at MIN_F = 0.001 % (so we never report "zero" size that would
skew Calmar/Sharpe — we count it but it doesn't move equity meaningfully).

Mathematical references:
    Kelly (1956)         — full Kelly fraction
    Breiman (1961)       — log-utility optimality
    Merton (1969)        — continuous-time f* = μ/(γσ²)
    Thorp (1962, 2006)   — half-Kelly for parameter uncertainty
    Vince (1990)         — Optimal f (geometric HPR max)
    Grossman-Zhou (1993) — drawdown-constrained closed-form
    Black-Jones (1987)   — CPPI with constant multiplier
    Hamilton (1989)      — 2-state regime HMM
    Bollerslev (1986)    — GARCH(1,1) variance forecast
    MacLean-Thorp-Ziemba — Bayesian Kelly under uncertainty

Tested and compared empirically in Scripts/phd_sizer_shootout_v24.py.
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from src.dynamic_sizer_v21 import MertonGZSizer, MertonGZSizerConfig


# =====================================================================
# Constants
# =====================================================================
HARD_CAP_F = 0.005   # 0.5 % absolute per-trade cap (prop-firm safety belt)
MIN_F = 0.00001      # 0.001 % floor (keeps f > 0 so trade still counts)


# =====================================================================
# Data model
# =====================================================================
@dataclass
class TradeMeta:
    """Immutable trade descriptor passed to sizer."""
    symbol: str
    entry_time: float       # unix seconds
    exit_time: float        # unix seconds
    side: int               # +1 long, -1 short
    entry_price: float
    stop_price: float
    exit_price: float
    realised_R: float       # signed R-multiple (pnl / initial_risk_$)
    original_net_pnl: float # the $ the orig engine booked — useful for sanity


@dataclass
class History:
    """Mutable running state shared by every sizer during replay."""
    closed: List[TradeMeta] = field(default_factory=list)
    by_symbol: Dict[str, List[TradeMeta]] = field(default_factory=lambda: defaultdict(list))
    equity: float = 100_000.0
    peak: float = 100_000.0
    start_equity: float = 100_000.0
    equity_curve: List[float] = field(default_factory=list)

    def feedback(self, trade: TradeMeta, pnl: float) -> None:
        self.closed.append(trade)
        self.by_symbol[trade.symbol].append(trade)
        self.equity += pnl
        self.peak = max(self.peak, self.equity)
        self.equity_curve.append(self.equity)

    def dd_pct(self) -> float:
        if self.peak <= 0:
            return 0.0
        return max(0.0, (self.peak - self.equity) / self.peak)


# =====================================================================
# Base class
# =====================================================================
class Sizer:
    def __init__(self, name: str):
        self.name = name
    def reset(self) -> None:
        pass
    def size(self, hist: History, trade: TradeMeta) -> float:
        raise NotImplementedError
    def on_closed(self, trade: TradeMeta, realised_R: float) -> None:
        pass


def _clip(f: float) -> float:
    if not math.isfinite(f) or f < 0:
        return MIN_F
    return max(MIN_F, min(HARD_CAP_F, f))


# =====================================================================
# S01-S04 — Flat controls
# =====================================================================
class FlatSizer(Sizer):
    """Constant fraction. Controls the experiment."""
    def __init__(self, fraction: float, name: Optional[str] = None):
        super().__init__(name or f"Flat_{fraction*100:.2f}pct")
        self.f = fraction
    def size(self, hist: History, trade: TradeMeta) -> float:
        return _clip(self.f)


# =====================================================================
# Kelly family
# =====================================================================
def _kelly_from_history(r_list: List[float], min_n: int = 10
                         ) -> Optional[Tuple[float, float, float]]:
    """Return (p, b, q) or None if too few trades or degenerate."""
    if len(r_list) < min_n:
        return None
    wins = [r for r in r_list if r > 0]
    losses = [r for r in r_list if r < 0]
    if not wins or not losses:
        return None
    p = len(wins) / len(r_list)
    b = float(np.mean(wins)) / abs(float(np.mean(losses)))
    q = 1.0 - p
    return p, b, q


class FractionalKellySizer(Sizer):
    """
    Classic Kelly × mult. Uses a rolling `window` of realised R-multiples
    to estimate p (win rate) and b (avg_win / avg_loss).

        f = mult × (p·b − q) / b      (interpreted as fraction of equity)

    mult = 1.0 → full Kelly (aggressive, known to blow up on estimation error)
    mult = 0.5 → half Kelly (Thorp's recommended default)
    mult = 0.25 → quarter Kelly (conservative)
    """
    def __init__(self, mult: float, base_f: float = 0.001, window: int = 40,
                 min_n: int = 10, name: Optional[str] = None):
        super().__init__(name or f"Kelly_{mult:.2f}x")
        self.mult = mult
        self.base_f = base_f
        self.window = window
        self.min_n = min_n
    def size(self, hist: History, trade: TradeMeta) -> float:
        all_r = [t.realised_R for t in hist.closed[-self.window:]]
        pb = _kelly_from_history(all_r, min_n=self.min_n)
        if pb is None:
            return _clip(self.base_f)  # warmup
        p, b, q = pb
        kelly = (p * b - q) / b
        if kelly <= 0:
            return MIN_F  # no edge
        return _clip(self.mult * kelly)


class BayesianKellySizer(Sizer):
    """
    Bayesian Kelly with Beta posterior on win-rate and CI-floor.

        p | data ~ Beta(α₀ + wins, β₀ + losses)
        p_lower = quantile(p, ci_level)   # 10th percentile default
        kelly   = (p_lower · b − q) / b   with q = 1 − p_lower
        f       = mult × kelly

    The CI-floor shrinks size when sample is small (wide posterior), so
    warm-up never sizes large. A known remedy for Kelly's fragility.

    Reference: MacLean-Thorp-Ziemba "The Kelly Capital Growth Investment
    Criterion" (2011), Chapter 33.
    """
    def __init__(self, prior_a: float = 2.0, prior_b: float = 2.0,
                 ci_level: float = 0.10, mult: float = 0.5,
                 base_f: float = 0.001, window: int = 40,
                 name: Optional[str] = None):
        super().__init__(name or f"BayesKelly_ci{int(ci_level*100)}_m{mult:.2f}")
        self.prior_a = prior_a
        self.prior_b = prior_b
        self.ci_level = ci_level
        self.mult = mult
        self.base_f = base_f
        self.window = window
    def size(self, hist: History, trade: TradeMeta) -> float:
        try:
            from scipy.stats import beta as _beta
        except ImportError:
            # Graceful fallback: use point estimate
            return FractionalKellySizer(self.mult, self.base_f, self.window).size(hist, trade)
        all_r = [t.realised_R for t in hist.closed[-self.window:]]
        if len(all_r) < 5:
            return _clip(self.base_f)
        wins = sum(1 for r in all_r if r > 0)
        losses = len(all_r) - wins
        a = self.prior_a + wins
        b_post = self.prior_b + losses
        p_lower = float(_beta.ppf(self.ci_level, a, b_post))
        win_r = [r for r in all_r if r > 0]
        loss_r = [r for r in all_r if r < 0]
        if not win_r or not loss_r:
            return _clip(self.base_f)
        b_ratio = float(np.mean(win_r)) / abs(float(np.mean(loss_r)))
        q = 1.0 - p_lower
        kelly = (p_lower * b_ratio - q) / b_ratio
        if kelly <= 0:
            return MIN_F
        return _clip(self.mult * kelly)


# =====================================================================
# Merton-GZ family (wrappers around production sizer)
# =====================================================================
class MertonGZWrapper(Sizer):
    """
    Wraps the production MertonGZSizer at a specific γ (risk-aversion).
    γ < 2 = less conservative (larger sizes in good regimes).
    γ > 2 = more conservative.
    """
    def __init__(self, gamma: float = 2.0, base_f: float = 0.0011,
                 cap_mult: float = 3.0, dd_cap: float = 0.04,
                 name: Optional[str] = None):
        super().__init__(name or f"MertonGZ_g{gamma:.1f}")
        self.cfg = MertonGZSizerConfig(
            base_risk_pct=base_f,
            cap_mult=cap_mult,
            gamma=gamma,
            ewma_alpha=0.20,
            warmup_trades=15,
            dd_cap_pct=dd_cap,
            pool_symbols=True,
            no_edge_multiplier=1.0,
        )
        self._sizer = MertonGZSizer(self.cfg)
    def reset(self) -> None:
        self._sizer.reset()
    def size(self, hist: History, trade: TradeMeta) -> float:
        f = self._sizer.compute_risk_pct(
            trade.symbol, hist.equity, hist.peak, []
        )
        return _clip(f)
    def on_closed(self, trade: TradeMeta, realised_R: float) -> None:
        self._sizer.on_trade_closed(trade.symbol, realised_R)


# =====================================================================
# GARCH-Merton: replaces rolling var with GARCH(1,1) forecast
# =====================================================================
class GARCHMertonSizer(Sizer):
    """
    GARCH(1,1) forecast of next-trade variance, then Merton f* = μ̂/(γ·σ̂²).

        σ²_{t+1} = ω + α·ε²_t + β·σ²_t     (Bollerslev 1986)
        ε_t = R_t − μ̂
        ω = (1 − α − β) · sample_var      (reparameterised so E[σ²]=sample_var)

    This reacts faster to vol clustering than rolling σ and is the standard
    tool in quantitative finance for vol forecasting.
    """
    def __init__(self, gamma: float = 2.0, base_f: float = 0.001,
                 window: int = 40, alpha: float = 0.10, beta: float = 0.85,
                 name: Optional[str] = None):
        super().__init__(name or f"GARCHMerton_g{gamma:.1f}")
        self.gamma = gamma
        self.base_f = base_f
        self.window = window
        self.alpha = alpha
        self.beta = beta
        if self.alpha + self.beta >= 1.0:
            raise ValueError("GARCH stationarity requires α + β < 1")
    def size(self, hist: History, trade: TradeMeta) -> float:
        all_r = [t.realised_R for t in hist.closed[-self.window:]]
        if len(all_r) < 10:
            return _clip(self.base_f)
        r_arr = np.array(all_r, dtype=float)
        mu_hat = float(r_arr.mean())
        sample_var = float(r_arr.var())
        if sample_var <= 0:
            return MIN_F
        omega_eff = (1.0 - self.alpha - self.beta) * sample_var
        # Roll GARCH forward bar-by-bar from the start of the window
        sigma2 = sample_var
        for r in all_r:
            eps = r - mu_hat
            sigma2 = omega_eff + self.alpha * eps * eps + self.beta * sigma2
            sigma2 = max(sigma2, 1e-6)
        # σ²_{t+1} is now our forecast for the NEXT trade
        if mu_hat <= 0:
            return MIN_F
        f_star = mu_hat / (self.gamma * sigma2)
        return _clip(f_star)


# =====================================================================
# Pure Grossman-Zhou barrier (no Merton core)
# =====================================================================
class GrossmanZhouSizer(Sizer):
    """
    Pure closed-form drawdown-aware sizing.

        f(t) = f_base · max(0, 1 − DD(t)/DD_cap)^η

    At DD=0: f = f_base. At DD=DD_cap: f = 0 (exit the market).
    η = γ/(γ−1) with γ > 1 gives the log-utility solution (Grossman-Zhou 1993).
    Larger η → more aggressive barrier (sharper cut as DD grows).
    """
    def __init__(self, base_f: float = 0.0015, dd_cap: float = 0.04,
                 eta: float = 2.0, name: Optional[str] = None):
        super().__init__(name or f"GZ_eta{eta:.1f}_b{base_f*100:.2f}pct")
        self.base_f = base_f
        self.dd_cap = dd_cap
        self.eta = eta
    def size(self, hist: History, trade: TradeMeta) -> float:
        dd = hist.dd_pct()
        if dd >= self.dd_cap:
            return MIN_F
        barrier = max(0.0, 1.0 - dd / self.dd_cap) ** self.eta
        return _clip(self.base_f * barrier)


# =====================================================================
# Vince Optimal f
# =====================================================================
class VinceOptimalFSizer(Sizer):
    """
    Numerical maximiser of geometric HPR:
        f* = argmax Σ log(1 + f · R_i)
    over the empirical R-distribution (rolling window).

    Ralph Vince (1990) showed this is equivalent to maximising terminal
    capital under certainty of the future R-distribution. In practice
    the *estimated* f* is aggressive → we use `fraction_of_optimal` as
    a Thorp-style shrinkage (default 0.20 = 1/5 of Vince's optimum).

    Search space: f ∈ [0.0005, 0.05]. If any R in the window would make
    1 + f·R ≤ 0 for f in the search, we cap f below that point.
    """
    def __init__(self, fraction_of_optimal: float = 0.2, base_f: float = 0.001,
                 window: int = 40, name: Optional[str] = None):
        super().__init__(name or f"Vince_{int(fraction_of_optimal*100)}pct_of_f*")
        self.fraction = fraction_of_optimal
        self.base_f = base_f
        self.window = window
    def size(self, hist: History, trade: TradeMeta) -> float:
        all_r = [t.realised_R for t in hist.closed[-self.window:]]
        if len(all_r) < 20:
            return _clip(self.base_f)
        r_arr = np.array(all_r, dtype=float)
        worst = float(r_arr.min())
        # Largest safe f: ensures 1 + f·R > 0 for all R
        f_max = 0.999 / abs(worst) if worst < 0 else 1.0
        f_max = min(f_max, 0.05)
        grid = np.linspace(0.0005, f_max, 200)
        best_f, best_gm = 1e-6, -1e99
        for f in grid:
            h = np.log1p(f * r_arr)
            if not np.all(np.isfinite(h)):
                break
            gm = h.sum()
            if gm > best_gm:
                best_gm = gm
                best_f = float(f)
        return _clip(self.fraction * best_f)


# =====================================================================
# Van Tharp ATR-inverse (using R-std as ATR proxy per symbol)
# =====================================================================
class VanTharpInverseVolSizer(Sizer):
    """
    Target constant $-volatility per trade: size ∝ 1 / σ_R.

        f = base × clip(σ_target / σ_recent, min=0.33, max=3.0)

    σ_recent is the std of realised R over the last `window` trades in
    the SAME symbol. If the symbol has been choppy (high σ), size down.
    If it's been quiet (low σ), size up.

    (Note: this uses *realised-R* std as the vol proxy, not price-level
    ATR — they're equivalent up to constant scaling since R is already
    normalised per unit of initial risk.)
    """
    def __init__(self, base_f: float = 0.001, target_vol: float = 1.0,
                 window: int = 20, max_boost: float = 3.0,
                 min_cut: float = 0.33, name: Optional[str] = None):
        super().__init__(name or "VanTharp_InvVol")
        self.base_f = base_f
        self.target_vol = target_vol
        self.window = window
        self.max_boost = max_boost
        self.min_cut = min_cut
    def size(self, hist: History, trade: TradeMeta) -> float:
        all_r = [t.realised_R for t in hist.by_symbol.get(trade.symbol, [])[-self.window:]]
        if len(all_r) < 5:
            return _clip(self.base_f)
        sigma = float(np.std(all_r))
        if sigma <= 0:
            return _clip(self.base_f)
        mult = self.target_vol / sigma
        mult = max(self.min_cut, min(self.max_boost, mult))
        return _clip(self.base_f * mult)


# =====================================================================
# HMM regime-conditional
# =====================================================================
class HMMRegimeSizer(Sizer):
    """
    Scales risk by precomputed 2-state HMM trend probability.

        f = base × (low_mult + (high_mult − low_mult) × P(trend))

    trend_p_by_symbol_date: Dict[symbol, Dict[datetime.date, float]]
        where the float is P(state=trend | data up to and including that day).
    Missing dates default to P=0.5 (neutral).
    """
    def __init__(self, trend_p_by_symbol_date: Dict[str, Dict[Any, float]],
                 base_f: float = 0.001, low_mult: float = 0.5,
                 high_mult: float = 1.5, name: Optional[str] = None):
        super().__init__(name or f"HMMRegime_{low_mult:.1f}x-{high_mult:.1f}x")
        self.trend_p = trend_p_by_symbol_date
        self.base_f = base_f
        self.low_mult = low_mult
        self.high_mult = high_mult
    def size(self, hist: History, trade: TradeMeta) -> float:
        from datetime import datetime
        d = datetime.fromtimestamp(trade.entry_time).date()
        p = self.trend_p.get(trade.symbol, {}).get(d, 0.5)
        mult = self.low_mult + (self.high_mult - self.low_mult) * p
        return _clip(self.base_f * mult)


# =====================================================================
# CPPI (Constant Proportion Portfolio Insurance)
# =====================================================================
class CPPISizer(Sizer):
    """
    Black-Jones (1987) CPPI cushion sizing:

        floor = floor_pct × start_equity
        cushion = max(0, equity − floor)
        f = (m × cushion / equity) · base_f_per_cushion_unit

    As DD approaches floor, f → 0 automatically. As equity grows,
    cushion grows → more risk per trade.

    m = 3 is Black-Jones's canonical multiplier.
    floor_pct = 0.96 → floor at 4 % loss (our DD cap).
    """
    def __init__(self, floor_pct: float = 0.96, multiplier: float = 3.0,
                 base_f: float = 0.001, name: Optional[str] = None):
        super().__init__(name or f"CPPI_m{multiplier:.1f}_f{int((1-floor_pct)*100)}pct")
        self.floor_pct = floor_pct
        self.multiplier = multiplier
        self.base_f = base_f
    def size(self, hist: History, trade: TradeMeta) -> float:
        floor = self.floor_pct * hist.start_equity
        cushion = max(0.0, hist.equity - floor)
        if hist.equity <= 0:
            return MIN_F
        cushion_frac = cushion / hist.equity
        # cushion_frac ∈ [0, 1−floor_pct]; scale so full cushion → base_f × m
        max_cushion = 1.0 - self.floor_pct
        if max_cushion <= 0:
            return MIN_F
        f = self.base_f * self.multiplier * (cushion_frac / max_cushion)
        return _clip(f)


# =====================================================================
# Ensemble (arithmetic mean of N sizers with weights)
# =====================================================================
class EnsembleSizer(Sizer):
    """
    Weighted mean of N sizers. Typically used with the top-3 sizers from
    an IS leaderboard, equally weighted, evaluated on OOS.

    This provides diversification across sizer philosophies: e.g.
    (Kelly + Merton + GZ) averages a profit-seeker, a regime-adapter,
    and a DD-protector.
    """
    def __init__(self, sizers: List[Sizer], weights: Optional[List[float]] = None,
                 name: Optional[str] = None):
        if weights is None:
            weights = [1.0] * len(sizers)
        if len(weights) != len(sizers):
            raise ValueError("sizers and weights must be same length")
        w = np.array(weights, dtype=float)
        w /= w.sum()
        nm = name or f"Ensemble_{'+'.join(s.name.split('_')[0] for s in sizers[:3])}"
        super().__init__(nm)
        self.sizers = sizers
        self.weights = w.tolist()
    def reset(self) -> None:
        for s in self.sizers:
            s.reset()
    def size(self, hist: History, trade: TradeMeta) -> float:
        fs = [s.size(hist, trade) for s in self.sizers]
        avg = float(np.dot(fs, self.weights))
        return _clip(avg)
    def on_closed(self, trade: TradeMeta, realised_R: float) -> None:
        for s in self.sizers:
            s.on_closed(trade, realised_R)


# =====================================================================
# Registry — the full zoo for the shootout
# =====================================================================
def build_zoo(trend_p_by_symbol_date: Optional[Dict[str, Dict[Any, float]]] = None
              ) -> List[Sizer]:
    """Returns the full set of sizers to test in the shootout."""
    sizers: List[Sizer] = []

    # S01-S04: Flat controls
    for pct in (0.0005, 0.0010, 0.0015, 0.0020):
        sizers.append(FlatSizer(pct))

    # S05-S07: Kelly family
    sizers.append(FractionalKellySizer(mult=0.50, name="Kelly_Half"))
    sizers.append(FractionalKellySizer(mult=0.25, name="Kelly_Quarter"))
    sizers.append(BayesianKellySizer(ci_level=0.10, mult=0.50, name="BayesKelly_half_ci10"))

    # S08-S10: Merton-GZ γ sweep
    for g in (1.5, 2.0, 3.0):
        sizers.append(MertonGZWrapper(gamma=g, base_f=0.0011))

    # S11: GARCH-Merton
    sizers.append(GARCHMertonSizer(gamma=2.0, base_f=0.001))

    # S12-S13: Grossman-Zhou barrier (two η variants)
    sizers.append(GrossmanZhouSizer(base_f=0.0015, dd_cap=0.04, eta=2.0))
    sizers.append(GrossmanZhouSizer(base_f=0.0015, dd_cap=0.04, eta=3.0))

    # S14: Vince
    sizers.append(VinceOptimalFSizer(fraction_of_optimal=0.20))

    # S15: Van Tharp inverse-vol
    sizers.append(VanTharpInverseVolSizer(base_f=0.0012))

    # S16: HMM-regime (only if we have HMM data)
    if trend_p_by_symbol_date:
        sizers.append(HMMRegimeSizer(
            trend_p_by_symbol_date=trend_p_by_symbol_date,
            base_f=0.0012, low_mult=0.5, high_mult=1.8,
            name="HMMRegime_0.5-1.8x",
        ))

    # S17: CPPI
    sizers.append(CPPISizer(floor_pct=0.96, multiplier=3.0, base_f=0.001))

    # (S18 Ensemble is built after the first IS pass — see shootout script)
    return sizers


__all__ = [
    "HARD_CAP_F", "MIN_F", "TradeMeta", "History", "Sizer",
    "FlatSizer", "FractionalKellySizer", "BayesianKellySizer",
    "MertonGZWrapper", "GARCHMertonSizer", "GrossmanZhouSizer",
    "VinceOptimalFSizer", "VanTharpInverseVolSizer", "HMMRegimeSizer",
    "CPPISizer", "EnsembleSizer", "build_zoo",
]
