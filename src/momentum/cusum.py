"""
CUSUMDetector — Page's dual-sided CUSUM change-point detector.

Recursion (Page 1954):

    S+_t = max(0, S+_{t-1} + z_t - k)
    S-_t = max(0, S-_{t-1} - z_t - k)

where z_t is a standardised return.  Fires LONG when S+ > h, SHORT when S- > h.
Both accumulators reset on fire.

Proven minimax-optimal for detection delay given a fixed false-alarm rate
(Moustakides 1986, *Annals of Statistics* 14(4):1379-1387).
"""

from __future__ import annotations


class CUSUMDetector:
    __slots__ = ("_k", "_h", "_s_plus", "_s_minus", "_last_fire")

    def __init__(self, k: float = 0.5, h: float = 4.5):
        """
        k  reference value (half the smallest shift we care about, in σ units)
        h  detection threshold (higher = fewer false alarms, longer delay)
        """
        if k < 0 or h <= 0:
            raise ValueError("k >= 0 and h > 0 required")
        self._k = k
        self._h = h
        self._s_plus = 0.0
        self._s_minus = 0.0
        self._last_fire = 0      # +1 long fire, -1 short fire, 0 none

    # ------------------------------------------------------------------
    def update(self, z: float) -> int:
        """
        Feed one standardised return.  Returns +1 / -1 / 0.

        On fire, the triggering side resets to 0.  This is the standard
        "reset-after-detection" variant.
        """
        self._s_plus = max(0.0, self._s_plus + z - self._k)
        self._s_minus = max(0.0, self._s_minus - z - self._k)

        fire = 0
        if self._s_plus > self._h:
            fire = 1
            self._s_plus = 0.0
        elif self._s_minus > self._h:
            fire = -1
            self._s_minus = 0.0
        self._last_fire = fire
        return fire

    # ------------------------------------------------------------------
    def confidence(self) -> float:
        """
        Distance of the leading accumulator above 0 relative to 2 h.
        Saturated at 1.  Used by BayesianSizer for the conviction term.
        """
        lead = max(self._s_plus, self._s_minus)
        return max(0.0, min(1.0, lead / (2.0 * self._h)))

    # ------------------------------------------------------------------
    @property
    def s_plus(self) -> float:
        return self._s_plus

    @property
    def s_minus(self) -> float:
        return self._s_minus

    @property
    def last_fire(self) -> int:
        return self._last_fire

    def reset(self) -> None:
        self._s_plus = 0.0
        self._s_minus = 0.0
        self._last_fire = 0
