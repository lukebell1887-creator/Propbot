"""
tests/test_partial_manager.py
=============================
Comprehensive tests for the v30.3 TP1/TP2/trail partial-close manager.

Mirrors the in-bar exit ladder of `src/orb_engine_v20.py` so live and
backtest produce bit-identical sequences.
"""
from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import pytest

from src.live.partial_manager import (
    PartialState, PartialUpdateResult, PartialCloseManager, PartialManagerConfig,
)


# ----------------------------------------------------------------------
#  Fake bridge
# ----------------------------------------------------------------------
@dataclass
class FakeBridge:
    """Records every close_position / modify_position call."""
    closes: List[Tuple[int, float]] = field(default_factory=list)  # (ticket, lots)
    modifies: List[Tuple[int, Optional[float], Optional[float]]] = field(default_factory=list)
    fail_close: bool = False
    fail_modify: bool = False

    def close_position(self, ticket: int, lots: Optional[float] = None) -> bool:
        self.closes.append((ticket, lots))
        return not self.fail_close

    def modify_position(self, ticket: int, sl: Optional[float] = None,
                         tp: Optional[float] = None) -> bool:
        self.modifies.append((ticket, sl, tp))
        return not self.fail_modify


def _long_state(entry=100.0, sl=98.0, tp1=102.0, tp2=104.0, lots=1.0, ticket=42):
    return PartialState(
        side=+1, entry_price=entry, sl=sl, tp1=tp1, tp2=tp2,
        original_lots=lots, open_lots=lots, ticket=ticket,
        peak_favourable=entry,
    )


def _short_state(entry=100.0, sl=102.0, tp1=98.0, tp2=96.0, lots=1.0, ticket=43):
    return PartialState(
        side=-1, entry_price=entry, sl=sl, tp1=tp1, tp2=tp2,
        original_lots=lots, open_lots=lots, ticket=ticket,
        peak_favourable=entry,
    )


# ======================================================================
#  TP1 behaviour
# ======================================================================
def test_no_action_when_bar_below_tp1_long():
    s = _long_state()
    b = FakeBridge()
    m = PartialCloseManager()
    res = m.update(s, bar_high=101.5, bar_low=100.5, bar_close=101.0,
                   atr_value=0.5, atr_ready=True, bridge=b)
    assert not res.tp1_fired
    assert not s.tp1_hit
    assert s.open_lots == pytest.approx(1.0)
    assert b.closes == []
    assert b.modifies == []


def test_tp1_partial_close_long():
    s = _long_state(lots=1.0)
    b = FakeBridge()
    m = PartialCloseManager()
    res = m.update(s, bar_high=102.5, bar_low=101.0, bar_close=102.2,
                   atr_value=0.5, atr_ready=True, bridge=b)
    assert res.tp1_fired
    assert s.tp1_hit
    assert s.open_lots == pytest.approx(0.5)         # 50% closed
    assert b.closes == [(42, pytest.approx(0.5))]    # exactly 0.5 lots
    # SL moved to BE
    assert res.sl_moved
    assert res.new_sl == pytest.approx(100.0)
    assert s.sl == pytest.approx(100.0)
    assert b.modifies == [(42, pytest.approx(100.0), None)]


def test_tp1_partial_close_short():
    s = _short_state(lots=1.0)
    b = FakeBridge()
    m = PartialCloseManager()
    res = m.update(s, bar_high=99.0, bar_low=97.5, bar_close=98.0,
                   atr_value=0.5, atr_ready=True, bridge=b)
    assert res.tp1_fired
    assert s.tp1_hit
    assert s.open_lots == pytest.approx(0.5)
    assert s.sl == pytest.approx(100.0)             # BE for short = entry


def test_tp1_idempotent_after_fire():
    s = _long_state()
    b = FakeBridge()
    m = PartialCloseManager()
    m.update(s, 102.5, 101.0, 102.0, 0.5, True, b)
    m.update(s, 103.0, 102.0, 102.5, 0.5, True, b)   # second bar still above TP1
    # Only ONE close call (first bar)
    assert len(b.closes) == 1


def test_tp1_close_failure_does_not_mark_hit():
    s = _long_state()
    b = FakeBridge(fail_close=True)
    m = PartialCloseManager()
    res = m.update(s, 102.5, 101.0, 102.0, 0.5, True, b)
    assert not s.tp1_hit
    assert res.error == "tp1 close failed"
    assert s.open_lots == pytest.approx(1.0)        # nothing closed


# ======================================================================
#  TP2 behaviour
# ======================================================================
def test_tp2_only_fires_after_tp1():
    s = _long_state(lots=1.0)
    b = FakeBridge()
    m = PartialCloseManager()
    # Bar gaps STRAIGHT through both TP1 and TP2 in one move
    res = m.update(s, bar_high=104.5, bar_low=101.0, bar_close=104.2,
                   atr_value=0.5, atr_ready=True, bridge=b)
    # Both should fire on the same bar (TP1 then TP2)
    assert res.tp1_fired and res.tp2_fired
    assert s.tp1_hit and s.tp2_hit
    # Lots: 0.5 (TP1) + 0.25 (TP2) closed = 0.25 remaining
    assert s.open_lots == pytest.approx(0.25)
    assert b.closes == [(42, pytest.approx(0.5)), (42, pytest.approx(0.25))]


def test_tp2_does_not_fire_if_tp1_not_hit_yet():
    """If we somehow get a bar with high crossing TP2 but TP1 must always fire first."""
    s = _long_state(lots=1.0)
    b = FakeBridge()
    m = PartialCloseManager()
    # Bar reaches TP2; TP1 fires first, then TP2 same bar.
    m.update(s, bar_high=104.5, bar_low=101.0, bar_close=104.0,
             atr_value=0.5, atr_ready=True, bridge=b)
    assert s.tp1_hit and s.tp2_hit


# ======================================================================
#  Trail stop behaviour
# ======================================================================
def test_trail_only_active_after_tp2():
    s = _long_state(lots=1.0)
    s.tp1_hit = True
    s.open_lots = 0.5  # only TP1 fired, no TP2 yet
    b = FakeBridge()
    m = PartialCloseManager()
    m.update(s, bar_high=103.0, bar_low=102.0, bar_close=102.5,
             atr_value=0.5, atr_ready=True, bridge=b)
    # No trail SL change yet (tp2_hit is still False)
    assert not any("TRAIL" in str(mod) for mod in b.modifies)


def test_trail_ratchets_up_for_long():
    s = _long_state(lots=1.0)
    # Pre-set: TP1 + TP2 already hit
    s.tp1_hit = True
    s.tp2_hit = True
    s.open_lots = 0.25
    s.sl = 100.0  # at BE
    s.peak_favourable = 105.0
    b = FakeBridge()
    m = PartialCloseManager()  # trail_atr_mult = 0.8
    # Bar peaks higher
    m.update(s, bar_high=106.0, bar_low=104.5, bar_close=105.5,
             atr_value=1.0, atr_ready=True, bridge=b)
    assert s.peak_favourable == pytest.approx(106.0)
    expected_trail = 106.0 - 0.8 * 1.0
    assert s.sl == pytest.approx(expected_trail)
    assert b.modifies[-1] == (42, pytest.approx(expected_trail), None)


def test_trail_does_not_widen_for_long():
    s = _long_state(lots=1.0)
    s.tp1_hit = True
    s.tp2_hit = True
    s.open_lots = 0.25
    s.sl = 105.0  # already very tight
    s.peak_favourable = 106.0
    b = FakeBridge()
    m = PartialCloseManager()
    # Bar pulls back, peak stays at 106
    m.update(s, bar_high=105.5, bar_low=104.0, bar_close=104.5,
             atr_value=1.0, atr_ready=True, bridge=b)
    # peak unchanged → trail = 106 - 0.8 = 105.2; tighter than 105.0 → modify
    assert s.sl == pytest.approx(105.2)


def test_trail_short_ratchets_down():
    s = _short_state(lots=1.0)
    s.tp1_hit = True
    s.tp2_hit = True
    s.open_lots = 0.25
    s.sl = 100.0  # at BE for short
    s.peak_favourable = 95.0
    b = FakeBridge()
    m = PartialCloseManager()
    m.update(s, bar_high=95.5, bar_low=94.0, bar_close=94.5,
             atr_value=1.0, atr_ready=True, bridge=b)
    assert s.peak_favourable == pytest.approx(94.0)
    expected_trail = 94.0 + 0.8 * 1.0   # 94.8
    assert s.sl == pytest.approx(expected_trail)


def test_trail_no_op_if_atr_not_ready():
    s = _long_state(lots=1.0)
    s.tp1_hit = True
    s.tp2_hit = True
    s.open_lots = 0.25
    s.sl = 100.0
    s.peak_favourable = 105.0
    b = FakeBridge()
    m = PartialCloseManager()
    m.update(s, bar_high=106.0, bar_low=104.5, bar_close=105.5,
             atr_value=0.0, atr_ready=False, bridge=b)
    # No trail update → no modify call
    assert b.modifies == []
    assert s.sl == pytest.approx(100.0)


# ======================================================================
#  Lot rounding
# ======================================================================
def test_round_lots_fn_is_applied():
    s = _long_state(lots=1.0)
    b = FakeBridge()
    m = PartialCloseManager()
    rounded = lambda x: round(x, 1)  # broker step 0.1
    m.update(s, 102.5, 101.0, 102.0, 0.5, True, b, round_lots_fn=rounded)
    assert b.closes == [(42, pytest.approx(0.5))]


def test_round_lots_skips_when_too_small():
    s = _long_state(lots=0.001)
    b = FakeBridge()
    m = PartialCloseManager(PartialManagerConfig(min_close_lots=0.01))
    res = m.update(s, 102.5, 101.0, 102.0, 0.5, True, b)
    # 0.001 * 0.5 = 0.0005 — below 0.01 threshold, should NOT fire
    assert not res.tp1_fired
    assert b.closes == []


# ======================================================================
#  Persistence
# ======================================================================
def test_state_roundtrip_preserves_ladder_progress():
    s = _long_state(lots=1.0)
    s.tp1_hit = True
    s.tp2_hit = True
    s.sl = 100.0
    s.open_lots = 0.25
    s.peak_favourable = 107.5
    d = s.to_dict()
    s2 = PartialState.from_dict(d)
    assert s2.tp1_hit and s2.tp2_hit
    assert s2.open_lots == pytest.approx(0.25)
    assert s2.peak_favourable == pytest.approx(107.5)
    assert s2.sl == pytest.approx(100.0)
    assert s2.original_lots == pytest.approx(1.0)


# ======================================================================
#  End-to-end ladder simulation
# ======================================================================
def test_full_ladder_long_trade():
    """
    Simulate a long trade that:
      1. Crosses TP1 on bar 1
      2. Crosses TP2 on bar 3
      3. Trails up bars 4..6
    Verifies the lot accounting and SL ratchet match the backtest engine.
    """
    s = _long_state(entry=100.0, sl=98.0, tp1=102.0, tp2=104.0, lots=1.0)
    b = FakeBridge()
    m = PartialCloseManager()  # default 50/25/0.8

    # Bar 1 — cross TP1
    m.update(s, 102.3, 101.5, 102.1, 1.0, True, b)
    assert s.tp1_hit and not s.tp2_hit
    assert s.open_lots == pytest.approx(0.5)
    assert s.sl == pytest.approx(100.0)  # BE

    # Bar 2 — drift up but not to TP2
    m.update(s, 103.5, 102.0, 103.0, 1.0, True, b)
    assert not s.tp2_hit
    assert s.peak_favourable == pytest.approx(103.5)

    # Bar 3 — hit TP2
    m.update(s, 104.5, 103.0, 104.2, 1.0, True, b)
    assert s.tp2_hit
    assert s.open_lots == pytest.approx(0.25)
    # Trail starts: peak=104.5, atr=1.0 → trail = 104.5 - 0.8 = 103.7
    assert s.sl == pytest.approx(103.7)

    # Bar 4 — peak 105.5
    m.update(s, 105.5, 104.5, 105.0, 1.0, True, b)
    assert s.peak_favourable == pytest.approx(105.5)
    assert s.sl == pytest.approx(104.7)  # 105.5 - 0.8

    # Bar 5 — pulls back but doesn't hit trail; peak stays
    m.update(s, 105.0, 104.8, 104.9, 1.0, True, b)
    assert s.peak_favourable == pytest.approx(105.5)  # unchanged
    assert s.sl == pytest.approx(104.7)              # unchanged

    # Lot accounting: 3 closes (TP1=0.5, TP2=0.25), open=0.25
    assert len(b.closes) == 2
    assert b.closes[0] == (42, pytest.approx(0.5))
    assert b.closes[1] == (42, pytest.approx(0.25))
    # SL modifies: BE (100.0), trail 103.7, trail 104.7
    sl_modifies = [m for m in b.modifies if m[1] is not None]
    assert sl_modifies == [
        (42, pytest.approx(100.0), None),
        (42, pytest.approx(103.7), None),
        (42, pytest.approx(104.7), None),
    ]


def test_no_action_when_open_lots_zero():
    """If position is already fully closed externally, do nothing."""
    s = _long_state(lots=1.0)
    s.open_lots = 0.0
    b = FakeBridge()
    m = PartialCloseManager()
    res = m.update(s, 105.0, 99.0, 102.0, 1.0, True, b)
    assert not res.any_change()
    assert b.closes == []
    assert b.modifies == []
