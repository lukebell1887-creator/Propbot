"""
src/execution/layer1_tracker.py
================================

Stateful per-ticket Layer-1 envelope tracker. Wraps `decide_exit()` from
`src.execution.layer1` and remembers when each position FIRST breached
its SL trigger so the 60-second envelope can be honoured across many
poll cycles.

Live engine usage
-----------------
Instantiate once at startup:

    self.layer1 = Layer1Tracker()

In the per-position poll loop, when you have a position with a known
SL trigger:

    decision = self.layer1.update_and_decide(
        ticket=pos.ticket,
        symbol=pos.symbol,
        side=+1 if pos.type == "BUY" else -1,
        sl_trigger_px=state.original_sl,
        current_px=current_bid_or_ask_for_side,
        now=time.time(),
    )

    if decision.action == "CLOSE_NOW" or decision.action == "FALLBACK_CLOSE":
        bridge.close_position(ticket)
        self.layer1.clear(ticket)
        # log decision.to_jsonable() to v30_live_slippage.jsonl

When you place an order, ALSO use this module's helper to compute the
emergency SL price the broker should hold:

    emergency_sl = original_sl - emergency_sl_offset_for(symbol)   # long
    emergency_sl = original_sl + emergency_sl_offset_for(symbol)   # short
    bridge.send_order(... sl=emergency_sl ...)

Threadsafe? No — the tracker is only updated/read by the live engine's
single bot-thread (the same thread that polls positions, decides, and
sends close orders). If you ever multi-thread the live engine, wrap the
tracker in a Lock.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from src.execution.layer1 import (
    LAYER1_ENVELOPE_S,
    Layer1Decision,
    decide_exit,
    emergency_sl_offset_for,
)


@dataclass
class _BreachState:
    """Per-ticket state — recorded the first time a position is observed
    to be in breach of its SL trigger."""
    first_breach_t: float    # epoch seconds when SL was first crossed
    cap_pts: float           # captured at first breach for telemetry


class Layer1Tracker:
    """Tracks SL-breach timestamps per ticket so the 60s envelope works
    across poll cycles."""

    def __init__(self, envelope_s: float = LAYER1_ENVELOPE_S):
        self._envelope_s = float(envelope_s)
        self._state: dict[int, _BreachState] = {}

    # ----------------------------------------------------------------
    # PUBLIC API
    # ----------------------------------------------------------------
    def update_and_decide(
        self,
        ticket: int,
        symbol: str,
        side: int,
        sl_trigger_px: float,
        current_px: float,
        now: float,
    ) -> Layer1Decision:
        """
        Call once per poll cycle for each open position. Updates internal
        breach-state and returns a Layer1Decision.

        Args:
            ticket:        unique broker ticket id
            symbol:        broker-internal symbol name
            side:          +1 long / -1 short
            sl_trigger_px: original (intended) SL price
            current_px:    current adverse-side price (bid for long, ask for short)
            now:           epoch seconds (e.g. time.time())
        """
        # Compute raw breach amount in adverse direction
        if side > 0:
            raw_slip = max(0.0, sl_trigger_px - current_px)
        else:
            raw_slip = max(0.0, current_px - sl_trigger_px)

        # Capture first-breach time, if applicable
        if raw_slip > 0:
            if ticket not in self._state:
                self._state[ticket] = _BreachState(
                    first_breach_t=now,
                    cap_pts=0.0,  # filled in below
                )
            seconds_since_breach = now - self._state[ticket].first_breach_t
        else:
            # Price came back inside SL — clear any previous breach state
            self._state.pop(ticket, None)
            seconds_since_breach = 0.0

        decision = decide_exit(
            symbol=symbol, side=side,
            sl_trigger_px=sl_trigger_px, current_px=current_px,
            seconds_since_breach=seconds_since_breach,
            envelope_s=self._envelope_s,
        )

        # Cache cap for telemetry on next reads
        if ticket in self._state:
            self._state[ticket] = _BreachState(
                first_breach_t=self._state[ticket].first_breach_t,
                cap_pts=decision.cap_pts,
            )

        return decision

    def clear(self, ticket: int) -> None:
        """Drop tracked state for a ticket (call when position closes)."""
        self._state.pop(ticket, None)

    def is_in_breach(self, ticket: int) -> bool:
        return ticket in self._state

    def seconds_in_breach(self, ticket: int, now: float) -> float:
        state = self._state.get(ticket)
        if state is None:
            return 0.0
        return now - state.first_breach_t

    def snapshot(self, now: float) -> list[dict]:
        """Telemetry helper — return a JSON-serialisable view of all
        active breach trackers. Useful for the heartbeat log."""
        return [
            {
                "ticket": t,
                "first_breach_t": s.first_breach_t,
                "seconds_in_breach": now - s.first_breach_t,
                "cap_pts": s.cap_pts,
            }
            for t, s in self._state.items()
        ]

    @property
    def envelope_s(self) -> float:
        return self._envelope_s

    @property
    def n_breached(self) -> int:
        return len(self._state)


__all__ = ["Layer1Tracker", "emergency_sl_offset_for"]
