"""
KalmanForecast — 1-D Kalman drift filter with online posterior variance.

Model (Kalman 1960; Harvey 1989):

    r_t   = μ_t + ε_t,           ε_t ~ N(0, σ_obs²)        (observation)
    μ_t   = μ_{t-1} + η_t,       η_t ~ N(0, σ_proc²)       (state walk)

Given info up to t, posterior over μ_t is N(μ̂_t, P_t).  Kalman is the
minimum-MSE linear estimator for this class (Kalman 1960, proof in Harvey 1989 §3).

Usage:
    kf = KalmanForecast(sigma_obs=0.0005, sigma_proc=1e-6)
    for r in returns:
        mu_hat, P = kf.update(r)
    signal = kf.signal(tau_k=1.96, p_crit=1e-6)   # +1 / 0 / -1

Confidence scalar (Φ(|μ̂| / √P)) is available via `kf.confidence()`.
"""

from __future__ import annotations
import math


class KalmanForecast:
    __slots__ = (
        "_sigma_obs2",
        "_sigma_proc2",
        "_mu",
        "_P",
        "_initialised",
    )

    def __init__(self, sigma_obs: float = 5e-4, sigma_proc: float = 1e-6,
                 mu0: float = 0.0, P0: float = 1e-3):
        """
        sigma_obs   observation noise std dev (per-bar return magnitude)
        sigma_proc  state-walk noise std dev (very small — drift changes slowly)
        mu0, P0     prior mean and variance of μ
        """
        self._sigma_obs2 = sigma_obs * sigma_obs
        self._sigma_proc2 = sigma_proc * sigma_proc
        self._mu = mu0
        self._P = P0
        self._initialised = False

    # ------------------------------------------------------------------
    #  Core Kalman recursion  (1-D closed form)
    # ------------------------------------------------------------------
    def update(self, r: float) -> tuple[float, float]:
        """
        One Kalman step.  Returns (μ̂_t, P_t).

        Predict  : μ_- = μ_{t-1},          P_- = P_{t-1} + σ²_proc
        Update   : K   = P_- / (P_- + σ²_obs)
                  μ̂_t = μ_- + K (r - μ_-)
                  P_t  = (1 - K) P_-
        """
        # Predict
        P_pred = self._P + self._sigma_proc2
        mu_pred = self._mu

        # Innovation
        S = P_pred + self._sigma_obs2
        K = P_pred / S
        innov = r - mu_pred

        # Update
        self._mu = mu_pred + K * innov
        self._P = (1.0 - K) * P_pred
        self._initialised = True
        return self._mu, self._P

    # ------------------------------------------------------------------
    #  Forecast  μ̂_{t+h}  — random-walk state ⇒ unchanged mean, variance grows
    # ------------------------------------------------------------------
    def forecast(self, h: int = 1) -> tuple[float, float]:
        if h < 1:
            raise ValueError("h must be >= 1")
        return self._mu, self._P + h * self._sigma_proc2

    # ------------------------------------------------------------------
    #  Decision outputs
    # ------------------------------------------------------------------
    def signal(self, tau_k: float = 1.96, p_crit: float = 1e-6) -> int:
        """
        +1 if μ̂ significantly > 0, -1 if significantly < 0, else 0.

        Significance uses the posterior z-score |μ̂| / √P > tau_k.
        P_crit additionally requires the posterior to be tight enough.
        """
        if not self._initialised or self._P <= 0 or self._P > p_crit * 1e6:
            return 0
        z = self._mu / math.sqrt(self._P)
        if z > tau_k:
            return 1
        if z < -tau_k:
            return -1
        return 0

    def confidence(self) -> float:
        """
        Posterior-z mapped into [0,1] via standard-normal CDF of |μ̂|/√P.

        Φ(0) = 0.5  →  doubled-and-mapped to 0.0  (no info)
        Φ(∞) = 1.0  →  maps to 1.0                (rock solid)
        """
        if not self._initialised or self._P <= 0:
            return 0.0
        z = abs(self._mu) / math.sqrt(self._P)
        # standard-normal cdf via erf
        phi = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        # remap (0.5, 1.0] -> [0.0, 1.0]
        return max(0.0, min(1.0, 2.0 * (phi - 0.5)))

    @property
    def mu(self) -> float:
        return self._mu

    @property
    def P(self) -> float:
        return self._P

    def reset(self, mu0: float = 0.0, P0: float = 1e-3) -> None:
        self._mu = mu0
        self._P = P0
        self._initialised = False
