"""
test_close_px_inference.py — v30.2 sizer-feedback close-price inference.

The MT5 bridge (SHF_Bridge.mq5) does NOT report the broker's actual close
fill price when SL/TP fires. Previously v30 fell back to `last_m1_close`,
which under-counts both wins (TP fills past M1 close in our favour) and
losses (SL fills past M1 close against us). The bias is one-sided and
leaks into the Merton EWMA.

`V30Live._infer_broker_close_px` snaps to whichever of {SL, TP1} the price
crossed (or, on ambiguous bars, to the closer level). These tests pin down
that behaviour symbolically — no bridge / live state needed.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.live.v30_live import V30Live


def _fake_state(*, side: str, sl: float, tp1: float, last: float) -> SimpleNamespace:
    """Build the minimal state object _infer_broker_close_px reads."""
    return SimpleNamespace(
        open_side=side,
        open_sl=sl,
        open_tp1=tp1,
        last_m1_close=last,
    )


# Use V30Live as an unbound method holder — _infer_broker_close_px does not
# touch self, so we don't need to construct a full live runner.
infer = V30Live._infer_broker_close_px


# ---------------------------------------------------------------------
# LONG cases  (TP above entry, SL below entry)
# ---------------------------------------------------------------------

def test_long_tp_hit_obvious() -> None:
    """LONG with last close past TP → snap to TP."""
    st = _fake_state(side="LONG", sl=99.0, tp1=110.0, last=110.5)
    px, src = infer(None, st)
    assert px == 110.0
    assert src == "snap_tp1"


def test_long_sl_hit_obvious() -> None:
    """LONG with last close past SL → snap to SL."""
    st = _fake_state(side="LONG", sl=99.0, tp1=110.0, last=98.5)
    px, src = infer(None, st)
    assert px == 99.0
    assert src == "snap_sl"


def test_long_ambiguous_close_to_tp() -> None:
    """LONG, last close inside the range but closer to TP → snap to TP."""
    st = _fake_state(side="LONG", sl=99.0, tp1=110.0, last=109.0)
    px, src = infer(None, st)
    assert px == 110.0
    assert src == "snap_tp1"


def test_long_ambiguous_close_to_sl() -> None:
    """LONG, last close inside the range but closer to SL → snap to SL."""
    st = _fake_state(side="LONG", sl=99.0, tp1=110.0, last=100.0)
    px, src = infer(None, st)
    assert px == 99.0
    assert src == "snap_sl"


# ---------------------------------------------------------------------
# SHORT cases  (TP below entry, SL above entry)
# ---------------------------------------------------------------------

def test_short_tp_hit_obvious() -> None:
    """SHORT with last close past TP → snap to TP."""
    st = _fake_state(side="SHORT", sl=110.0, tp1=99.0, last=98.5)
    px, src = infer(None, st)
    assert px == 99.0
    assert src == "snap_tp1"


def test_short_sl_hit_obvious() -> None:
    """SHORT with last close past SL → snap to SL."""
    st = _fake_state(side="SHORT", sl=110.0, tp1=99.0, last=110.5)
    px, src = infer(None, st)
    assert px == 110.0
    assert src == "snap_sl"


def test_short_ambiguous_close_to_tp() -> None:
    """SHORT, last close inside the range but closer to TP → snap to TP."""
    st = _fake_state(side="SHORT", sl=110.0, tp1=99.0, last=100.0)
    px, src = infer(None, st)
    assert px == 99.0
    assert src == "snap_tp1"


def test_short_ambiguous_close_to_sl() -> None:
    """SHORT, last close inside the range but closer to SL → snap to SL."""
    st = _fake_state(side="SHORT", sl=110.0, tp1=99.0, last=109.0)
    px, src = infer(None, st)
    assert px == 110.0
    assert src == "snap_sl"


# ---------------------------------------------------------------------
# The actual production bug we fixed:  DE40 SHORT 2026-04-28
#   entry=24097.01  SL=24156.90  TP1=24014.68  last_m1_close=24026.32
#   Real TP1 fill paid +$271 ; M1 close estimate said +$198. The fix
#   must snap to TP1, not stay on M1 close.
# ---------------------------------------------------------------------

def test_real_de40_short_2026_04_28() -> None:
    st = _fake_state(side="SHORT", sl=24156.90, tp1=24014.68, last=24026.32)
    px, src = infer(None, st)
    assert px == 24014.68
    assert src == "snap_tp1"


# ---------------------------------------------------------------------
# Defensive: missing data → fall back gracefully without raising.
# ---------------------------------------------------------------------

def test_no_sl_falls_back_to_m1_close() -> None:
    st = _fake_state(side="LONG", sl=None, tp1=110.0, last=109.0)
    px, src = infer(None, st)
    assert px == 109.0
    assert src == "m1_close"


def test_no_tp_falls_back_to_m1_close() -> None:
    st = _fake_state(side="LONG", sl=99.0, tp1=None, last=100.0)
    px, src = infer(None, st)
    assert px == 100.0
    assert src == "m1_close"


def test_no_last_close_returns_zero_safely() -> None:
    st = _fake_state(side="LONG", sl=99.0, tp1=110.0, last=None)
    px, src = infer(None, st)
    assert px == 0.0
    assert src == "m1_close"


# ---------------------------------------------------------------------
# Sanity: the snap value must match one of the two real broker levels
# in every non-fallback branch (so realised_R cannot be silently wrong).
# ---------------------------------------------------------------------

@pytest.mark.parametrize("side,sl,tp1,last", [
    ("LONG",  99.0, 110.0, 110.5),
    ("LONG",  99.0, 110.0,  98.5),
    ("LONG",  99.0, 110.0, 105.0),
    ("SHORT", 110.0, 99.0,  98.5),
    ("SHORT", 110.0, 99.0, 110.5),
    ("SHORT", 110.0, 99.0, 105.0),
])
def test_snap_always_lands_on_real_level(side, sl, tp1, last) -> None:
    st = _fake_state(side=side, sl=sl, tp1=tp1, last=last)
    px, src = infer(None, st)
    assert src in ("snap_tp1", "snap_sl")
    assert px in (sl, tp1)
