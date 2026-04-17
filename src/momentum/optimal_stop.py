"""
OptimalStopper — Shiryaev-style closed-form exit override.

The full Shiryaev 1963 free-boundary problem for a Brownian drift with
unknown sign has a known solution:

    close  ⇔  posterior sign has flipped AND unrealised reward ≥ c · (1 - p)/p

For our purposes the masterplan prescribes the practical reduction:

    close when  sign(μ̂_t) ≠ sign(entry μ̂) AND current R ≥ R_min

which is the first-order expansion of the full free boundary when the
switching cost is approximately constant in R.  That is what ships in v7.

Reference: Shiryaev A.N., "On optimal methods in quickest detection
problems", Theory Probab. Appl. 8 (1963) 22-46.
"""

from __future__ import annotations


class OptimalStopper:
    __slots__ = ("_r_min", "_entry_sign")

    def __init__(self, r_min: float = 1.0):
        """
        r_min  minimum accumulated R before an optimal-stop exit can fire
        """
        self._r_min = r_min
        self._entry_sign = 0

    def arm(self, entry_mu: float) -> None:
        """Call on position open with the Kalman μ̂ at entry."""
        if entry_mu > 0:
            self._entry_sign = 1
        elif entry_mu < 0:
            self._entry_sign = -1
        else:
            self._entry_sign = 0

    def should_exit(self, current_mu: float, current_r: float) -> bool:
        """
        Fire if Kalman drift sign flipped AND we've banked at least r_min R.
        """
        if self._entry_sign == 0:
            return False
        if current_r < self._r_min:
            return False
        if self._entry_sign > 0 and current_mu <= 0.0:
            return True
        if self._entry_sign < 0 and current_mu >= 0.0:
            return True
        return False

    def disarm(self) -> None:
        self._entry_sign = 0

    @property
    def armed(self) -> bool:
        return self._entry_sign != 0
