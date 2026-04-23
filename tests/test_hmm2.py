"""Unit tests for src/regime/hmm2.py — 2-state Gaussian HMM."""
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.regime.hmm2 import HMM2, fit_hmm2, daily_range_feature, _log_gauss, _logsumexp


# ----------------------------------------------------------------------
#  Numerical helpers
# ----------------------------------------------------------------------
def test_log_gauss_peak_at_mean():
    # log N(mu; mu, sigma^2) = -0.5*log(2πσ²)
    v = _log_gauss(5.0, 5.0, 2.0)
    assert math.isclose(v, -0.5 * math.log(2 * math.pi * 4.0), rel_tol=1e-9)


def test_log_gauss_symmetric():
    # Symmetric around the mean
    a = _log_gauss(3.0, 5.0, 2.0)
    b = _log_gauss(7.0, 5.0, 2.0)
    assert math.isclose(a, b, rel_tol=1e-12)


def test_logsumexp_basic():
    # logsumexp([0, 0]) = log(2)
    assert math.isclose(_logsumexp(np.array([0.0, 0.0])), math.log(2.0), rel_tol=1e-12)
    # logsumexp with huge values should not overflow
    big = np.array([1000.0, 1001.0])
    expected = 1000.0 + math.log(1.0 + math.e)
    assert math.isclose(_logsumexp(big), expected, rel_tol=1e-9)


# ----------------------------------------------------------------------
#  Synthetic-data fits
# ----------------------------------------------------------------------
def _gen_two_state_series(n=300, mu=(0.8, 1.3), sigma=(0.15, 0.25),
                          A=((0.92, 0.08), (0.10, 0.90)), seed=42):
    rng = np.random.default_rng(seed)
    s = 0; out = np.zeros(n); states = np.zeros(n, dtype=int)
    for t in range(n):
        states[t] = s
        out[t] = rng.normal(mu[s], sigma[s])
        s = int(rng.choice([0, 1], p=A[s]))
    return out, states


def test_fit_recovers_two_state_means_tolerant():
    x, truth = _gen_two_state_series(n=500, seed=0)
    m = fit_hmm2(x, n_iter=50, seed=0)
    # state 0 should be the lower-mean state after identifiability fix
    assert m.mu[0] < m.mu[1]
    # means within 20% of ground truth (0.8, 1.3)
    assert abs(m.mu[0] - 0.8) < 0.2
    assert abs(m.mu[1] - 1.3) < 0.2
    # transition matrix is sticky (diag >= 0.7)
    assert m.A[0, 0] > 0.7
    assert m.A[1, 1] > 0.7


def test_posterior_probs_sum_to_one():
    x, _ = _gen_two_state_series(n=100, seed=7)
    m = fit_hmm2(x, n_iter=20, seed=7)
    post = m.posterior(x)
    assert post.shape == (100, 2)
    sums = post.sum(axis=1)
    np.testing.assert_allclose(sums, np.ones(100), atol=1e-6)


def test_filter_probs_sum_to_one_and_causal():
    x, _ = _gen_two_state_series(n=80, seed=3)
    m = fit_hmm2(x, n_iter=20, seed=3)
    filt = m.filter(x)
    assert filt.shape == (80, 2)
    np.testing.assert_allclose(filt.sum(axis=1), np.ones(80), atol=1e-6)
    # Filter at t should equal filter on x[:t+1] at position t (causality)
    filt_short = m.filter(x[:30])
    np.testing.assert_allclose(filt_short[29], filt[29], atol=1e-9)


def test_viterbi_matches_truth_majority():
    x, truth = _gen_two_state_series(n=400, seed=11)
    m = fit_hmm2(x, n_iter=40, seed=11)
    path = m.viterbi(x)
    acc = float(np.mean(path == truth))
    assert acc > 0.75, f"Viterbi accuracy {acc:.2%} below 75% on synthetic data"


def test_log_likelihood_non_decreasing_in_em():
    # Run fit with few iters, then check loglik improves as we give more iters
    x, _ = _gen_two_state_series(n=200, seed=2)
    ll5 = fit_hmm2(x, n_iter=5, seed=2).loglik_
    ll30 = fit_hmm2(x, n_iter=30, seed=2).loglik_
    assert ll30 >= ll5 - 1e-6


def test_fit_rejects_too_short_series():
    try:
        fit_hmm2([1.0, 2.0, 3.0])
    except ValueError:
        return
    raise AssertionError("Expected ValueError for too-short series")


def test_daily_range_feature_causal_and_nan_warmup():
    highs = np.arange(30, dtype=float) + 0.5
    lows  = np.arange(30, dtype=float) - 0.5
    feat = daily_range_feature(highs, lows, atr_window=20)
    # first 20 should be NaN
    assert np.all(np.isnan(feat[:20]))
    # from index 20 onwards should be finite
    assert np.all(np.isfinite(feat[20:]))
    # constant range → feature == 1.0 exactly
    np.testing.assert_allclose(feat[20:], np.ones(10), atol=1e-12)


if __name__ == "__main__":
    # Run all tests manually
    passed = failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
                passed += 1
            except Exception as e:
                print(f"  FAIL  {name}: {e}")
                failed += 1
    print(f"\n  Total: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
