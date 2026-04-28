"""
src/live/partial_manager.py
===========================
TP1 / TP2 / ATR-trail partial-close manager — live engine port of
`src/orb_engine_v20.py`'s in-bar exit ladder.

Identical math to the v30 backtest:
  * On TP1 cross : market-close `tp1_close_frac` (default 0.50) of the
                   original lots; ratchet broker SL up to break-even.
  * On TP2 cross : market-close `tp2_close_frac` (default 0.25) of the
                   original lots.
  * After TP2    : track peak favourable excursion; trail broker SL at
                   `peak ± trail_atr_mult × ATR(14)`. Trail only ratchets
                   in the favourable direction (never widens).

Designed to be **side-effect-only and testable**:
  * The `update()` method takes a pure `PartialState`, the latest M1 bar,
    a current ATR value, and a `bridge` handle (any object with the same
    `close_position(ticket, lots)` and `modify_position(ticket, sl)`
    signatures as `src.execution.mt5_bridge.MT5Bridge`).
  * It returns a `PartialUpdateResult` describing what happened, so the
    caller can persist state, write to the trade log, etc.

Concurrency
-----------
Caller is responsible for serialising calls per (symbol, ticket).  The
v30 live runner only processes M1 bars on a single asyncio task, so this
is automatic.

Race conditions vs broker
-------------------------
If the broker SL fires concurrently with this method (very rare on M1
boundaries), `bridge.close_position()` will fail-soft because the ticket
no longer exists.  The caller's broker-sync routine then picks up the
POS_CLOSED_BY_BROKER event on the next tick and snaps the realised price
to the current SL via the existing close-px inference logic.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Protocol, runtime_checkable

log = logging.getLogger("v30.partials")


# ----------------------------------------------------------------------
#  Protocols (duck-typing — bridge can be a real MT5Bridge or a fake)
# ----------------------------------------------------------------------
@runtime_checkable
class _BridgeLike(Protocol):
    def close_position(self, ticket: int, lots: Optional[float] = ...) -> bool: ...
    def modify_position(self, ticket: int, sl: Optional[float] = ...,
                         tp: Optional[float] = ...) -> bool: ...


# ----------------------------------------------------------------------
#  Per-position state owned by the live engine and serialised to disk
# ----------------------------------------------------------------------
@dataclass
class PartialState:
    """Mutable state for one open position's partial-close ladder."""

    side: int                 # +1 long, -1 short
    entry_price: float
    sl: float                 # CURRENT broker SL (may have been moved up)
    tp1: float                # absolute price level
    tp2: float                # absolute price level
    original_lots: float      # full lot size at entry (never changes)
    open_lots: float          # remaining lots NOT yet closed
    ticket: int               # broker ticket
    # ladder progress flags
    tp1_hit: bool = False
    tp2_hit: bool = False
    # peak favourable excursion price (for trail) — initialised to entry_price
    peak_favourable: float = 0.0

    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "side":            int(self.side),
            "entry_price":     float(self.entry_price),
            "sl":              float(self.sl),
            "tp1":             float(self.tp1),
            "tp2":             float(self.tp2),
            "original_lots":   float(self.original_lots),
            "open_lots":       float(self.open_lots),
            "ticket":          int(self.ticket),
            "tp1_hit":         bool(self.tp1_hit),
            "tp2_hit":         bool(self.tp2_hit),
            "peak_favourable": float(self.peak_favourable),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PartialState":
        return cls(
            side=int(d["side"]),
            entry_price=float(d["entry_price"]),
            sl=float(d["sl"]),
            tp1=float(d["tp1"]),
            tp2=float(d["tp2"]),
            original_lots=float(d["original_lots"]),
            open_lots=float(d.get("open_lots", d.get("original_lots", 0.0))),
            ticket=int(d["ticket"]),
            tp1_hit=bool(d.get("tp1_hit", False)),
            tp2_hit=bool(d.get("tp2_hit", False)),
            peak_favourable=float(d.get("peak_favourable", d.get("entry_price", 0.0))),
        )


# ----------------------------------------------------------------------
#  Result of a single update() — for logging / telemetry
# ----------------------------------------------------------------------
@dataclass
class PartialUpdateResult:
    tp1_fired: bool = False
    tp2_fired: bool = False
    sl_moved: bool = False
    new_sl: Optional[float] = None
    closed_lots: float = 0.0    # lots closed THIS update
    fully_closed: bool = False  # last 25% trail-stop fired
    error: Optional[str] = None

    def any_change(self) -> bool:
        return (
            self.tp1_fired or self.tp2_fired or self.sl_moved
            or self.closed_lots > 0.0 or self.fully_closed
        )


# ----------------------------------------------------------------------
#  Config
# ----------------------------------------------------------------------
@dataclass
class PartialManagerConfig:
    tp1_close_frac: float = 0.50
    tp2_close_frac: float = 0.25
    trail_atr_mult: float = 0.8
    # Sanity floor on lots to close (broker min_lot enforced by caller via
    # round_to_step); we just refuse to issue a 0-lot close.
    min_close_lots: float = 0.001
    # If True, after TP1 we move broker SL to entry_price (break-even).
    # Backtest behaviour — kept togglable for safety drills.
    move_sl_to_be_after_tp1: bool = True


# ----------------------------------------------------------------------
#  Manager
# ----------------------------------------------------------------------
class PartialCloseManager:
    """
    Stateless service object.  All mutable state lives in `PartialState`
    instances passed to `update()`.

    Call once per M1 bar BEFORE your normal broker-sync / time-stop logic.
    """

    def __init__(self, cfg: Optional[PartialManagerConfig] = None) -> None:
        self.cfg = cfg or PartialManagerConfig()

    # ------------------------------------------------------------------
    def update(
        self,
        state: PartialState,
        bar_high: float,
        bar_low: float,
        bar_close: float,
        atr_value: float,
        atr_ready: bool,
        bridge: _BridgeLike,
        round_lots_fn=None,   # Optional[Callable[[float], float]] — broker step rounding
        symbol: str = "",     # for logging only
    ) -> PartialUpdateResult:
        """
        Advance the ladder for one M1 bar.

        Order of operations (matches `orb_engine_v20.py::_step_position`):
          1) Update peak favourable excursion using bar high/low.
          2) Check TP1 — partial close + (optionally) move SL to BE.
          3) Check TP2 — partial close.
          4) After TP2, compute trail SL from peak, ratchet broker SL up
             (long) or down (short) if it tightens.

        Returns a `PartialUpdateResult` describing what changed.
        """
        res = PartialUpdateResult()
        if state.open_lots <= 0.0:
            return res

        # 1) Peak favourable excursion
        if state.side > 0:
            if bar_high > state.peak_favourable:
                state.peak_favourable = float(bar_high)
        else:
            if bar_low < state.peak_favourable or state.peak_favourable == 0.0:
                state.peak_favourable = float(bar_low)

        # ---------------------------------------------------------------- TP1
        if not state.tp1_hit:
            crossed = (
                (state.side > 0 and bar_high >= state.tp1) or
                (state.side < 0 and bar_low  <= state.tp1)
            )
            if crossed:
                lots_to_close = state.original_lots * self.cfg.tp1_close_frac
                if round_lots_fn is not None:
                    lots_to_close = round_lots_fn(lots_to_close)
                lots_to_close = min(lots_to_close, state.open_lots)
                if lots_to_close >= self.cfg.min_close_lots:
                    ok = self._safe_close(bridge, state.ticket, lots_to_close, symbol, "TP1")
                    if ok:
                        state.tp1_hit = True
                        state.open_lots = max(0.0, state.open_lots - lots_to_close)
                        res.tp1_fired = True
                        res.closed_lots += lots_to_close
                        # Move broker SL to break-even
                        if self.cfg.move_sl_to_be_after_tp1:
                            new_sl = state.entry_price
                            # only ratchet in favourable direction
                            if (state.side > 0 and new_sl > state.sl) or \
                               (state.side < 0 and new_sl < state.sl):
                                if self._safe_modify_sl(bridge, state.ticket, new_sl, symbol, "BE-after-TP1"):
                                    state.sl = new_sl
                                    res.sl_moved = True
                                    res.new_sl = new_sl
                    else:
                        res.error = "tp1 close failed"
                        # don't flip tp1_hit — caller will retry next bar

        # ---------------------------------------------------------------- TP2
        if state.tp1_hit and not state.tp2_hit and state.open_lots > 0.0:
            crossed = (
                (state.side > 0 and bar_high >= state.tp2) or
                (state.side < 0 and bar_low  <= state.tp2)
            )
            if crossed:
                lots_to_close = state.original_lots * self.cfg.tp2_close_frac
                if round_lots_fn is not None:
                    lots_to_close = round_lots_fn(lots_to_close)
                lots_to_close = min(lots_to_close, state.open_lots)
                if lots_to_close >= self.cfg.min_close_lots:
                    ok = self._safe_close(bridge, state.ticket, lots_to_close, symbol, "TP2")
                    if ok:
                        state.tp2_hit = True
                        state.open_lots = max(0.0, state.open_lots - lots_to_close)
                        res.tp2_fired = True
                        res.closed_lots += lots_to_close
                    else:
                        res.error = "tp2 close failed"

        # ---------------------------------------------------------------- TRAIL
        # After TP2 hits, the remaining 25% trails. peak_favourable was
        # already updated this bar at step 1; compute the trail SL and
        # ratchet broker SL up/down only if it tightens.
        if state.tp2_hit and state.open_lots > 0.0 and atr_ready and atr_value > 0.0:
            offset = self.cfg.trail_atr_mult * atr_value
            if state.side > 0:
                trail_sl = state.peak_favourable - offset
                if trail_sl > state.sl:
                    if self._safe_modify_sl(bridge, state.ticket, trail_sl, symbol, "TRAIL"):
                        state.sl = trail_sl
                        res.sl_moved = True
                        res.new_sl = trail_sl
            else:
                trail_sl = state.peak_favourable + offset
                if trail_sl < state.sl:
                    if self._safe_modify_sl(bridge, state.ticket, trail_sl, symbol, "TRAIL"):
                        state.sl = trail_sl
                        res.sl_moved = True
                        res.new_sl = trail_sl

        # NB: we do NOT close-on-trail-cross from this method — the broker
        # SL we just modified is what fires when price reverses, exactly
        # as the backtest models it (where pos.sl is checked by the bar
        # iterator).  The runner's existing POS_CLOSED_BY_BROKER handler
        # picks up the close.

        if state.open_lots <= 0.0:
            res.fully_closed = True

        return res

    # ------------------------------------------------------------------
    def _safe_close(self, bridge, ticket, lots, symbol, tag) -> bool:
        try:
            ok = bridge.close_position(ticket, lots=lots)
            if ok:
                log.info("[%s] %s partial-close OK ticket=%d lots=%.4f",
                         symbol, tag, ticket, lots)
            else:
                log.warning("[%s] %s partial-close REJECTED ticket=%d lots=%.4f",
                            symbol, tag, ticket, lots)
            return bool(ok)
        except Exception as e:
            log.error("[%s] %s partial-close raised: %s", symbol, tag, e)
            return False

    def _safe_modify_sl(self, bridge, ticket, new_sl, symbol, tag) -> bool:
        try:
            ok = bridge.modify_position(ticket, sl=new_sl)
            if ok:
                log.info("[%s] %s SL modify OK ticket=%d new_sl=%.5f",
                         symbol, tag, ticket, new_sl)
            else:
                log.warning("[%s] %s SL modify REJECTED ticket=%d new_sl=%.5f",
                            symbol, tag, ticket, new_sl)
            return bool(ok)
        except Exception as e:
            log.error("[%s] %s SL modify raised: %s", symbol, tag, e)
            return False
