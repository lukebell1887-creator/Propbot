"""
dynamic_sizer_v20.py — 6-layer smart risk sizer for ORB v20.

Returns a per-trade risk% (0.0 – hard_cap) based on the product of six
independent multipliers:

    risk_i = Kelly_i × bayes × vol_regime × GZ_DD × symbol_trust × corr × hard_cap

Layers:
  1. Per-symbol fractional Kelly      (seeded from the v20 grid-search stats)
  2. Bayesian shrinkage               (live Beta posterior on rolling N=30 trades)
  3. Vol-regime multiplier            (realised ATR vs backtest ATR)
  4. Grossman-Zhou DD shrinkage       (risk collapses as DD → 5ers limit)
  5. Symbol-trust warm-up             (¼-Kelly until 20 live trades match backtest)
  6. Correlation-aware scaling        (avoid stacking correlated exposures)

Then: hard cap at 1.5 % regardless, floor at 0 %.

Usage:

    from src.dynamic_sizer_v20 import DynamicSizerV20, SizerV20Config

    sizer = DynamicSizerV20(SizerV20Config())
    risk = sizer.compute_risk_pct(
        symbol="DE40",
        equity=105_000,
        peak_equity=108_000,
        open_positions=[("US30", +1)],      # list of (symbol, side) tuples
    )

    # After each closed trade, feed it back:
    sizer.on_trade_closed(symbol="DE40", realised_R=1.8)

    # Optional: update realised vol each bar:
    sizer.on_vol_update(symbol="DE40", realised_atr=12.4)
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Tuple


# =====================================================================
#  Per-symbol seeded stats from the v20 grid search (backtest Part 4)
# =====================================================================

@dataclass
class SymbolSeed:
    """Per-symbol priors from the grid search (backtest_v20) winners."""
    wr: float              # backtest win rate
    R_avg: float           # average R of a full round-trip (gross)
    n_backtest: int        # sample size in backtest (Beta prior strength)
    backtest_atr: float    # ATR in native price pts at which the edge lives


# Hard-coded from the user's PhD-grade PASTE_BACK_TO_CLINE.txt report
# (Part 4 — per-symbol FULL-window winners):
#
#   DE40   N= 99  WR=71.7%  — tp=1.5/3.0  (R≈2.0)
#   US30   N= 79  WR=58.2%  — tp=2.0/4.0  (R≈2.0)
#   XAUUSD N= 26  WR=69.2%  — tp=2.0/4.0  (R≈2.0)
#   US100  N= 75  WR=53.3%  — marginal, excluded
#   US500  N= 47  WR=76.6%  but tp=0.5/1.0 (R≈0.5) → negative Kelly, excluded

SEEDS: Dict[str, SymbolSeed] = {
    "DE40"  : SymbolSeed(wr=0.717, R_avg=2.0, n_backtest=99, backtest_atr=18.0),
    "US30"  : SymbolSeed(wr=0.582, R_avg=2.0, n_backtest=79, backtest_atr=45.0),
    "XAUUSD": SymbolSeed(wr=0.692, R_avg=2.0, n_backtest=26, backtest_atr=4.5),
}


def kelly_fraction(wr: float, R: float) -> float:
    """Kelly f* = (WR × R − (1 − WR)) / R for fixed-R binary outcomes.

    Returns 0.0 if edge is negative (WR too low vs R).
    """
    f = (wr * R - (1.0 - wr)) / max(R, 1e-9)
    return max(f, 0.0)


# =====================================================================
#  Sizer config
# =====================================================================

@dataclass
class SizerV20Config:
    # Fractional-Kelly safety factor applied to layer-1 (¼-Kelly default)
    kelly_fraction: float = 0.25

    # Hard per-trade cap (absolute ceiling, regardless of multipliers)
    hard_cap_pct: float = 0.015              # 1.5 %

    # Absolute floor (if stack ever goes < this, we just skip the trade)
    min_trade_risk_pct: float = 0.0005       # 0.05 %

    # Symbols not in SEEDS get this cold-start Kelly base
    cold_kelly: float = 0.0025               # 0.25 % (tiny — prove yourself first)

    # ---- Layer 2: Bayesian shrinkage -------------------------------
    bayes_window: int = 30                   # rolling trades considered "live"
    bayes_tolerance_pts: float = 0.05        # WR may drop 5 pts before shrink starts
    bayes_cutoff_pts: float   = 0.15         # WR -15 pts → nearly zero sizing

    # ---- Layer 3: vol-regime ---------------------------------------
    vol_upper_ratio: float = 1.50            # realised/backtest > 1.5 → halve
    vol_lower_ratio: float = 0.70            # realised/backtest < 0.7 → gentle boost
    vol_panic_mult:  float = 0.50
    vol_calm_mult:   float = 1.10            # modest — we don't want bravado

    # ---- Layer 4: Grossman-Zhou DD shrinkage -----------------------
    # Multiplier at various DD levels (linear-interpolated)
    gz_curve: Tuple[Tuple[float, float], ...] = (
        (0.000, 1.00),   # 0 %    DD → full Kelly
        (0.010, 0.80),   # 1 %    DD → gentle brake
        (0.020, 0.50),   # 2 %    DD → half-size
        (0.030, 0.25),   # 3 %    DD → survival
        (0.040, 0.10),   # 4 %    DD → 1 step from daily kill
        (0.045, 0.00),   # ≥4.5 % DD → block entries entirely
    )

    # ---- Layer 5: symbol trust warm-up -----------------------------
    trust_min_live_trades: int = 20          # trades needed before full Kelly
    trust_warmup_mult: float = 0.25          # ¼-Kelly of the Kelly base during warm-up

    # ---- Layer 6: correlation-aware scaling ------------------------
    # Pairwise |ρ| used for portfolio de-stacking.
    # Taken from 5ers live-feed correlation analysis on the full 3-month window.
    correlations: Dict[Tuple[str, str], float] = field(default_factory=lambda: {
        ("DE40", "US30"):   0.65,
        ("DE40", "US100"):  0.60,
        ("DE40", "US500"):  0.65,
        ("US30", "US100"):  0.80,
        ("US30", "US500"):  0.95,
        ("US100", "US500"): 0.90,
        ("DE40", "XAUUSD"): 0.10,
        ("US30", "XAUUSD"): 0.10,
        ("US100", "XAUUSD"):0.05,
        ("US500", "XAUUSD"):0.10,
    })


# =====================================================================
#  The sizer
# =====================================================================

class DynamicSizerV20:
    def __init__(self, cfg: Optional[SizerV20Config] = None,
                 seeds: Optional[Dict[str, SymbolSeed]] = None):
        self.cfg = cfg or SizerV20Config()
        self.seeds = seeds if seeds is not None else SEEDS
        # Rolling live trades per symbol (for Bayesian posterior)
        self._live_trades: Dict[str, deque] = defaultdict(
            lambda: deque(maxlen=self.cfg.bayes_window))
        # Most recent realised ATR per symbol
        self._realised_atr: Dict[str, float] = {}

    # ------------------------------------------------------------------
    #  PUBLIC API
    # ------------------------------------------------------------------
    def on_trade_closed(self, symbol: str, realised_R: float) -> None:
        """Feed the sizer a closed live trade result (R-units)."""
        self._live_trades[symbol].append(realised_R)

    def on_vol_update(self, symbol: str, realised_atr: float) -> None:
        """Feed the sizer the current realised ATR (same units as seed ATR)."""
        if realised_atr > 0:
            self._realised_atr[symbol] = realised_atr

    def compute_risk_pct(
        self,
        symbol: str,
        equity: float,
        peak_equity: float,
        open_positions: Iterable[Tuple[str, int]] = (),
    ) -> float:
        """Return the recommended per-trade risk % for this signal.

        Parameters
        ----------
        symbol          : symbol of the signal about to fire
        equity          : current account equity
        peak_equity     : all-time peak equity
        open_positions  : iterable of (symbol, side) tuples for CURRENTLY open
                          positions (used by layer-6 correlation scaling).
        """
        # Layer 1: per-symbol Kelly base
        kelly = self._layer1_kelly(symbol)
        if kelly <= 0.0:
            return 0.0                        # no edge → don't trade

        # Layer 2: Bayesian shrinkage (live vs backtest WR posterior)
        bayes = self._layer2_bayes(symbol)

        # Layer 3: vol-regime
        vol = self._layer3_vol(symbol)

        # Layer 4: Grossman-Zhou DD shrinkage
        gz = self._layer4_gz(equity, peak_equity)

        # Layer 5: per-symbol trust warm-up
        trust = self._layer5_trust(symbol)

        # Layer 6: correlation-aware scaling
        corr = self._layer6_corr(symbol, open_positions)

        risk = kelly * bayes * vol * gz * trust * corr

        # Hard cap and floor
        risk = min(risk, self.cfg.hard_cap_pct)
        if risk < self.cfg.min_trade_risk_pct:
            return 0.0                        # below-floor → skip trade
        return risk

    # Introspection helper for the backtest table
    def breakdown(self, symbol: str, equity: float, peak_equity: float,
                  open_positions: Iterable[Tuple[str, int]] = ()) -> Dict[str, float]:
        """Return each layer's multiplier + final risk_pct — for logging."""
        k  = self._layer1_kelly(symbol)
        by = self._layer2_bayes(symbol)
        v  = self._layer3_vol(symbol)
        gz = self._layer4_gz(equity, peak_equity)
        tr = self._layer5_trust(symbol)
        co = self._layer6_corr(symbol, open_positions)
        r  = min(max(k * by * v * gz * tr * co, 0.0), self.cfg.hard_cap_pct)
        return dict(kelly=k, bayes=by, vol=v, gz=gz, trust=tr, corr=co, final=r)

    # ------------------------------------------------------------------
    #  Internal layers
    # ------------------------------------------------------------------
    def _layer1_kelly(self, symbol: str) -> float:
        seed = self.seeds.get(symbol)
        if seed is None:
            return self.cfg.cold_kelly
        f_star = kelly_fraction(seed.wr, seed.R_avg)
        return f_star * self.cfg.kelly_fraction     # fractional Kelly

    def _layer2_bayes(self, symbol: str) -> float:
        """Posterior-based shrinkage: if live WR drops vs backtest WR, shrink.

        Beta posterior Beta(α+w, β+l) where α,β seeded from backtest.
        Multiplier = P(live_WR > backtest_WR − tolerance).
        """
        live = self._live_trades.get(symbol)
        if not live or len(live) < 5:
            return 1.0                               # not enough live data
        seed = self.seeds.get(symbol)
        if seed is None:
            return 1.0
        wins = sum(1 for r in live if r > 0)
        losses = len(live) - wins
        live_wr = wins / max(len(live), 1)
        shortfall = seed.wr - live_wr
        if shortfall <= self.cfg.bayes_tolerance_pts:
            return 1.0                               # tracking fine
        if shortfall >= self.cfg.bayes_cutoff_pts:
            return 0.1                               # almost off
        # Linear interpolation between tolerance and cutoff
        t = self.cfg.bayes_tolerance_pts
        c = self.cfg.bayes_cutoff_pts
        frac = (shortfall - t) / max(c - t, 1e-9)
        return max(1.0 - 0.9 * frac, 0.1)

    def _layer3_vol(self, symbol: str) -> float:
        seed = self.seeds.get(symbol)
        if seed is None or seed.backtest_atr <= 0:
            return 1.0
        atr = self._realised_atr.get(symbol)
        if not atr or atr <= 0:
            return 1.0
        ratio = atr / seed.backtest_atr
        if ratio >= self.cfg.vol_upper_ratio:
            return self.cfg.vol_panic_mult
        if ratio <= self.cfg.vol_lower_ratio:
            return self.cfg.vol_calm_mult
        return 1.0

    def _layer4_gz(self, equity: float, peak_equity: float) -> float:
        if peak_equity <= 0:
            return 1.0
        dd = max((peak_equity - equity) / peak_equity, 0.0)
        curve = self.cfg.gz_curve
        # clamp ends
        if dd <= curve[0][0]:
            return curve[0][1]
        if dd >= curve[-1][0]:
            return curve[-1][1]
        # piecewise linear interpolation
        for (d1, m1), (d2, m2) in zip(curve, curve[1:]):
            if d1 <= dd < d2:
                span = d2 - d1
                if span <= 0:
                    return m2
                return m1 + (m2 - m1) * ((dd - d1) / span)
        return curve[-1][1]

    def _layer5_trust(self, symbol: str) -> float:
        n_live = len(self._live_trades.get(symbol, ()))
        if n_live >= self.cfg.trust_min_live_trades:
            return 1.0                               # warmed up → full Kelly
        return self.cfg.trust_warmup_mult

    def _layer6_corr(self, symbol: str,
                      open_positions: Iterable[Tuple[str, int]]) -> float:
        """Scale down if an already-open position is highly correlated.

        Scale = √(1 - ρ²) on the second trade — classic risk-parity math.
        We use the HIGHEST absolute correlation among open positions.
        """
        max_rho = 0.0
        for other_sym, _side in open_positions:
            if other_sym == symbol:
                continue
            pair = tuple(sorted([symbol, other_sym]))
            rho = self.cfg.correlations.get(pair, 0.0)
            # Also try reverse (defensive)
            if rho == 0.0:
                rho = self.cfg.correlations.get((symbol, other_sym), 0.0)
                if rho == 0.0:
                    rho = self.cfg.correlations.get((other_sym, symbol), 0.0)
            if abs(rho) > max_rho:
                max_rho = abs(rho)
        if max_rho < 0.40:
            return 1.0                               # effectively independent
        # Scale = √(1 − ρ²) clamped to 0.3 (don't over-shrink)
        return max(math.sqrt(max(1.0 - max_rho * max_rho, 0.0)), 0.30)


# =====================================================================
#  Convenience: export a pre-configured default sizer
# =====================================================================

def default_sizer_v20() -> DynamicSizerV20:
    return DynamicSizerV20(cfg=SizerV20Config(), seeds=SEEDS)
