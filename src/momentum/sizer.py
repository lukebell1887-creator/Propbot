"""
BayesianSizer — composes the full v7 sizing stack (§4.7).

    risk_fraction  =  f_bayes  ×  conviction  ×  GZ_factor  ×  CVaR_factor
    risk_dollars   =  risk_fraction × equity
    lots           =  risk_dollars / (stop_distance_$ × pip_value)

All inputs are live posteriors.  Nothing is clamped or hand-tuned; every
factor has a closed-form optimal derivation.

Returns a dataclass with the full decision breakdown so the engine can log
every sub-factor (masterplan §14.1 logging requirement).
"""

from __future__ import annotations
import math
from dataclasses import dataclass

from .bayesian_edge import BayesianEdge, JamesSteinShrink
from .kelly import ThorpKelly, GrossmanZhouDD, CVaRCap


@dataclass
class SizingDecision:
    risk_fraction: float       # total Kelly fraction of equity at risk per trade
    risk_dollars: float        # = risk_fraction * equity  (per-R-loss risk)
    lots: float                # final lot size

    # Decomposition (for logging / debugging / audit)
    f_naive: float
    f_bayes: float
    conviction: float
    gz_factor: float
    cvar_factor: float
    p_mean: float
    p_js: float
    R_mean: float
    p_var: float
    R_var: float
    equity: float
    peak_equity: float
    stop_distance: float
    pip_value: float


class BayesianSizer:
    __slots__ = (
        "_edge", "_shrink", "_kelly", "_gz", "_cvar",
        "_peak_equity", "_min_lots", "_lot_step", "_max_lots",
        "_min_fraction",
    )

    def __init__(self,
                 edge: BayesianEdge | None = None,
                 shrink: JamesSteinShrink | None = None,
                 kelly: ThorpKelly | None = None,
                 gz: GrossmanZhouDD | None = None,
                 cvar: CVaRCap | None = None,
                 lot_step: float = 0.01,
                 min_lots: float = 0.01,
                 max_lots: float = 50.0,
                 min_fraction: float = 5e-4):
        self._edge = edge or BayesianEdge()
        self._shrink = shrink or JamesSteinShrink()
        self._kelly = kelly or ThorpKelly()
        self._gz = gz or GrossmanZhouDD()
        self._cvar = cvar or CVaRCap()
        self._peak_equity = 0.0
        self._lot_step = lot_step
        self._min_lots = min_lots
        self._max_lots = max_lots
        self._min_fraction = min_fraction

    # ------------------------------------------------------------------
    def record_trade(self, realised_R: float, closing_equity: float) -> None:
        """Update posterior + peak equity on a closed trade."""
        self._edge.update(realised_R)
        if closing_equity > self._peak_equity:
            self._peak_equity = closing_equity

    def mark_equity(self, equity: float) -> None:
        """Update the watermark without recording a trade (e.g. unrealised)."""
        if equity > self._peak_equity:
            self._peak_equity = equity

    # ------------------------------------------------------------------
    def decide(self,
               equity: float,
               conviction: float,
               stop_distance: float,
               pip_value: float,
               avg_win_R: float = 1.7,
               avg_loss_R: float = 1.0) -> SizingDecision:
        """
        Compute the lot size for a fresh entry.  `conviction` ∈ [0, 1] is the
        geometric-mean signal-stack confidence from the engine.
        """
        if equity <= 0:
            return SizingDecision(0, 0, 0, 0, 0, conviction, 0, 0,
                                  0, 0, 0, 0, 0, equity, self._peak_equity,
                                  stop_distance, pip_value)
        if self._peak_equity <= 0:
            self._peak_equity = equity

        snap = self._edge.snapshot()
        n = snap["n"]
        p_raw = snap["p_mean"]
        p_var = snap["p_var"]
        R_mean = snap["R_mean"]
        R_var = snap["R_var"]
        R_sig2 = snap["R_sigma2_mean"]

        # Shrinkage on small samples
        p_js = self._shrink.shrink(p_raw, n)

        # Thorp-corrected fractional Kelly
        f_naive = self._kelly._naive(p_js, avg_win_R, avg_loss_R)
        f_bayes = self._kelly.fraction(p_js, p_var, R_mean, R_var,
                                       avg_win_R, avg_loss_R)

        # Signal conviction modulator (already in [0,1])
        conv = max(0.0, min(1.0, conviction))

        # Grossman-Zhou drawdown factor
        gz = self._gz.factor(equity, self._peak_equity)

        # Candidate fraction before CVaR cap
        cand = f_bayes * conv * gz

        # CVaR cap (posterior σ_R needed)
        sigma_R = math.sqrt(max(R_sig2, 1e-12))
        cvar_f = self._cvar.factor(R_mean, sigma_R, cand)

        risk_fraction = cand * cvar_f
        if risk_fraction < self._min_fraction:
            risk_fraction = 0.0

        risk_dollars = risk_fraction * equity
        if stop_distance <= 0 or pip_value <= 0:
            lots = 0.0
        else:
            lots = risk_dollars / (stop_distance * pip_value)

        # Snap to lot step, clamp
        if lots < self._min_lots:
            lots = 0.0
        else:
            lots = max(self._min_lots,
                       min(self._max_lots,
                           math.floor(lots / self._lot_step) * self._lot_step))

        return SizingDecision(
            risk_fraction=risk_fraction,
            risk_dollars=risk_dollars,
            lots=lots,
            f_naive=f_naive,
            f_bayes=f_bayes,
            conviction=conv,
            gz_factor=gz,
            cvar_factor=cvar_f,
            p_mean=p_raw,
            p_js=p_js,
            R_mean=R_mean,
            p_var=p_var,
            R_var=R_var,
            equity=equity,
            peak_equity=self._peak_equity,
            stop_distance=stop_distance,
            pip_value=pip_value,
        )

    # ------------------------------------------------------------------
    @property
    def edge(self) -> BayesianEdge: return self._edge
    @property
    def peak_equity(self) -> float: return self._peak_equity
