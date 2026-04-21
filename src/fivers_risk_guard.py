"""
fivers_risk_guard.py
====================
The 5%ers "can't lose the account" brake.

Sits on top of DynamicSizerV16. Does three things:

1. Tracks TODAY's realised DD (resets at 00:00 UTC rollover).
2. Tracks TOTAL realised DD from starting balance (never resets).
3. Returns a multiplier in [0.0, 1.0] that progressively shrinks trade
   size as DD approaches the 5%ers caps, and flips to 0.0 (= stop
   trading) before the caps are actually hit.

Thresholds (hard-coded for 5%ers Multi-Trader Business $100k):

    DAILY CAP: $4,000
        * 0-50% used ($0-$2,000)     -> full size (multiplier = 1.0)
        * 50-75% used ($2,000-$3,000) -> linear taper from 1.0 -> 0.5
        * 75-100% used ($3,000-$4,000) -> linear taper from 0.5 -> 0.0
        * >= 75% used                 -> STOP FOR DAY (multiplier = 0.0)

    TOTAL CAP: $10,000
        * 0-50% used ($0-$5,000)     -> full size (multiplier = 1.0)
        * 50-70% used ($5,000-$7,000) -> linear taper from 1.0 -> 0.5
        * >= 70% used                 -> STOP PERMANENTLY, alert user

The effective multiplier is min(daily, total).

This has no dials. It's tuned to 5%ers rules. Full stop.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Optional


@dataclass
class FiversGuardState:
    """Read-only snapshot of the guard's current state, for logging/telemetry."""
    today_dd_usd: float
    total_dd_usd: float
    today_dd_pct_of_cap: float
    total_dd_pct_of_cap: float
    multiplier: float
    phase: str              # "green" / "yellow" / "red" / "STOP"
    halted_today: bool
    halted_permanently: bool
    reason: str


@dataclass
class FiversRiskGuard:
    """
    5%ers-aware risk brake.

    Parameters
    ----------
    start_equity : float
        The account balance when this guard was created (used for total-DD).
    daily_cap_usd : float
        5%ers daily-loss cap. Default $4,000 for $100k MTB.
    total_cap_usd : float
        5%ers total-loss cap. Default $10,000 for $100k MTB.
    daily_soft_pct, daily_hard_pct : float
        Fraction of daily cap at which the taper starts/ends.
    total_soft_pct, total_hard_pct : float
        Fraction of total cap at which the taper starts/ends.
    """
    start_equity: float
    daily_cap_usd: float = 4_000.0
    total_cap_usd: float = 10_000.0

    daily_soft_pct: float = 0.50   # start tapering at 50 % of daily cap
    daily_hard_pct: float = 0.75   # stop trading at 75 % of daily cap (still $1,000 below!)
    total_soft_pct: float = 0.50   # start tapering at 50 % of total cap
    total_hard_pct: float = 0.70   # stop permanently at 70 % of total cap ($3,000 below!)

    # Internal state
    _today_date: Optional[str] = None
    _today_start_equity: float = 0.0      # equity at start of today
    _peak_equity: float = 0.0             # all-time peak
    _today_halted: bool = False
    _permanently_halted: bool = False

    def __post_init__(self) -> None:
        self._today_start_equity = self.start_equity
        self._peak_equity = self.start_equity

    # ─── public api ──────────────────────────────────────────────────
    def update_equity(self, equity: float, now_utc: Optional[datetime] = None) -> None:
        """Call this every bar (or at least every heartbeat)."""
        now = now_utc or datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")

        if self._today_date is None:
            # First call ever — seed with start_equity (NOT current), so a
            # guard created at $100k and immediately called with equity=$97k
            # correctly reports $3k of daily loss (not $0).
            self._today_date = date_str
            self._today_start_equity = self.start_equity
            self._today_halted = False
        elif self._today_date != date_str:
            # Real day rollover (00:00 UTC) → reset today's anchor & halt flag
            self._today_date = date_str
            self._today_start_equity = equity
            self._today_halted = False

        # Peak-equity for total-DD tracking (high-water-mark style)
        self._peak_equity = max(self._peak_equity, equity)


    def multiplier(self, equity: float, now_utc: Optional[datetime] = None) -> FiversGuardState:
        """
        Returns the current multiplier and full state snapshot.

        Multiplier is what every Kelly-sized trade MUST be scaled by
        before being sent to the broker.
        """
        self.update_equity(equity, now_utc)

        # ─── DAILY DD ───
        today_dd = max(0.0, self._today_start_equity - equity)
        today_pct = today_dd / self.daily_cap_usd

        if today_pct >= self.daily_hard_pct:
            self._today_halted = True

        if self._today_halted:
            daily_m = 0.0
        elif today_pct <= self.daily_soft_pct:
            daily_m = 1.0
        elif today_pct <= self.daily_hard_pct:
            # linear taper from 1.0 at soft_pct to 0.0 at hard_pct
            span = self.daily_hard_pct - self.daily_soft_pct
            daily_m = 1.0 - (today_pct - self.daily_soft_pct) / span
        else:
            daily_m = 0.0

        # ─── TOTAL DD ───
        total_dd = max(0.0, self._peak_equity - equity)
        # Use higher of (peak - eq) or (start - eq) so the guard bites early
        total_dd = max(total_dd, max(0.0, self.start_equity - equity))
        total_pct = total_dd / self.total_cap_usd

        if total_pct >= self.total_hard_pct:
            self._permanently_halted = True

        if self._permanently_halted:
            total_m = 0.0
        elif total_pct <= self.total_soft_pct:
            total_m = 1.0
        elif total_pct <= self.total_hard_pct:
            span = self.total_hard_pct - self.total_soft_pct
            total_m = 1.0 - (total_pct - self.total_soft_pct) / span
        else:
            total_m = 0.0

        mult = min(daily_m, total_m)

        # Phase label
        if self._permanently_halted:
            phase, reason = "STOP", "total DD hard cap"
        elif self._today_halted:
            phase, reason = "STOP", "daily DD hard cap"
        elif mult >= 0.95:
            phase, reason = "green", "normal"
        elif mult >= 0.5:
            phase, reason = "yellow", (
                f"soft brake (daily={today_pct*100:.0f}% of cap, total={total_pct*100:.0f}%)"
            )
        else:
            phase, reason = "red", (
                f"hard brake (daily={today_pct*100:.0f}% of cap, total={total_pct*100:.0f}%)"
            )

        return FiversGuardState(
            today_dd_usd=today_dd,
            total_dd_usd=total_dd,
            today_dd_pct_of_cap=today_pct,
            total_dd_pct_of_cap=total_pct,
            multiplier=mult,
            phase=phase,
            halted_today=self._today_halted,
            halted_permanently=self._permanently_halted,
            reason=reason,
        )

    # ─── convenience ──────────────────────────────────────────────────
    def reset(self) -> None:
        self._today_halted = False
        self._permanently_halted = False
        self._today_start_equity = self._peak_equity = self.start_equity
        self._today_date = None
