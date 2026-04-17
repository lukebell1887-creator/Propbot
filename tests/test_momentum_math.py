"""
Unit tests for v7 momentum math kernels.

Runs quickly without requiring SciPy / arch / filterpy - compares the
Rust-equivalent Python implementations against known closed-form references
and analytic invariants.

    python tests/test_momentum_math.py
"""

from __future__ import annotations
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

from src.momentum import (  # noqa: E402
    KalmanForecast, CUSUMDetector, HawkesIntensity, OptimalStopper,
    GarchOne, GpdTail, EVTGarchStop,
    BayesianEdge, JamesSteinShrink,
    ThorpKelly, GrossmanZhouDD, CVaRCap, BayesianSizer,
)


# ----------------------------------------------------------------------
def test_kalman_detects_drift_step():
    rng = np.random.default_rng(42)
    drift = np.concatenate([np.zeros(200), np.full(200, 0.002)])
    noise = rng.normal(0, 0.001, size=400)
    r = drift + noise
    kf = KalmanForecast(sigma_obs=0.001, sigma_proc=5e-5)
    sigs = []
    for t, ret in enumerate(r):
        kf.update(ret)
        if t > 250:
            sigs.append(kf.signal(tau_k=1.5, p_crit=1.0))
    up = sum(1 for s in sigs if s == 1)
    dn = sum(1 for s in sigs if s == -1)
    assert up > 3 * max(1, dn), f"Kalman drift: up={up}, dn={dn}"
    print(f"  kalman drift detection: up={up}, dn={dn}  OK")


# ----------------------------------------------------------------------
def test_cusum_recursion_exact():
    """CUSUM with k=0.5, h=4.5, strong +ve drift should fire."""
    cu = CUSUMDetector(k=0.5, h=4.5)
    # Standardised returns well above k so S+ grows steadily:
    seq = [1.5, 1.8, 2.0, 1.2, 1.6, 2.1, 1.4]
    fires = [cu.update(z) for z in seq]
    assert any(f == 1 for f in fires), f"CUSUM failed to fire: {fires}"
    first = fires.index(1)
    # Reset-after-detection: immediately after, no fire on small value
    f = cu.update(0.1)
    assert f == 0
    print(f"  cusum fires at step {first}  OK")


# ----------------------------------------------------------------------
def test_hawkes_ratio_behaves():
    hk = HawkesIntensity(mu0=0.1, alpha=0.4, beta=1.0)
    for t in range(20):
        hk.update(float(t) * 0.5, +0.01)
    ratio_up = hk.ratio()
    for t in range(20, 40):
        hk.update(float(t) * 0.5, -0.01)
    ratio_mix = hk.ratio()
    assert ratio_up > 2.0, f"Hawkes up burst ratio too low: {ratio_up}"
    assert ratio_mix < ratio_up, "Ratio should fall after down ticks"
    print(f"  hawkes ratio up={ratio_up:.2f} -> mix={ratio_mix:.2f}  OK")


# ----------------------------------------------------------------------
def test_garch_forecast_positive():
    rng = np.random.default_rng(0)
    rets = rng.normal(0, 0.002, size=2500)
    g = GarchOne()
    for r in rets:
        g.update(r)
    sig1 = g.sigma()
    assert 1e-6 < sig1 < 1e-1
    assert abs(sig1 - rets.std()) / rets.std() < 0.4, \
        f"GARCH sigma off: {sig1} vs {rets.std()}"
    print(f"  garch sigma = {sig1:.6f}  (empirical {rets.std():.6f})  OK")


# ----------------------------------------------------------------------
def test_gpd_quantile_monotone():
    rng = np.random.default_rng(0)
    z = rng.standard_t(4, size=3000)
    g = GpdTail(window=3000, refit_every=1000, threshold_pct=95.0)
    for zi in z:
        g.update(zi)
    q99 = g.quantile(0.01)
    q95 = g.quantile(0.05)
    q50 = g.quantile(0.5)
    assert q99 > q95 > q50 > 0, f"GPD quantile not monotone: {q50},{q95},{q99}"
    assert g.xi > 0.05, f"GPD xi too small on t4: {g.xi}"
    print(f"  gpd  q50={q50:.2f} q95={q95:.2f} q99={q99:.2f} xi={g.xi:.2f}  OK")


# ----------------------------------------------------------------------
def test_bayesian_edge_converges():
    rng = random.Random(0)
    edge = BayesianEdge()
    true_p, win_R, loss_R = 0.55, 1.8, 1.0
    for _ in range(500):
        if rng.random() < true_p:
            edge.update(win_R)
        else:
            edge.update(-loss_R)
    snap = edge.snapshot()
    assert abs(snap["p_mean"] - true_p) < 0.05
    expected = true_p * win_R - (1 - true_p) * loss_R
    assert abs(snap["R_mean"] - expected) < 0.2
    print(f"  bayes  p={snap['p_mean']:.3f} R={snap['R_mean']:.3f}  OK")


# ----------------------------------------------------------------------
def test_grossman_zhou_boundaries():
    gz = GrossmanZhouDD(max_dd=0.09, gamma=2.0)
    assert abs(gz.factor(1000, 1000) - 1.0) < 1e-6
    f3 = gz.factor(970, 1000)
    f7 = gz.factor(930, 1000)
    assert 0.85 < f3 < 0.92, f"GZ@3%: {f3}"
    assert 0.35 < f7 < 0.45, f"GZ@7%: {f7}"
    assert gz.factor(910, 1000) < 1e-6
    print(f"  gz  @3%={f3:.3f} @7%={f7:.3f}  OK")


# ----------------------------------------------------------------------
def test_bayesian_sizer_integration():
    sz = BayesianSizer()
    for _ in range(54):
        sz.record_trade(1.7, 5000.0)
    for _ in range(46):
        sz.record_trade(-1.0, 5000.0)
    sz.mark_equity(5000.0)
    dec = sz.decide(equity=5000.0, conviction=0.8, stop_distance=20.0,
                    pip_value=1.0, avg_win_R=1.7, avg_loss_R=1.0)
    assert 0.0005 < dec.risk_fraction < 0.03, f"risk_fraction: {dec.risk_fraction}"
    assert dec.lots > 0.0
    print(f"  sizer  f={dec.risk_fraction*100:.2f}%  lots={dec.lots:.2f}  OK")


# ----------------------------------------------------------------------
def test_evt_stop_end_to_end():
    rng = np.random.default_rng(0)
    stop = EVTGarchStop()
    price = 20000.0
    for _ in range(2500):
        r = rng.normal(0, 0.0008)
        price *= math.exp(r)
        stop.update(price, r, price * 1.001, price * 0.999)
    d_entry = stop.entry_stop_distance(price, regime=1, alpha=0.005)
    d_trail = stop.trail_distance(price, regime=1, alpha=0.10)
    assert d_entry > d_trail > 0
    assert 0.0005 < d_entry / price < 0.05
    print(f"  evt_stop entry={d_entry:.2f} ({d_entry/price*100:.3f}%), "
          f"trail={d_trail:.2f} ({d_trail/price*100:.3f}%)  OK")


# ----------------------------------------------------------------------
def test_optimal_stopper():
    os_ = OptimalStopper(r_min=1.0)
    os_.arm(0.002)
    assert not os_.should_exit(+0.001, 0.5)
    assert not os_.should_exit(+0.001, 1.2)
    assert os_.should_exit(-0.0005, 1.2)
    print("  optimal_stop fires correctly on sign flip  OK")


# ----------------------------------------------------------------------
def main():
    tests = [
        test_kalman_detects_drift_step,
        test_cusum_recursion_exact,
        test_hawkes_ratio_behaves,
        test_garch_forecast_positive,
        test_gpd_quantile_monotone,
        test_bayesian_edge_converges,
        test_grossman_zhou_boundaries,
        test_bayesian_sizer_integration,
        test_evt_stop_end_to_end,
        test_optimal_stopper,
    ]
    failed = 0
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"  FAIL  {t.__name__}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {t.__name__}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n  {len(tests) - failed}/{len(tests)} tests passed")
    return failed


if __name__ == "__main__":
    sys.exit(main())
