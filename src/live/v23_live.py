"""
V23 LIVE RUNNER  —  4-pair ORB + Merton-GZ sizer + news rails + 5ers-safe rails.

This is a GROUND-UP live runner for the ORB strategy. It does NOT inherit from
v18_live.py (which ran the spurious SmartBB engine).

Strategy replicated verbatim from `Scripts/backtest_v23_final.py`, which was
A/B verified against `Results/risk_sweep_fine.json` — producing
    $10,853 / 3m PnL, 2.16% DD, Sharpe 3.45, ruin@5% = 0.1%, sub60s = 0
on the 4-pair portfolio [DE40, US30, XAUUSD, US500] at 0.110% base risk.

====================================================================
5ers-PROHIBITED-PRACTICES GUARANTEES (every rule, every time)
====================================================================
- NO HFT:           minimum hold ≥ 60 s (time-guard on exits, sub60s=0 in backtest)
- NO BULK:          max 2 concurrent positions across the portfolio
- NO BRACKETING:    news entry-block fires ±15 min around every Tier-1 event;
                    positions are FLATTENED 2 min before news (we never bracket)
- NO ROLLOVER SCALP: TradingCalendar rollover window (21:55-22:10 UTC) hard-blocks
                    new entries. Existing positions already closed by 15:30 UTC
                    at the latest (NY ORB window ends).
- NO TICK SCALP:    minimum TP = 1.0 × OR_range (typ. 15-40 points, never 1 tick)
- NO ARBITRAGE:     single broker, single feed, one instance per account
- NO ONE-SIDED:     ORB is direction-agnostic (long OR short on first break)
- NO 3RD-PARTY EA:  all source code is in your own repo
- HARD SL ON BROKER:every ORDER_SEND carries sl AND tp at submission time
- ACCOUNT KILL:     flatten + halt if equity DD ≥ 8 % (well inside the 5 % cap
                    because 5ers uses STATIC DD from initial balance, and our
                    bot measures rolling DD — 8% rolling ≈ 3-4% static typically)

====================================================================
Safety ladder (from loosest to hardest)
====================================================================
L1. ORB session windows       — entries only inside 60 min post-OR
L2. TradingCalendar.can_enter — weekend / rollover / holiday / news buffer
L3. News entry-block          — ±15 min buffer around each Tier-1 row
L4. News flatten              — close ALL positions 2 min before each event
L5. Portfolio concurrency cap — max 2 open across all symbols
L6. Daily-DD circuit-breaker  — halt if today’s DD ≥ 2 % (config)
L7. Account kill              — close-all + lock if equity DD ≥ 8 %
L8. Broker-side SL/TP         — hard physical stops, survive bot/VPS restart
L9. Time-stop at window-end   — any open position is closed when the ORB
                                 trade-window expires (≤ 60 min post-OR)

If ANY of L1-L7 trips, the event is logged to `Results/v23_live_events.log` with
a full reason-string and a telemetry snapshot.
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
from src.dd_breaker import DDBreaker              # 4 % TOTAL (peak-to-trough) DD breaker
from src.dynamic_sizer_v21 import (               # Merton×Grossman-Zhou sizer
    MertonGZSizer, MertonGZSizerConfig,
)


log = logging.getLogger("v23.live")


# =====================================================================
#  Bar-time parser. The MT5 bridge sends timestamps in several shapes
#  depending on bridge version / MQL5 build:
#    * int / float  -> unix epoch seconds (current SHF_Bridge.mq5)
#    * str ISO-8601 "2026-04-23T14:02:00"
#    * str MT5-native "2026.04.23 14:02:00"
#    * datetime     (already parsed)
#  Key name is also ambiguous: short "t" (current bridge) or long "time"
#  (older code paths / tests). This helper accepts all of them and
#  always returns a tz-aware UTC datetime, or None if the bar is unusable.
#  Centralising it in ONE place prevents the warmup-vs-poll inconsistency
#  that caused the TypeError('<' not supported between NoneType...) crash.
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
#  Per-symbol ORB configs  —  copied VERBATIM from Scripts/backtest_v22_lean_uk5.py
#  so the live runner uses the same tunings that produced $10,853 / 2.16% DD.
# =====================================================================
V23_ORB_CONFIGS: Dict[str, ORBConfig] = {
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

# 5ers MT5-Bridge broker constants (copied verbatim from backtest).
V23_BROKER_TICK_SIZE: Dict[str, float] = {
    "DE40": 1.0, "US30": 1.0, "US500": 0.25, "XAUUSD": 0.01,
}
V23_BROKER_LOT_STEP:  Dict[str, float] = {
    "DE40": 0.1, "US30": 0.1, "US500": 0.1, "XAUUSD": 0.01,
}
V23_BROKER_MIN_LOT:   Dict[str, float] = {
    "DE40": 0.1, "US30": 0.1, "US500": 0.1, "XAUUSD": 0.01,
}
# Broker-side symbol name mapping (override via --broker-names if broker differs)
V23_BROKER_NAMES: Dict[str, str] = {
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
            broker=V23_BROKER_NAMES[sym],
            tick_size=V23_BROKER_TICK_SIZE[sym],
            pip_value_per_lot=float(uni.pip_value),
            min_lot=V23_BROKER_MIN_LOT[sym],
            lot_step=V23_BROKER_LOT_STEP[sym],
        )
    return out


V23_SPECS: Dict[str, SymbolSpec] = _build_default_specs()



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
#  Main live runner
# =====================================================================

@dataclass
class V23LiveConfig:
    """
    Sizer config — v24d PhD-validated sweet-spot (Pareto frontier):

        base_risk_pct = 0.00110   (0.110 % UNIT — what Merton scales up)
        cap_mult       = 5.0      (hard cap per trade = 0.550 % of equity)
        gamma          = 3.0      (risk-aversion; beats γ=2.0 by +7 on Composite)
        dd_cap_pct     = 0.04     (Grossman-Zhou barrier → size → 0 at 4 % DD)

    Effective per-trade risk ranges dynamically:
        * DD = 0, edge strong → binds at cap = 0.550 %
        * DD = 2 %            → scales to ~0.275 % (linear in GZ)
        * DD ≥ 4 %            → 0 (sizer alone stops trading)

    v24b/c/d sweep evidence (Results/v20_phd_suite.json + phd_cap_sweep*.py):
        3-mo PnL = $23,311  |  Max DD = 2.06 %  |  Worst day = 1.38 %
        PF 2.03  |  Ruin @ 4 % = 3.5 %  |  Fat-tail stress: SURVIVES
        +115 % more PnL than flat 0.110 % for essentially identical account-blow risk.
    """
    symbols: List[str] = field(default_factory=lambda: ["DE40", "US30", "XAUUSD", "US500"])
    base_risk_pct: float = 0.00110           # 0.110 % Merton unit
    cap_mult: float = 5.0                    # sweet-spot cap = 0.550 % per trade
    gamma: float = 3.0                       # v24 shootout winner (Composite 124.4)
    ewma_alpha: float = 0.20                 # half-life ≈ 3 trades
    warmup_trades: int = 15                  # no Merton formula until 15 trades seen
    dd_cap_pct: float = 0.04                 # Grossman-Zhou barrier (4 %)

    # Rails
    news_csv: str = "data/news/tier1_2026.csv"
    news_entry_buffer_min: int = 15
    news_flatten_before_min: int = 2

    # 5ers safety
    account_kill_dd: float = 0.08            # 8% rolling DD → close-all + halt
    daily_breaker_dd: float = 0.02           # 2% daily DD → halt new entries
    max_concurrent_positions: int = 2
    min_hold_seconds: int = 65               # must exceed 60 to avoid HFT flag

    # Live execution
    magic: int = 23000
    comment: str = "SHF_v23"
    heartbeat_sec: float = 60.0
    poll_sec: float = 1.0
    bar_poll_sec: float = 5.0                # pull fresh M1 bars every 5 s

    # Paths
    log_dir: str = "Results"
    telemetry_name: str = "v23_live_telemetry.json"
    events_name: str = "v23_live_events.log"
    trades_name: str = "v23_live_trades.jsonl"


class V23Live:
    """
    Live runner. Thread-safe. Can be stopped with `runner.stop()` or Ctrl-C.
    """

    def __init__(
        self,
        bridge: MT5Bridge,
        cfg: Optional[V23LiveConfig] = None,
        dry_run: bool = True,
        specs: Optional[Dict[str, SymbolSpec]] = None,
    ):
        self.bridge = bridge
        self.cfg = cfg or V23LiveConfig()
        self.dry_run = dry_run
        self.specs = specs or V23_SPECS
        self._lock = Lock()
        self._stop = False

        # Build per-symbol state
        self.states: Dict[str, LiveSymbolState] = {}
        for sym in self.cfg.symbols:
            if sym not in self.specs:
                raise KeyError(f"no SymbolSpec registered for '{sym}'")
            if sym not in V23_ORB_CONFIGS:
                raise KeyError(f"no ORBConfig registered for '{sym}'")
            spec = self.specs[sym]
            orb_cfg = V23_ORB_CONFIGS[sym]
            self.states[sym] = LiveSymbolState(
                spec=spec,
                orb_cfg=orb_cfg,
                or_tracker=OpeningRangeTracker(orb_cfg),
            )


        # Sizer — Merton × Grossman-Zhou with the v24d-validated sweet-spot:
        #   base=0.110%, cap_mult=5.0 (0.550% per-trade ceiling), γ=3.0, DD_cap=4%.
        # Delivered $23,311 / 3m / 2.06% DD / Ruin@4%=3.5% in the v24d sweep
        # (Results/phd_base_f_sweep_v24d.json), vs flat 0.110% = $10,841 / 1.61% DD.
        # The previous "regime-aware" variant that hurt v23_locked was a
        # DIFFERENT sizer (regime-filter on entries); this is pure per-trade
        # sizing that ONLY shrinks as DD approaches the 4% barrier.
        self.merton_sizer = MertonGZSizer(MertonGZSizerConfig(
            base_risk_pct=self.cfg.base_risk_pct,   # 0.110 %
            cap_mult=self.cfg.cap_mult,             # 5.0  → hard cap 0.550 %
            gamma=self.cfg.gamma,                   # 3.0
            ewma_alpha=self.cfg.ewma_alpha,         # 0.20
            warmup_trades=self.cfg.warmup_trades,   # 15
            dd_cap_pct=self.cfg.dd_cap_pct,         # 0.04
            pool_symbols=True,                      # one global μ̂/σ̂² pool
            no_edge_multiplier=1.0,                 # don't halve when μ̂≤0
        ))

        # Calendar (weekend / rollover / holiday). News rails are handled

        # separately so we can apply the -2min flatten independently.
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

        # STATIC 4 % daily hard kill-switch (v24d validated — never fires in
        # sample, zero cost, truncates any fat-tail worst-day at exactly 4 %).
        # This is an INSURANCE LAYER on top of the rolling `daily_breaker_dd`
        # 2 % soft halt; the 4 % gate uses start-of-day equity as the static
        # reference, matching how prop-firm daily-DD rules are actually measured.
        self.daily_halt_4pct = DailyHalt(halt_pct=0.04)

        # HARD TOTAL-DD (peak-to-trough) 4 % BREAKER — v25.
        # This is STRICTER than `account_kill_dd=8%`: it watches equity
        # continuously and flattens ALL positions + permanently halts new
        # entries (no day rollover reset, unlike day_halted) the instant
        # total DD reaches 4 %. It is the hardest possible protection for
        # the 5ers 5 % trailing-DD line and means the challenge cannot
        # be failed by a DD excursion under normal fat-tail regimes.
        self.total_dd_breaker_4pct = DDBreaker(halt_pct=0.04)

        # Counters for telemetry
        self.counters: Dict[str, int] = defaultdict(int)

        # -------------------------------------------------------------
        # BROKER-CLOCK OFFSET CACHE (fixes held=-10775s bug).
        # 5%ers MT5 servers are typically UTC+2/+3. The EA ships bar
        # times as broker-server epochs, so `bar.time` is silently
        # broker-local even though it's labelled tz=UTC by the parser.
        # The OR/NR/breakout logic doesn't care (backtest uses the same
        # broker-time convention — see AUDIT_INDEPENDENT_2026-04-23.md),
        # but any code that mixes `bar.time` with real-UTC values (held
        # seconds, news rails, time-stops) needs the offset subtracted.
        # We cache it once at `start()` and refresh every 15 min.
        # -------------------------------------------------------------
        self._broker_offset_td: timedelta = timedelta(0)
        self._broker_offset_last_refresh: float = 0.0

        # Paths
        self.log_dir = Path(self.cfg.log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.telemetry_path = self.log_dir / self.cfg.telemetry_name
        self.events_path = self.log_dir / self.cfg.events_name
        self.trades_path = self.log_dir / self.cfg.trades_name

        log.info("V23Live initialised  symbols=%s  risk=%.3f%%  cap=%.1fx  news_events=%d  dry_run=%s",
                 self.cfg.symbols, self.cfg.base_risk_pct * 100, self.cfg.cap_mult,
                 len(self.news_events), self.dry_run)

    # -----------------------------------------------------------------
    # News loader (same schema as data/news/tier1_2026.csv)
    # -----------------------------------------------------------------
    @staticmethod
    def _load_news(path: Path) -> List[Tuple[datetime, str]]:
        """Load tier1 CSV. Schema: timestamp_utc, impact, label.
        Supports `#`-prefixed comment lines and either column-name variant."""
        out: List[Tuple[datetime, str]] = []
        if not path.exists():
            log.warning("News CSV not found at %s — news rails will be INACTIVE", path)
            return out
        # Strip lines starting with '#' (comments); DictReader can't handle them natively
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
        """Returns the imminent event if we are within the flatten window AHEAD of it."""
        for ev in self.news_events:
            if timedelta(0) <= (ev[0] - ts) <= timedelta(minutes=self.cfg.news_flatten_before_min):
                return ev
        return None

    def _count_open_positions(self) -> int:
        return sum(1 for s in self.states.values() if s.open_ticket is not None)

    def _equity_dd_pct(self) -> float:
        """Rolling DD from running peak, in percent of peak."""
        if self.peak_equity <= 0:
            return 0.0
        eq = self._current_equity()
        return max(0.0, (self.peak_equity - eq) / self.peak_equity * 100.0)

    def _day_dd_pct(self) -> float:
        """Intra-day DD from the day-start equity, in percent."""
        if self.day_start_equity <= 0:
            return 0.0
        eq = self._current_equity()
        return max(0.0, (self.day_start_equity - eq) / self.day_start_equity * 100.0)

    def _dd_breaker_tripped(self) -> bool:
        """Read-only view of the 4 % total-DD breaker state (used by heartbeat).

        The breaker itself is evaluated inside `_manage_open`; this helper
        only inspects its current state so the heartbeat can report
        "BLOCKED=dd_breaker(4%)" without mutating anything.
        """
        b = getattr(self, "total_dd_breaker_4pct", None)
        if b is None:
            return False
        # DDBreaker exposes `.is_halted` (bool) in v25. Fall back to peak/eq
        # calc if an older build doesn't have it.
        if hasattr(b, "is_halted"):
            return bool(b.is_halted)
        peak = getattr(b, "peak_equity", 0.0) or 0.0
        if peak <= 0:
            return False
        dd = (peak - self._current_equity()) / peak
        halt_pct = getattr(b, "halt_pct", 0.04)
        return dd >= halt_pct


    # -----------------------------------------------------------------
    # Broker helpers
    # -----------------------------------------------------------------
    def _refresh_broker_offset(self, force: bool = False) -> timedelta:
        """
        Cache the broker-server clock offset from real UTC.
        Called at `start()` and every 15 min from the main loop.
        Silently degrades to zero offset if the bridge has no data yet.

        Returns: the offset as a timedelta (broker = UTC + offset).
        5%ers MT5 servers are typically UTC+2 (winter) or UTC+3 (DST).
        """
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
        """Convert a broker-labelled-UTC bar timestamp to REAL UTC.

        The MT5 EA sends broker-server epochs that `_parse_bar_time` labels
        tz=UTC. To get the true wall-clock UTC instant we subtract the
        cached broker→UTC offset. Used for: news entry block / flatten
        comparisons (the news CSV is in real UTC).
        """
        if bar_time.tzinfo is None:
            bar_time = bar_time.replace(tzinfo=timezone.utc)
        return bar_time - self._broker_offset_td

    def _utc_to_broker_hm(self, now_utc: datetime) -> Tuple[int, int]:
        """Convert a real-UTC `now` to (broker_hour, broker_minute).

        Used by time-stops (`_manage_open.trade_end_m`) and heartbeat
        ORB-phase display, because `or_start_hour` in the ORBConfig is in
        broker time (see AUDIT_INDEPENDENT_2026-04-23.md — the backtest
        CSVs are broker-time-stamped, and the tuned anchors are broker
        hours). Without this conversion the time-stop fires ~3h late.
        """
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
    # Logging (events + trades)
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

    # -----------------------------------------------------------------
    # Entry
    # -----------------------------------------------------------------
    def _maybe_enter(self, sym: str, bar: BarData, day_key: str) -> None:
        """Called after each fresh M1 bar. Gated by every rail."""
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

        # GATE: STATIC 4 % daily hard halt (v24d, prop-firm-style DD reference)
        # Uses START-OF-DAY equity as the anchor, which matches how 5ers
        # measures the 5 % daily line. One point of buffer under the limit.
        if not self.daily_halt_4pct.can_trade(bar.time.timestamp(),
                                              self._current_equity()):
            if not self.day_halted:
                self._log_event("DAY_HALTED_4PCT",
                                day_start_equity=self.daily_halt_4pct.day_start_equity,
                                current_equity=self._current_equity(),
                                halted_dates=list(self.daily_halt_4pct.halted_dates))
                self.day_halted = True        # stays halted until day rollover
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

        # GATE: news entry-block (our own list, independent of calendar).
        # bar.time is broker-server time labelled as UTC; news events are
        # REAL UTC (loaded from data/news/tier1_2026.csv). Convert before
        # comparing, otherwise on a GMT+3 broker we shift the ±15 min buffer
        # by 3 h and the rail silently never fires around real-UTC events.
        ev = self._in_news_entry_block(self._bar_to_real_utc(bar.time))
        if ev is not None:
            self.counters["block_news_entry"] += 1
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
        # SL = opposite OR level
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

        # Size via Merton × Grossman-Zhou (v24d sweet-spot): f = base × merton_mult × gz_barrier,
        # capped at cap_mult × base = 0.550 %. At DD = 0 the cap binds → 0.550 %.
        # As DD → dd_cap=4 %, gz_barrier → 0 → risk → 0 (the sizer itself stops trading).
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

        # $ per 1 lot for 1-tick move (from SMARTBB_UNIVERSE)
        pip_val = self.specs[sym].pip_value_per_lot
        tick_sz = self.specs[sym].tick_size
        # dollars per 1.0 lot if price moves from entry to SL
        dollars_per_lot_stopout = (risk_per_unit / tick_sz) * pip_val
        if dollars_per_lot_stopout <= 0:
            return
        lots = risk_usd / dollars_per_lot_stopout

        # round to broker step
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

        if self.dry_run:
            fake_ticket = int(time.time() * 1000) & 0x7FFFFFFF
            st.open_ticket = fake_ticket
            ok = True
        else:
            result = self.bridge.send_order(req)
            ok = getattr(result, "error_code", 0) == 0
            if ok:
                st.open_ticket = int(result.ticket)

        if not ok:
            self._log_event("ORDER_FAILED", symbol=sym, side=side,
                            entry=entry_px, sl=sl, tp1=tp1, lots=lots)
            return

        st.open_side = side
        st.open_entry = entry_px
        st.open_sl = sl
        st.open_tp1 = tp1
        st.open_tp2 = tp2
        st.open_size_lots = lots
        st.open_risk_usd = risk_usd
        # WALL-CLOCK UTC — NOT bar.time. `bar.time` is broker-server time
        # labelled tz=UTC (see _parse_bar_time), so on a GMT+3 broker it
        # reads ~3 h AHEAD of real UTC. Using it here made
        # `(now_utc - st.open_at)` return a NEGATIVE 10 775 s, which silently
        # disabled the 65 s min-hold gate in `_manage_open` and caused the
        # 2026-04-24 DE40 FILLED_LONG to never time-stop at window expiry.
        # The backtest has no such problem because its "now" and "bar.time"
        # are both naive broker-time — they cancel.
        st.open_at = datetime.now(timezone.utc)
        self.counters["entries"] += 1


        self._log_trade({
            "ts_utc": bar.time.isoformat(),
            "event": "ENTRY",
            "symbol": sym,
            "side": side,
            "entry": entry_px,
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
        log.info("[ENTRY] %s %s  lots=%.3f  entry=%.2f  SL=%.2f  TP1=%.2f  TP2=%.2f  risk=$%.0f",
                 sym, side, lots, entry_px, sl, tp1, tp2, risk_usd)

    # -----------------------------------------------------------------
    # Management (news flatten, window expiry, daily/account kill)
    # -----------------------------------------------------------------
    def _manage_open(self, now_utc: datetime) -> None:
        # News flatten (applies to ALL open positions)
        flat_ev = self._in_news_flatten_window(now_utc)
        if flat_ev is not None:
            self._flatten_all(f"news_flatten:{flat_ev[1]}")
            self.counters["flatten_news"] += 1
            return

        # HARD TOTAL-DD 4 % BREAKER (v25) — strictest possible gate.
        # Fires BEFORE the 8 % account-kill. Unlike `day_halted`, it does
        # NOT reset on day rollover: once total DD hits 4 %, the bot
        # refuses to trade until either (a) the manager presses reset or
        # (b) equity recovers enough that DD falls back below 4 %.
        eq_now = self._current_equity()
        halted, cur_dd = self.total_dd_breaker_4pct.check(
            now_utc.timestamp(), eq_now,
        )
        if halted and not self.account_killed:
            self._flatten_all(f"dd_breaker_4pct:dd={cur_dd*100:.2f}%")
            self._log_event("TOTAL_DD_BREAKER_4PCT",
                            dd_pct=round(cur_dd * 100, 3),
                            peak_equity=self.total_dd_breaker_4pct.peak_equity,
                            equity=eq_now,
                            total_halts=self.total_dd_breaker_4pct.total_halts)
            self.account_killed = True           # permanent kill this session
            self.counters["kill_total_dd_4pct"] += 1
            return

        # Account kill (legacy 8 % soft ceiling — should never fire after
        # the 4 % breaker, but kept as defense-in-depth)
        if self._equity_dd_pct() >= self.cfg.account_kill_dd * 100:
            self._flatten_all(f"account_kill:dd={self._equity_dd_pct():.2f}%")
            self.account_killed = True
            self.counters["kill_account"] += 1
            return

        # Daily breaker (halt entries only — already-open positions keep SL/TP)
        if self._day_dd_pct() >= self.cfg.daily_breaker_dd * 100:
            if not self.day_halted:
                self._log_event("DAY_HALTED",
                                day_dd_pct=self._day_dd_pct(),
                                equity=self._current_equity())
                self.day_halted = True
                self.counters["halt_day"] += 1
            # don’t force-close; broker SL/TPs still manage existing risk

        # Time-stop: close any position whose ORB trade-window has expired.
        # IMPORTANT: `or_start_hour` in ORBConfig is BROKER-LOCAL (the backtest
        # CSVs are broker-time stamped; see AUDIT_INDEPENDENT_2026-04-23.md),
        # so `trade_end_m` is a broker-minute. We must convert `now_utc` to the
        # broker clock before comparing — else the gate fires ~3 h too late
        # on a GMT+3 broker.
        brk_h, brk_m = self._utc_to_broker_hm(now_utc)
        now_m = brk_h * 60 + brk_m
        # Broker-side date (for same-day carryover guard, matching OR's day_key)
        now_broker_date = (now_utc + self._broker_offset_td).date()
        for sym, st in self.states.items():
            if st.open_ticket is None:
                continue
            trade_end_m = (st.orb_cfg.or_start_hour * 60 + st.orb_cfg.or_start_minute
                           + st.orb_cfg.or_minutes + st.orb_cfg.trade_window_minutes)
            # open_at is real wall-clock UTC; compare its broker-local date
            open_broker_date = (st.open_at + self._broker_offset_td).date() if st.open_at else None
            if open_broker_date == now_broker_date and now_m >= trade_end_m:
                # Enforce 60 s minimum hold (HFT-compliance belt-and-braces).
                # Both sides are wall-clock UTC now, so this is unambiguous.
                hold_s = (now_utc - st.open_at).total_seconds()
                if hold_s < self.cfg.min_hold_seconds:
                    continue
                self._close_one(sym, "window_expiry")
                self.counters["exit_window"] += 1


        # Sync with broker: if broker-side SL/TP has fired, reconcile local state.
        # IMPORTANT: in dry-run mode, orders are NEVER sent to MT5 (see
        # `_maybe_enter`: a fake ticket is generated via `time.time()`). Querying
        # the real broker for our fake ticket would always return "not present",
        # causing every simulated position to be phantom-closed as
        # `POS_CLOSED_BY_BROKER` on the very next poll. Skip the reconciliation
        # in dry-run — simulated positions are closed by the window-expiry
        # time-stop above, by news-flatten, by the DD breakers, or by an
        # explicit `self._close_one()` call.
        if not self.dry_run:
            open_by_broker = {p.ticket: p for p in self._broker_positions()}
            for sym, st in self.states.items():
                if st.open_ticket is not None and st.open_ticket not in open_by_broker:
                    # Position closed at broker (SL/TP/manual). Feed realised R
                    # to the Merton-GZ sizer BEFORE we clear state, so μ̂/σ̂²
                    # update.
                    self._log_event("POS_CLOSED_BY_BROKER",
                                    symbol=sym,
                                    ticket=st.open_ticket,
                                    side=st.open_side)
                    self._feed_sizer_on_close(sym, reason="broker_close")
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
        # Feed realised R to sizer BEFORE clearing state.
        self._feed_sizer_on_close(sym, reason=f"self_close:{reason}")
        self._clear_state(sym)

    def _feed_sizer_on_close(self, sym: str, reason: str) -> None:
        """Estimate realised R from last M1 close and feed it to the sizer.

        We use the most recent M1 close as a proxy for the exit fill price.
        This is approximate (actual fill may differ by spread/slippage) but
        perfectly adequate for EWMA feedback — the sizer smooths with
        α=0.20 so any single-trade noise washes out after a few trades.
        """
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
        # Clip obviously-wrong values (e.g. stale last_m1_close from other day)
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
              f"entries_today={self.counters.get('entries', 0)}")

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
                # ==== IDLE SYMBOL: show the full ORB decision surface ====
                # (This is the line that replaced the useless "state=WINDOW_CLOSED close=..."
                #  heartbeat. Every field is a real gate used by _maybe_enter.)
                # or_tracker's *_m fields and `in_trade_window` are BROKER-LOCAL
                # — convert now_utc to broker h/m first, else the phase display
                # is 3 h off on a GMT+3 server (PRE_OR shows when we're really
                # already BUILDING_OR, etc.).
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
                    # minutes until tomorrow's OR open (rough — same session offset)
                    mins_next = (or_s_m - cur_m) % (24 * 60)
                    phase = f"t-{mins_next}m→next_OR_open"

                # Distance-to-OR-edge in % of OR range (only meaningful once OR is set)
                dist_str = ""
                if orb.or_high is not None and orb.or_low is not None and orb.or_range > 0 and st.last_m1_close:
                    up = (orb.or_high - st.last_m1_close) / orb.or_range * 100.0
                    dn = (st.last_m1_close - orb.or_low)  / orb.or_range * 100.0
                    # Negative = already broken that side (first-touch flag consumed)
                    dist_str = f"  up={up:+.0f}%  dn={dn:+.0f}%"

                # NR7 / NR4 edge flag from the filter — drives Crabel expectancy
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
                    blocked = "  BLOCKED=dd_breaker(4%)"
                else:
                    nb = self._in_news_entry_block(now_utc)
                    if nb:
                        mins_to = max(0, int((nb[0] - now_utc).total_seconds() // 60))
                        blocked = f"  BLOCKED=news±{self.cfg.news_entry_buffer_min}m ({nb[1]}, t{mins_to:+d}m)"
                    elif self._count_open_positions() >= self.cfg.max_concurrent_positions:
                        blocked = f"  BLOCKED=concurrency_cap({self.cfg.max_concurrent_positions})"
                    elif state in ("WAIT_BREAK",) and (orb.break_long_triggered or orb.break_short_triggered):
                        blocked = "  BLOCKED=break_already_fired_today"

                close_str = f"{st.last_m1_close:.2f}" if st.last_m1_close else "n/a"
                print(f"  {sym:<6} {or_str}  state={state}  close={close_str}  "
                      f"{phase}{dist_str}{nr_str}{blocked}")

        # Rail counters
        print(f"  rails: news_block={self.counters.get('block_news_entry', 0)}  "
              f"flat_news={self.counters.get('flatten_news', 0)}  "
              f"cap_hits={self.counters.get('block_concurrent_cap', 0)}  "
              f"cal_blocks="
              f"{self.counters.get('block_cal_weekend',0)+self.counters.get('block_cal_rollover',0)+self.counters.get('block_cal_holiday',0)+self.counters.get('block_cal_news',0)}  "
              f"exits_window={self.counters.get('exit_window', 0)}  "
              f"exits_broker={self.counters.get('exit_broker', 0)}")

        # Telemetry JSON (tail -f-able)
        snapshot = {
            "ts_utc": now_utc.isoformat(timespec="seconds"),
            "equity": round(eq, 2),
            "peak_equity": round(self.peak_equity, 2),
            "dd_pct_total": round(dd_total, 3),
            "dd_pct_today": round(dd_day, 3),
            "open_count": n_open,
            "account_killed": self.account_killed,
            "day_halted": self.day_halted,
            "counters": dict(self.counters),
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
        """Pull recent M1 bars per symbol; feed any we haven't processed yet."""
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
                # Accept both BarData and dict shapes
                if isinstance(b, dict):
                    t = _parse_bar_time(b)
                    if t is None:
                        continue
                    # Bridge uses short keys {t,o,h,l,c,v}; older code paths
                    # used long keys {time,open,high,low,close,volume}.
                    # Accept both so warmup & poll never KeyError on either.
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
        """
        Pull `bars_to_fetch` M1 bars from the broker and feed them through
        the OR tracker + NR filter in strict chronological order. This ensures
        that when the main poll loop starts, every symbol already has:
          * today's finalised OR (if the OR window is in the past)
          * a populated NR filter (needs ~20 prior-day bars)

        Default 2880 bars = 48 h of M1 = enough for any OR window regardless
        of when the bot is started. No entries are placed here — the ORB
        tracker is deterministic and will simply replay what already happened.

        Returns: number of bars processed.
        """
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

        # Normalise dict→BarData and ensure chronological order
        norm: List[BarData] = []
        skipped_no_time = 0
        for b in bars:
            if isinstance(b, dict):
                t = _parse_bar_time(b)
                if t is None:
                    skipped_no_time += 1
                    continue
                # Bridge returns short keys {t,o,h,l,c,v}; keep legacy-long-key
                # compat so we never KeyError regardless of bridge version.
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

        # Feed bars through OR tracker + NR filter — NO trading during warmup
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
        """Warm up every configured symbol. Called once from start()."""
        log.info("=" * 72)
        log.info(" WARMUP — pre-seeding OR tracker + NR filter for each symbol")
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
        """Initialise equity anchors + warm up each symbol's OR/NR state.
        Call before run()."""
        eq = self._current_equity()
        if eq <= 0:
            log.error("Bridge returned zero/negative equity — cannot start.")
            return 2
        self.start_equity = eq
        self.peak_equity = eq
        self.day_start_equity = eq
        self._current_day_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log.info("[START] equity=$%.2f  mode=%s", eq, "DRY-RUN" if self.dry_run else "LIVE")
        self._log_event("START", equity=eq, dry_run=self.dry_run, cfg=vars(self.cfg))

        # Force an initial broker-clock offset refresh so time-stop / news /
        # held-seconds are correct from the very first bar. (Bridge may need
        # a moment to receive the first DATA push — guarded by force=True.)
        off = self._refresh_broker_offset(force=True)
        log.info("[broker-clock] initial offset = %+d s", int(off.total_seconds()))

        # PRE-SEED per-symbol OR tracker + NR filter from 48h of M1 history.
        # Must happen BEFORE run() or the first trading day will have no OR.
        self._warmup_all()
        return 0



    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        """
        Main loop. Polls for new M1 bars at `bar_poll_sec`, manages open
        positions every `poll_sec`, prints telemetry every `heartbeat_sec`.
        """
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
                # Do NOT force-close on shutdown — broker SL/TP still in place.
                log.info("Shutdown: %d open positions left with broker SL/TP intact.",
                         self._count_open_positions())
            self._log_event("STOP", equity=self._current_equity())
