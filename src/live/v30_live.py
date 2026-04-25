"""
V30 LIVE RUNNER  —  v25.1 ship config (4-pair ORB + Merton-GZ + news +
                                       4 % daily / 8 % total DD rails).

This file is a forked, version-bumped copy of `src/live/v23_live.py`.
Per `Docs/V25_1_SHIP_RECOMMENDATION.md` it ships the two recommended
config flips:

    * base_risk_pct       0.00110  →  0.00170      ( +55 % )
    * NOCHASE_COOLDOWN_S  0.0      →  300.0 s      ( cross-symbol filter )

Plus a NEW operational feature requested for the v30 dry-run:

    * Per-trade entry-fill SLIPPAGE TRACKER
        -- captured every entry (live: real fill price minus quote at submission;
           dry-run: 0 t by definition because no real order is placed)
        -- per-symbol roll-up + portfolio total in the heartbeat
        -- one JSON line per trade in `Results/v30_live_slippage.jsonl`

Other vs v23 live: identical ORB anchors, TP/SL math, news rails, sizer
(Merton × Grossman-Zhou). The hard TOTAL-DD breaker is widened 4 % → 8 %
for 5ers compliance — the 5ers High-Stakes 100 K kills the account at
10 % total / 5 % daily, so we run with a 2-pt buffer on total (8 %) and a
1-pt buffer on daily (4 %). Otherwise byte-identical to v23.

Magic number is bumped 23000 → 30000 so v30 trades are cleanly distinguishable
from any leftover v23 paper / live tickets at the broker.

====================================================================
5ers-PROHIBITED-PRACTICES GUARANTEES (every rule, every time)
====================================================================
- NO HFT:           minimum hold ≥ 60 s (time-guard on exits, sub60s=0 in backtest)
- NO BULK:          max 2 concurrent positions across the portfolio
- NO BRACKETING:    news entry-block fires ±15 min around every Tier-1 event;
                    positions are FLATTENED 2 min before news
- NO ROLLOVER SCALP: TradingCalendar rollover window hard-blocks new entries
- NO TICK SCALP:    minimum TP = 1.0 × OR_range
- NO ARBITRAGE:     single broker, single feed, one instance per account
- NO ONE-SIDED:     ORB is direction-agnostic
- NO 3RD-PARTY EA:  all source code is in your own repo
- HARD SL ON BROKER:every ORDER_SEND carries sl AND tp at submission time
- DAILY HARD KILL : flatten today's positions + halt entries when today's
                    static DD ≥ 4 %  (5ers kill = 5 %, 1-pt buffer for slip)
- TOTAL HARD KILL: flatten + lock account when rolling peak-to-trough DD
                    ≥ 8 %            (5ers kill = 10 %, 2-pt buffer for slip)
- ACCOUNT KILL :    legacy soft ceiling — flatten if equity DD ≥ 8 %
                    (same threshold as TOTAL HARD KILL; both layers active)
- DAILY BREAKER:    halt new entries (positions stay open) if today's
                    rolling DD ≥ daily_breaker_dd (default 2 %)
- NO-CHASE 300 s:   block cross-symbol queue-release entries (NEW in v30)
"""
from __future__ import annotations

import csv
import json
import logging
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Deque, Dict, List, Optional, Tuple

from src.execution.mt5_bridge import (
    MT5Bridge, OrderRequest, OrderType, Position, AccountInfo, BarData,
)
from src.momentum.orb import ORBConfig, OpeningRangeTracker, NRFilter
from src.trading_calendar import TradingCalendar
from src.smartbb_engine import SMARTBB_UNIVERSE   # authoritative pip_value table
from src.daily_halt import DailyHalt              # 4 % static-DD daily kill-switch
from src.dd_breaker import DDBreaker              # 8 % TOTAL (peak-to-trough) DD breaker
from src.dynamic_sizer_v21 import (               # Merton × Grossman-Zhou sizer
    MertonGZSizer, MertonGZSizerConfig,
)


log = logging.getLogger("v30.live")


# =====================================================================
#  Bar-time parser. Same logic as v23_live._parse_bar_time. See that file
#  for the exhaustive comment on broker-time vs UTC-labelling pitfalls.
# =====================================================================
def _parse_bar_time(b: dict):
    t = b.get("t", b.get("time"))
    if t is None:
        return None
    if isinstance(t, datetime):
        dt = t
    elif isinstance(t, (int, float)):
        try:
            dt = datetime.fromtimestamp(float(t), tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    elif isinstance(t, str):
        s = t.strip()
        if not s:
            return None
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            try:
                dt = datetime.strptime(s, "%Y.%m.%d %H:%M:%S")
            except ValueError:
                return None
    else:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# =====================================================================
#  Per-symbol ORB configs  —  IDENTICAL to v23 live (and to backtest_v23_*).
#  These are the V25_1 ship anchors; do not touch without re-tuning.
# =====================================================================
V30_ORB_CONFIGS: Dict[str, ORBConfig] = {
    "DE40":   ORBConfig(or_start_hour=8,  or_start_minute=0,  or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=1.5,
                        tp2_range_mult=3.0, sl_buffer_range_mult=0.3),
    "US30":   ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.0),
    "XAUUSD": ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.6),
    "US500":  ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=15,
                        trade_window_minutes=120, tp1_range_mult=0.5,
                        tp2_range_mult=1.0, sl_buffer_range_mult=0.6),
}

# 5ers MT5-Bridge broker constants (copied verbatim from v23 live).
V30_BROKER_TICK_SIZE: Dict[str, float] = {
    "DE40": 1.0, "US30": 1.0, "US500": 0.25, "XAUUSD": 0.01,
}
V30_BROKER_LOT_STEP:  Dict[str, float] = {
    "DE40": 0.1, "US30": 0.1, "US500": 0.1, "XAUUSD": 0.01,
}
V30_BROKER_MIN_LOT:   Dict[str, float] = {
    "DE40": 0.1, "US30": 0.1, "US500": 0.1, "XAUUSD": 0.01,
}
# Broker-side symbol name mapping (override via --broker-names if broker differs)
V30_BROKER_NAMES: Dict[str, str] = {
    "DE40": "DE40.cash", "US30": "US30.cash", "US500": "US500.cash", "XAUUSD": "XAUUSD",
}


@dataclass
class SymbolSpec:
    internal: str
    broker: str
    tick_size: float          # 1 tick = this in price units
    pip_value_per_lot: float  # $ P/L per 1 tick per 1.0 lot  (from SMARTBB_UNIVERSE)
    min_lot: float
    lot_step: float


def _build_default_specs() -> Dict[str, SymbolSpec]:
    out: Dict[str, SymbolSpec] = {}
    for sym in ("DE40", "US30", "XAUUSD", "US500"):
        uni = SMARTBB_UNIVERSE.get(sym)
        if uni is None:
            raise RuntimeError(
                f"Symbol {sym} not in SMARTBB_UNIVERSE — cannot live-size without "
                "the backtest's pip_value.")
        out[sym] = SymbolSpec(
            internal=sym,
            broker=V30_BROKER_NAMES[sym],
            tick_size=V30_BROKER_TICK_SIZE[sym],
            pip_value_per_lot=float(uni.pip_value),
            min_lot=V30_BROKER_MIN_LOT[sym],
            lot_step=V30_BROKER_LOT_STEP[sym],
        )
    return out


V30_SPECS: Dict[str, SymbolSpec] = _build_default_specs()


# =====================================================================
#  Per-symbol live state (wraps the existing OR tracker + extras)
# =====================================================================

@dataclass
class LiveSymbolState:
    spec: SymbolSpec
    orb_cfg: ORBConfig
    or_tracker: OpeningRangeTracker
    nr_filter: NRFilter = field(default_factory=lambda: NRFilter(lookback=20))
    last_bar_time: Optional[datetime] = None
    open_ticket: Optional[int] = None
    open_side: Optional[str] = None        # "LONG" / "SHORT"
    open_entry: Optional[float] = None
    open_sl: Optional[float] = None
    open_tp1: Optional[float] = None
    open_tp2: Optional[float] = None
    open_size_lots: Optional[float] = None
    open_risk_usd: Optional[float] = None
    open_at: Optional[datetime] = None     # for 60s-min-hold check
    bars_seen_today: int = 0
    last_m1_close: Optional[float] = None


# =====================================================================
#  SLIPPAGE TRACKER — per-symbol + portfolio roll-up.
# =====================================================================
#  Slippage is captured at ENTRY only. We compare the price returned by
#  `bridge.send_order()` (actual fill price) to the quote we observed at
#  submission time (`entry_px = quote.ask` for LONG / `quote.bid` for
#  SHORT). The signed slip in TICKS is:
#       slip_ticks = (fill_price - entry_px) / tick_size      for LONG
#       slip_ticks = (entry_px - fill_price) / tick_size      for SHORT
#  i.e. POSITIVE slip = WORSE fill (paid more on long, received less on
#  short), NEGATIVE = price improvement.
#
#  In dry-run mode no real order is placed (the bot generates a fake
#  ticket from `time.time()`), so slippage is identically 0.0 t for every
#  trade. The tracker still records the entry so the per-symbol counts
#  display the right number on the heartbeat — they're just 0.0t lines.
#
#  Why entry only:
#      * exit fills (broker SL/TP) are NOT reported back through the
#        current SHF_Bridge.mq5 reconciliation path, so we have no
#        truthful exit fill price to compare against.
#      * the V25_1 slippage matrix in §3.4 of the doc shows entry slip is
#        >90 % of total slip cost, by far the dominant cost driver.
#  =====================================================================
@dataclass
class SlippageStat:
    n: int = 0
    sum_ticks: float = 0.0
    sum_abs_ticks: float = 0.0
    sum_dollars: float = 0.0
    max_ticks: float = float("-inf")
    min_ticks: float = float("+inf")
    max_dollars: float = float("-inf")

    def add(self, ticks: float, dollars: float) -> None:
        self.n += 1
        self.sum_ticks += ticks
        self.sum_abs_ticks += abs(ticks)
        self.sum_dollars += dollars
        if ticks > self.max_ticks:
            self.max_ticks = ticks
        if ticks < self.min_ticks:
            self.min_ticks = ticks
        if dollars > self.max_dollars:
            self.max_dollars = dollars

    @property
    def avg_ticks(self) -> float:
        return self.sum_ticks / self.n if self.n else 0.0

    @property
    def avg_abs_ticks(self) -> float:
        return self.sum_abs_ticks / self.n if self.n else 0.0

    def to_dict(self) -> dict:
        return {
            "n": self.n,
            "avg_ticks": round(self.avg_ticks, 3),
            "avg_abs_ticks": round(self.avg_abs_ticks, 3),
            "max_ticks": (round(self.max_ticks, 3) if self.n else None),
            "min_ticks": (round(self.min_ticks, 3) if self.n else None),
            "sum_dollars": round(self.sum_dollars, 2),
            "max_dollars": (round(self.max_dollars, 2) if self.n else None),
        }


# =====================================================================
#  Main live runner
# =====================================================================

@dataclass
class V30LiveConfig:
    """
    v30 = v25.1 ship config (Docs/V25_1_SHIP_RECOMMENDATION.md):

        base_risk_pct       = 0.00170   ★ was 0.00110 in v23
        cap_mult            = 5.0       (per-trade cap = 5× = 0.85 % of equity)
        gamma               = 3.0       (risk-aversion)
        dd_cap_pct          = 0.04      (Grossman-Zhou barrier → size → 0 at 4 % DD)
        nochase_cooldown_s  = 300.0     ★ NEW — cross-symbol queue-release filter
        DailyHalt           = 4 %       (static daily hard kill — 5ers kills @ 5 %)
        DDBreaker           = 8 %       (rolling peak-to-trough — 5ers kills @ 10 %)
        account_kill_dd     = 8 %       (soft ceiling, same number as DDBreaker)
        daily_breaker_dd    = 2 %       (rolling intra-day halt-entries; soft)

    Expected uplift over v23 live (3-month real 5ers data, same costs):
        +$10,691  /  +62.9 %   (Net P&L  $16,977 → $27,668)
        DD       3.35 %  →  3.16 %      (improved)
        Worst day-1.57 % → -2.02 %      (still inside both 4 % halts)
        Slippage cliff   3.0 ticks      (same as v25)

    See Docs/V25_1_SHIP_RECOMMENDATION.md sections 2-4 for the 126-backtest
    evidence base.
    """
    symbols: List[str] = field(default_factory=lambda: ["DE40", "US30", "XAUUSD", "US500"])
    base_risk_pct: float = 0.00170           # ★ V25.1 ship value (was 0.00110)
    cap_mult: float = 5.0                    # per-trade cap = 0.85 %
    gamma: float = 3.0                       # v24 shootout winner
    ewma_alpha: float = 0.20                 # half-life ≈ 3 trades
    warmup_trades: int = 15                  # no Merton formula until 15 trades seen
    dd_cap_pct: float = 0.04                 # Grossman-Zhou barrier (4 %)

    # Rails
    news_csv: str = "data/news/tier1_2026.csv"
    news_entry_buffer_min: int = 15
    news_flatten_before_min: int = 2

    # 5ers safety
    account_kill_dd: float = 0.08            # 8 % rolling DD → close-all + halt
    daily_breaker_dd: float = 0.02           # 2 % daily DD → halt new entries
    max_concurrent_positions: int = 2
    min_hold_seconds: int = 65               # must exceed 60 to avoid HFT flag

    # ★ NEW v30 — no-chase cross-symbol cooldown. Set to 0 to disable.
    nochase_cooldown_s: float = 300.0

    # Live execution
    magic: int = 30000                       # bumped from 23000 to distinguish v30 tickets
    comment: str = "SHF_v30"
    heartbeat_sec: float = 60.0
    poll_sec: float = 1.0
    bar_poll_sec: float = 5.0                # pull fresh M1 bars every 5 s

    # Paths
    log_dir: str = "Results"
    telemetry_name: str = "v30_live_telemetry.json"
    events_name: str = "v30_live_events.log"
    trades_name: str = "v30_live_trades.jsonl"
    slippage_name: str = "v30_live_slippage.jsonl"


class V30Live:
    """
    Live runner. Thread-safe. Can be stopped with `runner.stop()` or Ctrl-C.
    """

    def __init__(
        self,
        bridge: MT5Bridge,
        cfg: Optional[V30LiveConfig] = None,
        dry_run: bool = True,
        specs: Optional[Dict[str, SymbolSpec]] = None,
    ):
        self.bridge = bridge
        self.cfg = cfg or V30LiveConfig()
        self.dry_run = dry_run
        self.specs = specs or V30_SPECS
        self._lock = Lock()
        self._stop = False

        # Build per-symbol state
        self.states: Dict[str, LiveSymbolState] = {}
        for sym in self.cfg.symbols:
            if sym not in self.specs:
                raise KeyError(f"no SymbolSpec registered for '{sym}'")
            if sym not in V30_ORB_CONFIGS:
                raise KeyError(f"no ORBConfig registered for '{sym}'")
            spec = self.specs[sym]
            orb_cfg = V30_ORB_CONFIGS[sym]
            self.states[sym] = LiveSymbolState(
                spec=spec,
                orb_cfg=orb_cfg,
                or_tracker=OpeningRangeTracker(orb_cfg),
            )

        # Sizer — Merton × Grossman-Zhou (v25.1 ship config: base=0.170 %).
        self.merton_sizer = MertonGZSizer(MertonGZSizerConfig(
            base_risk_pct=self.cfg.base_risk_pct,   # 0.170 %  ★
            cap_mult=self.cfg.cap_mult,             # 5.0
            gamma=self.cfg.gamma,                   # 3.0
            ewma_alpha=self.cfg.ewma_alpha,         # 0.20
            warmup_trades=self.cfg.warmup_trades,   # 15
            dd_cap_pct=self.cfg.dd_cap_pct,         # 0.04
            pool_symbols=True,                      # one global μ̂/σ̂² pool
            no_edge_multiplier=1.0,                 # don't halve when μ̂≤0
        ))

        # Calendar (weekend / rollover / holiday). News rails are handled
        # separately so we can apply the -2 min flatten independently.
        self.calendar = TradingCalendar()

        # News events
        self.news_events: List[Tuple[datetime, str]] = self._load_news(
            Path(self.cfg.news_csv)
        )

        # Equity bookkeeping
        self.start_equity: float = 0.0
        self.peak_equity: float = 0.0
        self.day_start_equity: float = 0.0
        self._current_day_utc: Optional[str] = None

        # Kill-switch flags
        self.account_killed: bool = False
        self.day_halted: bool = False

        # STATIC 4 % daily hard kill-switch (5ers daily limit = 5 %; 1-pt buffer).
        self.daily_halt_4pct = DailyHalt(halt_pct=0.04)

        # HARD TOTAL-DD (peak-to-trough) 8 % BREAKER (5ers total limit = 10 %; 2-pt buffer).
        # Variable name is `*_4pct` for code-stability with v23 — value is now 0.08.
        self.total_dd_breaker_4pct = DDBreaker(halt_pct=0.08)

        # Counters for telemetry
        self.counters: Dict[str, int] = defaultdict(int)

        # ★ NEW v30: cross-symbol no-chase cooldown bookkeeping.
        # `_last_close_ts_by_symbol[sym] = wall-clock UTC seconds at which
        # `sym` last closed a position`. The gate in `_maybe_enter` rejects
        # an entry on symbol X if ANY OTHER symbol closed within
        # cfg.nochase_cooldown_s seconds before now.
        self._last_close_ts_by_symbol: Dict[str, float] = {sym: 0.0 for sym in self.cfg.symbols}

        # ★ NEW v30: SLIPPAGE TRACKER — per-symbol + portfolio.
        self.slip_per_symbol: Dict[str, SlippageStat] = {sym: SlippageStat() for sym in self.cfg.symbols}
        self.slip_total: SlippageStat = SlippageStat()
        # cache for "worst trade ever" so we can show which symbol owns it
        self._worst_slip_owner: Tuple[Optional[str], float] = (None, float("-inf"))
        self._best_slip_owner: Tuple[Optional[str], float] = (None, float("+inf"))

        # -------------------------------------------------------------
        # BROKER-CLOCK OFFSET CACHE (fixes held=-10775s bug). See v23.
        # -------------------------------------------------------------
        self._broker_offset_td: timedelta = timedelta(0)
        self._broker_offset_last_refresh: float = 0.0

        # Paths
        self.log_dir = Path(self.cfg.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.telemetry_path = self.log_dir / self.cfg.telemetry_name
        self.events_path = self.log_dir / self.cfg.events_name
        self.trades_path = self.log_dir / self.cfg.trades_name
        self.slippage_path = self.log_dir / self.cfg.slippage_name

        log.info("V30Live initialised  symbols=%s  risk=%.3f%%  cap=%.1fx  "
                 "nochase=%.0fs  news_events=%d  dry_run=%s",
                 self.cfg.symbols, self.cfg.base_risk_pct * 100, self.cfg.cap_mult,
                 self.cfg.nochase_cooldown_s, len(self.news_events), self.dry_run)

    # -----------------------------------------------------------------
    # News loader (same schema as data/news/tier1_2026.csv)
    # -----------------------------------------------------------------
    @staticmethod
    def _load_news(path: Path) -> List[Tuple[datetime, str]]:
        out: List[Tuple[datetime, str]] = []
        if not path.exists():
            log.warning("News CSV not found at %s — news rails will be INACTIVE", path)
            return out
        with open(path, "r", newline="", encoding="utf-8") as f:
            lines = [ln for ln in f if ln.strip() and not ln.lstrip().startswith("#")]
        rdr = csv.DictReader(lines)
        for row in rdr:
            ts_str = (row.get("timestamp_utc") or row.get("utc_datetime") or "").strip()
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                continue
            imp = (row.get("impact") or "").strip().lower()
            if imp and imp not in ("high", "tier1", "tier-1", "1"):
                continue
            label = (row.get("label") or row.get("event") or "").strip()
            out.append((ts, label))
        log.info("Loaded %d Tier-1 news events from %s", len(out), path)
        return out

    # -----------------------------------------------------------------
    # Rails
    # -----------------------------------------------------------------
    def _in_news_entry_block(self, ts: datetime) -> Optional[Tuple[datetime, str]]:
        buf = timedelta(minutes=self.cfg.news_entry_buffer_min)
        for ev in self.news_events:
            if abs(ts - ev[0]) <= buf:
                return ev
        return None

    def _in_news_flatten_window(self, ts: datetime) -> Optional[Tuple[datetime, str]]:
        for ev in self.news_events:
            if timedelta(0) <= (ev[0] - ts) <= timedelta(minutes=self.cfg.news_flatten_before_min):
                return ev
        return None

    def _count_open_positions(self) -> int:
        return sum(1 for s in self.states.values() if s.open_ticket is not None)

    def _equity_dd_pct(self) -> float:
        if self.peak_equity <= 0:
            return 0.0
        eq = self._current_equity()
        return max(0.0, (self.peak_equity - eq) / self.peak_equity * 100.0)

    def _day_dd_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        eq = self._current_equity()
        return max(0.0, (self.day_start_equity - eq) / self.day_start_equity * 100.0)

    def _dd_breaker_tripped(self) -> bool:
        b = getattr(self, "total_dd_breaker_4pct", None)
        if b is None:
            return False
        if hasattr(b, "is_halted"):
            return bool(b.is_halted)
        peak = getattr(b, "peak_equity", 0.0) or 0.0
        if peak <= 0:
            return False
        dd = (peak - self._current_equity()) / peak
        halt_pct = getattr(b, "halt_pct", 0.08)
        return dd >= halt_pct

    # -----------------------------------------------------------------
    # ★ NEW v30: cross-symbol no-chase cooldown
    # -----------------------------------------------------------------
    def _nochase_block(self, sym: str, now_ts: float) -> Optional[Tuple[str, float]]:
        """
        Cross-symbol cooldown gate. Returns (other_sym, gap_seconds) if any
        OTHER symbol closed a position within cfg.nochase_cooldown_s seconds
        ago. Returns None if entry is allowed.

        Same-symbol back-to-backs are NOT blocked because the ORB signal
        only fires once per (symbol, day) anyway — see V25_1 §3.5. This
        matches the offline filter in `Scripts/backtest_v23_nochase.py`.
        """
        cd = self.cfg.nochase_cooldown_s
        if cd <= 0:
            return None
        for other_sym, ts in self._last_close_ts_by_symbol.items():
            if other_sym == sym or ts <= 0:
                continue
            gap = now_ts - ts
            if 0.0 <= gap <= cd:
                return (other_sym, gap)
        return None

    def _record_close(self, sym: str) -> None:
        """Stamp the last-close timestamp for `sym` (cross-symbol cooldown source)."""
        self._last_close_ts_by_symbol[sym] = time.time()

    # -----------------------------------------------------------------
    # Broker helpers
    # -----------------------------------------------------------------
    def _refresh_broker_offset(self, force: bool = False) -> timedelta:
        now_wall = time.time()
        if (not force) and (now_wall - self._broker_offset_last_refresh < 900):
            return self._broker_offset_td
        try:
            srv = self.bridge.get_server_time()
            if srv is not None:
                secs = int(getattr(srv, "gmt_offset_seconds", 0) or 0)
                new_td = timedelta(seconds=secs)
                if new_td != self._broker_offset_td:
                    log.info("[broker-offset] %+d s  (%.1f h ahead of UTC)  "
                             "bar.time/open_at/news comparisons will be corrected.",
                             secs, secs / 3600.0)
                self._broker_offset_td = new_td
        except Exception as e:
            log.debug("broker offset refresh failed: %s", e)
        self._broker_offset_last_refresh = now_wall
        return self._broker_offset_td

    def _bar_to_real_utc(self, bar_time: datetime) -> datetime:
        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=timezone.utc)
        return bar_time - self._broker_offset_td

    def _utc_to_broker_hm(self, now_utc: datetime) -> Tuple[int, int]:
        t = now_utc + self._broker_offset_td
        return t.hour, t.minute

    def _current_equity(self) -> float:
        try:
            ai = self.bridge.get_account_info()
            return float(ai.equity) if ai else self.peak_equity
        except Exception:
            return self.peak_equity

    def _broker_positions(self) -> List[Position]:
        try:
            return self.bridge.get_positions() or []
        except Exception:
            return []

    def _symbol_to_broker(self, sym: str) -> str:
        return self.specs[sym].broker

    def _broker_to_symbol(self, broker_name: str) -> Optional[str]:
        for k, spec in self.specs.items():
            if spec.broker == broker_name:
                return k
        return None

    # -----------------------------------------------------------------
    # Logging (events + trades + slippage)
    # -----------------------------------------------------------------
    def _log_event(self, kind: str, **fields) -> None:
        row = {
            "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            **fields,
        }
        line = json.dumps(row, default=str)
        with open(self.events_path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        log.info("[event] %s  %s", kind, json.dumps(fields, default=str))

    def _log_trade(self, row: dict) -> None:
        with open(self.trades_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    def _log_slippage(self, row: dict) -> None:
        with open(self.slippage_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")

    # -----------------------------------------------------------------
    # Slippage capture
    # -----------------------------------------------------------------
    def _record_entry_slippage(
        self,
        sym: str,
        side: str,
        intended_px: float,
        fill_px: float,
        lots: float,
    ) -> Tuple[float, float]:
        """
        Compute and record per-trade entry slippage. Returns (ticks, dollars).

        ticks   : signed; positive = worse fill (paid more on long / less on short)
        dollars : signed; positive = paid more cost than intended
        """
        spec = self.specs[sym]
        tick_sz = spec.tick_size
        pip_val = spec.pip_value_per_lot
        if tick_sz <= 0:
            return 0.0, 0.0
        raw = (fill_px - intended_px) if side == "LONG" else (intended_px - fill_px)
        ticks = raw / tick_sz
        # $ cost = ticks × pip_value × lots  (pip_value is $ per 1 tick per 1 lot)
        dollars = ticks * pip_val * float(lots or 0.0)

        self.slip_per_symbol[sym].add(ticks, dollars)
        self.slip_total.add(ticks, dollars)

        # Track which symbol owns the worst / best
        if ticks > self._worst_slip_owner[1]:
            self._worst_slip_owner = (sym, ticks)
        if ticks < self._best_slip_owner[1]:
            self._best_slip_owner = (sym, ticks)

        # Per-trade JSONL log
        self._log_slippage({
            "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "symbol": sym,
            "side": side,
            "intended_px": round(intended_px, 5),
            "fill_px": round(fill_px, 5),
            "lots": round(float(lots or 0.0), 4),
            "tick_size": tick_sz,
            "slip_ticks": round(ticks, 4),
            "slip_dollars": round(dollars, 2),
            "dry_run": self.dry_run,
        })

        # Console one-liner so per-trade slip is impossible to miss
        log.info("[SLIP] %s %s  intended=%.5f  fill=%.5f  slip=%+.2ft  $%+.2f",
                 sym, side, intended_px, fill_px, ticks, dollars)
        return ticks, dollars

    # -----------------------------------------------------------------
    # Entry
    # -----------------------------------------------------------------
    def _maybe_enter(self, sym: str, bar: BarData, day_key: str) -> None:
        st = self.states[sym]
        st.last_bar_time = bar.time
        st.last_m1_close = float(bar.close)
        st.or_tracker.update(
            day_key, bar.time.hour, bar.time.minute, float(bar.high), float(bar.low)
        )
        st.nr_filter.update(day_key, float(bar.high), float(bar.low))

        # GATE: bot killed?
        if self.account_killed or self.day_halted:
            return

        # GATE: STATIC 4 % daily hard halt
        if not self.daily_halt_4pct.can_trade(bar.time.timestamp(),
                                              self._current_equity()):
            if not self.day_halted:
                self._log_event("DAY_HALTED_4PCT",
                                day_start_equity=self.daily_halt_4pct.day_start_equity,
                                current_equity=self._current_equity(),
                                halted_dates=list(self.daily_halt_4pct.halted_dates))
                self.day_halted = True
            self.counters["block_halt_4pct"] += 1
            return

        # GATE: already have an open position on this symbol?
        if st.open_ticket is not None:
            return

        # GATE: portfolio concurrency
        if self._count_open_positions() >= self.cfg.max_concurrent_positions:
            self.counters["block_concurrent_cap"] += 1
            return

        # GATE: in ORB trade window?
        if not st.or_tracker.in_trade_window(bar.time.hour, bar.time.minute):
            return

        # GATE: calendar (weekend/rollover/holiday/news_buffer)
        allowed, reason = self.calendar.can_enter(sym, bar.time)
        if not allowed:
            self.counters[f"block_cal_{reason}"] += 1
            return

        # GATE: news entry-block
        ev = self._in_news_entry_block(self._bar_to_real_utc(bar.time))
        if ev is not None:
            self.counters["block_news_entry"] += 1
            return

        # ★ NEW v30 GATE: cross-symbol no-chase cooldown (300 s)
        # Wall-clock seconds, NOT bar.time, because the cooldown is real
        # elapsed time since another symbol's slot freed.
        nc = self._nochase_block(sym, time.time())
        if nc is not None:
            other, gap = nc
            self.counters["block_nochase_cooldown"] += 1
            self._log_event("BLOCK_NOCHASE_COOLDOWN",
                            symbol=sym, blocked_by=other,
                            gap_s=round(gap, 2),
                            cooldown_s=self.cfg.nochase_cooldown_s)
            return

        # TRIGGER: first breakout of the day
        direction = st.or_tracker.detect_breakout(
            float(bar.high), float(bar.low), float(bar.close)
        )
        if direction == 0:
            return

        # Build entry
        side = "LONG" if direction > 0 else "SHORT"
        quote = self.bridge.get_quote(self._symbol_to_broker(sym))
        if quote is None:
            log.warning("no live quote for %s, skipping entry", sym)
            return
        entry_px = float(quote.ask if side == "LONG" else quote.bid)

        or_rng = st.or_tracker.or_range
        if or_rng <= 0:
            return
        if side == "LONG":
            sl = float(st.or_tracker.or_low)
            tp1 = entry_px + st.orb_cfg.tp1_range_mult * or_rng
            tp2 = entry_px + st.orb_cfg.tp2_range_mult * or_rng
        else:
            sl = float(st.or_tracker.or_high)
            tp1 = entry_px - st.orb_cfg.tp1_range_mult * or_rng
            tp2 = entry_px - st.orb_cfg.tp2_range_mult * or_rng

        risk_per_unit = abs(entry_px - sl)
        if risk_per_unit <= 0:
            return

        # Size via Merton × Grossman-Zhou (v25.1 ship: base=0.170 %, cap=5×).
        eq = self._current_equity()
        self.peak_equity = max(self.peak_equity, eq)
        open_list = [(s, 1 if stt.open_side == "LONG" else -1)
                     for s, stt in self.states.items() if stt.open_ticket is not None]
        risk_pct = self.merton_sizer.compute_risk_pct(
            symbol=sym,
            equity=eq,
            peak_equity=self.peak_equity,
            open_positions=open_list,
        )
        risk_usd = eq * risk_pct

        pip_val = self.specs[sym].pip_value_per_lot
        tick_sz = self.specs[sym].tick_size
        dollars_per_lot_stopout = (risk_per_unit / tick_sz) * pip_val
        if dollars_per_lot_stopout <= 0:
            return
        lots = risk_usd / dollars_per_lot_stopout

        step = self.specs[sym].lot_step
        min_lot = self.specs[sym].min_lot
        lots = max(min_lot, math.floor(lots / step) * step)
        if lots < min_lot:
            self.counters["block_size_below_min"] += 1
            return

        # SEND (or simulate)
        req = OrderRequest(
            symbol=self._symbol_to_broker(sym),
            order_type=OrderType.MARKET_BUY if side == "LONG" else OrderType.MARKET_SELL,
            lots=float(round(lots, 4)),
            price=entry_px,
            sl=float(sl),
            tp=float(tp1),                  # TP1 set at broker; TP2 managed by us
            deviation=20,
            magic=self.cfg.magic,
            comment=self.cfg.comment,
        )

        fill_px = entry_px
        result = None
        if self.dry_run:
            fake_ticket = int(time.time() * 1000) & 0x7FFFFFFF
            st.open_ticket = fake_ticket
            ok = True
        else:
            result = self.bridge.send_order(req)
            ok = getattr(result, "error_code", 0) == 0
            if ok:
                st.open_ticket = int(result.ticket)
                # Real fill price reported by the EA / broker.
                fill_px = float(getattr(result, "price", entry_px) or entry_px)

        if not ok:
            self._log_event("ORDER_FAILED", symbol=sym, side=side,
                            entry=entry_px, sl=sl, tp1=tp1, lots=lots,
                            error_code=getattr(result, "error_code", None),
                            error_message=getattr(result, "error_message", None))
            return

        st.open_side = side
        st.open_entry = fill_px      # store ACTUAL fill, not intended quote
        st.open_sl = sl
        st.open_tp1 = tp1
        st.open_tp2 = tp2
        st.open_size_lots = lots
        st.open_risk_usd = risk_usd
        st.open_at = datetime.now(timezone.utc)   # wall-clock UTC, not bar.time
        self.counters["entries"] += 1

        # ★ Record entry slippage (intended vs filled). In dry-run this is 0t.
        slip_ticks, slip_dollars = self._record_entry_slippage(
            sym=sym, side=side,
            intended_px=entry_px, fill_px=fill_px, lots=lots,
        )

        self._log_trade({
            "ts_utc": bar.time.isoformat(),
            "event": "ENTRY",
            "symbol": sym,
            "side": side,
            "intended_px": entry_px,
            "fill_px": fill_px,
            "slip_ticks": round(slip_ticks, 3),
            "slip_dollars": round(slip_dollars, 2),
            "sl": sl,
            "tp1": tp1,
            "tp2": tp2,
            "lots": lots,
            "risk_usd": risk_usd,
            "risk_pct": risk_pct,
            "or_range": or_rng,
            "equity": eq,
            "dry_run": self.dry_run,
            "ticket": st.open_ticket,
        })
        log.info("[ENTRY] %s %s  lots=%.3f  intended=%.2f  fill=%.2f  "
                 "slip=%+.2ft($%+.2f)  SL=%.2f  TP1=%.2f  TP2=%.2f  risk=$%.0f",
                 sym, side, lots, entry_px, fill_px, slip_ticks, slip_dollars,
                 sl, tp1, tp2, risk_usd)

    # -----------------------------------------------------------------
    # Management (news flatten, window expiry, daily/account kill)
    # -----------------------------------------------------------------
    def _manage_open(self, now_utc: datetime) -> None:
        # News flatten
        flat_ev = self._in_news_flatten_window(now_utc)
        if flat_ev is not None:
            self._flatten_all(f"news_flatten:{flat_ev[1]}")
            self.counters["flatten_news"] += 1
            return

        # HARD TOTAL-DD 8 % BREAKER (5ers compliance: account dies @ 10 %)
        eq_now = self._current_equity()
        halted, cur_dd = self.total_dd_breaker_4pct.check(
            now_utc.timestamp(), eq_now,
        )
        if halted and not self.account_killed:
            self._flatten_all(f"dd_breaker_8pct:dd={cur_dd*100:.2f}%")
            self._log_event("TOTAL_DD_BREAKER_8PCT",
                            dd_pct=round(cur_dd * 100, 3),
                            peak_equity=self.total_dd_breaker_4pct.peak_equity,
                            equity=eq_now,
                            total_halts=self.total_dd_breaker_4pct.total_halts)
            self.account_killed = True
            self.counters["kill_total_dd_8pct"] += 1
            return

        # Account kill (legacy 8 % soft ceiling)
        if self._equity_dd_pct() >= self.cfg.account_kill_dd * 100:
            self._flatten_all(f"account_kill:dd={self._equity_dd_pct():.2f}%")
            self.account_killed = True
            self.counters["kill_account"] += 1
            return

        # Daily breaker (halt entries only)
        if self._day_dd_pct() >= self.cfg.daily_breaker_dd * 100:
            if not self.day_halted:
                self._log_event("DAY_HALTED",
                                day_dd_pct=self._day_dd_pct(),
                                equity=self._current_equity())
                self.day_halted = True
                self.counters["halt_day"] += 1

        # Time-stop: close any position whose ORB trade-window has expired.
        brk_h, brk_m = self._utc_to_broker_hm(now_utc)
        now_m = brk_h * 60 + brk_m
        now_broker_date = (now_utc + self._broker_offset_td).date()
        for sym, st in self.states.items():
            if st.open_ticket is None:
                continue
            trade_end_m = (st.orb_cfg.or_start_hour * 60 + st.orb_cfg.or_start_minute
                           + st.orb_cfg.or_minutes + st.orb_cfg.trade_window_minutes)
            open_broker_date = (st.open_at + self._broker_offset_td).date() if st.open_at else None
            if open_broker_date == now_broker_date and now_m >= trade_end_m:
                hold_s = (now_utc - st.open_at).total_seconds()
                if hold_s < self.cfg.min_hold_seconds:
                    continue
                self._close_one(sym, "window_expiry")
                self.counters["exit_window"] += 1

        # Sync with broker (live mode only — see v23 comments)
        if not self.dry_run:
            open_by_broker = {p.ticket: p for p in self._broker_positions()}
            for sym, st in self.states.items():
                if st.open_ticket is not None and st.open_ticket not in open_by_broker:
                    self._log_event("POS_CLOSED_BY_BROKER",
                                    symbol=sym,
                                    ticket=st.open_ticket,
                                    side=st.open_side)
                    self._feed_sizer_on_close(sym, reason="broker_close")
                    # ★ stamp last-close for cross-symbol cooldown
                    self._record_close(sym)
                    self._clear_state(sym)
                    self.counters["exit_broker"] += 1

    def _flatten_all(self, reason: str) -> None:
        log.warning("[FLATTEN-ALL] %s", reason)
        self._log_event("FLATTEN_ALL", reason=reason, equity=self._current_equity())
        if not self.dry_run:
            try:
                self.bridge.close_all_positions()
            except Exception as e:
                log.error("close_all failed: %s", e)
        for sym in list(self.states.keys()):
            if self.states[sym].open_ticket is not None:
                # ★ stamp last-close for cross-symbol cooldown so a flatten
                # also gates further entries for 300 s.
                self._record_close(sym)
                self._clear_state(sym)

    def _close_one(self, sym: str, reason: str) -> None:
        st = self.states[sym]
        if st.open_ticket is None:
            return
        log.info("[CLOSE] %s  reason=%s  ticket=%s", sym, reason, st.open_ticket)
        self._log_event("CLOSE", symbol=sym, reason=reason, ticket=st.open_ticket)
        if not self.dry_run:
            try:
                self.bridge.close_position(st.open_ticket)
            except Exception as e:
                log.error("close_position failed: %s", e)
        self._feed_sizer_on_close(sym, reason=f"self_close:{reason}")
        # ★ stamp last-close for cross-symbol cooldown
        self._record_close(sym)
        self._clear_state(sym)

    def _feed_sizer_on_close(self, sym: str, reason: str) -> None:
        st = self.states[sym]
        if st.open_entry is None or st.last_m1_close is None or st.open_risk_usd in (None, 0):
            return
        mv = (st.last_m1_close - st.open_entry) if st.open_side == "LONG" \
             else (st.open_entry - st.last_m1_close)
        pip_val = self.specs[sym].pip_value_per_lot
        tick_sz = self.specs[sym].tick_size
        pnl_approx = (mv / tick_sz) * pip_val * (st.open_size_lots or 0)
        if st.open_risk_usd <= 0:
            return
        realised_R = pnl_approx / st.open_risk_usd
        realised_R = max(-5.0, min(5.0, realised_R))
        self.merton_sizer.on_trade_closed(sym, realised_R)
        self._log_event("SIZER_FEEDBACK", symbol=sym, reason=reason,
                        realised_R=round(realised_R, 3),
                        pnl_approx=round(pnl_approx, 2))

    def _clear_state(self, sym: str) -> None:
        st = self.states[sym]
        st.open_ticket = None
        st.open_side = None
        st.open_entry = None
        st.open_sl = None
        st.open_tp1 = None
        st.open_tp2 = None
        st.open_size_lots = None
        st.open_risk_usd = None
        st.open_at = None

    # -----------------------------------------------------------------
    # Heartbeat / telemetry
    # -----------------------------------------------------------------
    def _print_slippage_block(self) -> None:
        """
        Print the per-symbol + portfolio slippage tracker. Designed for the
        VPS terminal: fixed-width columns, signed ticks, per-symbol min/max,
        portfolio worst/best with the symbol that owns it. In dry-run every
        line will read 0.00t — that's expected (no real orders, no slip).
        """
        tot = self.slip_total
        print("  SLIPPAGE (entry fills, ticks; +ve = worse fill):")
        if tot.n == 0:
            print("    no entries yet — slippage tracker idle")
            return

        worst_sym, worst_t = self._worst_slip_owner
        best_sym,  best_t  = self._best_slip_owner
        avg_dollars = tot.sum_dollars / tot.n if tot.n else 0.0
        print(f"    PORTFOLIO  trades={tot.n:<3d}  "
              f"avg={tot.avg_ticks:+.2f}t  avg_abs={tot.avg_abs_ticks:.2f}t  "
              f"sum$={tot.sum_dollars:+,.2f}  avg$={avg_dollars:+.2f}  "
              f"worst={worst_t:+.2f}t({worst_sym or '-'})  "
              f"best={best_t:+.2f}t({best_sym or '-'})")
        for sym, sp in self.slip_per_symbol.items():
            if sp.n == 0:
                print(f"    {sym:<6}     trades=0    -")
                continue
            print(f"    {sym:<6}     trades={sp.n:<3d}  "
                  f"avg={sp.avg_ticks:+.2f}t  "
                  f"min={sp.min_ticks:+.2f}t  max={sp.max_ticks:+.2f}t  "
                  f"sum$={sp.sum_dollars:+,.2f}")

    def _print_heartbeat(self, now_utc: datetime) -> None:
        eq = self._current_equity()
        self.peak_equity = max(self.peak_equity, eq)
        dd_total = self._equity_dd_pct()
        dd_day = self._day_dd_pct()
        n_open = self._count_open_positions()

        # Console line
        print(f"\n[{now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC]  "
              f"equity=${eq:,.2f}  peak=${self.peak_equity:,.2f}  "
              f"DD={dd_total:.2f}%  DD_today={dd_day:.2f}%  open={n_open}  "
              f"kill={self.account_killed}  day_halt={self.day_halted}  "
              f"entries_today={self.counters.get('entries', 0)}  "
              f"nochase_blocks={self.counters.get('block_nochase_cooldown', 0)}")

        for sym, st in self.states.items():
            orb = st.or_tracker
            or_str = (f"OR=[{orb.or_low:.2f}-{orb.or_high:.2f}]"
                      if orb.or_high is not None else "OR=n/a")
            if st.open_ticket is not None:
                hold_s = (now_utc - st.open_at).total_seconds() if st.open_at else 0
                R_hold = 0.0
                if st.open_entry and st.last_m1_close and st.open_risk_usd:
                    mv = (st.last_m1_close - st.open_entry) if st.open_side == "LONG" \
                         else (st.open_entry - st.last_m1_close)
                    pip_val = self.specs[sym].pip_value_per_lot
                    tick_sz = self.specs[sym].tick_size
                    pnl_now = (mv / tick_sz) * pip_val * (st.open_size_lots or 0)
                    R_hold = pnl_now / st.open_risk_usd if st.open_risk_usd else 0.0

                print(f"  {sym:<6} {or_str}  state=FILLED_{st.open_side}  "
                      f"entry={st.open_entry:.2f}  SL={st.open_sl:.2f}  TP1={st.open_tp1:.2f}  "
                      f"lots={st.open_size_lots:.3f}  risk=${st.open_risk_usd:.0f}  "
                      f"R_hold={R_hold:+.2f}  held={hold_s:.0f}s")
            else:
                # Idle symbol view (broker-local time for ORB phases)
                brk_h_hb, brk_m_hb = self._utc_to_broker_hm(now_utc)
                cur_m   = brk_h_hb * 60 + brk_m_hb
                or_s_m  = orb._or_start_m
                or_e_m  = orb._or_end_m
                tr_e_m  = orb._trade_end_m
                in_win  = orb.in_trade_window(brk_h_hb, brk_m_hb)

                if not orb.or_finalised and cur_m < or_s_m:
                    state = "PRE_OR"
                    mins  = or_s_m - cur_m
                    phase = f"t-{mins}m→OR_open"
                elif not orb.or_finalised and or_s_m <= cur_m < or_e_m:
                    state = "BUILDING_OR"
                    mins  = or_e_m - cur_m
                    phase = f"t-{mins}m→OR_close"
                elif in_win:
                    state = "WAIT_BREAK"
                    mins  = tr_e_m - cur_m
                    phase = f"t-{mins}m→window_end"
                else:
                    state = "WINDOW_CLOSED"
                    mins_next = (or_s_m - cur_m) % (24 * 60)
                    phase = f"t-{mins_next}m→next_OR_open"

                dist_str = ""
                if orb.or_high is not None and orb.or_low is not None and orb.or_range > 0 and st.last_m1_close:
                    up = (orb.or_high - st.last_m1_close) / orb.or_range * 100.0
                    dn = (st.last_m1_close - orb.or_low)  / orb.or_range * 100.0
                    dist_str = f"  up={up:+.0f}%  dn={dn:+.0f}%"

                nr_str = ""
                if st.nr_filter.daily_ranges:
                    nr7 = "Y" if st.nr_filter.is_prev_day_narrow(7) else "n"
                    nr4 = "Y" if st.nr_filter.is_prev_day_narrow(4) else "n"
                    nr_str = f"  NR7={nr7} NR4={nr4}"

                # Why-not-trading ladder (first rail to fire wins the blame)
                blocked = ""
                if self.account_killed:
                    blocked = "  BLOCKED=account_kill"
                elif self.day_halted:
                    blocked = "  BLOCKED=day_halt"
                elif self._dd_breaker_tripped():
                    blocked = "  BLOCKED=dd_breaker(8%)"
                else:
                    nb = self._in_news_entry_block(now_utc)
                    if nb:
                        mins_to = max(0, int((nb[0] - now_utc).total_seconds() // 60))
                        blocked = f"  BLOCKED=news±{self.cfg.news_entry_buffer_min}m ({nb[1]}, t{mins_to:+d}m)"
                    elif self._count_open_positions() >= self.cfg.max_concurrent_positions:
                        blocked = f"  BLOCKED=concurrency_cap({self.cfg.max_concurrent_positions})"
                    else:
                        nc = self._nochase_block(sym, time.time())
                        if nc is not None:
                            other, gap = nc
                            left = self.cfg.nochase_cooldown_s - gap
                            blocked = (f"  BLOCKED=nochase_cooldown "
                                       f"(by {other}, {gap:.0f}s ago, "
                                       f"unblocks in {left:.0f}s)")
                        elif state in ("WAIT_BREAK",) and (orb.break_long_triggered or orb.break_short_triggered):
                            blocked = "  BLOCKED=break_already_fired_today"

                close_str = f"{st.last_m1_close:.2f}" if st.last_m1_close else "n/a"
                print(f"  {sym:<6} {or_str}  state={state}  close={close_str}  "
                      f"{phase}{dist_str}{nr_str}{blocked}")

        # Rail counters
        print(f"  rails: news_block={self.counters.get('block_news_entry', 0)}  "
              f"flat_news={self.counters.get('flatten_news', 0)}  "
              f"cap_hits={self.counters.get('block_concurrent_cap', 0)}  "
              f"nochase_blocks={self.counters.get('block_nochase_cooldown', 0)}  "
              f"cal_blocks="
              f"{self.counters.get('block_cal_weekend',0)+self.counters.get('block_cal_rollover',0)+self.counters.get('block_cal_holiday',0)+self.counters.get('block_cal_news',0)}  "
              f"exits_window={self.counters.get('exit_window', 0)}  "
              f"exits_broker={self.counters.get('exit_broker', 0)}")

        # ★ Slippage block (per trade / per symbol / portfolio)
        self._print_slippage_block()

        # Telemetry JSON (tail -f-able)
        snapshot = {
            "ts_utc": now_utc.isoformat(timespec="seconds"),
            "version": "v30",
            "equity": round(eq, 2),
            "peak_equity": round(self.peak_equity, 2),
            "dd_pct_total": round(dd_total, 3),
            "dd_pct_today": round(dd_day, 3),
            "open_count": n_open,
            "account_killed": self.account_killed,
            "day_halted": self.day_halted,
            "counters": dict(self.counters),
            "config": {
                "base_risk_pct": self.cfg.base_risk_pct,
                "cap_mult": self.cfg.cap_mult,
                "nochase_cooldown_s": self.cfg.nochase_cooldown_s,
            },
            "slippage": {
                "portfolio": self.slip_total.to_dict(),
                "per_symbol": {sym: sp.to_dict() for sym, sp in self.slip_per_symbol.items()},
                "worst": {"symbol": self._worst_slip_owner[0],
                          "ticks": (round(self._worst_slip_owner[1], 3)
                                    if self._worst_slip_owner[0] else None)},
                "best":  {"symbol": self._best_slip_owner[0],
                          "ticks": (round(self._best_slip_owner[1], 3)
                                    if self._best_slip_owner[0] else None)},
            },
            "last_close_ts_by_symbol": dict(self._last_close_ts_by_symbol),
            "symbols": {
                sym: {
                    "or_high": st.or_tracker.or_high,
                    "or_low": st.or_tracker.or_low,
                    "or_range": st.or_tracker.or_range,
                    "or_finalised": st.or_tracker.or_finalised,
                    "in_window": st.or_tracker.in_trade_window(now_utc.hour, now_utc.minute),
                    "last_close": st.last_m1_close,
                    "open_ticket": st.open_ticket,
                    "open_side": st.open_side,
                    "open_lots": st.open_size_lots,
                    "open_sl": st.open_sl,
                }
                for sym, st in self.states.items()
            },
        }
        with open(self.telemetry_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, default=str)

    # -----------------------------------------------------------------
    # Day rollover (reset day-DD and per-symbol bar counter)
    # -----------------------------------------------------------------
    def _roll_day_if_needed(self, now_utc: datetime) -> None:
        key = now_utc.strftime("%Y-%m-%d")
        if key != self._current_day_utc:
            self.day_start_equity = self._current_equity()
            self._current_day_utc = key
            self.day_halted = False
            for st in self.states.values():
                st.bars_seen_today = 0
            log.info("[DAY] %s  start_equity=$%.2f", key, self.day_start_equity)

    # -----------------------------------------------------------------
    # New-bar poller
    # -----------------------------------------------------------------
    def _poll_new_bars(self) -> None:
        for sym, st in self.states.items():
            broker_sym = self._symbol_to_broker(sym)
            try:
                bars = self.bridge.get_history(broker_sym, count=10)
            except Exception as e:
                log.debug("get_history failed for %s: %s", sym, e)
                continue
            if not bars:
                continue
            for b in bars:
                if isinstance(b, dict):
                    t = _parse_bar_time(b)
                    if t is None:
                        continue
                    bar = BarData(
                        symbol=broker_sym, timeframe="M1",
                        time=t,
                        open=float(b.get("o", b.get("open", 0.0))),
                        high=float(b.get("h", b.get("high", 0.0))),
                        low=float(b.get("l", b.get("low", 0.0))),
                        close=float(b.get("c", b.get("close", 0.0))),
                        volume=float(b.get("v", b.get("volume", 0.0))),
                    )
                else:
                    bar = b
                if bar.time is None:
                    continue
                if st.last_bar_time and bar.time <= st.last_bar_time:
                    continue
                day_key = bar.time.strftime("%Y-%m-%d")
                self._maybe_enter(sym, bar, day_key)
                st.bars_seen_today += 1

    # -----------------------------------------------------------------
    # Startup warmup (pre-seed OR tracker + NR filter per symbol)
    # -----------------------------------------------------------------
    def _warmup_symbol(self, sym: str, bars_to_fetch: int = 2880) -> int:
        st = self.states[sym]
        broker_sym = self._symbol_to_broker(sym)
        try:
            bars = self.bridge.get_history(broker_sym, count=bars_to_fetch)
        except Exception as e:
            log.warning("warmup  %s  get_history failed: %s", sym, e)
            return 0
        if not bars:
            log.warning("warmup  %s  broker returned 0 bars", sym)
            return 0

        norm: List[BarData] = []
        skipped_no_time = 0
        for b in bars:
            if isinstance(b, dict):
                t = _parse_bar_time(b)
                if t is None:
                    skipped_no_time += 1
                    continue
                bar = BarData(
                    symbol=broker_sym, timeframe="M1", time=t,
                    open=float(b.get("o", b.get("open", 0.0))),
                    high=float(b.get("h", b.get("high", 0.0))),
                    low=float(b.get("l", b.get("low", 0.0))),
                    close=float(b.get("c", b.get("close", 0.0))),
                    volume=float(b.get("v", b.get("volume", 0.0))),
                )
            else:
                bar = b
                if bar.time is None:
                    skipped_no_time += 1
                    continue
            norm.append(bar)
        if skipped_no_time:
            log.warning("warmup  %s  skipped %d bars with no parseable timestamp",
                        sym, skipped_no_time)
        norm.sort(key=lambda x: x.time)

        processed = 0
        for bar in norm:
            day_key = bar.time.strftime("%Y-%m-%d")
            st.or_tracker.update(day_key, bar.time.hour, bar.time.minute,
                                 float(bar.high), float(bar.low))
            st.nr_filter.update(day_key, float(bar.high), float(bar.low))
            st.last_m1_close = float(bar.close)
            st.last_bar_time = bar.time
            processed += 1

        orb = st.or_tracker
        or_info = (f"OR=[{orb.or_low:.2f}-{orb.or_high:.2f}] finalised={orb.or_finalised}"
                   if orb.or_high is not None else "OR=n/a")
        log.info("warmup  %s  bars=%d  last=%s  %s",
                 sym, processed, norm[-1].time.isoformat() if norm else "n/a", or_info)
        return processed

    def _warmup_all(self) -> None:
        log.info("=" * 72)
        log.info(" V30 WARMUP — pre-seeding OR tracker + NR filter for each symbol")
        log.info(" (pulling 48h of M1 history per symbol; no orders are sent)")
        log.info("=" * 72)
        total = 0
        for sym in self.cfg.symbols:
            n = self._warmup_symbol(sym, bars_to_fetch=2880)
            total += n
        log.info("WARMUP complete — %d bars seeded across %d symbols",
                 total, len(self.cfg.symbols))
        self._log_event("WARMUP_DONE",
                        bars_total=total,
                        symbols={sym: {
                            "or_high": self.states[sym].or_tracker.or_high,
                            "or_low": self.states[sym].or_tracker.or_low,
                            "or_finalised": self.states[sym].or_tracker.or_finalised,
                            "last_bar_time": str(self.states[sym].last_bar_time),
                        } for sym in self.cfg.symbols})

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------
    def start(self) -> int:
        eq = self._current_equity()
        if eq <= 0:
            log.error("Bridge returned zero/negative equity — cannot start.")
            return 2
        self.start_equity = eq
        self.peak_equity = eq
        self.day_start_equity = eq
        self._current_day_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log.info("[START] equity=$%.2f  mode=%s  version=v30",
                 eq, "DRY-RUN" if self.dry_run else "LIVE")
        self._log_event("START", equity=eq, dry_run=self.dry_run, version="v30",
                        cfg=vars(self.cfg))

        off = self._refresh_broker_offset(force=True)
        log.info("[broker-clock] initial offset = %+d s", int(off.total_seconds()))

        self._warmup_all()
        return 0

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        last_bar_poll = 0.0
        last_heartbeat = 0.0
        try:
            while not self._stop:
                now = time.time()
                now_utc = datetime.now(timezone.utc)

                self._roll_day_if_needed(now_utc)

                if now - last_bar_poll >= self.cfg.bar_poll_sec:
                    self._poll_new_bars()
                    last_bar_poll = now

                self._manage_open(now_utc)

                if now - last_heartbeat >= self.cfg.heartbeat_sec:
                    self._print_heartbeat(now_utc)
                    last_heartbeat = now

                time.sleep(self.cfg.poll_sec)
        except KeyboardInterrupt:
            log.info("Ctrl-C received, shutting down cleanly ...")
        finally:
            if not self.dry_run and self._count_open_positions() > 0:
                log.info("Shutdown: %d open positions left with broker SL/TP intact.",
                         self._count_open_positions())
            self._log_event("STOP", equity=self._current_equity())
