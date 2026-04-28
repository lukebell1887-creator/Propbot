"""
src/live/atr_tracker.py
=======================
Wilder ATR(N) per symbol, fed from M1 bars.

Identical math to `src/orb_engine_v20.py`'s `_ATRWilder` so the live engine
gets bit-identical trail-stop offsets to the v30 backtest.

Usage
-----
    tracker = ATRTracker(window=14)
    for bar in m1_bars:
        tracker.update(bar.high, bar.low, bar.close)
    if tracker.ready:
        atr = tracker.value          # Wilder-smoothed ATR(14) on M1

The first `window` bars seed the simple-mean TR; from bar `window+1` onwards
we apply Wilder's recursive smoothing
        atr_t = ((window-1) * atr_{t-1} + tr_t) / window

This matches both:
  * `src/orb_engine_v20.py::_ATRWilder` (used by the v30 backtest)
  * Standard Wilder ATR as published in *New Concepts in Technical Trading
    Systems* (Welles Wilder, 1978).

Persistence
-----------
`to_dict()` / `from_dict()` allow the live engine to persist the tracker
across restarts (last close, smoothed value, bar count). This means a bot
restart at 11:30 UTC won't have to re-warm 14 minutes of ATR before being
allowed to trail.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any


@dataclass
class ATRTracker:
    """Wilder ATR over M1 bars."""

    window: int = 14
    # internal state
    _seed_trs: List[float] = field(default_factory=list)
    _atr: float = 0.0
    _last_close: Optional[float] = None
    _bars_seen: int = 0
    _ready: bool = False

    # ------------------------------------------------------------------
    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def value(self) -> float:
        """Current Wilder-smoothed ATR. Returns 0.0 if not ready."""
        return self._atr if self._ready else 0.0

    @property
    def bars_seen(self) -> int:
        return self._bars_seen

    # ------------------------------------------------------------------
    def update(self, high: float, low: float, close: float) -> None:
        """
        Feed one M1 bar. Order matters — call once per closed bar in time order.

        TR_t = max( H_t - L_t,
                    |H_t - C_{t-1}|,
                    |L_t - C_{t-1}| )
        """
        if self._last_close is None:
            tr = float(high) - float(low)
        else:
            tr = max(
                float(high) - float(low),
                abs(float(high) - self._last_close),
                abs(float(low)  - self._last_close),
            )

        self._bars_seen += 1
        self._last_close = float(close)

        if self._bars_seen <= self.window:
            # accumulate seed TRs
            self._seed_trs.append(tr)
            if self._bars_seen == self.window:
                # initial ATR = simple mean of first `window` TRs
                self._atr = sum(self._seed_trs) / float(self.window)
                self._ready = True
                # we don't need the seed list any more — drop to save memory
                self._seed_trs = []
        else:
            # Wilder recursive smoothing
            self._atr = ((self.window - 1) * self._atr + tr) / float(self.window)

    # ------------------------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        return {
            "window":     int(self.window),
            "seed_trs":   list(self._seed_trs),
            "atr":        float(self._atr),
            "last_close": (None if self._last_close is None else float(self._last_close)),
            "bars_seen":  int(self._bars_seen),
            "ready":      bool(self._ready),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ATRTracker":
        t = cls(window=int(d.get("window", 14)))
        t._seed_trs   = [float(x) for x in d.get("seed_trs", [])]
        t._atr        = float(d.get("atr", 0.0))
        lc = d.get("last_close", None)
        t._last_close = (None if lc is None else float(lc))
        t._bars_seen  = int(d.get("bars_seen", 0))
        t._ready      = bool(d.get("ready", False))
        return t
