"""
Optimal-stopping exit for v14 SmartBB — Gaussian-drift completion probability.

The decision at every bar of an open trade is:
    "what's the chance this trade still completes profitably (hits TP before SL)
     in the remaining time?"

Modelling price as a drifted Brownian motion conditioned on the Kalman
posterior (μ̂, P) of the drift, we approximate the barrier-hitting
probability with a first-order Bellman-value proxy:

    μ̂_T   = μ̂ * T_rem                     (expected cumulative log-return)
    σ²_T   = (P + σ²_obs) * T_rem          (total variance over T_rem bars)

With barriers at +d_tp (log-distance to TP, positive) and -d_sl (log-distance
to SL, positive), rotate so side=+1 means we want positive returns:

    p_tp = 1 - Φ( (d_tp - μ̂_T) / σ_T )     (P cum return >= +d_tp)
    p_sl =     Φ( (-d_sl - μ̂_T) / σ_T )    (P cum return <= -d_sl)
    P_win = p_tp / (p_tp + p_sl)           (competitive barrier approx)

Exit rule:
    if T_rem <= 0:                      exit ("time_stop")
    elif bars_held >= min_bars and P_win < threshold:   exit ("optimal_stop")
    else:                               hold.

This is strictly more information-efficient than v13's hand-crafted
"bars >= 4 AND |μ̂/√P| > 1σ AND running < 0" rule because:
    * no arbitrary bar count (only the unit of T_rem matters)
    * no arbitrary σ threshold (posterior already knows how noisy drift is)
    * no running-sign hack (barriers are baked in)

Reference: Shiryaev 1963 (optimal stopping); Ferebee 1982 (barrier hitting).
"""

from __future__ import annotations

import math


_SQRT_2 = math.sqrt(2.0)


def _phi(x: float) -> float:
    """Standard normal CDF Φ(x) via erf."""
    return 0.5 * (1.0 + math.erf(x / _SQRT_2))


def completion_probability(
    mu_hat: float,
    post_var: float,
    sigma_obs: float,
    dist_tp: float,
    dist_sl: float,
    side: int,
    bars_remaining: int,
) -> float:
    """
    Estimate P(hit TP before SL in `bars_remaining` bars | posterior drift).

    mu_hat          posterior drift (log-return per bar), for side=+1
                    *interpretation*: >0 means price is drifting up
    post_var        posterior variance of mu_hat
    sigma_obs       observation-noise std-dev of single-bar log-return
    dist_tp         positive log-distance from current price to TP
                    (rotated so +ve is 'toward TP' for our side)
    dist_sl         positive log-distance from current price to SL
                    (rotated so +ve is 'away from SL' for our side)
    side            +1 long / -1 short
    bars_remaining  integer T_rem >= 1

    Returns a probability in [0.0, 1.0].  0.5 is the "no info" fallback.
    """
    if bars_remaining <= 0:
        # Already timed out — purely determined by current fill, caller
        # should be handling this outside.  Return 0.5 as a safe no-info.
        return 0.5

    # Rotate so that "positive return" == "moving toward TP"
    mu_eff = mu_hat if side >= 0 else -mu_hat

    T = float(bars_remaining)
    drift = mu_eff * T
    var_total = max(post_var + sigma_obs * sigma_obs, 0.0) * T
    sigma_total = math.sqrt(max(var_total, 1e-18))
    if sigma_total < 1e-12:
        # Deterministic — just check drift
        return 1.0 if drift >= dist_tp else 0.0

    d_tp = max(dist_tp, 0.0)
    d_sl = max(dist_sl, 0.0)
    p_tp = 1.0 - _phi((d_tp - drift) / sigma_total)
    p_sl = _phi((-d_sl - drift) / sigma_total)

    denom = p_tp + p_sl
    if denom <= 1e-9:
        # Path almost certainly stays inside both barriers → flat expectancy
        return 0.5
    p_win = p_tp / denom
    return max(0.0, min(1.0, p_win))


class OptimalStopV14:
    """
    Convenience wrapper: stateful, one per open position, holds the
    invariants set at entry so the runtime call is small.
    """

    __slots__ = (
        "_threshold", "_min_bars", "_sigma_obs",
        "_side", "_armed",
    )

    def __init__(
        self,
        threshold: float = 0.40,
        min_bars: int = 3,
        sigma_obs: float = 0.0015,
    ):
        if not (0.0 < threshold < 1.0):
            raise ValueError("threshold must be in (0,1)")
        self._threshold = threshold
        self._min_bars = max(1, int(min_bars))
        self._sigma_obs = sigma_obs
        self._side = 0
        self._armed = False

    def arm(self, side: int) -> None:
        self._side = 1 if side > 0 else (-1 if side < 0 else 0)
        self._armed = self._side != 0

    def disarm(self) -> None:
        self._armed = False
        self._side = 0

    @property
    def armed(self) -> bool:
        return self._armed

    def should_exit(
        self,
        mu_hat: float,
        post_var: float,
        dist_tp: float,
        dist_sl: float,
        bars_held: int,
        bars_remaining: int,
    ) -> tuple[bool, float]:
        """
        Return (exit?, p_win).  exit is True iff we should close NOW.
        p_win is the completion probability at this bar (for logging).
        """
        if not self._armed:
            return False, 0.5
        if bars_held < self._min_bars:
            return False, 0.5
        p_win = completion_probability(
            mu_hat=mu_hat,
            post_var=post_var,
            sigma_obs=self._sigma_obs,
            dist_tp=dist_tp,
            dist_sl=dist_sl,
            side=self._side,
            bars_remaining=max(bars_remaining, 1),
        )
        return (p_win < self._threshold), p_win
