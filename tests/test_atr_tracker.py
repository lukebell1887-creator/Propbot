"""tests/test_atr_tracker.py — Wilder ATR(14) parity with the backtest engine."""
import pytest
from src.live.atr_tracker import ATRTracker


def test_not_ready_until_window_seeded():
    t = ATRTracker(window=14)
    assert not t.ready
    for i in range(13):
        t.update(high=100 + i, low=99 + i, close=99.5 + i)
        assert not t.ready, f"should not be ready after only {i+1} bars"
    t.update(high=113, low=112, close=112.5)
    assert t.ready, "should be ready exactly at bar 14"


def test_seed_atr_is_simple_mean_of_first_14_trs():
    t = ATRTracker(window=14)
    # Constant range bars: H-L = 2.0 every bar. No previous-close adjustment
    # on bar 1; from bar 2 onwards H_t-C_{t-1} = 2.0 too (we'll choose closes
    # that keep this true).
    for _ in range(14):
        t.update(high=102.0, low=100.0, close=101.0)
    # All TRs = 2.0 → mean = 2.0
    assert t.value == pytest.approx(2.0, abs=1e-9)


def test_wilder_smoothing_after_seed():
    """ATR_t = ((W-1)*ATR_{t-1} + TR_t)/W with W=14."""
    t = ATRTracker(window=14)
    for _ in range(14):
        t.update(high=102.0, low=100.0, close=101.0)
    assert t.value == pytest.approx(2.0)
    # Now feed a much wider TR=10 bar.
    # New ATR = (13*2 + 10)/14 = 36/14 = 2.571428...
    t.update(high=110.0, low=100.0, close=101.0)
    assert t.value == pytest.approx(36.0 / 14.0, abs=1e-9)
    # Another bar with TR=2 (back to normal)
    # New ATR = (13*2.571428 + 2)/14
    expected = (13 * 36.0 / 14.0 + 2.0) / 14.0
    t.update(high=103.0, low=101.0, close=102.0)
    assert t.value == pytest.approx(expected, abs=1e-9)


def test_tr_uses_previous_close():
    t = ATRTracker(window=14)
    # Bar 1: H=100, L=99, C=99.5  → TR=1.0 (no prev close)
    t.update(100, 99, 99.5)
    # Bar 2: H=105, L=104, C=104.5 → TR = max(1, |105-99.5|, |104-99.5|) = 5.5
    t.update(105, 104, 104.5)
    # Internal: _seed_trs should now hold [1.0, 5.5]
    assert t._seed_trs == [pytest.approx(1.0), pytest.approx(5.5)]


def test_persistence_roundtrip_preserves_state():
    t = ATRTracker(window=14)
    for i in range(20):  # 20 bars: 14 seed + 6 wilder steps
        t.update(high=100 + 0.5 * i, low=99 + 0.5 * i, close=99.5 + 0.5 * i)
    snap = t.to_dict()
    t2 = ATRTracker.from_dict(snap)
    assert t2.ready == t.ready
    assert t2.value == pytest.approx(t.value)
    assert t2.bars_seen == t.bars_seen
    # Continue feeding both — they should stay in lock-step
    for i in range(5):
        h, l, c = 110 + i, 109 + i, 109.5 + i
        t.update(h, l, c)
        t2.update(h, l, c)
        assert t2.value == pytest.approx(t.value, abs=1e-12)


def test_value_is_zero_until_ready():
    t = ATRTracker(window=14)
    for i in range(5):
        t.update(102, 100, 101)
    assert t.value == 0.0
    assert not t.ready


def test_short_window_works():
    t = ATRTracker(window=3)
    t.update(102, 100, 101)  # TR=2
    t.update(103, 101, 102)  # TR=max(2, 2, 1)=2
    t.update(104, 102, 103)  # TR=max(2, 2, 1)=2
    assert t.ready
    assert t.value == pytest.approx(2.0)
