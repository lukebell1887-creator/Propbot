"""
RollingQuantile — exact q-quantile over a sliding window of the last N samples.

Used by v14 SmartBB for:
    * adaptive |Z| entry threshold      (per-symbol, per-volatility regime)
    * adaptive Hurst regime threshold   (per-symbol, per-market regime)

Implementation: bisect-maintained sorted list paired with a FIFO deque.
    push    : bisect.insort  -> O(log W)
    evict   : bisect_left + pop -> O(log W + W) worst case, but W=500 is fine
    value   : O(1) on the sorted list

This is exact (not streaming-approximate like P²). W up to a few thousand is
trivially fast — we're called at most once per M5 bar (~12/hour) per symbol.

Boundary-case guarantees:
    * Returns 0.0 until `ready` is True (min samples configurable).
    * Rank() returns 0.5 until the buffer has >= 2 items.
    * Quantile value is linearly interpolated between neighbours.
"""

from __future__ import annotations

import bisect
import math
from collections import deque
from typing import Deque, List


class RollingQuantile:
    __slots__ = ("_q", "_window", "_min_samples", "_buf", "_sorted")

    def __init__(self, q: float, window: int = 500, min_samples: int = 50):
        if not (0.0 < q < 1.0):
            raise ValueError(f"q must be in (0,1), got {q}")
        if window < 2:
            raise ValueError(f"window must be >= 2, got {window}")
        self._q = q
        self._window = window
        self._min_samples = max(2, min(min_samples, window))
        self._buf: Deque[float] = deque(maxlen=window)
        self._sorted: List[float] = []

    # ------------------------------------------------------------------
    def update(self, x: float) -> None:
        """Add observation x and maintain both FIFO and sorted views."""
        if not math.isfinite(x):
            return
        if len(self._buf) == self._window:
            old = self._buf[0]
            idx = bisect.bisect_left(self._sorted, old)
            if idx < len(self._sorted) and self._sorted[idx] == old:
                self._sorted.pop(idx)
        self._buf.append(x)
        bisect.insort(self._sorted, x)

    # ------------------------------------------------------------------
    @property
    def ready(self) -> bool:
        return len(self._buf) >= self._min_samples

    @property
    def n(self) -> int:
        return len(self._buf)

    # ------------------------------------------------------------------
    def value(self) -> float:
        """
        Return the q-quantile of the current window with linear interpolation
        between adjacent order statistics.  0.0 if not ready.
        """
        n = len(self._sorted)
        if n == 0 or not self.ready:
            return 0.0
        if n == 1:
            return self._sorted[0]
        # Position within sorted list for quantile q:
        #   pos = q * (n - 1)   (Type 7, Hyndman & Fan 1996 — R's default)
        pos = self._q * (n - 1)
        lo = int(math.floor(pos))
        hi = min(lo + 1, n - 1)
        frac = pos - lo
        return self._sorted[lo] * (1.0 - frac) + self._sorted[hi] * frac

    # ------------------------------------------------------------------
    def rank(self, x: float) -> float:
        """
        Return the rank of x in the current sorted window, mapped to [0, 1].
        rank=0.5 means median, rank=1.0 means largest ever seen.
        Returns 0.5 when the window is empty.
        """
        n = len(self._sorted)
        if n < 2:
            return 0.5
        idx = bisect.bisect_left(self._sorted, x)
        return idx / (n - 1)

    # ------------------------------------------------------------------
    def reset(self) -> None:
        self._buf.clear()
        self._sorted.clear()
