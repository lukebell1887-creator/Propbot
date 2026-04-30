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
from src.live.heartbeat import write_heartbeat    # ★ Stage 2 — atomic JSON snapshots
# v30.3 — TP1+TP2+trail in-bar partial-close ladder (parity with backtest)
from src.live.atr_tracker import ATRTracker
from src.live.partial_manager import PartialCloseManager, PartialState
# v31 — Layer 1 slippage defense (envelope tracker + per-symbol price caps).
#   * `emergency_sl_offset_for(sym)` → cap × 1.5 = the price-distance past the
#      original SL at which we set the BROKER-side stop. This is the worst-
#      possible fill if the bot disconnects mid-trade.
#   * `Layer1Tracker.update_and_decide(...)` → returns CLOSE_NOW / WAIT /
#      FALLBACK_CLOSE per the same math the backtest applies.
#   See Docs/V31_DEFENSE_PROOF_RESULTS.md + src/execution/layer1.py.
from src.execution.layer1 import (
    config_summary as layer1_config_summary,
    emergency_sl_offset_for,
)
from src.execution.layer1_tracker import Layer1Tracker


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

# 5ers MT5-Bridge broker constants.
# 2026-04-29 v30.3-hotfix-3: US500 tick_size changed 0.25 → 1.0 to match
# the 5ers / Eightcap broker spec sheet, which lists US500 with the same
# Contract Size (1) as DE40/US30/NAS100 — i.e. the broker treats SP500
# identically to the other indices ($1 / point / lot, no special 0.25
# tick CME-futures convention).  Fills are at 0.05 increments, but tick
# math runs cleanly on 1.0 (any 0.05 multiple is also a 1.0 multiple).
V30_BROKER_TICK_SIZE: Dict[str, float] = {
    "DE40": 1.0, "US30": 1.0, "US500": 1.0, "XAUUSD": 0.01,
}

# 2026-04-30 PARITY FIX (v30.4):
#   5ers spec sheet says ALL FOUR symbols have min_lot=0.01 and step=0.01.
#   Previous values (0.1 for indices) were over-quantising live lots vs the
#   backtest, which uses SymbolSpec defaults (0.01/0.01).  This caused live
#   to either OVER-size (small risk → forced to 0.10 lots) or UNDER-size
#   (rounded down to nearest 0.10).  See Docs/V30_LIVE_BACKTEST_PARITY.md.
#   Verified against the 5ers Trading Conditions page on 2026-04-30:
#       DE40   : Min lot 0.01,  Incremental Step 0.01
#       US30   : Min lot 0.01,  Incremental Step 0.01
#       SP500  : Min lot 0.01,  Incremental Step 0.01
#       Gold   : Min lot 0.01,  Incremental Step 0.01
V30_BROKER_LOT_STEP:  Dict[str, float] = {
    "DE40": 0.01, "US30": 0.01, "US500": 0.01, "XAUUSD": 0.01,
}
V30_BROKER_MIN_LOT:   Dict[str, float] = {
    "DE40": 0.01, "US30": 0.01, "US500": 0.01, "XAUUSD": 0.01,
}
# Broker-side symbol name mapping (override via --broker-names if broker differs)
V30_BROKER_NAMES: Dict[str, str] = {
    "DE40": "DE40.cash", "US30": "US30.cash", "US500": "US500.cash", "XAUUSD": "XAUUSD",
}

# =====================================================================
# 2026-04-28 v30.3-hotfix-2: BROKER CONTRACT SIZES (5ers / Eightcap).
#
# Source of truth: 5ers symbol spec sheet supplied by the trader, 2026-04-28.
#
#   Indices (DE40, US30, US500):
#       Contract Size     = 1     (i.e. 1 lot = 1 unit of the index)
#       Min lot           = 0.1
#       Lot step          = 0.01 (broker), but we trade in 0.1 steps for safety
#       Margin Rate       = 4
#       => $/(price-unit)/lot = 1.0 dollars per index point per lot
#
#   Gold (XAUUSD):
#       Contract Size     = 100   (1 lot = 100 troy ounces)
#       Min lot           = 0.01  (1 oz minimum)
#       Lot step          = 0.01
#       => $/(price-unit)/lot = 100.0 dollars per $1 of gold price per lot
#
# The downstream sizer/PnL/slippage formulas in this file all use the
# convention:
#       dollars_per_lot_stopout = (distance_in_price / tick_size) * pip_value_per_lot
# i.e. they want pip_value_per_lot in units of "$ per TICK per LOT".
#
# That equals:  contract_size * tick_size  (broker-truth derivation).
# =====================================================================
V30_BROKER_CONTRACT_SIZE: Dict[str, float] = {
    "DE40":   1.0,    # 1 lot = 1 index unit
    "US30":   1.0,    # 1 lot = 1 index unit
    "US500":  1.0,    # 1 lot = 1 index unit
    "XAUUSD": 100.0,  # 1 lot = 100 troy ounces
}

# Derived: $ P/L per 1 tick per 1.0 lot (broker-truth).
# We deliberately do NOT consult SMARTBB_UNIVERSE.pip_value here, because
# that table is internally inconsistent (uses different unit conventions
# for indices vs metals).  See git log for hotfix history.
V30_DOLLARS_PER_TICK_PER_LOT: Dict[str, float] = {
    sym: V30_BROKER_CONTRACT_SIZE[sym] * V30_BROKER_TICK_SIZE[sym]
    for sym in ("DE40", "US30", "US500", "XAUUSD")
}
# Concrete values (verify against broker statement on every restart):
#   DE40    = 1.0 * 1.0   = $1.00 / point / lot
#   US30    = 1.0 * 1.0   = $1.00 / point / lot
#   US500   = 1.0 * 1.0   = $1.00 / point / lot   ★ hotfix-3 (was 0.25)
#   XAUUSD  = 100 * 0.01  = $1.00 / tick  / lot   ($100  / $1 of price)



@dataclass
class SymbolSpec:
    internal: str
    broker: str
    tick_size: float          # 1 tick = this in price units
    pip_value_per_lot: float  # $ P/L per 1 tick per 1.0 lot  (from SMARTBB_UNIVERSE)
    min_lot: float
    lot_step: float


def _build_default_specs() -> Dict[str, SymbolSpec]:
    """
    Build per-symbol sizing specs from BROKER-TRUTH constants.

    History
    -------
    * Pre-2026-04-28: copied SMARTBB_UNIVERSE.pip_value raw → US500 sized
      4× too small (tick=0.25, ignored).  XAUUSD/DE40/US30 happened to be
      correct purely by numerical coincidence.
    * 2026-04-28 v30.3-hotfix-1 (commit daaeade): unconditionally
      multiplied by tick_size.  This fixed US500 but broke XAUUSD
      (`pip_value_per_lot` collapsed from $1 to $0.01, 100× too small).
    * 2026-04-28 v30.3-hotfix-2 (this commit): stop trusting the universe
      entirely, derive `pip_value_per_lot` from `contract_size × tick_size`
      grounded in the 5ers / Eightcap symbol spec sheet.

    Invariant (verified by `Scripts/preflight_v30.py` check #5):
        pip_value_per_lot == V30_DOLLARS_PER_TICK_PER_LOT[sym]
                          == V30_BROKER_CONTRACT_SIZE[sym]
                             * V30_BROKER_TICK_SIZE[sym]
    """
    out: Dict[str, SymbolSpec] = {}
    for sym in ("DE40", "US30", "XAUUSD", "US500"):
        # The universe lookup is kept ONLY as a safety check that the
        # symbol is recognised by the rest of the codebase.  Its
        # pip_value field is intentionally NOT consulted here — see the
        # block comment above V30_DOLLARS_PER_TICK_PER_LOT for the
        # detailed reasoning.
        if sym not in SMARTBB_UNIVERSE:
            raise RuntimeError(
                f"Symbol {sym} not in SMARTBB_UNIVERSE — refusing to size "
                "an unknown symbol.")
        if sym not in V30_DOLLARS_PER_TICK_PER_LOT:
            raise RuntimeError(
                f"Symbol {sym} missing from V30_DOLLARS_PER_TICK_PER_LOT — "
                "add a row to V30_BROKER_CONTRACT_SIZE before live trading.")
        out[sym] = SymbolSpec(
            internal=sym,
            broker=V30_BROKER_NAMES[sym],
            tick_size=V30_BROKER_TICK_SIZE[sym],
            # Broker-truth: $ P/L per 1 tick per 1.0 lot.
            pip_value_per_lot=V30_DOLLARS_PER_TICK_PER_LOT[sym],
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
    # v30.3 — partial-close ladder state (None until first entry; reset on close)
    partial_state: Optional[PartialState] = None
    # v30.3 — Wilder ATR(14) tracker, owned per-symbol
    atr_tracker: ATRTracker = field(default_factory=lambda: ATRTracker(window=14))


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
    base_risk_pct: float = 0.00185           # ★ v31 ship value (was 0.00170, then 0.00110)
    cap_mult: float = 5.0                    # per-trade cap = 0.925 %
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

    # ★ NEW v30.1 — STATE PERSISTENCE
    # Live μ̂/σ̂² (sizer) and DD-breaker peak/halted state are written
    # atomically after every trade close. On startup the bot tries:
    #   (1) load live state    (Results/v30_state/*.json)
    #   (2) on miss → seed sizer from a 3-month backtest trade list
    #   (3) on miss → cold-start (warm-up risk for 15 trades)
    state_dir: str = "Results/v30_state"
    sizer_state_name: str = "sizer_mertongz.json"
    breaker_state_name: str = "dd_breaker.json"
    seed_trades_path: str = "Results/v30_fresh_trades.json"
    # Reject saved state older than 14 days — we'd rather re-seed from a
    # fresh backtest than resume on a stale view of the world.
    state_max_age_days: float = 14.0


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
        # v30.3 — single shared partial-close manager (TP1 50% + TP2 25% + 0.8×ATR trail)
        self.partial_mgr = PartialCloseManager()

        # v31 — Layer 1 envelope tracker. One instance for the whole engine;
        # state is keyed by broker ticket, cleared in `_clear_state`. See
        # `_manage_open` for the per-cycle decide-and-close loop.
        self.layer1 = Layer1Tracker()
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

        # ★ NEW v30.1 — state persistence paths
        self.state_dir = Path(self.cfg.state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # ★ NEW v30.4 — broker pip-value override bookkeeping (set on start()).
        # Pre-fix the engine hardcoded $1/pt for every symbol, which is wrong
        # for EUR-quoted DAX40 on a USD account (true value ≈ $1.166/pt = €1
        # × EURUSD).  start() now queries the running MT5 terminal directly
        # via `src.live.broker_specs.fetch_live_pip_values` and overrides
        # self.specs[sym].pip_value_per_lot before any sizing call.  These
        # two fields let the heartbeat / banner / preflight #14 confirm the
        # override actually came from the broker (not a silent fallback).
        self._pip_value_source: str = "uninitialised"
        self._pip_value_overrides: Dict[str, Tuple[float, float]] = {}
        # {bot_sym: (old_hardcoded, new_broker_truth)}

        self.sizer_state_path = self.state_dir / self.cfg.sizer_state_name
        self.breaker_state_path = self.state_dir / self.cfg.breaker_state_name
        # ★ Stage 2 — heartbeat JSON snapshot path + uptime anchor
        self.heartbeat_path = self.log_dir / "heartbeat_v30.json"
        self._started_at_unix: float = time.time()
        # Init counters used by persistence layer
        self._restore_source: str = "cold_start"
        self._restore_detail: str = ""
        # Try to restore live state (resume after restart) before any
        # ticks are processed. If that fails, fall back to seeding from
        # the most recent backtest trade list. If THAT fails, cold-start.
        self._init_persistence()

        log.info("V30Live initialised  symbols=%s  risk=%.3f%%  cap=%.1fx  "
                 "nochase=%.0fs  news_events=%d  dry_run=%s  restore=%s",
                 self.cfg.symbols, self.cfg.base_risk_pct * 100, self.cfg.cap_mult,
                 self.cfg.nochase_cooldown_s, len(self.news_events), self.dry_run,
                 self._restore_source)

    # -----------------------------------------------------------------
    # ★ NEW v30.1 — STARTUP STATE RESTORE
    # -----------------------------------------------------------------
    #  Priority order (first that succeeds wins; no double-loading):
    #
    #    (1) live state files     (Results/v30_state/*.json)
    #          – produced by save calls below after every closed trade.
    #          – schema-checked, age-checked (≤14 d), config-checked.
    #
    #    (2) seed from backtest   (Results/v30_fresh_trades.json)
    #          – 264 trades from `Scripts/backtest_v30_fresh.py`.
    #          – replays each through on_trade_closed in order, building
    #            up a realistic μ̂/σ̂² before live trading begins.
    #
    #    (3) cold start           (warm-up risk for first 15 trades)
    #          – preserves the legacy v23 behaviour as a safety net.
    #
    #  All three outcomes are logged + announced via WARMUP_RESTORE event,
    #  so post-mortems can see which branch the bot took on every restart.
    # -----------------------------------------------------------------
    def _init_persistence(self) -> None:
        max_age = float(self.cfg.state_max_age_days) * 86400.0

        # ---- (1) live sizer state ----
        ok, reason = self.merton_sizer.load_state(
            self.sizer_state_path, max_age_seconds=max_age,
        )
        if ok:
            self._restore_source = "live_state"
            self._restore_detail = f"sizer: {reason}"
            log.info("[restore] sizer  ✓ %s", reason)
        else:
            log.info("[restore] sizer  ✗ %s — falling back to seed", reason)
            # ---- (2) seed from backtest trade list ----
            seed_path = Path(self.cfg.seed_trades_path)
            if seed_path.exists():
                try:
                    trades = json.loads(seed_path.read_text(encoding="utf-8"))
                    n = self.merton_sizer.seed_from_trades(trades)
                    if n > 0:
                        self._restore_source = "seeded_from_backtest"
                        self._restore_detail = (
                            f"seeded sizer with {n} trades from "
                            f"{seed_path.name}")
                        log.info("[restore] sizer  ✓ seeded with %d trades from %s",
                                 n, seed_path.name)
                    else:
                        log.warning("[restore] sizer  seed file had 0 valid trades")
                        self._restore_source = "cold_start"
                        self._restore_detail = "seed file empty"
                except (OSError, json.JSONDecodeError) as e:
                    log.warning("[restore] sizer  seed load failed: %s", e)
                    self._restore_source = "cold_start"
                    self._restore_detail = f"seed load error: {e}"
            else:
                log.info("[restore] sizer  no seed file at %s — cold start", seed_path)
                self._restore_source = "cold_start"
                self._restore_detail = "no seed file"

        # ---- breaker state (independent — peak_equity is critical) ----
        ok, reason = self.total_dd_breaker_4pct.load_state(self.breaker_state_path)
        if ok:
            log.info("[restore] dd_breaker  ✓ %s", reason)
        else:
            log.info("[restore] dd_breaker  ✗ %s — starting fresh", reason)

        self._log_event("WARMUP_RESTORE",
                        sizer_source=self._restore_source,
                        sizer_detail=self._restore_detail,
                        breaker_loaded=ok,
                        breaker_detail=(reason if ok else None),
                        sizer_n_seen=sum(self.merton_sizer._n_seen.values()),
                        breaker_peak=self.total_dd_breaker_4pct.peak_equity)

    def _save_state_safe(self) -> None:
        """Persist sizer + breaker state. Never raises — IO errors logged but
        cannot crash the live loop."""
        try:
            self.merton_sizer.save_state(self.sizer_state_path)
        except Exception as e:
            log.error("save_state(sizer) failed: %s", e)
        try:
            self.total_dd_breaker_4pct.save_state(self.breaker_state_path)
        except Exception as e:
            log.error("save_state(breaker) failed: %s", e)

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

    # v30.3 — broker lot-step rounding helper (used by PartialCloseManager)
    def _round_lots(self, lots: float) -> float:
        """Round lots down to broker step (default 0.01). Returns 0.0 if too small."""
        rounded = round(float(lots), 2)
        return rounded if rounded >= 0.01 else 0.0

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

        # v30.3 — feed ATR every closed M1 bar (always, regardless of position state)
        st.atr_tracker.update(float(bar.high), float(bar.low), float(bar.close))

        # v30.3 — if a position is open, run the partial-close ladder in-bar
        if st.open_ticket is not None and st.partial_state is not None:
            res = self.partial_mgr.update(
                state=st.partial_state,
                bar_high=float(bar.high),
                bar_low=float(bar.low),
                bar_close=float(bar.close),
                atr_value=st.atr_tracker.value,
                atr_ready=st.atr_tracker.ready,
                bridge=self.bridge,
                round_lots_fn=self._round_lots,
            )
            if res.tp1_fired:
                self._log_event("TP1_PARTIAL", symbol=sym, ticket=st.open_ticket,
                                lots_closed=round(st.partial_state.original_lots * 0.5, 4))
                st.open_size_lots = st.partial_state.open_lots
                st.open_sl = st.partial_state.sl  # SL → BE after TP1
            if res.tp2_fired:
                self._log_event("TP2_PARTIAL", symbol=sym, ticket=st.open_ticket,
                                lots_closed=round(st.partial_state.original_lots * 0.25, 4))
                st.open_size_lots = st.partial_state.open_lots
            if res.sl_moved:
                st.open_sl = st.partial_state.sl
                self._log_event("TRAIL_SL", symbol=sym, ticket=st.open_ticket,
                                new_sl=round(st.partial_state.sl, 5))
            if res.error:
                log.error("[PARTIAL_MGR] %s: %s", sym, res.error)

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
        # 2026-04-30 PARITY FIX (v30.4):
        #   Backtest (orb_engine_v20.py) widens the SL by
        #     sl_buf = sl_buffer_range_mult * or_range
        #   so that random retests of the OR boundary don't stop us out.
        #   Per-symbol values (V30_ORB_CONFIGS):
        #     DE40 = 0.30   US30 = 0.00   XAUUSD = 0.60   US500 = 0.60
        #   Live previously used the raw OR_low / OR_high → SL was tighter
        #   than backtest, more whipsaws, more SL hits, lots inflated.
        #   See Docs/V30_LIVE_BACKTEST_PARITY.md for derivation.
        sl_buf = float(st.orb_cfg.sl_buffer_range_mult) * or_rng
        if side == "LONG":
            sl = float(st.or_tracker.or_low) - sl_buf
            tp1 = entry_px + st.orb_cfg.tp1_range_mult * or_rng
            tp2 = entry_px + st.orb_cfg.tp2_range_mult * or_rng
        else:
            sl = float(st.or_tracker.or_high) + sl_buf
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
        # ------------------------------------------------------------------
        # v31 LAYER 1 — broker-side SL is WIDENED to original ± cap*1.5 so a
        # bot disconnect mid-trade still has a safety net at the worst-case
        # fallback fill. The bot itself watches the price and intercepts at
        # the ORIGINAL `sl` via `Layer1Tracker.update_and_decide()` inside
        # `_manage_open()`. Every other downstream calc (R, partial ladder,
        # trail) keeps using `st.open_sl = sl` (the ORIGINAL), so backtest
        # parity is preserved bar-for-bar.
        # ------------------------------------------------------------------
        emerg_offset = emergency_sl_offset_for(sym)
        broker_sl = (sl - emerg_offset) if side == "LONG" else (sl + emerg_offset)
        req = OrderRequest(
            symbol=self._symbol_to_broker(sym),
            order_type=OrderType.MARKET_BUY if side == "LONG" else OrderType.MARKET_SELL,
            lots=float(round(lots, 4)),
            price=entry_px,
            sl=float(broker_sl),            # v31 — emergency SL = original ± cap*1.5
            tp=0.0,                         # v30.3 — TP managed in-bar by PartialCloseManager (not at broker)
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
        # v30.3 — seed the partial-close ladder for in-bar TP1/TP2/trail management
        _entry_for_ladder = float(st.open_entry) if st.open_entry is not None else float(entry_px)
        st.partial_state = PartialState(
            side=+1 if side == "LONG" else -1,
            entry_price=_entry_for_ladder,
            sl=float(sl),
            tp1=float(tp1),
            tp2=float(tp2),
            original_lots=float(lots),
            open_lots=float(lots),
            ticket=int(st.open_ticket) if st.open_ticket is not None else 0,
            peak_favourable=_entry_for_ladder,
        )
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

        # ------------------------------------------------------------------
        # ★ v31 LAYER 1 — slippage-cap intercept (client-side hybrid)
        # ------------------------------------------------------------------
        # For every open position, get the current adverse-side price and ask
        # the tracker if it has breached the ORIGINAL SL trigger.  Outcomes:
        #   * CLOSE_NOW       → slip is within cap, force market close now
        #   * FALLBACK_CLOSE  → 60 s envelope expired, force market close
        #   * WAIT            → either not breached, or in-envelope, do nothing
        # Both close paths route through `_close_one` which respects dry-run.
        # ------------------------------------------------------------------
        now_ts = now_utc.timestamp()
        for sym, st in self.states.items():
            if (st.open_ticket is None
                    or st.open_sl is None
                    or st.open_side is None):
                continue
            broker_sym = self._symbol_to_broker(sym)
            quote = self.bridge.get_quote(broker_sym)
            if quote is None:
                continue
            # adverse-side price: bid for LONG (price falling), ask for SHORT (price rising)
            cur_px = float(quote.bid if st.open_side == "LONG" else quote.ask)
            side_int = +1 if st.open_side == "LONG" else -1
            decision = self.layer1.update_and_decide(
                ticket=int(st.open_ticket),
                symbol=sym,                  # internal symbol — matches LAYER1_CAPS keys
                side=side_int,
                sl_trigger_px=float(st.open_sl),
                current_px=cur_px,
                now=now_ts,
            )
            if decision.action in ("CLOSE_NOW", "FALLBACK_CLOSE"):
                self._log_event("LAYER1_FIRED", **decision.to_jsonable())
                self._close_one(sym, f"layer1_{decision.action.lower()}")
                key = f"exit_layer1_{decision.action.lower()}"
                self.counters[key] = self.counters.get(key, 0) + 1

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

    def _infer_broker_close_px(self, st) -> Tuple[float, str]:
        """v30.2 — best-effort inference of the broker's actual close fill.

        Background
        ----------
        The MT5 bridge (SHF_Bridge.mq5) reports the **open** fill price but
        not the **close** fill price. When the broker's server-side SL or TP
        fires, Python only sees `POS_CLOSED_BY_BROKER` — the price field is
        gone. Previously we fell back to `last_m1_close`, which under-counts
        both wins (TP fills below the M1 close in our favour) and losses
        (SL fills above the M1 close against us). The bias is one-sided and
        leaks into the Merton EWMA, systematically under-estimating μ̂ and
        therefore under-sizing live trades.

        Inference rule
        --------------
        Because the broker only carries SL + TP1 server-side (TP2 is bot-
        managed and routes through `_close_one`, not the broker-close path),
        a `broker_close` event MUST be either an SL hit or a TP1 hit.
        We classify by the relationship between the most recent M1 close
        and the two levels:

            LONG   close ≥ TP1 → TP1 hit
                   close ≤ SL  → SL hit
                   else        → ambiguous (price retraced inside the range
                                 between fill and bar-close); pick the level
                                 closer to last_m1_close (this is the level
                                 the broker most likely fired against).
            SHORT  symmetric.

        Returns
        -------
        (close_px, source) where source ∈ {"snap_tp1", "snap_sl", "m1_close"}
        for telemetry / auditing.
        """
        if st.open_sl is None or st.open_tp1 is None or st.last_m1_close is None:
            return float(st.last_m1_close or 0.0), "m1_close"
        last = float(st.last_m1_close)
        sl = float(st.open_sl)
        tp1 = float(st.open_tp1)
        if st.open_side == "LONG":
            if last >= tp1:
                return tp1, "snap_tp1"
            if last <= sl:
                return sl, "snap_sl"
        elif st.open_side == "SHORT":
            if last <= tp1:
                return tp1, "snap_tp1"
            if last >= sl:
                return sl, "snap_sl"
        # Ambiguous (intra-bar retrace): snap to the closer of the two.
        d_tp = abs(tp1 - last)
        d_sl = abs(sl - last)
        return (tp1, "snap_tp1") if d_tp < d_sl else (sl, "snap_sl")

    def _feed_sizer_on_close(self, sym: str, reason: str) -> None:
        st = self.states[sym]
        if st.open_entry is None or st.last_m1_close is None or st.open_risk_usd in (None, 0):
            return
        # v30.2 — only the broker-close path needs inference; bot-initiated
        # closes (TP2 manage, time stop, news flatten, daily-halt flatten)
        # close at the prevailing market price, which is well-approximated
        # by the most recent M1 close.
        if reason == "broker_close":
            close_px, close_src = self._infer_broker_close_px(st)
        else:
            close_px, close_src = float(st.last_m1_close), "m1_close"
        mv = (close_px - st.open_entry) if st.open_side == "LONG" \
             else (st.open_entry - close_px)
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
                        pnl_approx=round(pnl_approx, 2),
                        close_px=round(close_px, 5),
                        close_px_src=close_src)
        # ★ v30.1 — flush sizer + breaker state to disk RIGHT NOW so a crash
        # immediately after this trade still preserves the freshly-updated
        # μ̂/σ̂². save_state is atomic (write-tmp + replace), so concurrent
        # readers (e.g. another Python process inspecting state) cannot
        # observe a half-written file.
        self._save_state_safe()

    def _clear_state(self, sym: str) -> None:
        st = self.states[sym]
        # v31 — drop the Layer 1 breach state for this ticket so a future
        # ticket reused by the broker can't inherit a stale 60 s envelope.
        if st.open_ticket is not None:
            try:
                self.layer1.clear(int(st.open_ticket))
            except Exception:
                pass
        st.open_ticket = None
        st.open_side = None
        st.open_entry = None
        st.open_sl = None
        st.open_tp1 = None
        st.open_tp2 = None
        st.open_size_lots = None
        st.open_risk_usd = None
        st.partial_state = None  # v30.3 — clear ladder state on close
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

        # ------------------------------------------------------------------
        # MARKET STATUS banner - honest about weekend/rollover/holiday/news
        # so the per-symbol "t-XXXm->next_OR_open" countdowns aren't read
        # as "the bot will fire at that moment". The calendar gates *every*
        # entry attempt; if we are in any of these windows nothing fires.
        # ------------------------------------------------------------------
        try:
            allowed, reason = self.calendar.can_enter("ANY", now_utc)
        except Exception:
            allowed, reason = True, ""
        if not allowed:
            if reason == "weekend":
                # compute time-until Sunday 22:00 UTC re-open
                week_min = now_utc.weekday() * 1440 + now_utc.hour * 60 + now_utc.minute
                reopen_min = 6 * 1440 + 22 * 60          # Sun 22:00 UTC
                if week_min < reopen_min:
                    delta = reopen_min - week_min
                else:                                    # Fri 21:00 -> next Sun 22:00 wraps
                    delta = (7 * 1440 - week_min) + reopen_min
                hh, mm = divmod(delta, 60)
                banner = (f"  *** MARKET STATUS: WEEKEND CLOSED  "
                          f"-> re-opens Sun 22:00 UTC ({hh}h {mm}m away).  "
                          f"NO TRADES will fire — bot stays warm. ***")
            elif reason == "rollover":
                banner = ("  *** MARKET STATUS: BROKER ROLLOVER  "
                          "(spread blackout) — entries blocked, positions managed. ***")
            elif reason == "holiday":
                banner = "  *** MARKET STATUS: HOLIDAY — entries blocked. ***"
            elif reason == "news":
                banner = "  *** MARKET STATUS: TIER-1 NEWS BUFFER — entries blocked. ***"
            else:
                banner = f"  *** MARKET STATUS: BLOCKED ({reason}) ***"
            print(banner)
        else:
            print("  MARKET STATUS: OPEN — entries allowed by calendar (subject to OR window + rails).")

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

        # ★ Stage 2 — atomic JSON heartbeat snapshot for VPS-side monitors.
        # Wrapped: a heartbeat IO error MUST NOT take down the live loop.
        # `write_heartbeat` is itself defensive (each block in build_v30_snapshot
        # has its own try/except), so by the time we get here a failure is
        # almost always a disk-level problem (full / readonly / removed),
        # which we want to log but otherwise ignore.
        try:
            write_heartbeat(self.heartbeat_path, self,
                            started_at_unix=self._started_at_unix)
        except Exception as e:
            log.warning("heartbeat write failed: %s", e)

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
            # v30.3 — feed ATR tracker during warmup so it's ready at first live bar
            st.atr_tracker.update(float(bar.high), float(bar.low), float(bar.close))
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

        # ----------------------------------------------------------------
        # ★ PROMINENT STARTUP BANNER — answers the "where do I stand?"
        # questions at a glance, every launch:
        #   * Did sizer restore from live state, seed, or cold-start?
        #   * Current μ̂/σ̂² (live edge estimate)
        #   * DD breaker peak — is this a fresh start or a resumed run?
        #   * Heartbeat path — for the VPS-side monitor
        #   * Persistence paths — so you know where state lives
        # ----------------------------------------------------------------
        # Sizer summary — handle both pooled (_GLOBAL_) and per-symbol modes.
        # The sizer stores μ̂/σ̂² in `_mu` / `_var` defaultdicts keyed by
        # `self._key(symbol)` which returns "_GLOBAL_" when cfg.pool_symbols=True.
        n_seen = sum(self.merton_sizer._n_seen.values())
        if self.merton_sizer.cfg.pool_symbols:
            key = "_GLOBAL_"
            if key in self.merton_sizer._mu:
                mu = self.merton_sizer._mu[key]
                var = self.merton_sizer._var[key]
                edge_str = f"μ̂={mu:+.3f}  σ̂²={var:.3f}"
                if n_seen >= self.merton_sizer.cfg.warmup_trades:
                    edge_str += "  (Merton ACTIVE)"
                else:
                    edge_str += f"  (warm-up {n_seen}/{self.merton_sizer.cfg.warmup_trades})"
            else:
                edge_str = f"μ̂=n/a  (cold start, no trades yet)"
        else:
            # Per-symbol mode: show count of symbols with edge data
            n_syms_with_edge = len(self.merton_sizer._mu)
            edge_str = f"per-symbol mode  ({n_syms_with_edge} symbols with edge data)"

        # Mode-specific colouring
        mode_label = "DRY-RUN" if self.dry_run else "LIVE TRADING"

        # Equity guards
        ddb = self.total_dd_breaker_4pct
        breaker_state = (
            "RESUMED" if (ddb.peak_equity and ddb.peak_equity != eq) else "FRESH"
        )

        banner = "\n".join([
            "",
            "=" * 78,
            f"  V30 BOT STARTING — {mode_label}",
            "=" * 78,
            f"  starting_equity     ${eq:>14,.2f}",
            f"  symbols             {', '.join(self.cfg.symbols)}",
            f"  base_risk_pct       {self.cfg.base_risk_pct * 100:.3f}% "
            f"(per-trade cap = {self.cfg.cap_mult:.0f}× = "
            f"{self.cfg.base_risk_pct * self.cfg.cap_mult * 100:.2f}% of equity)",
            f"  daily_kill          {self.daily_halt_4pct.halt_pct * 100:.0f}%   "
            f"(5ers daily limit = 5%)",
            f"  total_kill          {ddb.halt_pct * 100:.0f}%   "
            f"(5ers total limit = 10%)",
            f"  no-chase cooldown   {self.cfg.nochase_cooldown_s:.0f}s   "
            f"(cross-symbol)",
            f"  max_concurrent      {self.cfg.max_concurrent_positions} positions",
            "-" * 78,
            f"  STATE RESTORE       source = {self._restore_source.upper()}",
            f"                      detail = {self._restore_detail or '(none)'}",
            f"  SIZER (Merton-GZ)   trades_seen = {n_seen}   {edge_str}",
            f"  DD BREAKER          peak = ${ddb.peak_equity:>12,.2f}   "
            f"({breaker_state})",
            "-" * 78,
            f"  TELEMETRY (60s)     {self.telemetry_path}",
            f"  HEARTBEAT  (60s)    {self.heartbeat_path}",
            f"  TRADES jsonl        {self.trades_path}",
            f"  EVENTS log          {self.events_path}",
            f"  STATE dir           {self.state_dir} (auto-saved on every close)",
            "=" * 78,
        ])
        # Use both `print` (visible on console) and `log.info` (goes to log file
        # via the StreamHandler in run_v30_live.py).
        print(banner, flush=True)
        for line in banner.splitlines():
            log.info(line)

        off = self._refresh_broker_offset(force=True)
        log.info("[broker-clock] initial offset = %+d s", int(off.total_seconds()))

        # ----------------------------------------------------------------
        # ★ NEW v30.4 — BROKER PIP-VALUE OVERRIDE
        # Replaces hardcoded $1/pt for every symbol with the **broker-truth**
        # tick_value queried live from the running MT5 terminal.  Necessary
        # because EUR-quoted DAX40 on a USD account pays €1/pt × EURUSD ≈
        # $1.166/pt, NOT $1.00/pt.  Without this override the bot under-
        # reports planned risk_$ on DAX40 by ~14-17 % and consequently
        # over-sizes lots, which is exactly what produced the −$1,019 actual
        # loss vs −$853 planned on 2026-04-30.
        # ----------------------------------------------------------------
        self._apply_broker_pip_value_overrides()

        self._warmup_all()
        return 0

    # -----------------------------------------------------------------
    # ★ NEW v30.4 — broker pip-value override (FX-aware sizing).
    # -----------------------------------------------------------------
    def _apply_broker_pip_value_overrides(self) -> None:
        """Query MT5 for live tick_value per symbol and overwrite the
        hardcoded `pip_value_per_lot` in self.specs.

        Failure modes (all non-fatal — bot continues with hardcoded values):
          * `MetaTrader5` python package not installed on the VPS
          * `mt5.initialize()` failure (terminal not running / blocked)
          * a single symbol returning insane / zero tick_value

        The source string is stored in `self._pip_value_source` and surfaced
        through:
          * the loud banner printed below (visible in the PowerShell window)
          * the heartbeat JSON  (heartbeat.py reads `_pip_value_source`)
          * preflight check #14 (asserts source != "fallback_*")
          * the WARMUP_RESTORE event log
        """
        from src.live.broker_specs import (
            fetch_live_pip_values, log_pip_values_banner,
        )

        bot_to_broker  = {sym: self.specs[sym].broker    for sym in self.specs}
        bot_tick_sizes = {sym: self.specs[sym].tick_size for sym in self.specs}
        pip_values, source = fetch_live_pip_values(bot_to_broker, bot_tick_sizes)
        self._pip_value_source = source

        # Print the loud banner FIRST — operator sees broker numbers before
        # any "starting equity" or "warmup" lines scroll past.
        log_pip_values_banner(pip_values, source)

        # Apply overrides + record what changed (for heartbeat / audit log).
        self._pip_value_overrides = {}
        for sym, new_val in pip_values.items():
            spec = self.specs.get(sym)
            if spec is None:
                continue
            old_val = float(spec.pip_value_per_lot)
            # Only register an "override" if the broker-truth value
            # materially differs from the hardcoded fallback.
            if abs(new_val - old_val) > 1e-4:
                self._pip_value_overrides[sym] = (old_val, float(new_val))
                spec.pip_value_per_lot = float(new_val)
                log.info(
                    "[pip-value] %s  $%.4f → $%.4f /pt/lot   "
                    "(broker-truth, %+.2f%%)",
                    sym, old_val, new_val,
                    (new_val / old_val - 1.0) * 100.0 if old_val > 0 else 0.0,
                )
            else:
                log.info(
                    "[pip-value] %s  $%.4f /pt/lot  (unchanged)",
                    sym, old_val,
                )

        self._log_event(
            "PIP_VALUE_OVERRIDE",
            source=source,
            overrides={sym: {"old": old, "new": new}
                       for sym, (old, new) in self._pip_value_overrides.items()},
            current_values={sym: float(self.specs[sym].pip_value_per_lot)
                            for sym in self.specs},
        )


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
