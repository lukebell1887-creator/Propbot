"""
TradingCalendar — time-of-day / day-of-week / news / holiday blackouts.

Purpose
-------
Tells the engine "can I open a NEW trade at time T?"  When False, a reason
string is returned so we can log / count blackouts.  It has NO effect on
already-open positions — those are still managed normally, so broker-held
SL/TPs stay armed and we never lose control of live money.

Zero external deps, pure stdlib.  Same instance is used by the v16 backtest
and the v16 live runner.  One source of truth.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------
#  Default holiday list - major US/UK/DE market holidays 2026-2027
#  (indices + gold are effectively untradeable on these UTC dates)
# ---------------------------------------------------------------------
DEFAULT_HOLIDAYS_UTC: frozenset[str] = frozenset({
    # 2026
    "2026-01-01",  # New Year
    "2026-01-19",  # MLK Jr Day (US)
    "2026-02-16",  # Presidents Day (US)
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day (US)
    "2026-06-19",  # Juneteenth (US)
    "2026-07-03",  # July 4 observed (US)
    "2026-09-07",  # Labor Day (US)
    "2026-11-26",  # Thanksgiving (US)
    "2026-12-25",  # Christmas
    # 2027
    "2027-01-01",
    "2027-01-18",
    "2027-02-15",
    "2027-03-26",  # Good Friday
    "2027-05-31",
    "2027-06-18",
    "2027-07-05",
    "2027-09-06",
    "2027-11-25",
    "2027-12-24",
})


@dataclass
class CalendarConfig:
    # Weekend (UTC)  -  default Fri 21:00 UTC  ->  Sun 22:00 UTC
    weekend_close_day:  int = 4     # Friday  (Mon = 0)
    weekend_close_hour: int = 21
    weekend_close_min:  int = 0
    weekend_open_day:   int = 6     # Sunday
    weekend_open_hour:  int = 22
    weekend_open_min:   int = 0

    # Daily rollover blackout (UTC) - spreads blow out 5-10x here
    rollover_start_hour: int = 21
    rollover_start_min:  int = 55
    rollover_end_hour:   int = 22
    rollover_end_min:    int = 10

    # Holiday set
    holidays: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_HOLIDAYS_UTC))

    # News blackout (+/- N minutes around each high-impact event)
    news_buffer_min: int = 15
    # News events:  list of (UTC datetime, label)
    news_events: list[tuple[datetime, str]] = field(default_factory=list)


class TradingCalendar:
    """
    Usage:
        cal = TradingCalendar()                 # sensible defaults
        allowed, reason = cal.can_enter(sym, ts_utc)
    """

    def __init__(self, cfg: Optional[CalendarConfig] = None):
        self.cfg = cfg or CalendarConfig()

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------
    def _weekend_window_min(self) -> tuple[int, int]:
        c = self.cfg
        close = c.weekend_close_day * 1440 + c.weekend_close_hour * 60 + c.weekend_close_min
        open_ = c.weekend_open_day  * 1440 + c.weekend_open_hour  * 60 + c.weekend_open_min
        return close, open_

    def _week_minute(self, ts: datetime) -> int:
        return ts.weekday() * 1440 + ts.hour * 60 + ts.minute

    def _in_weekend(self, ts: datetime) -> bool:
        close, open_ = self._weekend_window_min()
        wm = self._week_minute(ts)
        if close <= open_:
            return close <= wm < open_
        # wraps midnight of Mon -> Fri case (rare custom config)
        return wm >= close or wm < open_

    def _in_rollover(self, ts: datetime) -> bool:
        c = self.cfg
        start = c.rollover_start_hour * 60 + c.rollover_start_min
        end   = c.rollover_end_hour   * 60 + c.rollover_end_min
        mod = ts.hour * 60 + ts.minute
        return start <= mod < end

    def _is_holiday(self, ts: datetime) -> bool:
        return ts.strftime("%Y-%m-%d") in self.cfg.holidays

    def _in_news_buffer(self, ts: datetime) -> bool:
        if not self.cfg.news_events:
            return False
        buf = timedelta(minutes=self.cfg.news_buffer_min)
        for ev_ts, _ in self.cfg.news_events:
            if abs(ts - ev_ts) <= buf:
                return True
        return False

    # -----------------------------------------------------------------
    # Public
    # -----------------------------------------------------------------
    def can_enter(self, symbol: str, ts_utc: datetime) -> tuple[bool, str]:
        """
        Returns (allowed, reason).
        - allowed=True  means a NEW entry may be attempted
        - allowed=False and reason is one of:
              'weekend'    market closed
              'rollover'   daily broker rollover window (spread blowup)
              'holiday'    major market holiday
              'news'       +/- news_buffer_min of a high-impact event
        """
        if self._in_weekend(ts_utc):     return False, "weekend"
        if self._in_rollover(ts_utc):    return False, "rollover"
        if self._is_holiday(ts_utc):     return False, "holiday"
        if self._in_news_buffer(ts_utc): return False, "news"
        return True, ""

    def can_manage(self, symbol: str, ts_utc: datetime) -> bool:
        """
        Managing an OPEN position (trailing stops, TP, time stop) is always
        allowed.  Safety takes precedence over blackout logic.
        """
        return True

    # -----------------------------------------------------------------
    # News CSV loader (optional)
    # -----------------------------------------------------------------
    @staticmethod
    def load_news_csv(path: Path, impact: str = "High") -> list[tuple[datetime, str]]:
        """
        Load high-impact events from a CSV with columns:
            utc_datetime, impact, event
        Rows whose impact != `impact` are skipped.
        """
        out: list[tuple[datetime, str]] = []
        p = Path(path)
        if not p.exists():
            return out
        with open(p, "r", newline="") as f:
            rdr = csv.DictReader(f)
            for row in rdr:
                try:
                    ts = datetime.fromisoformat(row["utc_datetime"])
                except Exception:
                    continue
                if (row.get("impact") or "").strip().lower() != impact.lower():
                    continue
                out.append((ts, row.get("event") or ""))
        return out
