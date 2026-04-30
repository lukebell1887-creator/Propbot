"""
tests/test_layer1_tracker.py
=============================

Lock the breach-time book-keeping behaviour of `Layer1Tracker`.
This is the stateful glue v30_live uses to honour the 60-second
envelope across many poll cycles.
"""
from __future__ import annotations

import pytest

from src.execution.layer1 import LAYER1_ENVELOPE_S
from src.execution.layer1_tracker import Layer1Tracker


# ----------------------------------------------------------------------------
# Basic state lifecycle
# ----------------------------------------------------------------------------
class TestTrackerLifecycle:

    def test_no_breach_no_state(self):
        tr = Layer1Tracker()
        # Long DE40, current price still above SL
        d = tr.update_and_decide(
            ticket=111, symbol="DE40", side=+1,
            sl_trigger_px=23500.0, current_px=23510.0,
            now=1000.0,
        )
        assert d.action == "WAIT"
        assert tr.n_breached == 0
        assert tr.is_in_breach(111) is False

    def test_first_breach_records_timestamp(self):
        tr = Layer1Tracker()
        d = tr.update_and_decide(
            ticket=222, symbol="DE40", side=+1,
            sl_trigger_px=23500.0, current_px=23485.0,  # 15pt > 5pt cap
            now=1000.0,
        )
        assert d.action == "WAIT"            # in envelope
        assert d.seconds_since_breach == 0.0
        assert tr.is_in_breach(222) is True
        assert tr.n_breached == 1
        assert tr.seconds_in_breach(222, now=1010.0) == 10.0

    def test_envelope_grows_across_calls(self):
        tr = Layer1Tracker()
        # First call records breach
        tr.update_and_decide(
            ticket=333, symbol="DE40", side=+1,
            sl_trigger_px=23500.0, current_px=23485.0,
            now=2000.0,
        )
        # 30s later — still in envelope
        d2 = tr.update_and_decide(
            ticket=333, symbol="DE40", side=+1,
            sl_trigger_px=23500.0, current_px=23485.0,
            now=2030.0,
        )
        assert d2.action == "WAIT"
        assert d2.seconds_since_breach == 30.0

        # 61s later — envelope expired → FALLBACK_CLOSE
        d3 = tr.update_and_decide(
            ticket=333, symbol="DE40", side=+1,
            sl_trigger_px=23500.0, current_px=23485.0,
            now=2061.0,
        )
        assert d3.action == "FALLBACK_CLOSE"
        assert d3.seconds_since_breach == 61.0

    def test_within_cap_closes_now_and_does_not_arm_envelope(self):
        tr = Layer1Tracker()
        d = tr.update_and_decide(
            ticket=444, symbol="DE40", side=+1,
            sl_trigger_px=23500.0, current_px=23498.0,  # 2pt within cap
            now=3000.0,
        )
        assert d.action == "CLOSE_NOW"
        # Even though we breached, the engine will close immediately so
        # the envelope state is irrelevant. We DO record it for telemetry,
        # but a follow-up clear() drops it.
        assert tr.is_in_breach(444) is True
        tr.clear(444)
        assert tr.is_in_breach(444) is False

    def test_price_recovery_clears_state(self):
        """If price goes back above the trigger, the breach state is
        wiped — a future re-breach starts a NEW envelope."""
        tr = Layer1Tracker()
        # 1) breach
        tr.update_and_decide(
            ticket=555, symbol="US30", side=-1,
            sl_trigger_px=49000.0, current_px=49010.0,  # short, 10pt above
            now=5000.0,
        )
        assert tr.is_in_breach(555)

        # 2) recover (price drops below SL again on a short)
        d_back = tr.update_and_decide(
            ticket=555, symbol="US30", side=-1,
            sl_trigger_px=49000.0, current_px=48995.0,  # back below
            now=5010.0,
        )
        assert d_back.action == "WAIT"
        assert d_back.raw_slip_pts == 0.0
        assert tr.is_in_breach(555) is False

        # 3) re-breach 30s later — envelope starts fresh
        d_new = tr.update_and_decide(
            ticket=555, symbol="US30", side=-1,
            sl_trigger_px=49000.0, current_px=49010.0,
            now=5040.0,
        )
        assert d_new.action == "WAIT"  # within 60s envelope, still
        assert d_new.seconds_since_breach == 0.0  # FRESH start
        assert tr.is_in_breach(555)


# ----------------------------------------------------------------------------
# Multi-position isolation
# ----------------------------------------------------------------------------
class TestMultiPosition:

    def test_independent_envelopes_per_ticket(self):
        tr = Layer1Tracker()
        # Two positions breach at different times
        tr.update_and_decide(
            ticket=1, symbol="DE40", side=+1,
            sl_trigger_px=23500, current_px=23485,
            now=10000.0,
        )
        tr.update_and_decide(
            ticket=2, symbol="US30", side=+1,
            sl_trigger_px=49000, current_px=48980,  # 20pt > 5pt cap
            now=10040.0,
        )

        # At t=10070, ticket 1 is at 70s (expired) but ticket 2 is at 30s
        d1 = tr.update_and_decide(
            ticket=1, symbol="DE40", side=+1,
            sl_trigger_px=23500, current_px=23485,
            now=10070.0,
        )
        d2 = tr.update_and_decide(
            ticket=2, symbol="US30", side=+1,
            sl_trigger_px=49000, current_px=48980,
            now=10070.0,
        )
        assert d1.action == "FALLBACK_CLOSE"
        assert d2.action == "WAIT"

    def test_clear_one_does_not_affect_another(self):
        tr = Layer1Tracker()
        tr.update_and_decide(ticket=1, symbol="DE40", side=+1,
                             sl_trigger_px=23500, current_px=23485, now=20000.0)
        tr.update_and_decide(ticket=2, symbol="DE40", side=+1,
                             sl_trigger_px=23500, current_px=23485, now=20000.0)
        assert tr.n_breached == 2
        tr.clear(1)
        assert tr.n_breached == 1
        assert tr.is_in_breach(2)


# ----------------------------------------------------------------------------
# Telemetry
# ----------------------------------------------------------------------------
class TestSnapshot:
    def test_empty_snapshot(self):
        tr = Layer1Tracker()
        assert tr.snapshot(now=0.0) == []

    def test_snapshot_shape(self):
        tr = Layer1Tracker()
        tr.update_and_decide(
            ticket=99, symbol="US30", side=+1,
            sl_trigger_px=49000, current_px=48980,
            now=1000.0,
        )
        snap = tr.snapshot(now=1015.0)
        assert len(snap) == 1
        rec = snap[0]
        assert rec["ticket"] == 99
        assert rec["first_breach_t"] == 1000.0
        assert rec["seconds_in_breach"] == 15.0
        assert rec["cap_pts"] == 5.0   # US30 cap


# ----------------------------------------------------------------------------
# Configurable envelope window
# ----------------------------------------------------------------------------
class TestCustomEnvelope:
    def test_short_envelope_for_aggressive_mode(self):
        tr = Layer1Tracker(envelope_s=10.0)
        tr.update_and_decide(ticket=1, symbol="DE40", side=+1,
                             sl_trigger_px=23500, current_px=23485,
                             now=0.0)
        # At t=11s, exceeds 10s window → fallback
        d = tr.update_and_decide(ticket=1, symbol="DE40", side=+1,
                                 sl_trigger_px=23500, current_px=23485,
                                 now=11.0)
        assert d.action == "FALLBACK_CLOSE"

    def test_default_is_60s(self):
        tr = Layer1Tracker()
        assert tr.envelope_s == LAYER1_ENVELOPE_S == 60.0
