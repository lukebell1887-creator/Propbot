"""
EVTGarchStop — composed dynamic, tail-aware, regime-adapted stop distance.

Implements the §5 pipeline:

    stop_distance = q_α(GPD)  ·  σ̂_{GARCH,t+1}  ·  regime_mult
                  + microstructure_nudge

Exposes:
    `update(ret, high, low)`         — feed one M1 bar
    `entry_stop_distance(regime)`    — stop distance in price units (α = 0.005)
    `trail_distance(regime)`         — tighter quantile (α = 0.10)
    `price_to_stop(price, side, regime)`   — helper

Regime multipliers per §5.4:
    0 (trending-smooth) → 0.85
    1 (mixed)           → 1.00
    2 (high-vol/choppy) → 1.25
"""

from __future__ import annotations
from .garch import GarchOne
from .gpd import GpdTail
from .microstructure import MicrostructureCluster


_REGIME_MULT = {0: 0.85, 1: 1.00, 2: 1.25}


class EVTGarchStop:
    __slots__ = ("_garch", "_gpd", "_ms", "_last_price")

    def __init__(self,
                 garch: GarchOne | None = None,
                 gpd: GpdTail | None = None,
                 micro: MicrostructureCluster | None = None):
        self._garch = garch or GarchOne()
        self._gpd = gpd or GpdTail()
        self._ms = micro or MicrostructureCluster()
        self._last_price = 0.0

    # ------------------------------------------------------------------
    def update(self, price: float, ret: float, high: float, low: float) -> None:
        """Feed one M1 bar (price=close, ret=log-return, high/low for MS)."""
        self._garch.update(ret)
        sig = self._garch.sigma()
        if sig > 1e-12:
            self._gpd.update(ret / sig)
        self._ms.update(high, low)
        self._last_price = price

    # ------------------------------------------------------------------
    def _base_distance(self, alpha: float) -> float:
        q = self._gpd.quantile(alpha)
        sig1 = self._garch.forecast(1) ** 0.5   # sqrt(var)
        # return distance as a fraction of price (log-return space)
        return q * sig1

    def entry_stop_distance(self, price: float, regime: int = 1,
                            alpha: float = 0.005) -> float:
        """Absolute price-units distance for an entry stop."""
        frac = self._base_distance(alpha) * _REGIME_MULT.get(regime, 1.0)
        dist = price * frac
        # Microstructure nudge — if raw stop lands inside a cluster, push past it
        cluster_dist = self._ms.nearest_cluster(price - dist)
        if cluster_dist < 0.0015 * price:
            dist += 0.002 * price
        return max(dist, 1e-9)

    def trail_distance(self, price: float, regime: int = 1,
                       alpha: float = 0.10) -> float:
        """Absolute price-units distance for a trailing stop."""
        frac = self._base_distance(alpha) * _REGIME_MULT.get(regime, 1.0)
        return max(price * frac, 1e-9)

    # Convenience: returns actual SL price
    def price_to_stop(self, entry: float, side: int, regime: int = 1,
                      alpha: float = 0.005) -> float:
        d = self.entry_stop_distance(entry, regime, alpha)
        return entry - side * d

    # ------------------------------------------------------------------
    @property
    def garch(self) -> GarchOne: return self._garch
    @property
    def gpd(self) -> GpdTail: return self._gpd
    @property
    def micro(self) -> MicrostructureCluster: return self._ms
