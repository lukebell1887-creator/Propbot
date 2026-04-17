"""
GpdTail — peaks-over-threshold Generalised Pareto Distribution estimator.

Given a sample of standardised returns z_t, we:
  1. Pick threshold u = 95th percentile of |z|.
  2. Fit GPD(ξ, β) to the excesses {|z| − u : |z| > u} by MLE.
  3. Provide α-quantile evaluation for tail risk:

        q_α = u + (β/ξ) · [ ((1 − α) · N / N_u)^(-ξ) − 1 ]     ξ ≠ 0
        q_α = u − β · log((1 − α) · N / N_u)                    ξ = 0

(McNeil, Frey, Embrechts 2015, *Quantitative Risk Management* Ch. 7.
 Pickands 1975, *Annals of Statistics* 3(1)).

The masterplan (§5.5) uses q_{α=0.005} · σ̂ as the entry stop distance and
q_{α=0.10} · σ̂ as the trail.
"""

from __future__ import annotations
import math
import numpy as np


def _gpd_neg_log_lik(params: np.ndarray, x: np.ndarray) -> float:
    xi, beta = params
    if beta <= 0:
        return 1e20
    if xi < -0.5:
        return 1e20
    n = x.size
    if abs(xi) < 1e-8:
        return float(n * math.log(beta) + x.sum() / beta)
    arg = 1.0 + xi * x / beta
    if np.any(arg <= 0):
        return 1e20
    return float(n * math.log(beta) + (1.0 + 1.0 / xi) * np.sum(np.log(arg)))


def _fit_gpd(excesses: np.ndarray,
             init: tuple[float, float] = (0.2, 1.0)
             ) -> tuple[float, float]:
    if excesses.size < 10:
        return (0.2, float(excesses.mean() if excesses.size else 1.0))

    best = np.array(init, dtype=float)
    best_nll = _gpd_neg_log_lik(best, excesses)
    step = np.array([0.1, 0.5])
    for _ in range(10):
        improved = False
        for i in range(2):
            for s in (+1.0, -1.0):
                cand = best.copy()
                cand[i] += s * step[i]
                if i == 1 and cand[1] <= 0:
                    continue
                nll = _gpd_neg_log_lik(cand, excesses)
                if nll < best_nll - 1e-8:
                    best_nll = nll
                    best = cand
                    improved = True
        if not improved:
            step *= 0.5
    return float(best[0]), float(best[1])


class GpdTail:
    """
    Online peaks-over-threshold GPD estimator.  Refit every `refit_every`
    updates against the current buffer (last `window` values).
    """

    __slots__ = (
        "_window", "_refit_every", "_threshold_pct",
        "_buf", "_xi", "_beta", "_u",
        "_n_total", "_n_excess",
        "_bars_since_fit",
    )

    def __init__(self,
                 window: int = 2000,
                 refit_every: int = 500,
                 threshold_pct: float = 95.0):
        if not 50.0 <= threshold_pct < 100.0:
            raise ValueError("threshold_pct should be in [50, 100)")
        self._window = window
        self._refit_every = refit_every
        self._threshold_pct = threshold_pct
        self._buf: list[float] = []
        self._xi = 0.2
        self._beta = 1.0
        self._u = 1.64           # default 95th %ile of |N(0,1)|
        self._n_total = 0
        self._n_excess = 0
        self._bars_since_fit = 0

    # ------------------------------------------------------------------
    def update(self, z: float) -> None:
        """Feed one standardised residual (signed)."""
        self._buf.append(abs(float(z)))
        if len(self._buf) > self._window:
            self._buf.pop(0)
        self._bars_since_fit += 1
        if (self._bars_since_fit >= self._refit_every
                and len(self._buf) >= 500):
            self._refit()
            self._bars_since_fit = 0

    def _refit(self) -> None:
        arr = np.asarray(self._buf, dtype=float)
        u = float(np.percentile(arr, self._threshold_pct))
        excesses = arr[arr > u] - u
        self._n_total = arr.size
        self._n_excess = excesses.size
        if excesses.size >= 30:
            xi, beta = _fit_gpd(excesses, (self._xi, max(self._beta, 0.1)))
            self._xi = xi
            self._beta = beta
            self._u = u

    # ------------------------------------------------------------------
    def quantile(self, alpha: float) -> float:
        """
        Right-tail quantile of |z|:  P(|z| > q_α) = α.

        Uses the semi-parametric POT estimator (McNeil et al. §7.2.3):

            q_α = u + (β/ξ) · [ (α N / N_u)^(-ξ) − 1 ]
        """
        if not 0.0 < alpha < 1.0:
            raise ValueError("alpha in (0,1)")
        # Fall back to empirical quantile until a fit is available
        if self._n_excess < 30:
            if not self._buf:
                return 1.64   # normal default
            return float(np.percentile(np.asarray(self._buf), 100.0 * (1.0 - alpha)))
        N = max(1, self._n_total)
        Nu = max(1, self._n_excess)
        if abs(self._xi) < 1e-8:
            return self._u - self._beta * math.log(alpha * N / Nu)
        val = (alpha * N / Nu) ** (-self._xi)
        return self._u + (self._beta / self._xi) * (val - 1.0)

    # ------------------------------------------------------------------
    @property
    def xi(self) -> float: return self._xi
    @property
    def beta_param(self) -> float: return self._beta
    @property
    def u(self) -> float: return self._u
    @property
    def n_excess(self) -> int: return self._n_excess

    def reset(self) -> None:
        self._buf.clear()
        self._xi = 0.2
        self._beta = 1.0
        self._u = 1.64
        self._n_total = 0
        self._n_excess = 0
        self._bars_since_fit = 0
