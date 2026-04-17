"""
BayesianEdge — online Bayesian posteriors over (p_win, μ_R, σ²_R).

Conjugate closed forms:

    p | trades      ~  Beta(α, β)
    (μ_R, σ²_R) | .  ~  Normal-Inverse-Gamma(m, κ, a, b)

Online updates are O(1).  See Gelman et al. *Bayesian Data Analysis* §3.3
(normal model, unknown mean and variance) for the NIG update equations.

JamesSteinShrink  — proven MSE-dominant small-sample shrinkage toward a
                    grand-mean prior (Stein 1956).  We use the scalar
                    version with a closed-form weight driven by sample size.
"""

from __future__ import annotations
from dataclasses import dataclass


@dataclass
class BetaPosterior:
    alpha: float
    beta: float

    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def var(self) -> float:
        s = self.alpha + self.beta
        return self.alpha * self.beta / (s * s * (s + 1.0))

    def update(self, win: bool) -> None:
        if win:
            self.alpha += 1.0
        else:
            self.beta += 1.0


@dataclass
class NIGPosterior:
    m: float      # posterior mean of μ
    kappa: float  # effective pseudo-count for mean
    a: float      # shape for σ² (gamma on 1/σ²)
    b: float      # rate for σ²

    def mean_mu(self) -> float:
        return self.m

    def var_mu(self) -> float:
        # marginal posterior variance of μ  =  b / [κ (a-1)]    for a>1
        if self.a <= 1.0:
            return float("inf")
        return self.b / (self.kappa * (self.a - 1.0))

    def mean_sigma2(self) -> float:
        if self.a <= 1.0:
            return float("inf")
        return self.b / (self.a - 1.0)

    def update(self, x: float) -> None:
        """
        Conjugate NIG update for a single new observation x ~ N(μ, σ²):

            κ' = κ + 1
            m' = (κ m + x) / (κ + 1)
            a' = a + 1/2
            b' = b + ½ κ (x - m)² / (κ + 1)
        """
        kappa1 = self.kappa + 1.0
        m1 = (self.kappa * self.m + x) / kappa1
        a1 = self.a + 0.5
        b1 = self.b + 0.5 * self.kappa * (x - self.m) ** 2 / kappa1
        self.m, self.kappa, self.a, self.b = m1, kappa1, a1, b1


class BayesianEdge:
    """
    Composed posterior over win probability and R-expectancy.

    Priors (weakly informative, masterplan §4.1):
        α₀ = β₀ = 5                       # Beta prior centered at 0.5
        m₀ = 0.25, κ₀ = 4, a₀ = 3, b₀ = 1 # NIG prior centered at +0.25R
    """

    __slots__ = ("p", "r", "n_trades")

    def __init__(self,
                 alpha0: float = 5.0, beta0: float = 5.0,
                 m0: float = 0.25, kappa0: float = 4.0,
                 a0: float = 3.0, b0: float = 1.0):
        self.p = BetaPosterior(alpha0, beta0)
        self.r = NIGPosterior(m0, kappa0, a0, b0)
        self.n_trades = 0

    def update(self, realised_R: float) -> None:
        """Feed one closed-trade R-multiple (positive or negative)."""
        self.n_trades += 1
        self.p.update(realised_R > 0)
        self.r.update(realised_R)

    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        return {
            "n": self.n_trades,
            "p_mean": self.p.mean(),
            "p_var": self.p.var(),
            "R_mean": self.r.mean_mu(),
            "R_var": self.r.var_mu(),
            "R_sigma2_mean": self.r.mean_sigma2(),
        }


class JamesSteinShrink:
    """
    Scalar James-Stein shrinkage toward a fixed grand mean.

    Given  x̂  and an effective sample size n, returns

        x̂_JS  =  (1 - w) x̂  +  w g
        w     =  1 / (1 + n/n₀)

    where n₀ controls the decay rate of the shrinkage (default 30 — matches
    the masterplan §4.2 formula).
    """

    def __init__(self, grand_mean: float = 0.52, n0: float = 30.0):
        self._g = grand_mean
        self._n0 = n0

    def shrink(self, x: float, n: int) -> float:
        w = 1.0 / (1.0 + n / self._n0)
        w = max(0.0, min(1.0, w))
        return (1.0 - w) * x + w * self._g
