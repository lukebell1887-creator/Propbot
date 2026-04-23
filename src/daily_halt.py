"""
daily_halt.py — Hard daily-loss kill-switch for prop-firm compliance.

Intent
------
5ers Stellar rules:
  • 10 %  STATIC max DD from $100 k starting balance  (irrevocable)
  •  5 %  DAILY loss line (resets at broker EOD)

This module enforces an INTERNAL 4 % daily hard stop, one full percentage point
inside the 5ers daily line so slippage + overnight gap can never breach 5 %.

Used by BOTH:
  - Live execution  (real-time intraday)
  - Backtest replay (apples-to-apples DD metrics vs live)

Design
------
Stateless per-day: at the first trade of a new server-date we record
``day_start_equity``.  Before every subsequent trade we ask:

    if (hist.equity - day_start_equity) / day_start_equity <= -halt_pct:
        return False  # skip this trade, day is DONE

The halt is **one-way**: once triggered it only reopens at the next server-date.
Positive recovery during the same day does NOT re-enable trading — we do not
hope, we obey the rule.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Optional


@dataclass
class DailyHalt:
    """Tracks per-day equity anchor and halt state.

    Parameters
    ----------
    halt_pct : float
        Fractional daily loss that triggers the halt.  0.04 = 4 %.
    buffer_pct : float
        Small positive buffer UNDER halt_pct at which we *soft-warn* but still
        trade.  Purely informational; default 0.03 (= 3 %).
    server_tz_offset_h : float
        Hours to ADD to UTC to get broker-server-date.  5ers runs on an
        EET / EEST server so normally +2 or +3.  Default 0 (= UTC) because
        the backtest trade timestamps are already broker-local.
    """
    halt_pct: float = 0.04
    buffer_pct: float = 0.03
    server_tz_offset_h: float = 0.0

    # Runtime state
    current_day: Optional[date] = None
    day_start_equity: float = 0.0
    halted_today: bool = False

    # Telemetry
    total_halts: int = 0
    days_seen: int = 0
    halted_dates: list = field(default_factory=list)

    def reset(self) -> None:
        self.current_day = None
        self.day_start_equity = 0.0
        self.halted_today = False
        self.total_halts = 0
        self.days_seen = 0
        self.halted_dates = []

    def _ts_to_date(self, entry_time_unix: float) -> date:
        """Convert unix timestamp → broker-server-date."""
        dt = datetime.utcfromtimestamp(entry_time_unix)
        # shift by server offset
        from datetime import timedelta
        dt += timedelta(hours=self.server_tz_offset_h)
        return dt.date()

    def can_trade(self, entry_time_unix: float, current_equity: float) -> bool:
        """Return True if a new trade is allowed; False if halted.

        Side effect: updates internal state (day rollover + anchor)."""
        d = self._ts_to_date(entry_time_unix)

        # Day rollover
        if self.current_day is None or d != self.current_day:
            self.current_day = d
            self.day_start_equity = current_equity
            self.halted_today = False
            self.days_seen += 1

        if self.halted_today:
            return False

        # Check drawdown from day-open
        if self.day_start_equity <= 0:
            return True  # defensive
        dd_today = (current_equity - self.day_start_equity) / self.day_start_equity
        if dd_today <= -self.halt_pct:
            self.halted_today = True
            self.total_halts += 1
            self.halted_dates.append(str(d))
            return False

        return True

    def status_str(self) -> str:
        return (f"DailyHalt: {self.total_halts} halts across "
                f"{self.days_seen} trading days "
                f"({self.total_halts/max(self.days_seen,1)*100:.1f}%)"
                f"  | current_day={self.current_day}"
                f"  halted_today={self.halted_today}")
