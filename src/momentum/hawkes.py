"""
HawkesIntensity — exponential-decay self-exciting intensity estimator.

Model (Hawkes 1971, *Biometrika* 58(1)):

    λ(t) = μ₀ + Σ_{t_i < t} α · exp(-β (t - t_i))

Efficient O(1) online update using the standard exponential-kernel recursion
(Ogata 1981).  We maintain two intensities (up-ticks, down-ticks) and return
the ratio.  When λ_up / λ_down > 2 we are inside a momentum burst.

References:
  - Bacry E., Mastromatteo I., Muzy J.-F., "Hawkes Processes in Finance",
    Market Microstructure & Liquidity 1 (2015) 1550005.
"""

from __future__ import annotations
import math


class HawkesIntensity:
    __slots__ = ("_mu0", "_alpha", "_beta", "_lam_up", "_lam_dn", "_t_prev")

    def __init__(self, mu0: float = 0.1, alpha: float = 0.4, beta: float = 1.0):
        """
        mu0    baseline intensity (events per unit time)
        alpha  jump size on each event (0 < alpha < beta for stationarity)
        beta   decay rate (higher = faster memory loss)
        """
        if alpha >= beta:
            raise ValueError("alpha < beta required for a stable Hawkes process")
        self._mu0 = mu0
        self._alpha = alpha
        self._beta = beta
        self._lam_up = mu0
        self._lam_dn = mu0
        self._t_prev = 0.0

    # ------------------------------------------------------------------
    def update(self, t: float, ret: float) -> tuple[float, float]:
        """
        Feed a bar close at time t (monotone increasing) with return `ret`.
        Signs the event as up-tick (ret>0) or down-tick (ret<0).  Zero returns
        decay both intensities toward baseline.

        Returns (λ_up, λ_dn) after the update.
        """
        dt = max(0.0, t - self._t_prev)
        decay = math.exp(-self._beta * dt)
        self._lam_up = self._mu0 + (self._lam_up - self._mu0) * decay
        self._lam_dn = self._mu0 + (self._lam_dn - self._mu0) * decay

        if ret > 0:
            self._lam_up += self._alpha
        elif ret < 0:
            self._lam_dn += self._alpha

        self._t_prev = t
        return self._lam_up, self._lam_dn

    # ------------------------------------------------------------------
    def ratio(self) -> float:
        """λ_up / λ_dn — asymmetric; feed abs() if you want a magnitude."""
        if self._lam_dn <= 1e-12:
            return float("inf")
        return self._lam_up / self._lam_dn

    def signal(self, threshold: float = 2.0) -> int:
        """+1 if up-burst, -1 if down-burst, 0 otherwise."""
        r = self.ratio()
        if r > threshold:
            return 1
        if r < 1.0 / threshold:
            return -1
        return 0

    def confidence(self) -> float:
        """
        log(λ_up/λ_dn) / log(10)  saturated to [0,1].

        Ratio 1 → 0   (no info)
        Ratio 10 → 1  (absolute dominance)
        """
        r = self.ratio()
        if r <= 0 or not math.isfinite(r):
            return 0.0
        if r < 1.0:
            r = 1.0 / r       # symmetric around 1
        return max(0.0, min(1.0, math.log(r) / math.log(10.0)))

    @property
    def lam_up(self) -> float:
        return self._lam_up

    @property
    def lam_dn(self) -> float:
        return self._lam_dn

    def reset(self) -> None:
        self._lam_up = self._mu0
        self._lam_dn = self._mu0
        self._t_prev = 0.0
