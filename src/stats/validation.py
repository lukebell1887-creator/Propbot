"""
validation.py — statistical validation of trading strategies.

Provides:
  • deflated_sharpe_ratio   — Bailey & López de Prado (2014). Adjusts the
                               observed Sharpe for (a) skew/kurtosis of the
                               returns, (b) multiple-testing when many
                               configs were tried.
  • max_drawdown            — from an equity/return series.
  • stationary_bootstrap    — Politis-Romano (1994) block bootstrap that
                               preserves serial dependence.
  • mc_bootstrap_dd         — resample the trade PnL stream and compute
                               percentiles of max-DD and ruin probability.

All pure-numpy, no scipy dependency (we implement the normal CDF by hand).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Sequence, Tuple

import numpy as np


# -----------------------------------------------------------------------
#  Helpers
# -----------------------------------------------------------------------
def _norm_cdf(x: float) -> float:
    """Standard normal CDF via erf — no scipy dependency."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _emc() -> float:
    """Euler-Mascheroni constant."""
    return 0.5772156649015329


def _z_for_quantile(q: float) -> float:
    """Inverse normal CDF (quantile) via Beasley-Springer-Moro-style approx."""
    # Acklam's approximation — accurate to ~1e-9 over (0, 1).
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
          1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
          6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
          3.754408661907416e+00]
    plow = 0.02425; phigh = 1 - plow
    if q < plow:
        r = math.sqrt(-2 * math.log(q))
        return (((((c[0]*r+c[1])*r+c[2])*r+c[3])*r+c[4])*r+c[5]) / \
               ((((d[0]*r+d[1])*r+d[2])*r+d[3])*r+1)
    if q > phigh:
        r = math.sqrt(-2 * math.log(1 - q))
        return -(((((c[0]*r+c[1])*r+c[2])*r+c[3])*r+c[4])*r+c[5]) / \
                ((((d[0]*r+d[1])*r+d[2])*r+d[3])*r+1)
    r = q - 0.5; s = r * r
    return (((((a[0]*s+a[1])*s+a[2])*s+a[3])*s+a[4])*s+a[5]) * r / \
           (((((b[0]*s+b[1])*s+b[2])*s+b[3])*s+b[4])*s+1)


# -----------------------------------------------------------------------
#  Sharpe / Deflated Sharpe
# -----------------------------------------------------------------------
def observed_sharpe(returns: Sequence[float]) -> float:
    """Per-trade Sharpe (not annualised): mean / std, over N trades."""
    r = np.asarray(returns, dtype=float)
    if len(r) < 2:
        return 0.0
    s = float(np.std(r, ddof=1))
    if s <= 1e-12:
        return 0.0
    return float(np.mean(r) / s)


def _sample_skew_kurt(r: np.ndarray) -> Tuple[float, float]:
    """Fisher skew (g1) and excess kurtosis (g2)."""
    n = len(r)
    if n < 4:
        return 0.0, 0.0
    mu = np.mean(r); s = np.std(r, ddof=0)
    if s <= 1e-12:
        return 0.0, 0.0
    m3 = np.mean((r - mu) ** 3); m4 = np.mean((r - mu) ** 4)
    g1 = m3 / s ** 3
    g2 = m4 / s ** 4 - 3.0
    return float(g1), float(g2)


@dataclass
class DSRResult:
    observed_sr: float      # per-trade Sharpe
    dsr: float              # probability the TRUE Sharpe > 0
    sr_zero_threshold: float  # expected-max-Sharpe under null (null = 0)
    n_trials: int           # number of configs tested
    n_obs: int              # number of trades in the chosen track
    skew: float
    kurt_excess: float


def deflated_sharpe_ratio(returns: Sequence[float],
                           n_trials: int = 1,
                           benchmark_sr: float = 0.0) -> DSRResult:
    """
    Bailey & López de Prado (2014) Deflated Sharpe Ratio.

    DSR = Prob[True Sharpe > benchmark | observed SR, N, skew, kurt, M trials]

    Steps:
      1. Compute the observed per-trade Sharpe.
      2. Compute the expected maximum Sharpe under the null (true SR = 0)
         if M independent trials were run.  (Kan-Smith / Bailey formula)
      3. Compute the standard error of the Sharpe estimator under
         non-Gaussian returns.
      4. DSR = Φ((SR - SR*) / std(SR)) where SR* = expected max under null.

    If DSR > 0.95 (with n_trials accounted for), the edge is real at the 5%
    level after correcting for multiple-testing.
    """
    r = np.asarray(returns, dtype=float)
    n = len(r)
    if n < 4:
        return DSRResult(0.0, 0.0, 0.0, n_trials, n, 0.0, 0.0)

    sr = observed_sharpe(r)
    g1, g2 = _sample_skew_kurt(r)

    # Expected max Sharpe under null (true SR = 0, M trials) — Bailey 2014 eq (7)
    # E[max_{i<=M} SR_i] ≈ (1 - γ) Φ^-1(1 - 1/M) + γ Φ^-1(1 - 1/(M·e))
    # with γ = Euler-Mascheroni.
    M = max(1, int(n_trials))
    if M == 1:
        sr_star = benchmark_sr
    else:
        gamma = _emc()
        z1 = _z_for_quantile(max(1 - 1.0 / M, 1e-12))
        z2 = _z_for_quantile(max(1 - 1.0 / (M * math.e), 1e-12))
        sr_star = benchmark_sr + (1.0 - gamma) * z1 + gamma * z2
        # Note: this is per-trade SR units. BLP's formulation usually assumes
        # annualised; scale-invariant if SR and sigma_hat are both per-trade.

    # Standard error of SR under non-normality (Mertens 2002 / BLP eq 9)
    # sigma_hat^2 = (1 - g1 * SR + (g2/4) * SR^2) / (N - 1)
    var_hat = (1.0 - g1 * sr + (g2 / 4.0) * (sr ** 2)) / max(1, n - 1)
    var_hat = max(var_hat, 1e-12)
    std_hat = math.sqrt(var_hat)

    # DSR
    dsr = _norm_cdf((sr - sr_star) / std_hat)
    return DSRResult(observed_sr=sr, dsr=float(dsr), sr_zero_threshold=sr_star,
                     n_trials=M, n_obs=n, skew=g1, kurt_excess=g2)


# -----------------------------------------------------------------------
#  Max drawdown
# -----------------------------------------------------------------------
def max_drawdown_from_pnls(pnls: Sequence[float], *,
                             start_balance: float = 100_000.0) -> float:
    """Max drawdown (fraction, e.g. 0.025 = 2.5 %) from a sequence of P&Ls."""
    eq = start_balance; peak = start_balance; mdd = 0.0
    for p in pnls:
        eq += p
        if eq > peak: peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > mdd: mdd = dd
    return mdd


# -----------------------------------------------------------------------
#  Stationary bootstrap (Politis-Romano 1994)
# -----------------------------------------------------------------------
def stationary_bootstrap_indices(n: int, avg_block_len: float,
                                   size: int, rng: np.random.Generator) -> np.ndarray:
    """Generate `size` indices for a stationary bootstrap sample of length
    `size` from a series of length n with average block length `avg_block_len`."""
    if avg_block_len < 1:
        avg_block_len = 1.0
    p = 1.0 / avg_block_len
    idx = np.empty(size, dtype=np.int64)
    cur = int(rng.integers(0, n))
    idx[0] = cur
    for t in range(1, size):
        if rng.random() < p:
            cur = int(rng.integers(0, n))
        else:
            cur = (cur + 1) % n
        idx[t] = cur
    return idx


@dataclass
class MCResult:
    n_paths: int
    mean_pnl: float
    median_pnl: float
    p01_pnl: float
    p05_pnl: float
    p95_pnl: float
    mean_dd: float
    median_dd: float
    p95_dd: float
    p99_dd: float
    ruin_prob: float          # P(dd > ruin_threshold)
    ruin_threshold: float


def mc_bootstrap_dd(pnls: Sequence[float], *,
                      n_paths: int = 1000,
                      avg_block_len: float = 5.0,
                      ruin_threshold: float = 0.04,
                      start_balance: float = 100_000.0,
                      seed: int = 0) -> MCResult:
    """
    Monte-Carlo max-DD stress test via stationary-block bootstrap.

    We resample the trade P&L sequence `n_paths` times (each path of the
    same length as the original), preserving local serial dependence via
    block-resampling. For each path we compute total PnL and max DD, then
    report percentiles and the probability max-DD exceeds `ruin_threshold`.
    """
    pnls = np.asarray(pnls, dtype=float)
    n = len(pnls)
    if n == 0:
        return MCResult(n_paths, 0, 0, 0, 0, 0, 0, 0, 0, 0, 1.0, ruin_threshold)
    rng = np.random.default_rng(seed)
    total_pnl = np.empty(n_paths); total_dd = np.empty(n_paths)
    for k in range(n_paths):
        idx = stationary_bootstrap_indices(n, avg_block_len, n, rng)
        path = pnls[idx]
        total_pnl[k] = float(np.sum(path))
        total_dd[k]  = max_drawdown_from_pnls(path, start_balance=start_balance)
    ruin = float(np.mean(total_dd > ruin_threshold))
    return MCResult(
        n_paths=n_paths,
        mean_pnl=float(np.mean(total_pnl)),
        median_pnl=float(np.median(total_pnl)),
        p01_pnl=float(np.percentile(total_pnl, 1)),
        p05_pnl=float(np.percentile(total_pnl, 5)),
        p95_pnl=float(np.percentile(total_pnl, 95)),
        mean_dd=float(np.mean(total_dd)),
        median_dd=float(np.median(total_dd)),
        p95_dd=float(np.percentile(total_dd, 95)),
        p99_dd=float(np.percentile(total_dd, 99)),
        ruin_prob=ruin, ruin_threshold=ruin_threshold,
    )
