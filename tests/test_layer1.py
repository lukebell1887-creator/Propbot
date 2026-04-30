"""
tests/test_layer1.py
====================

Unit tests for `src/execution/layer1.py` — the V31 Layer 1 slippage
defense. Lock the parity contract between the live `decide_exit()` and
the backtest `apply_layer1_slip()` so that the proof matrix
(Scripts/v31_final_proof_matrix.py) remains valid.

Coverage:
    1. Per-symbol caps match v31_proof_pipeline.py LAYER1_CAPS exactly
    2. apply_layer1_slip() math:
       - 0/negative slip → 0
       - within cap → unchanged
       - beyond cap → cap * 1.5 (time-fallback)
    3. decide_exit() branches (long+short × within_cap, beyond_cap, envelope_expired):
       - CLOSE_NOW, WAIT, FALLBACK_CLOSE
    4. Parity: for every realised slip, decide_exit() output is consistent
       with apply_layer1_slip()'s number
    5. Emergency SL placement: cap × 1.5 from trigger, in correct direction
    6. config_summary() round-trip (JSON-serialisable)
"""
from __future__ import annotations

import json

import pytest

from src.execution import layer1


# ============================================================================
# 1) PER-SYMBOL CAPS — must match v31_proof_pipeline.py exactly
# ============================================================================
def test_caps_match_proof_pipeline():
    """If these change, the MC proof matrix is invalidated."""
    expected = {
        "DE40":   5.0,
        "US30":   5.0,
        "US500":  3.0,
        "XAUUSD": 1.0,
    }
    assert layer1.LAYER1_CAPS == expected, (
        f"Layer 1 caps drifted from MC proof. Expected {expected}, "
        f"got {layer1.LAYER1_CAPS}. Re-run v31_final_proof_matrix.py."
    )


def test_cap_for_known_symbols():
    assert layer1.cap_for("DE40")   == 5.0
    assert layer1.cap_for("US30")   == 5.0
    assert layer1.cap_for("US500")  == 3.0
    assert layer1.cap_for("XAUUSD") == 1.0


def test_cap_for_unknown_symbol_defaults_to_5():
    """Unknown symbols use the index-tier default — defensive."""
    assert layer1.cap_for("FOOBAR") == 5.0
    assert layer1.cap_for("")       == 5.0


def test_fallback_mult_locked():
    assert layer1.LAYER1_FALLBACK_MULT == 1.5
    assert layer1.LAYER1_EMERGENCY_SL_MULT == 1.5


def test_envelope_locked_at_60s():
    assert layer1.LAYER1_ENVELOPE_S == 60.0


# ============================================================================
# 2) apply_layer1_slip() math — the backtest fill model
# ============================================================================
class TestApplyLayer1Slip:
    @pytest.mark.parametrize("slip,expected", [
        (0.0,   0.0),
        (-1.0,  0.0),     # negative shouldn't happen, but defensive
        (-0.01, 0.0),
    ])
    def test_zero_or_negative_returns_zero(self, slip, expected):
        for sym in ("DE40", "US30", "US500", "XAUUSD"):
            assert layer1.apply_layer1_slip(sym, slip) == expected

    @pytest.mark.parametrize("symbol,slip", [
        ("DE40",   0.5),
        ("DE40",   2.0),
        ("DE40",   4.99),
        ("DE40",   5.0),    # exactly at cap → still within
        ("US30",   1.0),
        ("US30",   5.0),
        ("US500",  1.5),
        ("US500",  3.0),
        ("XAUUSD", 0.25),
        ("XAUUSD", 1.0),
    ])
    def test_within_cap_unchanged(self, symbol, slip):
        assert layer1.apply_layer1_slip(symbol, slip) == slip

    @pytest.mark.parametrize("symbol,slip,expected", [
        ("DE40",   5.01,  7.5),    # 5 * 1.5
        ("DE40",   10.0,  7.5),
        ("DE40",   30.0,  7.5),
        ("US30",   5.5,   7.5),
        ("US30",   14.82, 7.5),    # today's actual US30 catastrophic
        ("US30",   100.0, 7.5),
        ("US500",  3.5,   4.5),    # 3 * 1.5
        ("US500",  20.0,  4.5),
        ("XAUUSD", 1.5,   1.5),    # 1 * 1.5
        ("XAUUSD", 50.0,  1.5),
    ])
    def test_beyond_cap_returns_fallback(self, symbol, slip, expected):
        assert layer1.apply_layer1_slip(symbol, slip) == expected


# ============================================================================
# 3) decide_exit() branches — the live SL handler
# ============================================================================
class TestDecideExit:
    """Each test isolates one branch of the decision tree."""

    # Branch 1: not yet breached
    def test_not_yet_breached_long(self):
        # long DE40, SL = 23500, current price still above SL
        d = layer1.decide_exit(
            symbol="DE40", side=+1,
            sl_trigger_px=23500.0, current_px=23510.0,
            seconds_since_breach=0.0,
        )
        assert d.action == "WAIT"
        assert d.raw_slip_pts == 0.0
        assert "not yet breached" in d.reason

    def test_not_yet_breached_short(self):
        # short DE40, SL = 23500, current price still below SL
        d = layer1.decide_exit(
            symbol="DE40", side=-1,
            sl_trigger_px=23500.0, current_px=23490.0,
            seconds_since_breach=0.0,
        )
        assert d.action == "WAIT"
        assert d.raw_slip_pts == 0.0

    # Branch 2: within cap → CLOSE_NOW
    @pytest.mark.parametrize("symbol,side,sl,current,slip", [
        ("DE40",   +1, 23500.0, 23498.0,  2.0),    # long, 2pt slip ≤ 5pt cap
        ("DE40",   -1, 23500.0, 23502.0,  2.0),    # short, 2pt slip ≤ 5pt cap
        ("DE40",   +1, 23500.0, 23495.0,  5.0),    # exactly at cap
        ("US30",   +1, 49000.0, 48995.0,  5.0),    # at US30 cap
        ("US500",  +1,  7200.0,  7198.0,  2.0),    # within US500 cap
        ("XAUUSD", -1,  4600.0,  4600.5,  0.5),    # within XAU cap
    ])
    def test_within_cap_closes_now(self, symbol, side, sl, current, slip):
        d = layer1.decide_exit(
            symbol=symbol, side=side,
            sl_trigger_px=sl, current_px=current,
            seconds_since_breach=0.0,
        )
        assert d.action == "CLOSE_NOW", (
            f"{symbol} side={side}: expected CLOSE_NOW for slip={slip}, "
            f"got {d.action}"
        )
        assert d.raw_slip_pts == pytest.approx(slip, abs=1e-9)
        assert d.cap_pts == layer1.cap_for(symbol)
        assert "within cap" in d.reason

    # Branch 3: beyond cap, envelope still open → WAIT
    @pytest.mark.parametrize("seconds_in", [0.0, 5.0, 30.0, 59.99])
    def test_beyond_cap_inside_envelope_waits(self, seconds_in):
        # long DE40, SL = 23500, market gapped to 23485 (15pt slip > 5pt cap)
        d = layer1.decide_exit(
            symbol="DE40", side=+1,
            sl_trigger_px=23500.0, current_px=23485.0,
            seconds_since_breach=seconds_in,
        )
        assert d.action == "WAIT"
        assert d.raw_slip_pts == 15.0
        assert d.cap_pts == 5.0
        assert "envelope" in d.reason

    # Branch 4: beyond cap AND envelope expired → FALLBACK_CLOSE
    @pytest.mark.parametrize("seconds_in", [60.0, 60.01, 120.0, 3600.0])
    def test_beyond_cap_envelope_expired_falls_back(self, seconds_in):
        d = layer1.decide_exit(
            symbol="DE40", side=+1,
            sl_trigger_px=23500.0, current_px=23485.0,
            seconds_since_breach=seconds_in,
        )
        assert d.action == "FALLBACK_CLOSE"
        assert d.raw_slip_pts == 15.0
        assert "expired" in d.reason

    def test_short_beyond_cap_inside_envelope(self):
        # short DE40, market gapped UP past SL by 12pt (> 5pt cap)
        d = layer1.decide_exit(
            symbol="DE40", side=-1,
            sl_trigger_px=23500.0, current_px=23512.0,
            seconds_since_breach=10.0,
        )
        assert d.action == "WAIT"
        assert d.raw_slip_pts == 12.0


# ============================================================================
# 4) PARITY CONTRACT — backtest math == live decision math
# ============================================================================
class TestParityWithBacktest:
    """For every (symbol, slip) sample the backtest sees, verify the
    live engine takes a CONSISTENT action that produces the SAME
    final realised slip when followed through.

    Contract:
        - if decide_exit returns CLOSE_NOW   →  realised_slip = raw_slip
        - if decide_exit returns FALLBACK_CLOSE → realised_slip = cap*1.5
        - apply_layer1_slip(symbol, raw_slip) MUST equal the realised_slip
    """

    @pytest.mark.parametrize("symbol,raw_slip", [
        ("DE40",   0.5),
        ("DE40",   3.0),
        ("DE40",   4.99),
        ("DE40",   5.0),
        ("DE40",   5.01),
        ("DE40",   10.0),
        ("DE40",   30.0),
        ("US30",   2.0),
        ("US30",   5.0),
        ("US30",   14.82),    # today's actual catastrophic
        ("US30",   100.0),
        ("US500",  1.0),
        ("US500",  3.0),
        ("US500",  3.01),
        ("US500",  20.0),
        ("XAUUSD", 0.25),
        ("XAUUSD", 1.0),
        ("XAUUSD", 1.5),
        ("XAUUSD", 50.0),
    ])
    def test_within_cap_lives_match_backtest(self, symbol, raw_slip):
        """If raw_slip ≤ cap, decide_exit returns CLOSE_NOW with that
        same raw_slip, AND apply_layer1_slip returns the same number."""
        cap = layer1.cap_for(symbol)
        backtest_slip = layer1.apply_layer1_slip(symbol, raw_slip)

        # Long-side, immediate evaluation (envelope open)
        sl = 1000.0
        cur = sl - raw_slip  # long, adverse below
        d = layer1.decide_exit(
            symbol=symbol, side=+1,
            sl_trigger_px=sl, current_px=cur,
            seconds_since_breach=0.0,
        )

        if raw_slip <= cap:
            # Within cap: close-now case
            assert d.action == "CLOSE_NOW"
            # Realised = raw (we close at current price)
            realised_live = d.raw_slip_pts
            assert realised_live == pytest.approx(raw_slip, abs=1e-9)
            assert backtest_slip == pytest.approx(raw_slip, abs=1e-9)
        else:
            # Beyond cap: WAIT (envelope just opened)
            assert d.action == "WAIT"
            # Now jump envelope to expired — backtest assumes fallback
            d2 = layer1.decide_exit(
                symbol=symbol, side=+1,
                sl_trigger_px=sl, current_px=cur,
                seconds_since_breach=layer1.LAYER1_ENVELOPE_S + 1.0,
            )
            assert d2.action == "FALLBACK_CLOSE"
            # Backtest returns cap * 1.5 — that's what the bot's
            # final fill realises (price still at cur, broker fills
            # at emergency SL = cap*1.5 worse).
            assert backtest_slip == pytest.approx(cap * 1.5, abs=1e-9)


# ============================================================================
# 5) EMERGENCY SL placement
# ============================================================================
class TestEmergencySL:
    def test_emergency_offset_long(self):
        # Long: emergency SL is BELOW the trigger
        d = layer1.decide_exit(
            symbol="DE40", side=+1,
            sl_trigger_px=23500.0, current_px=23499.0,
            seconds_since_breach=0.0,
        )
        # cap=5, emergency=5*1.5=7.5 → emergency_sl_px = 23500 - 7.5
        assert d.emergency_sl_px == pytest.approx(23492.5, abs=1e-9)

    def test_emergency_offset_short(self):
        # Short: emergency SL is ABOVE the trigger
        d = layer1.decide_exit(
            symbol="DE40", side=-1,
            sl_trigger_px=23500.0, current_px=23501.0,
            seconds_since_breach=0.0,
        )
        assert d.emergency_sl_px == pytest.approx(23507.5, abs=1e-9)

    def test_emergency_offset_function(self):
        assert layer1.emergency_sl_offset_for("DE40")   == 5.0  * 1.5
        assert layer1.emergency_sl_offset_for("US30")   == 5.0  * 1.5
        assert layer1.emergency_sl_offset_for("US500")  == 3.0  * 1.5
        assert layer1.emergency_sl_offset_for("XAUUSD") == 1.0  * 1.5


# ============================================================================
# 6) Decision serialisation
# ============================================================================
class TestSerialization:
    def test_decision_to_jsonable(self):
        d = layer1.decide_exit(
            symbol="DE40", side=+1,
            sl_trigger_px=23500.0, current_px=23498.0,
            seconds_since_breach=0.0,
        )
        as_json = d.to_jsonable()

        # All 10 fields present
        assert set(as_json.keys()) == {
            "action", "symbol", "side", "sl_trigger_px", "current_px",
            "raw_slip_pts", "cap_pts", "emergency_sl_px",
            "seconds_since_breach", "reason",
        }
        # Round-trip JSON-safe
        s = json.dumps(as_json)
        rt = json.loads(s)
        assert rt["action"] == d.action
        assert rt["raw_slip_pts"] == d.raw_slip_pts

    def test_config_summary_is_jsonable(self):
        cfg = layer1.config_summary()
        s = json.dumps(cfg)   # must not raise
        rt = json.loads(s)
        assert rt["layer1_caps"]["DE40"] == 5.0
        assert rt["layer1_envelope_s"] == 60.0
        assert rt["layer1_fallback_mult"] == 1.5


# ============================================================================
# 7) The "today's US30 14.82pt slip" regression — the trade that triggered
#     this whole project. Verify defense kicks in correctly.
# ============================================================================
def test_today_us30_14_82pt_regression():
    """The actual incident: US30 SHORT, SL hit, broker filled 14.82pt
    above SL (catastrophic 0.74 adversity). With Layer 1, this becomes
    a 7.5pt fallback close — saves $1,762 over the 3-month sample."""
    sl_trigger = 49000.0
    actual_fill = sl_trigger + 14.82  # short, adverse = above

    # First check: backtest fill model says cap*1.5 = 7.5pt
    realised_slip = layer1.apply_layer1_slip("US30", 14.82)
    assert realised_slip == pytest.approx(7.5, abs=1e-9), (
        "today's US30 catastrophic slip should map to fallback"
    )

    # Second check: live engine, 60s+ after breach → FALLBACK_CLOSE
    d_late = layer1.decide_exit(
        symbol="US30", side=-1,
        sl_trigger_px=sl_trigger, current_px=actual_fill,
        seconds_since_breach=61.0,
    )
    assert d_late.action == "FALLBACK_CLOSE"
    assert d_late.raw_slip_pts == pytest.approx(14.82, abs=1e-9)
    assert d_late.cap_pts == 5.0

    # Third check: live engine, immediately at breach → WAIT (envelope)
    d_now = layer1.decide_exit(
        symbol="US30", side=-1,
        sl_trigger_px=sl_trigger, current_px=actual_fill,
        seconds_since_breach=0.0,
    )
    assert d_now.action == "WAIT", (
        "On gap, bot should WAIT for envelope before forcing fallback"
    )

    # Fourth check: emergency SL placement is at +7.5pt
    assert d_now.emergency_sl_px == pytest.approx(sl_trigger + 7.5, abs=1e-9)
