"""
hmm2.py — 2-state Gaussian Hidden Markov Model for regime detection.

Purpose
-------
Classify each trading day as either TREND (state 0) or CHOP (state 1) based
on a single univariate daily feature (e.g. normalised daily range). Used
by the v22 ORB engine to GATE entries: only trade ORBs on days where the
forward-filtered P(trend|past data) exceeds a threshold.

Model
-----
  states         : S = {0: TREND, 1: CHOP}
  emission       : x_t | s_t = k ~ Normal(mu_k, sigma_k^2)
  transition     : P(s_{t+1}=j | s_t=i) = A[i,j]
  initial        : P(s_0=i) = pi[i]

Algorithms implemented:
  • Forward-Backward (alpha, beta, gamma, xi)  — E-step
  • Baum-Welch EM update                        — M-step
  • Viterbi (most-likely path, for reporting)
  • Online forward filter (for live gating)

All maths is in log-space to avoid underflow on long series.
This is a self-contained implementation — NO sklearn / hmmlearn dependency.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

LOG_2PI = math.log(2.0 * math.pi)


# -----------------------------------------------------------------------
#  Numerical helpers
# -----------------------------------------------------------------------
def _log_gauss(x: float, mu: float, sigma: float) -> float:
    """log N(x; mu, sigma^2).  sigma must be > 0."""
    if sigma <= 0:
        sigma = 1e-8
    return -0.5 * LOG_2PI - math.log(sigma) - 0.5 * ((x - mu) / sigma) ** 2


def _logsumexp(a: np.ndarray) -> float:
    m = np.max(a)
    if not np.isfinite(m):
        return float(m)
    return float(m + math.log(float(np.sum(np.exp(a - m)))))


# -----------------------------------------------------------------------
#  Model
# -----------------------------------------------------------------------
@dataclass
class HMM2:
    """2-state Gaussian HMM.  Trend = state 0, Chop = state 1.

    After fitting, state 0 is guaranteed to have the LOWER mean feature
    value if `identify_by_low_mean=True` (our default), since TREND days
    tend to have LOWER normalised daily range than CHOP days when the
    feature is `daily_range / ATR_k` minus some centring — we relabel
    post-fit to keep the semantics stable.
    """
    pi: np.ndarray         # shape (2,)         initial state prob
    A:  np.ndarray         # shape (2, 2)       transition matrix
    mu: np.ndarray         # shape (2,)         emission means
    sigma: np.ndarray      # shape (2,)         emission std-devs
    n_iter_: int = 0
    loglik_: float = -math.inf

    # ---------- evaluation ----------
    def log_emission(self, x: np.ndarray) -> np.ndarray:
        """Return (T,2) log-emission probabilities."""
        T = len(x); out = np.zeros((T, 2))
        for t in range(T):
            for k in (0, 1):
                out[t, k] = _log_gauss(float(x[t]), float(self.mu[k]),
                                        float(self.sigma[k]))
        return out

    def forward(self, x: np.ndarray) -> Tuple[np.ndarray, float]:
        """Returns (log_alpha [T,2], log-likelihood)."""
        T = len(x)
        logE = self.log_emission(x)
        logA = np.log(np.clip(self.A,  1e-300, None))
        logP = np.log(np.clip(self.pi, 1e-300, None))
        la = np.zeros((T, 2))
        la[0] = logP + logE[0]
        for t in range(1, T):
            for j in (0, 1):
                la[t, j] = _logsumexp(la[t - 1] + logA[:, j]) + logE[t, j]
        ll = _logsumexp(la[-1])
        return la, ll

    def backward(self, x: np.ndarray) -> np.ndarray:
        T = len(x)
        logE = self.log_emission(x)
        logA = np.log(np.clip(self.A, 1e-300, None))
        lb = np.zeros((T, 2))
        lb[-1] = 0.0
        for t in range(T - 2, -1, -1):
            for i in (0, 1):
                lb[t, i] = _logsumexp(logA[i] + logE[t + 1] + lb[t + 1])
        return lb

    def posterior(self, x: np.ndarray) -> np.ndarray:
        """Returns (T,2) smoothed P(s_t|x_1:T)."""
        la, ll = self.forward(x)
        lb = self.backward(x)
        lg = la + lb - ll
        return np.exp(lg)

    def filter(self, x: np.ndarray) -> np.ndarray:
        """Returns (T,2) ONLINE-filtered P(s_t|x_1:t). Causal, live-safe."""
        la, _ = self.forward(x)
        # normalise each row
        out = np.zeros_like(la)
        for t in range(la.shape[0]):
            m = _logsumexp(la[t])
            out[t] = np.exp(la[t] - m)
        return out

    def viterbi(self, x: np.ndarray) -> np.ndarray:
        """Returns most-likely state sequence (T,)."""
        T = len(x)
        logE = self.log_emission(x)
        logA = np.log(np.clip(self.A, 1e-300, None))
        logP = np.log(np.clip(self.pi, 1e-300, None))
        delta = np.zeros((T, 2)); psi = np.zeros((T, 2), dtype=int)
        delta[0] = logP + logE[0]
        for t in range(1, T):
            for j in (0, 1):
                scores = delta[t - 1] + logA[:, j]
                psi[t, j] = int(np.argmax(scores))
                delta[t, j] = scores[psi[t, j]] + logE[t, j]
        states = np.zeros(T, dtype=int)
        states[-1] = int(np.argmax(delta[-1]))
        for t in range(T - 2, -1, -1):
            states[t] = psi[t + 1, states[t + 1]]
        return states


# -----------------------------------------------------------------------
#  Fitting — Baum-Welch EM
# -----------------------------------------------------------------------
def fit_hmm2(x: Sequence[float], *,
             n_iter: int = 50, tol: float = 1e-4,
             seed: int = 0,
             identify_by_low_mean: bool = True) -> HMM2:
    """Fit a 2-state Gaussian HMM to 1-D sequence `x` via Baum-Welch.

    After fitting, if `identify_by_low_mean=True`, we permute states so
    that state 0 has the LOWER mean. This gives a stable "state 0 = trend
    day (tight range)" interpretation when x is e.g. daily_range / ATR20.
    """
    x = np.asarray(x, dtype=float)
    T = len(x)
    if T < 10:
        raise ValueError(f"Need at least 10 observations, got {T}")

    rng = np.random.default_rng(seed)
    # Smart init: k-means-esque split at median
    med = float(np.median(x))
    mu = np.array([float(np.mean(x[x <= med])), float(np.mean(x[x > med]))])
    s  = float(np.std(x) + 1e-6)
    sigma = np.array([s, s])
    # Small perturb to break symmetry
    mu += rng.normal(0, s * 0.01, size=2)
    pi = np.array([0.5, 0.5])
    A  = np.array([[0.9, 0.1], [0.1, 0.9]])  # sticky states prior

    prev_ll = -math.inf
    model = HMM2(pi=pi, A=A, mu=mu, sigma=sigma)
    for it in range(n_iter):
        # E-step --------------------------------------------------------
        logE = model.log_emission(x)
        la, ll = model.forward(x)
        lb = model.backward(x)
        lg = la + lb - ll
        gamma = np.exp(lg)
        logA = np.log(np.clip(model.A, 1e-300, None))
        # xi[t,i,j] = P(s_t=i, s_{t+1}=j | x, θ)
        xi = np.zeros((T - 1, 2, 2))
        for t in range(T - 1):
            denom = -math.inf
            scores = np.zeros((2, 2))
            for i in (0, 1):
                for j in (0, 1):
                    scores[i, j] = la[t, i] + logA[i, j] + logE[t + 1, j] + lb[t + 1, j]
                    denom = max(denom, scores[i, j])
            lse = _logsumexp(scores.reshape(-1))
            for i in (0, 1):
                for j in (0, 1):
                    xi[t, i, j] = math.exp(scores[i, j] - lse)

        # M-step --------------------------------------------------------
        new_pi = gamma[0] / (gamma[0].sum() + 1e-12)
        new_A  = xi.sum(axis=0)
        new_A /= (new_A.sum(axis=1, keepdims=True) + 1e-12)
        weights = gamma.sum(axis=0) + 1e-12
        new_mu = (gamma * x[:, None]).sum(axis=0) / weights
        new_sigma = np.sqrt(
            (gamma * (x[:, None] - new_mu) ** 2).sum(axis=0) / weights
        )
        # floor sigma to avoid degenerate collapse onto a single point
        new_sigma = np.maximum(new_sigma, 1e-4)

        model = HMM2(pi=new_pi, A=new_A, mu=new_mu, sigma=new_sigma,
                     n_iter_=it + 1, loglik_=ll)

        # Convergence ---------------------------------------------------
        if abs(ll - prev_ll) < tol and it > 2:
            break
        prev_ll = ll

    # Identifiability: ensure state 0 = lower mean (= "tight" day = trend)
    if identify_by_low_mean and model.mu[0] > model.mu[1]:
        model = HMM2(
            pi=model.pi[::-1].copy(),
            A=model.A[::-1, ::-1].copy(),
            mu=model.mu[::-1].copy(),
            sigma=model.sigma[::-1].copy(),
            n_iter_=model.n_iter_,
            loglik_=model.loglik_,
        )

    return model


# -----------------------------------------------------------------------
#  Feature helpers (for the v22 ORB gate)
# -----------------------------------------------------------------------
def daily_range_feature(highs: Sequence[float],
                         lows:  Sequence[float],
                         atr_window: int = 20) -> np.ndarray:
    """
    Feature used by v22's HMM gate:
        f_t = (high_t - low_t) / mean_range_{t-atr_window..t-1}

    f_t < 1.0  → today's range compressed vs recent avg → likely TREND day
    f_t > 1.0  → today's range expanded               → likely CHOP day

    Returns array of length len(highs); first `atr_window` entries are NaN
    and should be dropped before fitting. Uses ONLY past data for the
    denominator (causal, live-safe).
    """
    highs = np.asarray(highs, dtype=float)
    lows  = np.asarray(lows,  dtype=float)
    rng_  = highs - lows
    T = len(rng_)
    out = np.full(T, np.nan)
    for t in range(atr_window, T):
        denom = float(np.mean(rng_[t - atr_window:t]))
        if denom > 0:
            out[t] = rng_[t] / denom
    return out
