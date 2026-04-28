"""
dd_breaker.py — Hard **total-DD** circuit breaker for prop-firm compliance.

Companion to `src.daily_halt.DailyHalt`:
  • DailyHalt   : triggers on DAILY loss ≥ threshold (resets at EOD)
  • DDBreaker   : triggers on TOTAL equity peak-to-trough DD ≥ threshold
                  (flat all, halt for rest of day, resume at next daily open)

Why this exists
---------------
The Merton-GZ sizer's `dd_cap_pct` is a SOFT brake: it refuses NEW trades when
DD ≥ cap, but in-flight trades keep running. Under extreme regimes (3× vol +
multi-σ gaps) the overshoot can reach ~1 pp beyond the cap.

`DDBreaker` is a HARD circuit breaker:
  1. Tracks equity_peak and current DD
  2. When DD ≥ `halt_pct`:
       - emits `close_all=True` (live: flatten; backtest: truncate in-flight PnL)
       - halts opening of NEW trades until the next server-date boundary
  3. Next day: resumes normal trading (peak is preserved; DD is now relative
     to the SAME all-time peak, so if market keeps falling the breaker stays
     tripped until a new peak is made).

Live integration
----------------
In `src/live/v23_live.py` call `breaker.check(ts, equity)` before every new
trade and before/after every MT5 price tick. On `halted=True`, call
`close_all_positions()` once per trip and skip new entries.

Backtest integration
--------------------
Call `apply_dd_breaker(trades, starting_balance, halt_pct=0.04)` AFTER all
other safety rails. This simulates:
  - post-trigger trades are cancelled (removed from trade log)
  - no mark-to-market truncation of already-running trades (those still close
    at their original SL/TP because we don't re-simulate bar-by-bar here);
    this is intentionally CONSERVATIVE — the backtest number will still show
    a modest overshoot, but the LIVE bot will achieve tighter clamping because
    it actually flattens positions in real-time.
"""
from __future__ import annotations

import json
import os
import time as _time
from dataclasses import dataclass, field, replace
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# =============================================================================
#  Live / streaming state machine
# =============================================================================
@dataclass
class DDBreaker:
    """Real-time total-DD circuit breaker.

    Parameters
    ----------
    halt_pct : float
        Fractional DD (peak-to-trough) that triggers the halt.  0.04 = 4 %.
    server_tz_offset_h : float
        Hours to ADD to UTC to get broker-server-date.  0 = UTC.

    State
    -----
    peak_equity : float
        All-time peak equity since reset. Monotone non-decreasing.
    halted : bool
        Currently halted? Reset at the next server-date.
    current_day : date | None
        Server-date of the last observation (for daily reset).
    """
    halt_pct: float = 0.04
    server_tz_offset_h: float = 0.0

    # Runtime state
    peak_equity: float = 0.0
    halted: bool = False
    current_day: Optional[date] = None

    # Telemetry
    total_halts: int = 0
    trip_times: list = field(default_factory=list)   # list of (datetime, dd_pct)
    max_dd_pct_seen: float = 0.0

    # ------------------------------------------------------------------
    def reset(self) -> None:
        self.peak_equity = 0.0
        self.halted = False
        self.current_day = None
        self.total_halts = 0
        self.trip_times = []
        self.max_dd_pct_seen = 0.0

    def _ts_to_date(self, ts: float) -> date:
        return (datetime.utcfromtimestamp(ts)
                + timedelta(hours=self.server_tz_offset_h)).date()

    # ------------------------------------------------------------------
    def check(self, entry_time_unix: float, current_equity: float
              ) -> Tuple[bool, float]:
        """Observe an equity sample, update state.

        Returns
        -------
        (halted, dd_pct)
            halted : bool  — True if trading is halted RIGHT NOW
                             (either just tripped, or was already tripped today)
            dd_pct : float — current DD as a positive fraction (0.0 = at peak,
                             0.04 = 4 % below peak)
        """
        d = self._ts_to_date(entry_time_unix)

        # Day rollover resets the halt but preserves peak
        if self.current_day is None or d != self.current_day:
            self.current_day = d
            # Only resume if we've been halted; resetting starts a fresh day
            self.halted = False

        # Update peak (only grows)
        if current_equity > self.peak_equity:
            self.peak_equity = current_equity

        # Compute DD
        if self.peak_equity <= 0:
            return False, 0.0
        dd_pct = (self.peak_equity - current_equity) / self.peak_equity
        if dd_pct > self.max_dd_pct_seen:
            self.max_dd_pct_seen = dd_pct

        # Already halted today? Stay halted
        if self.halted:
            return True, dd_pct

        # Trip condition
        if dd_pct >= self.halt_pct:
            self.halted = True
            self.total_halts += 1
            self.trip_times.append(
                (datetime.utcfromtimestamp(entry_time_unix), dd_pct)
            )
            return True, dd_pct

        return False, dd_pct

    def status_str(self) -> str:
        return (f"DDBreaker: peak=${self.peak_equity:,.0f} "
                f"max_dd_seen={self.max_dd_pct_seen*100:.2f}% "
                f"trips={self.total_halts} "
                f"halted_today={self.halted} "
                f"({'BLOCKED' if self.halted else 'OPEN'})")

    # ------------------------------------------------------------------
    #  PERSISTENCE     (added v30.1 — 2026-04-28)
    # ------------------------------------------------------------------
    #  CRITICAL invariant: after a restart the breaker MUST resume with
    #  the same `peak_equity` it had before, otherwise the new (lower)
    #  starting equity becomes the peak and DD measurement resets to 0%.
    #  That would silently disable our 8% account-DD circuit-breaker.
    SCHEMA_VERSION = 1

    def to_state(self) -> Dict[str, Any]:
        return {
            "schema": self.SCHEMA_VERSION,
            "saved_at_unix": _time.time(),
            "halt_pct": self.halt_pct,
            "server_tz_offset_h": self.server_tz_offset_h,
            "peak_equity": self.peak_equity,
            "halted": self.halted,
            "current_day": self.current_day.isoformat() if self.current_day else None,
            "total_halts": self.total_halts,
            "max_dd_pct_seen": self.max_dd_pct_seen,
            "trip_times": [
                (dt.isoformat() if hasattr(dt, "isoformat") else str(dt), float(p))
                for (dt, p) in self.trip_times
            ],
        }

    def from_state(self, state: Dict[str, Any]) -> None:
        if state.get("schema") != self.SCHEMA_VERSION:
            raise ValueError(
                f"DDBreaker state schema {state.get('schema')!r} "
                f"!= current {self.SCHEMA_VERSION}")
        self.peak_equity = float(state.get("peak_equity", 0.0))
        self.halted = bool(state.get("halted", False))
        cd = state.get("current_day")
        if cd:
            try:
                self.current_day = date.fromisoformat(cd)
            except ValueError:
                self.current_day = None
        else:
            self.current_day = None
        self.total_halts = int(state.get("total_halts", 0))
        self.max_dd_pct_seen = float(state.get("max_dd_pct_seen", 0.0))
        self.trip_times = []
        for item in state.get("trip_times", []) or []:
            try:
                ts_str, pct = item
                self.trip_times.append((datetime.fromisoformat(ts_str), float(pct)))
            except (TypeError, ValueError):
                continue

    def save_state(self, path) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(self.to_state(), indent=2), encoding="utf-8")
        os.replace(tmp, p)

    def load_state(self, path) -> Tuple[bool, str]:
        p = Path(path)
        if not p.exists():
            return False, f"file not found: {p}"
        try:
            state = json.loads(p.read_text(encoding="utf-8"))
            self.from_state(state)
            return True, (f"peak=${self.peak_equity:,.0f} "
                          f"max_dd_seen={self.max_dd_pct_seen*100:.2f}% "
                          f"halted_today={self.halted}")
        except (OSError, ValueError, json.JSONDecodeError) as e:
            return False, str(e)


# =============================================================================
#  Backtest post-hoc filter
# =============================================================================
def apply_dd_breaker(trades,
                     starting_balance: float,
                     halt_pct: float = 0.04,
                     server_tz_offset_h: float = 0.0):
    """Simulate DDBreaker on a post-hoc trade list.

    Walks trades in chronological close-time order, maintains running equity
    & peak, and for every trade CHECKS at OPEN time whether the breaker is
    tripped.  If so: that trade is cancelled (dropped).

    Conservative approximation: we DO NOT truncate already-running trades
    (the ones that opened before the breaker tripped but close after); they
    close at their original SL/TP as simulated by the engine. This means the
    backtest DD can still overshoot `halt_pct` by the worst-case single-trade
    loss — live, it won't, because we'd flatten. So the live bot is STRICTER
    than this simulation; treat the backtest number as an upper bound.

    Parameters
    ----------
    trades : list of Trade-like objects (must have `.open_time_unix`,
             `.close_time_unix`, `.net_pnl`)
    starting_balance : float
    halt_pct : float   (default 0.04 = 4 %)
    server_tz_offset_h : float

    Returns
    -------
    kept_trades, breaker_state
        kept_trades : list   (subset of `trades`, same order, same objects)
        breaker_state : DDBreaker  (with telemetry: trip times, halt count, ...)
    """
    if not trades:
        return [], DDBreaker(halt_pct=halt_pct,
                             server_tz_offset_h=server_tz_offset_h)

    # Figure out how to extract timestamps / pnls
    def _open_ts(t):
        v = getattr(t, "open_time_unix", None) or getattr(t, "entry_time_unix", None)
        if v is None:
            # fall back to `.open_time` datetime
            dt = getattr(t, "open_time", None) or getattr(t, "entry_time", None)
            if dt is None:
                raise AttributeError("trade has no open/entry timestamp")
            return dt.timestamp() if isinstance(dt, datetime) else float(dt)
        return float(v)

    def _close_ts(t):
        v = getattr(t, "close_time_unix", None) or getattr(t, "exit_time_unix", None)
        if v is None:
            dt = getattr(t, "close_time", None) or getattr(t, "exit_time", None)
            if dt is None:
                raise AttributeError("trade has no close/exit timestamp")
            return dt.timestamp() if isinstance(dt, datetime) else float(dt)
        return float(v)

    def _pnl(t):
        return float(getattr(t, "net_pnl",
                             getattr(t, "pnl",
                                     getattr(t, "profit", 0.0))))

    br = DDBreaker(halt_pct=halt_pct, server_tz_offset_h=server_tz_offset_h)
    kept = []
    equity = starting_balance
    # sort by OPEN time to evaluate the breaker at the right instant
    trades_sorted = sorted(trades, key=_open_ts)

    # We need to update equity as trades CLOSE (not open), and check breaker
    # as new trades OPEN. Merge events chronologically:
    events = []
    for t in trades_sorted:
        events.append((_open_ts(t), 0, t))   # 0 = OPEN
    for t in trades_sorted:
        events.append((_close_ts(t), 1, t))  # 1 = CLOSE
    events.sort(key=lambda e: (e[0], e[1]))   # same ts: CLOSE before OPEN

    # Track which trades we've admitted (so we only CLOSE ones we kept)
    admitted = set()

    for ts, kind, t in events:
        if kind == 1:   # CLOSE event
            if id(t) in admitted:
                equity += _pnl(t)
                # update peak via check()
                br.check(ts, equity)
        else:           # OPEN event
            halted, _ = br.check(ts, equity)
            if not halted:
                kept.append(t)
                admitted.add(id(t))

    return kept, br


__all__ = ["DDBreaker", "apply_dd_breaker"]
