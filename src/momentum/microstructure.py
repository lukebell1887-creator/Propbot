"""
MicrostructureCluster — round-number & prior-swing liquidity-cluster detector.

Stops parked at round numbers (.00, .50) and at prior-day / prior-swing
highs and lows are disproportionately hunted.  The masterplan (§5.3)
nudges the stop outward by an amount if the computed stop falls inside
one such cluster.

This implementation keeps:
  * a rolling list of swing highs / lows (via a simple fractal rule)
  * a generator of round-number price levels near the current price
  * a helper `nearest_cluster_offset(price)` returning the signed distance
    to the closest cluster within the search radius

All online, O(1) per tick.
"""

from __future__ import annotations
import math
from collections import deque
from typing import Iterable


class MicrostructureCluster:
    __slots__ = (
        "_swing_radius", "_levels",
        "_recent_highs", "_recent_lows",
        "_round_step", "_radius_pct",
        "_prev_highs", "_prev_lows",
    )

    def __init__(self,
                 swing_radius: int = 3,
                 round_step: float | None = None,
                 radius_pct: float = 0.0015,
                 history: int = 500):
        """
        swing_radius   bars each side for a fractal swing (3 -> 7-bar Williams fractal)
        round_step     if None, auto from typical price:  NAS100 → 10,  XAU → 1.0
        radius_pct     search window around a candidate stop (0.0015 = 15 bp)
        history        number of past bars retained for swing lookup
        """
        self._swing_radius = swing_radius
        self._levels: set[float] = set()
        self._recent_highs: deque = deque(maxlen=history)
        self._recent_lows: deque = deque(maxlen=history)
        self._prev_highs: deque = deque(maxlen=history)
        self._prev_lows: deque = deque(maxlen=history)
        self._round_step = round_step
        self._radius_pct = radius_pct

    # ------------------------------------------------------------------
    def update(self, high: float, low: float) -> None:
        """Feed a bar's high & low.  Detects fractal swings inside the window."""
        self._prev_highs.append(high)
        self._prev_lows.append(low)

        r = self._swing_radius
        if len(self._prev_highs) < 2 * r + 1:
            return

        # Williams fractal: index len-1-r is centre
        mid = -1 - r
        centre_h = self._prev_highs[mid]
        centre_l = self._prev_lows[mid]

        highs_slice = list(self._prev_highs)[-(2 * r + 1):]
        lows_slice = list(self._prev_lows)[-(2 * r + 1):]

        if centre_h == max(highs_slice):
            self._recent_highs.append(centre_h)
            self._levels.add(round(centre_h, 4))
        if centre_l == min(lows_slice):
            self._recent_lows.append(centre_l)
            self._levels.add(round(centre_l, 4))

    # ------------------------------------------------------------------
    def round_levels(self, price: float) -> Iterable[float]:
        """Yield the round-number levels within ±radius_pct of `price`."""
        if self._round_step is None:
            # Auto-detect: nearest power of 10 giving 5–20 steps inside the window
            step = 10 ** (math.floor(math.log10(max(price, 1.0))) - 2)
        else:
            step = self._round_step
        lo = price * (1.0 - self._radius_pct)
        hi = price * (1.0 + self._radius_pct)
        n_lo = math.ceil(lo / step)
        n_hi = math.floor(hi / step)
        for n in range(n_lo, n_hi + 1):
            yield n * step

    # ------------------------------------------------------------------
    def nearest_cluster(self, price: float) -> float:
        """
        Return the distance (in price units) from `price` to the nearest
        cluster level within ±radius_pct.  Returns math.inf if none.
        """
        best = math.inf
        lo = price * (1.0 - self._radius_pct)
        hi = price * (1.0 + self._radius_pct)

        # Round-number scan
        for lvl in self.round_levels(price):
            d = abs(lvl - price)
            if d < best:
                best = d

        # Prior-swing scan
        for lvl in list(self._recent_highs) + list(self._recent_lows):
            if lo <= lvl <= hi:
                d = abs(lvl - price)
                if d < best:
                    best = d
        return best
