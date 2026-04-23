"""
test_dynamic_sizer_v21.py — unit tests for Merton×GZ sizer.

Each test targets a specific mathematical invariant of the sizer. Together they
verify that:
    1. The sizer is safe at cold-start (no NaN, no explosion, returns base).
    2. Grossman-Zhou drawdown factor scales risk linearly toward 0 at dd_cap.
    3. The cap_mult is NEVER exceeded, even for huge μ/σ² ratios.
    4. Positive edge scales risk UP; negative edge holds base (no_edge_multiplier=1).
    5. Pooling (pool_symbols=True) aggregates learning across instruments.
    6. Reset clears state cleanly.
    7. Thread safety: concurrent updates don't corrupt EWMA state.
"""
from __future__ import annotations
import math
import threading
import pytest

from src.dynamic_sizer_v21 import (
    MertonGZSizer, MertonGZSizerConfig, default_mertongz_sizer
)


# ---------------------------------------------------------------------
#  1.  Cold-start safety
# ---------------------------------------------------------------------
def test_cold_start_returns_base_risk():
    """No trades seen → should return base_risk_pct regardless of symbol."""
    s = MertonGZSizer(MertonGZSizerConfig(base_risk_pct=0.001, warmup_trades=5))
    r = s.compute_risk_pct("US30", 100_000, 100_000, [])
    assert r == pytest.approx(0.001)


def test_cold_start_never_nan_or_negative():
    s = default_mertongz_sizer()
    for sym in ["US30", "DE40", "XAUUSD", "NEW_SYMBOL_NEVER_SEEN"]:
        r = s.compute_risk_pct(sym, 100_000, 100_000, [])
        assert math.isfinite(r)
        assert r >= 0


# ---------------------------------------------------------------------
#  2.  Grossman-Zhou drawdown barrier
# ---------------------------------------------------------------------
def test_gz_linear_decay_to_zero():
    """At DD = dd_cap, risk should go to 0."""
    # Use warmup_trades=5 and feed 0 trades → warmup path always taken
    # → merton_mult=1.0 and risk = base × GZ exactly
    s = MertonGZSizer(MertonGZSizerConfig(
        base_risk_pct=0.001, warmup_trades=5, dd_cap_pct=0.04,
    ))
    r_peak = s.compute_risk_pct("X", 100_000, 100_000, [])
    r_half = s.compute_risk_pct("X", 98_000, 100_000, [])   # 2% DD = half way
    r_cap  = s.compute_risk_pct("X", 96_000, 100_000, [])   # 4% DD = at barrier
    r_over = s.compute_risk_pct("X", 95_000, 100_000, [])   # beyond barrier
    assert r_peak == pytest.approx(0.001)
    assert r_half == pytest.approx(0.0005, abs=1e-6)
    assert r_cap == pytest.approx(0.0, abs=1e-9)
    assert r_over == 0.0


# ---------------------------------------------------------------------
#  3.  Hard cap is NEVER exceeded (Thorp-style protection)
# ---------------------------------------------------------------------
def test_cap_mult_is_hard_ceiling():
    """Even with absurdly high Sharpe, risk ≤ cap_mult × base."""
    cfg = MertonGZSizerConfig(
        base_risk_pct=0.001, cap_mult=3.0, warmup_trades=0,
        gamma=2.0, ewma_alpha=1.0,  # α=1 means each new obs fully replaces μ
    )
    s = MertonGZSizer(cfg)
    # Feed 20 massive winners
    for _ in range(20):
        s.on_trade_closed("X", 100.0)          # realised_R = +100 (huge edge!)
    r = s.compute_risk_pct("X", 100_000, 100_000, [])
    assert r <= cfg.cap_mult * cfg.base_risk_pct + 1e-9
    assert r == pytest.approx(0.003, abs=1e-6)   # exactly at cap


# ---------------------------------------------------------------------
#  4.  Positive vs negative edge
# ---------------------------------------------------------------------
def test_positive_edge_scales_up():
    cfg = MertonGZSizerConfig(
        base_risk_pct=0.001, cap_mult=3.0, warmup_trades=0,
        gamma=2.0, ewma_alpha=0.5,
    )
    s = MertonGZSizer(cfg)
    for _ in range(10):
        s.on_trade_closed("X", 0.5)  # μ ≈ 0.5, var ≈ 0
    r = s.compute_risk_pct("X", 100_000, 100_000, [])
    assert r > cfg.base_risk_pct  # scaled up above base


def test_negative_edge_holds_base_when_noedge_multiplier_is_1():
    """With no_edge_multiplier=1.0 (our v21 winning config), negative μ stays at base."""
    cfg = MertonGZSizerConfig(
        base_risk_pct=0.001, warmup_trades=0,
        no_edge_multiplier=1.0,
    )
    s = MertonGZSizer(cfg)
    for _ in range(10):
        s.on_trade_closed("X", -0.5)
    r = s.compute_risk_pct("X", 100_000, 100_000, [])
    assert r == pytest.approx(0.001, abs=1e-9)   # base, not 0.5×base


def test_negative_edge_halves_when_noedge_multiplier_is_half():
    """Default no_edge_multiplier=0.5 → negative μ gets halved."""
    cfg = MertonGZSizerConfig(
        base_risk_pct=0.001, warmup_trades=0,
        no_edge_multiplier=0.5,
    )
    s = MertonGZSizer(cfg)
    for _ in range(10):
        s.on_trade_closed("X", -0.5)
    r = s.compute_risk_pct("X", 100_000, 100_000, [])
    assert r == pytest.approx(0.0005, abs=1e-9)


# ---------------------------------------------------------------------
#  5.  Pooling
# ---------------------------------------------------------------------
def test_pool_symbols_aggregates_learning():
    """With pool_symbols=True, a trade on symbol A updates the pool seen by symbol B."""
    cfg = MertonGZSizerConfig(
        base_risk_pct=0.001, pool_symbols=True, warmup_trades=5,
    )
    s = MertonGZSizer(cfg)
    # Feed 5 trades on various symbols — should satisfy pooled warmup
    for sym in ["A", "B", "C", "D", "E"]:
        s.on_trade_closed(sym, 0.3)
    stats = s.stats()
    assert "_GLOBAL_" in stats["per_symbol"]
    assert stats["per_symbol"]["_GLOBAL_"]["n_trades_seen"] == 5
    # Now a fresh symbol F should be OUT of warmup (pool has 5 observations)
    # and μ > 0 → should scale up from base
    r = s.compute_risk_pct("F", 100_000, 100_000, [])
    assert r > cfg.base_risk_pct


def test_per_symbol_isolates_learning():
    """With pool_symbols=False, symbol A's updates don't help symbol B."""
    cfg = MertonGZSizerConfig(
        base_risk_pct=0.001, pool_symbols=False, warmup_trades=5,
    )
    s = MertonGZSizer(cfg)
    for _ in range(5):
        s.on_trade_closed("A", 0.3)
    # B has 0 trades → still in warmup → returns base
    r_b = s.compute_risk_pct("B", 100_000, 100_000, [])
    assert r_b == pytest.approx(0.001, abs=1e-9)


# ---------------------------------------------------------------------
#  6.  Reset
# ---------------------------------------------------------------------
def test_reset_clears_all_state():
    s = default_mertongz_sizer()
    for _ in range(20):
        s.on_trade_closed("X", 0.5)
    s.compute_risk_pct("X", 99_000, 100_000, [])
    s.reset()
    assert s.stats()["n_calls"] == 0
    assert s.stats()["per_symbol"] == {}


# ---------------------------------------------------------------------
#  7.  Thread safety
# ---------------------------------------------------------------------
def test_concurrent_updates_dont_corrupt_state():
    """Hammer the sizer from 8 threads — internal invariants must hold."""
    s = default_mertongz_sizer()

    def worker():
        for i in range(500):
            s.on_trade_closed("X", 0.1 if i % 2 == 0 else -0.1)
            s.compute_risk_pct("X", 100_000, 100_000, [])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads: t.start()
    for t in threads: t.join()

    st = s.stats()
    assert st["n_calls"] == 8 * 500
    # Pool should have received all 4000 trade updates
    global_stats = st["per_symbol"].get("_GLOBAL_")
    assert global_stats is not None
    assert global_stats["n_trades_seen"] == 8 * 500
    # μ should be ≈ 0 (equal mix of +0.1 and -0.1)
    assert abs(global_stats["mu_ewma"]) < 0.2
    assert math.isfinite(global_stats["mu_ewma"])
    assert math.isfinite(global_stats["var_ewma"])


# ---------------------------------------------------------------------
#  8.  Regression: default factory matches v21 winning config
# ---------------------------------------------------------------------
def test_default_factory_uses_v21_winning_params():
    """Lock in the specific cocktail that produced +$14,622 / 3.36% DD."""
    s = default_mertongz_sizer()
    cfg = s.cfg
    assert cfg.base_risk_pct == 0.0015
    assert cfg.cap_mult == 3.0
    assert cfg.gamma == 2.0
    assert cfg.ewma_alpha == 0.20
    assert cfg.warmup_trades == 15
    assert cfg.dd_cap_pct == 0.04
    assert cfg.pool_symbols is True
    assert cfg.no_edge_multiplier == 1.0
