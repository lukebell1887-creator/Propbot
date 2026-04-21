"""
dynamic_sizer_v18.py  —  genuine PhD sizing stack (Grossman-Zhou core)

Layers, applied in order for every entry:

    1. f_GZ_bucket     Grossman-Zhou fraction for THIS (symbol, side)
                       = Kelly* × (α_cap / streak_buffer)
                       If N < min_trades_for_bucket, fall back to (2).

    2. f_GZ_global     Grossman-Zhou fraction using ALL trades  (pooled)
                       — this is ~1.6 % on the current 186-trade log.

    3. × shrinkage     Bayesian estimation-error discount:
                       s = sqrt((N-2)/N)            for N ≥ 3
                       s = 0.30                      for N < 3  (very rare)

    4. × conviction    Edge strength for THIS trade:
                       = 0.5 + 0.8 × clip((|z| − z_min)/(z_max − z_min), 0, 1)
                           + 0.2 × clip((0.5 − Hurst)/(0.5 − hurst_min), 0, 1)
                       Range is [0.5, 1.5]:  marginal setups shrink to 0.5×,
                       high-conviction setups grow to 1.5×.

    5. × fivers_guard  Progressive HARD brake, SAFETY-ONLY (no pre-emptive
                       haircut):
                         multiplier = 1.0                    below thresholds
                         multiplier = linear-interp → 0     in danger zone
                         multiplier = 0 (halt)              at cap
                       Defaults (on $100 k):
                         daily cap   $4 k, safety kicks in at $3 k (75 %)
                         total cap  $10 k, safety kicks in at $7 k (70 %)

    6. clip to         [min_risk_pct, max_risk_pct]
                       Defaults:  [0.20 %, 2.00 %]

  All breakdowns are returned in `last_breakdown` for telemetry.
"""
from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional, Tuple


@dataclass
class SizerV18Config:
    # Grossman-Zhou parameters
    alpha_cap:              float = 0.10     # 10 % total-DD budget for sizing
    min_streak_buffer:      int   = 3        # never divide by fewer than 3
    extra_streak_safety:    int   = 1        # +1 on top of observed streak

    # Fractional-Kelly safety
    kelly_fractional:       float = 0.50     # half of GZ-derived Kelly

    # When to trust bucket-specific stats vs pool
    min_trades_for_bucket:  int   = 20

    # Bayesian shrinkage
    shrink_min:             float = 0.30     # floor when N is tiny

    # Conviction scalar
    z_min:                  float = 2.0      # |z| below this → no boost
    z_max:                  float = 3.5      # |z| at or above → full boost
    hurst_min:              float = 0.30     # Hurst at or below → full boost
    conviction_min:         float = 0.50
    conviction_max:         float = 1.50

    # Absolute clip
    min_risk_pct:           float = 0.0020   # 0.20 %  hard floor
    max_risk_pct:           float = 0.0200   # 2.00 %  hard ceiling

    # Cold-start (before ANY bucket has history)
    cold_start_risk_pct:    float = 0.0050   # 0.50 %

    # 5%ers safety net thresholds (for $100 k; override in __post_init__
    # if account size differs)
    daily_cap_usd:          float = 4_000.0
    total_cap_usd:          float = 10_000.0
    daily_safety_frac:      float = 0.75     # kick in at 75 % of daily cap
    total_safety_frac:      float = 0.70     # kick in at 70 % of total cap

    # Known-bad bucket override — force to cold-start floor
    kill_losing_buckets:    bool  = True     # True = never size up buckets with E[R]<=0


@dataclass
class BucketStats:
    """Rolling window of realised-R per (symbol, side)."""
    realised_R: Deque[float] = field(default_factory=lambda: deque(maxlen=500))
    loss_streak_current: int = 0
    loss_streak_max:     int = 0

    def record(self, R: float) -> None:
        self.realised_R.append(R)
        if R <= 0:
            self.loss_streak_current += 1
            self.loss_streak_max = max(
                self.loss_streak_max, self.loss_streak_current)
        else:
            self.loss_streak_current = 0

    def summary(self) -> Optional[dict]:
        n = len(self.realised_R)
        if n == 0:
            return None
        rs = list(self.realised_R)
        mu  = sum(rs) / n
        var = sum((r - mu) ** 2 for r in rs) / max(n - 1, 1)
        return {"n": n, "mu": mu, "var": var,
                "streak_max": self.loss_streak_max}


class DynamicSizerV18:
    """
    Grossman-Zhou drawdown-constrained Kelly + Bayesian shrinkage + conviction
    scaling + 5%ers safety guard.  Every layer is documented in the module
    docstring; every computation's components are stashed in `last_breakdown`.
    """
    def __init__(self, cfg: Optional[SizerV18Config] = None):
        self.cfg = cfg or SizerV18Config()
        self.buckets: Dict[Tuple[str, int], BucketStats] = defaultdict(BucketStats)

        # Rolling PEAK tracking for the 5%ers guard.
        self._start_equity:   Optional[float] = None
        self._peak_equity:    float = 0.0
        self._today_open_eq:  float = 0.0
        self._today_key:      Optional[str] = None

        # Telemetry
        self.last_breakdown: dict = {}

    # ------------------------------------------------------------------
    #  State maintenance
    # ------------------------------------------------------------------
    def record_trade(self, symbol: str, side: int, realised_R: float) -> None:
        self.buckets[(symbol, side)].record(realised_R)

    def _roll_equity_markers(self, equity: float, now_day_key: str) -> None:
        if self._start_equity is None:
            self._start_equity = equity
            self._peak_equity  = equity
            self._today_open_eq = equity
            self._today_key = now_day_key
            return
        # New trading day → reset the daily reference
        if now_day_key != self._today_key:
            self._today_open_eq = equity
            self._today_key = now_day_key
        # Rolling peak (only ever ratchets up)
        if equity > self._peak_equity:
            self._peak_equity = equity

    # ------------------------------------------------------------------
    #  Grossman-Zhou building blocks
    # ------------------------------------------------------------------
    def _f_gz(self, mu: float, var: float, streak_max: int,
              alpha: float) -> float:
        if var <= 0 or mu <= 0:
            return 0.0
        kelly_star = mu / var
        streak_buf = max(streak_max + self.cfg.extra_streak_safety,
                         self.cfg.min_streak_buffer)
        return kelly_star * (alpha / streak_buf) * self.cfg.kelly_fractional

    def _pool_stats(self) -> Optional[dict]:
        all_R = [r for b in self.buckets.values() for r in b.realised_R]
        if not all_R:
            return None
        n = len(all_R)
        mu = sum(all_R) / n
        var = sum((r - mu) ** 2 for r in all_R) / max(n - 1, 1)
        # Pool streak = max of all bucket streaks (worst-case survival)
        streak = max((b.loss_streak_max for b in self.buckets.values()),
                     default=0)
        return {"n": n, "mu": mu, "var": var, "streak_max": streak}

    def _shrinkage(self, n: int) -> float:
        """Bayesian estimation-error discount.  f' = f × sqrt((n-2)/n)."""
        if n < 3:
            return self.cfg.shrink_min
        return math.sqrt(max(n - 2, 1) / n)

    def _conviction(self, abs_z: float, hurst: float) -> float:
        c = self.cfg
        # z-score component (0..1)
        z_term = 0.0
        if c.z_max > c.z_min:
            z_term = max(0.0, min(1.0, (abs_z - c.z_min) / (c.z_max - c.z_min)))
        # Hurst component (0..1) — only rewards REAL mean-reversion (H<0.5)
        h_term = 0.0
        if 0.5 > c.hurst_min:
            h_term = max(0.0, min(1.0, (0.5 - hurst) / (0.5 - c.hurst_min)))
        mult = 0.5 + 0.8 * z_term + 0.2 * h_term       # range ≈ [0.5, 1.5]
        return max(c.conviction_min, min(c.conviction_max, mult))

    def _fivers_guard_mult(self, equity: float) -> tuple[float, str]:
        """Safety-only: returns 1.0 until DD approaches caps, then linear→0."""
        c = self.cfg
        if self._start_equity is None:
            return 1.0, "no_baseline"

        total_dd = max(0.0, self._peak_equity - equity)
        daily_dd = max(0.0, self._today_open_eq - equity)

        # Total DD phase
        total_kick = c.total_cap_usd * c.total_safety_frac
        if total_dd >= c.total_cap_usd:
            return 0.0, "total_halt"
        if total_dd > total_kick:
            # linear 1 → 0 between kick-in and cap
            frac = (total_dd - total_kick) / (c.total_cap_usd - total_kick)
            total_mult = max(0.0, 1.0 - frac)
        else:
            total_mult = 1.0

        # Daily DD phase
        daily_kick = c.daily_cap_usd * c.daily_safety_frac
        if daily_dd >= c.daily_cap_usd:
            return 0.0, "daily_halt"
        if daily_dd > daily_kick:
            frac = (daily_dd - daily_kick) / (c.daily_cap_usd - daily_kick)
            daily_mult = max(0.0, 1.0 - frac)
        else:
            daily_mult = 1.0

        mult = min(total_mult, daily_mult)
        phase = ("green" if mult == 1.0 else
                 "amber" if mult > 0.0 else "red")
        return mult, phase

    # ------------------------------------------------------------------
    #  The main entry point
    # ------------------------------------------------------------------
    def compute_risk_pct(
        self,
        *,
        symbol: str,
        side: int,
        equity: float,
        day_key: str,
        abs_z: float,
        hurst: float,
    ) -> float:
        c = self.cfg
        self._roll_equity_markers(equity, day_key)

        bucket = self.buckets[(symbol, side)].summary()
        pool   = self._pool_stats()

        # Choose base f.  Bucket-specific if N is big enough, else pool.
        if bucket is not None and bucket["n"] >= c.min_trades_for_bucket:
            f_bucket_raw = self._f_gz(bucket["mu"], bucket["var"],
                                       bucket["streak_max"], c.alpha_cap)
            f_base = f_bucket_raw
            source = "bucket"
            n_for_shrink = bucket["n"]
            bucket_mu = bucket["mu"]
        elif pool is not None:
            f_pool_raw = self._f_gz(pool["mu"], pool["var"],
                                     pool["streak_max"], c.alpha_cap)
            f_base = f_pool_raw
            source = "pool"
            n_for_shrink = pool["n"]
            bucket_mu = (bucket["mu"] if bucket else None)
        else:
            # No data at all - emit cold-start
            self.last_breakdown = {
                "f_base":       c.cold_start_risk_pct,
                "shrink":       1.0,
                "conviction":   1.0,
                "guard":        1.0,
                "risk_pct":     c.cold_start_risk_pct,
                "source":       "cold_start",
                "phase":        "green",
            }
            return c.cold_start_risk_pct

        # Kill known-bad bucket
        if c.kill_losing_buckets and bucket_mu is not None and bucket_mu <= 0:
            f_base = 0.0
            source = "losing_bucket_killed"

        # Layer 3: Bayesian shrinkage
        shrink = self._shrinkage(n_for_shrink)

        # Layer 4: conviction scalar
        conv = self._conviction(abs_z=abs_z, hurst=hurst)

        # Layer 5: 5%ers SAFETY-ONLY guard
        guard_mult, guard_phase = self._fivers_guard_mult(equity)

        # Combine
        f = f_base * shrink * conv * guard_mult

        # Floor against 0 only if we actually have signal; otherwise cold-start floor
        if f_base == 0.0 and source == "losing_bucket_killed":
            f = 0.0   # do NOT trade losing buckets
        elif f <= 0.0:
            f = c.cold_start_risk_pct * guard_mult

        # Layer 6: hard clip
        f = max(0.0, min(f, c.max_risk_pct))
        if 0.0 < f < c.min_risk_pct:
            f = c.min_risk_pct

        # Telemetry
        self.last_breakdown = {
            "f_base":     f_base,
            "shrink":     shrink,
            "conviction": conv,
            "guard":      guard_mult,
            "risk_pct":   f,
            "source":     source,
            "phase":      guard_phase,
            "n":          n_for_shrink,
        }
        return f
