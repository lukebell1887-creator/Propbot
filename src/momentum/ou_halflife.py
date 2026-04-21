"""
Ornstein-Uhlenbeck half-life estimator.

Model (continuous-time mean-reversion SDE):

    dX_t = -θ (X_t - μ) dt + σ dW_t,        θ > 0

Discretise (Δt = 1 bar) as an AR(1):

    X_t - X_{t-1} = α + β · X_{t-1} + ε_t,
    β = -θ  (so β < 0 ⇒ mean-reverting)
    θ = -β
    μ = -α / β
    half-life  τ = ln 2 / θ       (bars)

Estimation is OLS on (X_{t-1}, ΔX_t).  Closed-form, no external libs.

Returned half-life is:
    +∞  if β >= 0 (non-stationary / trending series)
    < 1 if the series oscillates on sub-bar timescale (tagged as fast-MR)

Typical use (v14 SmartBB):
    - fit a rolling OU on the last W closes of the symbol
    - skip the trade if τ > τ_max (reversion too slow to beat costs)
    - set time_stop to 2τ (bounded by time_stop_max)

Reference:
    Ornstein & Uhlenbeck (1930); Phillips (1972) discrete-time MLE.
"""

from __future__ import annotations

import math
from typing import Sequence


def fit_ou(series: Sequence[float]) -> tuple[float, float, float]:
    """
    Fit AR(1): ΔX = α + β·X_{t-1}.

    Returns (theta, mu, half_life_bars).
      theta      = -β  (speed of mean reversion, bars⁻¹)
      mu         = -α/β (long-run mean level)
      half_life  = ln2/θ (in bars).  math.inf if β >= 0.

    Requirements:
        - at least 30 samples
        - non-degenerate variance in X_{t-1}
    """
    n = len(series)
    if n < 30:
        return 0.0, 0.0, math.inf

    # Build regressor arrays without copying the whole thing
    # Using statistics.fmean-equivalent two-pass formulas for OLS stability
    X = [float(series[i]) for i in range(n - 1)]
    dX = [float(series[i + 1]) - float(series[i]) for i in range(n - 1)]
    m = n - 1
    mx = sum(X) / m
    my = sum(dX) / m
    sxx = 0.0
    sxy = 0.0
    for xi, yi in zip(X, dX):
        dx_ = xi - mx
        dy_ = yi - my
        sxx += dx_ * dx_
        sxy += dx_ * dy_
    if sxx <= 1e-18:
        return 0.0, 0.0, math.inf

    beta = sxy / sxx
    alpha = my - beta * mx

    if beta >= 0.0 or not math.isfinite(beta):
        # series is trending (drift wins) — not mean reverting
        return 0.0, alpha / max(-beta, 1e-12) if beta < 0 else 0.0, math.inf

    theta = -beta
    mu = -alpha / beta
    half_life = math.log(2.0) / theta

    # Clamp pathological tiny/huge half-lives to sentinel values
    if not math.isfinite(half_life) or half_life <= 0.0:
        return theta, mu, math.inf
    return theta, mu, half_life


def ou_gate(
    series: Sequence[float],
    max_halflife_bars: float = 20.0,
) -> tuple[bool, float]:
    """
    Convenience:
        (allowed, half_life) = ou_gate(last_W_closes, max_halflife=20)

    allowed == True  iff the series is mean-reverting AND τ <= max_halflife.
    half_life is returned (math.inf if not MR).
    """
    _, _, hl = fit_ou(series)
    if not math.isfinite(hl):
        return False, hl
    return (hl <= max_halflife_bars), hl
