"""Unit tests for src/stats/validation.py — DSR, MC bootstrap, DD."""
import math
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.stats.validation import (
    observed_sharpe, deflated_sharpe_ratio, max_drawdown_from_pnls,
    stationary_bootstrap_indices, mc_bootstrap_dd,
    _norm_cdf, _z_for_quantile,
)


# ----------------------------------------------------------------------
#  Helpers
# ----------------------------------------------------------------------
def test_norm_cdf_basic():
    assert math.isclose(_norm_cdf(0.0), 0.5, abs_tol=1e-9)
    assert math.isclose(_norm_cdf(1.96), 0.975, abs_tol=1e-3)
    assert math.isclose(_norm_cdf(-1.96), 0.025, abs_tol=1e-3)


def test_z_for_quantile_roundtrip():
    # Φ(Φ^-1(q)) ≈ q
    for q in (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99):
        z = _z_for_quantile(q)
        assert math.isclose(_norm_cdf(z), q, abs_tol=1e-4), f"q={q}, z={z}"


# ----------------------------------------------------------------------
#  Sharpe / DSR
# ----------------------------------------------------------------------
def test_observed_sharpe_constant_zero_std():
    assert observed_sharpe([1.0, 1.0, 1.0]) == 0.0


def test_observed_sharpe_known():
    # mean=1, std≈1.41 (ddof=1 on [0,1,2]) → SR ≈ 1/1.0 = 1.0
    sr = observed_sharpe([0.0, 1.0, 2.0])
    assert math.isclose(sr, 1.0, rel_tol=1e-6)


def test_dsr_rejects_noise():
    # Zero-mean Gaussian noise → DSR should be ~0.5 (no edge), well below 0.95
    rng = np.random.default_rng(0)
    r = rng.normal(0, 1, 300)
    res = deflated_sharpe_ratio(r, n_trials=1)
    assert abs(res.dsr - 0.5) < 0.25, f"DSR on noise = {res.dsr:.3f}"


def test_dsr_accepts_strong_edge():
    # Very positive mean → DSR should be close to 1.0
    rng = np.random.default_rng(1)
    r = rng.normal(0.5, 1.0, 300)  # SR_true ≈ 0.5
    res = deflated_sharpe_ratio(r, n_trials=1)
    assert res.dsr > 0.95, f"DSR on strong edge = {res.dsr:.3f}"


def test_dsr_penalises_multiple_testing():
    # Same moderate edge, tested under 1 vs 1000 configs → DSR should drop
    rng = np.random.default_rng(2)
    r = rng.normal(0.15, 1.0, 250)
    d1 = deflated_sharpe_ratio(r, n_trials=1).dsr
    d1000 = deflated_sharpe_ratio(r, n_trials=1000).dsr
    assert d1 > d1000, f"DSR@1={d1:.3f} should exceed DSR@1000={d1000:.3f}"


def test_dsr_rejects_short_sample():
    res = deflated_sharpe_ratio([1.0, 2.0], n_trials=1)
    assert res.dsr == 0.0


# ----------------------------------------------------------------------
#  Max drawdown
# ----------------------------------------------------------------------
def test_max_dd_monotone_equity():
    # Strictly increasing equity → zero DD
    pnls = [100, 200, 150, 300, 400]
    # cumsum = [100, 300, 450, 750, 1150]  — monotone
    dd = max_drawdown_from_pnls(pnls, start_balance=100_000)
    assert math.isclose(dd, 0.0, abs_tol=1e-12)


def test_max_dd_known_case():
    # start 100k, +5k, -8k → peak 105k, trough 97k → dd = 8k/105k
    dd = max_drawdown_from_pnls([5000.0, -8000.0], start_balance=100_000)
    assert math.isclose(dd, 8000 / 105_000, rel_tol=1e-9)


# ----------------------------------------------------------------------
#  Stationary bootstrap / MC
# ----------------------------------------------------------------------
def test_stationary_bootstrap_shape_and_range():
    rng = np.random.default_rng(0)
    idx = stationary_bootstrap_indices(n=20, avg_block_len=3.0, size=100, rng=rng)
    assert idx.shape == (100,)
    assert idx.min() >= 0 and idx.max() < 20


def test_mc_bootstrap_on_positive_expectation():
    # Positive mean → mean_pnl > 0, ruin_prob small
    rng = np.random.default_rng(3)
    pnls = rng.normal(50.0, 500.0, 200)  # 200 trades, mean +$50
    res = mc_bootstrap_dd(pnls, n_paths=200, avg_block_len=3.0,
                           ruin_threshold=0.10, seed=3)
    assert res.mean_pnl > 0
    assert res.ruin_prob < 0.5


def test_mc_bootstrap_ruin_prob_reasonable():
    # Edge case: PnLs that would DEFINITELY ruin → ruin_prob ≈ 1.0
    pnls = [-5000.0] * 50  # losses every trade
    res = mc_bootstrap_dd(pnls, n_paths=50, avg_block_len=3.0,
                           ruin_threshold=0.04, seed=5)
    assert res.ruin_prob > 0.95


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn(); print(f"  PASS  {name}"); passed += 1
            except Exception as e:
                print(f"  FAIL  {name}: {e}"); failed += 1
    print(f"\n  Total: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)
