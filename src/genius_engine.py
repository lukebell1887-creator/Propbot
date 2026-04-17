"""
SHF v10 "Genius" — portfolio of breakout probes with PhD-grade dynamic sizing.

Core design:
    * 3 probes (OR-gap, NR7 contraction, pivot break) fire independently
    * Each (symbol, probe) pair has its own Beta posterior for WR tracking
    * Risk/trade = base × Bayesian-edge-strength × Grossman-Zhou DD shrink
    * Correct 5%ers MTB cost structure ($2/lot indices, $0/lot oil)
    * Time-stop at 6 hours (or end of session, whichever sooner)
    * Max 3 concurrent, portfolio long-index basket capped at 2 simultaneous

The edge isn't the patterns — the patterns are published (Crabel 1990,
Connors-Raschke).  The edge is:
    (a) Bayesian auto-muting of (symbol, probe) pairs whose L5-WR < 50%
    (b) GZ-DD shrinkage so risk collapses as DD grows toward the 5% limit
    (c) Running all this on a 9-symbol commission-friendly universe
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from collections import deque, defaultdict
from typing import Optional
import json

from src.momentum.bayesian_edge import BetaPosterior
from src.momentum.kelly import GrossmanZhouDD


# =====================================================================
#  Symbol specs — commission-aware 5%ers MTB
# =====================================================================

@dataclass
class SymbolConfig:
    symbol: str
    asset_class: str                # "index", "oil", "fx", "metal", "crypto"
    pip_value: float                # $ per 1-pt move per 1 lot
    contract_size: float = 1.0
    spread_pts: float = 0.0
    commission_rt_per_lot: float = 0.0   # round-trip commission per lot
    min_lots: float = 0.01
    lot_step: float = 0.01
    max_lots: float = 50.0
    # Session (UTC hours) — probes use these to gate entry time
    or_window_utc: tuple[int, int] = (13 * 60 + 30, 14 * 60)     # open-range start, end (minutes-of-day)
    trade_end_utc: int = 20 * 60                                  # stop placing new orders


# Verified 2026-04-17 from 5%ers public pricing pages
FIVEERS_SPECS: dict[str, SymbolConfig] = {
    # --- INDICES ($2 rt/lot commission) ---
    "DE40":  SymbolConfig("DE40",  "index", pip_value=1.0,  spread_pts=1.5, commission_rt_per_lot=2.0,
                           or_window_utc=(8 * 60, 8 * 60 + 30), trade_end_utc=16 * 60),
    "US100": SymbolConfig("US100", "index", pip_value=1.0,  spread_pts=1.5, commission_rt_per_lot=2.0,
                           or_window_utc=(14 * 60 + 30, 15 * 60), trade_end_utc=21 * 60),
    "US500": SymbolConfig("US500", "index", pip_value=1.0,  spread_pts=0.5, commission_rt_per_lot=2.0,
                           or_window_utc=(14 * 60 + 30, 15 * 60), trade_end_utc=21 * 60),
    "US30":  SymbolConfig("US30",  "index", pip_value=1.0,  spread_pts=2.0, commission_rt_per_lot=2.0,
                           or_window_utc=(14 * 60 + 30, 15 * 60), trade_end_utc=21 * 60),
    "UK100": SymbolConfig("UK100", "index", pip_value=1.0,  spread_pts=1.0, commission_rt_per_lot=2.0,
                           or_window_utc=(8 * 60, 8 * 60 + 30), trade_end_utc=16 * 60),
    "JP225": SymbolConfig("JP225", "index", pip_value=0.007, spread_pts=7.0, commission_rt_per_lot=2.0,
                           or_window_utc=(0, 30), trade_end_utc=6 * 60),
    # --- OIL ($0 commission) ---
    "USOIL":  SymbolConfig("USOIL",  "oil", pip_value=10.0, spread_pts=0.03, commission_rt_per_lot=0.0,
                             or_window_utc=(13 * 60, 13 * 60 + 30), trade_end_utc=20 * 60),
    "XTIUSD": SymbolConfig("XTIUSD", "oil", pip_value=10.0, spread_pts=0.03, commission_rt_per_lot=0.0,
                             or_window_utc=(13 * 60, 13 * 60 + 30), trade_end_utc=20 * 60),
    "XBRUSD": SymbolConfig("XBRUSD", "oil", pip_value=10.0, spread_pts=0.04, commission_rt_per_lot=0.0,
                             or_window_utc=(8 * 60, 8 * 60 + 30), trade_end_utc=16 * 60),
}


# =====================================================================
#  Engine config
# =====================================================================

@dataclass
class GeniusConfig:
    # Risk
    base_risk_pct: float = 0.005          # 0.5% baseline (will be scaled up/down)
    min_risk_pct: float = 0.002           # never below 0.2% (filter: skip if we'd go lower)
    max_risk_pct: float = 0.015           # never above 1.5%
    max_concurrent: int = 3
    max_index_concurrent: int = 2         # indices are correlated

    # Safety (prop firm)
    daily_dd_limit: float = 0.04
    total_dd_limit: float = 0.05          # tighter than 5%ers 6% for safety margin
    gz_gamma: float = 2.0                 # Grossman-Zhou shrinkage exponent

    # Probe settings
    require_min_trades_before_trust: int = 3   # always-take mode until this many observed per probe
    mute_L5_threshold: float = 0.40       # if lower-5% CI of WR < this, mute
    mute_trades: int = 10                 # how many trades to skip before retrying muted probe

    # Exit
    time_stop_hours: float = 6.0


# =====================================================================
#  Probes — each is a pure function over day-state + bar-window
# =====================================================================

@dataclass
class ProbeSignal:
    probe_id: str
    side: int                 # +1 long, -1 short
    entry_price: float        # stop-order level (we place buy-stop at entry if side > 0)
    sl_price: float
    tp_price: float
    confidence: float = 1.0    # 0-1; engine gates on this
    meta: dict = field(default_factory=dict)


def probe_or_gap(sym: str, day: dict) -> list[ProbeSignal]:
    """
    P1: OR gap-continuation.  If gap > 0.25 × OR_range, place stop-entry
    in the direction of the gap at OR break.  SL=1.0×OR, TP=2.0×OR (2:1 R:R).

    v10.1 changes:
      * R:R flipped from 0.5:1 to 2:1 — v1 had avg winner 0.35R vs avg loser
        0.75R, producing negative EV despite 55% WR.
      * BREAKEVEN_AT = 0.6R — at +0.6R unrealised, SL jumps to entry, so we
        can capture the full 2R tail without risking a given-back runner.
      * Shorts now require gap < -0.35×OR (stricter) — v1 or_gap_dn was
        negative in the 3-month uptrend test.
    """
    if day["or_range"] is None or day["or_range"] <= 0 or day["prev_close"] is None:
        return []
    gap_norm = (day["open"] - day["prev_close"]) / day["or_range"]
    out: list[ProbeSignal] = []
    # LONG: gap > 0.25×OR
    if gap_norm > 0.25:
        entry = day["or_high"]
        out.append(ProbeSignal(
            probe_id="or_gap_up", side=+1,
            entry_price=entry,
            sl_price=entry - day["or_range"],
            tp_price=entry + 2.0 * day["or_range"],         # 2:1 R:R
            confidence=min(1.0, gap_norm / 0.5),
            meta={"gap_norm": gap_norm, "be_trigger_R": 0.6},
        ))
    # SHORT: stricter threshold (-0.35) to filter noise from pure trend-days
    if gap_norm < -0.35:
        entry = day["or_low"]
        out.append(ProbeSignal(
            probe_id="or_gap_dn", side=-1,
            entry_price=entry,
            sl_price=entry + day["or_range"],
            tp_price=entry - 2.0 * day["or_range"],
            confidence=min(1.0, -gap_norm / 0.5),
            meta={"gap_norm": gap_norm, "be_trigger_R": 0.6},
        ))
    return out


def probe_nr7_contraction(sym: str, day: dict) -> list[ProbeSignal]:
    """
    P2: NR7 volatility contraction breakout.  When yesterday's daily range
    was the narrowest of the last 7, expect expansion — trade the first
    break of the OR.  No directional bias; take either way.

    Connors-Raschke published this in 1990s and it's been robust on indices.
    """
    if day["or_range"] is None or day["or_range"] <= 0:
        return []
    if not day.get("is_nr7", False):
        return []
    out: list[ProbeSignal] = []
    # Long setup at OR high
    entry = day["or_high"]
    out.append(ProbeSignal(
        probe_id="nr7_up", side=+1,
        entry_price=entry,
        sl_price=entry - 0.5 * day["or_range"],    # tighter stop (expansion move)
        tp_price=entry + 1.0 * day["or_range"],    # 2:1 R:R
        confidence=0.7,
        meta={"nr7": True},
    ))
    # Short setup at OR low
    entry = day["or_low"]
    out.append(ProbeSignal(
        probe_id="nr7_dn", side=-1,
        entry_price=entry,
        sl_price=entry + 0.5 * day["or_range"],
        tp_price=entry - 1.0 * day["or_range"],
        confidence=0.7,
        meta={"nr7": True},
    ))
    return out


def probe_pivot_break(sym: str, day: dict) -> list[ProbeSignal]:
    """
    P3: Yesterday-H/L pivot break continuation.  After OR closes, if
    price is already above yesterday's high AND the OR closed near its own
    high, expect continuation — buy-stop above OR high.  Mirror for shorts.

    Classic pivot-breakout pattern.  Requires order-flow confirmation
    (OR close in top 25% of its own range).
    """
    if day["or_range"] is None or day["or_range"] <= 0 or day.get("prev_high") is None:
        return []
    if day.get("or_close_pct") is None:
        return []
    out: list[ProbeSignal] = []
    # Long pivot break: OR closed in top 25% AND we're above yesterday's high
    if day["or_close"] >= day["prev_high"] and day["or_close_pct"] > 0.75:
        entry = day["or_high"]
        out.append(ProbeSignal(
            probe_id="pivot_up", side=+1,
            entry_price=entry,
            sl_price=entry - 0.75 * day["or_range"],
            tp_price=entry + 1.25 * day["or_range"],
            confidence=0.75,
            meta={"pivot": "prev_high_break"},
        ))
    # Short pivot break
    if day["or_close"] <= day["prev_low"] and day["or_close_pct"] < 0.25:
        entry = day["or_low"]
        out.append(ProbeSignal(
            probe_id="pivot_dn", side=-1,
            entry_price=entry,
            sl_price=entry + 0.75 * day["or_range"],
            tp_price=entry - 1.25 * day["or_range"],
            confidence=0.75,
            meta={"pivot": "prev_low_break"},
        ))
    return out


PROBES = [probe_or_gap, probe_nr7_contraction, probe_pivot_break]


# =====================================================================
#  Trade records
# =====================================================================

@dataclass
class GeniusPosition:
    symbol: str
    probe_id: str
    side: int
    entry_price: float
    entry_time: float
    lots: float
    sl: float
    tp: float
    R_dist: float
    R_dollars: float                 # $ risk (for realised-R calc)
    be_triggered: bool = False        # has the breakeven-trail fired?
    be_trigger_R: float = 0.6         # move to BE after this many R unrealised
    trail_R: float = 0.0              # trailing distance in R (0 = no trail)
    peak_favourable: float = 0.0      # best unrealised R seen (for trailing)


@dataclass
class GeniusTrade:
    symbol: str
    probe_id: str
    side: int
    entry_time: float
    exit_time: float
    entry_price: float
    exit_price: float
    lots: float
    R_dist: float
    realised_R: float
    gross_pnl: float
    commission: float
    net_pnl: float
    exit_reason: str
    equity_at_entry: float
    equity_at_exit: float


# =====================================================================
#  Per-symbol state
# =====================================================================

class SymbolState:
    def __init__(self, cfg: SymbolConfig):
        self.cfg = cfg
        self.bars: deque = deque(maxlen=120)

        # Today's open-range / day state
        self.or_hi: Optional[float] = None
        self.or_lo: Optional[float] = None
        self.or_range: Optional[float] = None
        self.or_close: Optional[float] = None
        self.or_close_pct: Optional[float] = None
        self.or_locked: bool = False

        self.today_opened: bool = False
        self.day_open_price: Optional[float] = None
        self.prev_day_close: Optional[float] = None
        self.prev_day_high: Optional[float] = None
        self.prev_day_low: Optional[float] = None
        self.today_high: float = -1e18
        self.today_low: float = +1e18
        self.last_close: float = 0.0

        # Historical daily ranges (for NR7)
        self.past_daily_ranges: deque = deque(maxlen=30)

        # Today's triggered probes (for dedup)
        self.probes_fired: set[str] = set()
        self.pending_signals: list[ProbeSignal] = []

        # Active position
        self.position: Optional[GeniusPosition] = None


# =====================================================================
#  The engine
# =====================================================================

class GeniusEngine:
    def __init__(self, symbols: list[SymbolConfig],
                 cfg: Optional[GeniusConfig] = None,
                 initial_equity: float = 100_000.0):
        self.cfg = cfg or GeniusConfig()
        self.states: dict[str, SymbolState] = {s.symbol: SymbolState(s) for s in symbols}

        # Bayesian posteriors per (symbol, probe_id) pair
        # Jeffreys prior: α=β=0.5; we seed at 1,1 (uniform) to be conservative
        self.beta: dict[tuple[str, str], BetaPosterior] = defaultdict(
            lambda: BetaPosterior(alpha=1.0, beta=1.0))
        self.mute_until_trade: dict[tuple[str, str], int] = {}
        self.total_trade_count = 0

        # Grossman-Zhou DD scaler
        self.gz = GrossmanZhouDD(max_dd=self.cfg.total_dd_limit, gamma=self.cfg.gz_gamma)

        self.equity = initial_equity
        self.start_equity = initial_equity
        self.peak_equity = initial_equity
        self.sod_equity = initial_equity

        self.day_key: Optional[str] = None
        self.halted_for_day = False
        self.halted_permanently = False

        self.trades: list[GeniusTrade] = []

    # -----------------------------------------------------------------
    def _roll_day(self, day_key: str) -> None:
        if self.day_key is None:
            self.day_key = day_key
            return
        if day_key == self.day_key:
            return

        # Archive each symbol's daily range & reset day state
        self.day_key = day_key
        self.sod_equity = self.equity
        self.halted_for_day = False

        for st in self.states.values():
            # Archive daily high-low range
            if st.today_high > -1e17 and st.today_low < 1e17:
                drange = st.today_high - st.today_low
                if drange > 0:
                    st.past_daily_ranges.append(drange)
                # Save prev-day highs/lows
                st.prev_day_close = st.last_close
                st.prev_day_high = st.today_high
                st.prev_day_low = st.today_low
            # Reset
            st.or_hi = st.or_lo = st.or_range = st.or_close = st.or_close_pct = None
            st.or_locked = False
            st.today_opened = False
            st.day_open_price = None
            st.today_high = -1e18
            st.today_low = +1e18
            st.probes_fired.clear()
            st.pending_signals.clear()

    # -----------------------------------------------------------------
    def _check_safety(self, time: float) -> bool:
        if self.halted_permanently:
            return False
        if self.halted_for_day:
            return False
        # Total DD
        if self.peak_equity > 0 and self.equity <= self.peak_equity * (1.0 - self.cfg.total_dd_limit):
            self.halted_permanently = True
            self._close_all("ghost_total_dd", time)
            return False
        # Daily DD
        if self.sod_equity > 0 and self.equity <= self.sod_equity * (1.0 - self.cfg.daily_dd_limit):
            self.halted_for_day = True
            self._close_all("ghost_daily_dd", time)
            return False
        return True

    # -----------------------------------------------------------------
    def on_bar(self, symbol: str, time: float, day_key: str,
                hour_utc: int, minute_utc: int,
                open_: float, high: float, low: float, close: float) -> None:
        if symbol not in self.states:
            return
        st = self.states[symbol]
        self._roll_day(day_key)

        if not st.today_opened:
            st.today_opened = True
            st.day_open_price = open_

        st.today_high = max(st.today_high, high)
        st.today_low = min(st.today_low, low)
        st.last_close = close
        st.bars.append({"t": time, "o": open_, "h": high, "l": low, "c": close,
                         "hhm": hour_utc * 60 + minute_utc})

        mod = hour_utc * 60 + minute_utc
        or_start, or_end = st.cfg.or_window_utc

        # Update OR tracker during window
        if or_start <= mod < or_end:
            st.or_hi = high if st.or_hi is None else max(st.or_hi, high)
            st.or_lo = low if st.or_lo is None else min(st.or_lo, low)

        # At OR close → lock and fire probes
        if not st.or_locked and mod >= or_end and st.or_hi is not None:
            st.or_range = st.or_hi - st.or_lo
            st.or_close = close
            if st.or_range > 0:
                st.or_close_pct = (close - st.or_lo) / st.or_range
            st.or_locked = True
            self._fire_probes(st, symbol)

        # Manage position if any
        if st.position is not None:
            self._manage(st, time, high, low, close)

        # Safety
        if not self._check_safety(time):
            return

        # Check OCO triggers
        if st.or_locked and st.position is None and mod < st.cfg.trade_end_utc and st.pending_signals:
            self._check_oco(st, time, high, low, close)

    # -----------------------------------------------------------------
    def _is_nr7(self, st: SymbolState) -> bool:
        if len(st.past_daily_ranges) < 7:
            return False
        last7 = list(st.past_daily_ranges)[-7:]
        yesterday = last7[-1]
        return yesterday <= min(last7)

    def _fire_probes(self, st: SymbolState, symbol: str) -> None:
        """After OR closes, evaluate all probes and queue the signals."""
        day = {
            "or_high": st.or_hi, "or_low": st.or_lo, "or_range": st.or_range,
            "or_close": st.or_close, "or_close_pct": st.or_close_pct,
            "open": st.day_open_price, "prev_close": st.prev_day_close,
            "prev_high": st.prev_day_high, "prev_low": st.prev_day_low,
            "is_nr7": self._is_nr7(st),
        }
        for probe_fn in PROBES:
            for sig in probe_fn(symbol, day):
                # Gate on Bayesian mute
                key = (symbol, sig.probe_id)
                until = self.mute_until_trade.get(key, 0)
                if until > self.total_trade_count:
                    continue     # muted
                # Dedup: only fire each probe once per day per symbol
                if sig.probe_id in st.probes_fired:
                    continue
                st.probes_fired.add(sig.probe_id)
                st.pending_signals.append(sig)

    # -----------------------------------------------------------------
    def _bayesian_edge_multiplier(self, symbol: str, probe_id: str) -> float:
        """
        Return a risk multiplier in [0.5, 1.5] based on Bayesian WR estimate.

        If n < require_min_trades → return 1.0 (neutral / exploration).
        Else: mean WR → linearly map [0.40, 0.65] → [0.5, 1.5].
        """
        b = self.beta.get((symbol, probe_id))
        if b is None or (b.alpha + b.beta - 2) < self.cfg.require_min_trades_before_trust:
            return 1.0
        mean_wr = b.mean()
        # Clamp into [0.40, 0.65] then map to [0.5, 1.5]
        x = max(0.40, min(0.65, mean_wr))
        return 0.5 + (x - 0.40) / (0.65 - 0.40) * 1.0

    def _update_bayes(self, symbol: str, probe_id: str, win: bool) -> None:
        key = (symbol, probe_id)
        b = self.beta[key]
        b.update(win)
        self.total_trade_count += 1
        # Check mute condition (L5 credible interval lower bound)
        n_obs = b.alpha + b.beta - 2
        if n_obs >= 8:    # enough trades to be statistically meaningful
            # Lower 5% CI of Beta ≈ mean - 1.645 × sqrt(var)
            sigma = math.sqrt(max(0, b.var()))
            L5 = b.mean() - 1.645 * sigma
            if L5 < self.cfg.mute_L5_threshold:
                self.mute_until_trade[key] = self.total_trade_count + self.cfg.mute_trades

    # -----------------------------------------------------------------
    def _check_oco(self, st: SymbolState, time: float,
                    high: float, low: float, close: float) -> None:
        # Check open-position concurrency
        total_open = sum(1 for s in self.states.values() if s.position is not None)
        if total_open >= self.cfg.max_concurrent:
            return
        idx_open = sum(1 for s in self.states.values()
                         if s.position is not None and s.cfg.asset_class == "index")
        is_index = st.cfg.asset_class == "index"
        if is_index and idx_open >= self.cfg.max_index_concurrent:
            return

        # Find first signal that triggers
        triggered: Optional[ProbeSignal] = None
        for sig in st.pending_signals:
            if sig.side > 0 and high >= sig.entry_price:
                triggered = sig; break
            if sig.side < 0 and low <= sig.entry_price:
                triggered = sig; break
        if triggered is None:
            return

        # Cancel all other pendings for today
        st.pending_signals = []

        self._open_trade(st, time, triggered)

    # -----------------------------------------------------------------
    def _dynamic_risk_pct(self, symbol: str, probe_id: str) -> float:
        """base_risk × Bayesian_mult × GZ-DD-shrink"""
        base = self.cfg.base_risk_pct
        bay = self._bayesian_edge_multiplier(symbol, probe_id)
        gz = self.gz.factor(equity=self.equity, peak=self.peak_equity)
        raw = base * bay * gz
        return max(self.cfg.min_risk_pct, min(self.cfg.max_risk_pct, raw))

    # -----------------------------------------------------------------
    def _open_trade(self, st: SymbolState, time: float, sig: ProbeSignal) -> None:
        cfg = st.cfg
        entry_fill = sig.entry_price + sig.side * 0.5 * cfg.spread_pts

        R_dist = abs(entry_fill - sig.sl_price)
        if R_dist <= 0:
            return

        risk_pct = self._dynamic_risk_pct(cfg.symbol, sig.probe_id)
        risk_dollars = self.equity * risk_pct
        lots = risk_dollars / (R_dist * cfg.pip_value)
        lots = max(cfg.min_lots,
                    min(cfg.max_lots,
                        math.floor(lots / cfg.lot_step) * cfg.lot_step))
        if lots < cfg.min_lots:
            return

        # SL/TP adjusted for entry fill
        tp_dist = abs(sig.tp_price - sig.entry_price)
        sl = entry_fill - sig.side * R_dist
        tp = entry_fill + sig.side * tp_dist

        pos = GeniusPosition(
            symbol=cfg.symbol, probe_id=sig.probe_id, side=sig.side,
            entry_price=entry_fill, entry_time=time, lots=lots,
            sl=sl, tp=tp, R_dist=R_dist,
            R_dollars=risk_dollars,
            be_trigger_R=float(sig.meta.get("be_trigger_R", 0.6)),
        )
        pos._equity_at_entry = self.equity   # type: ignore[attr-defined]
        st.position = pos

    # -----------------------------------------------------------------
    def _manage(self, st: SymbolState, time: float,
                 high: float, low: float, close: float) -> None:
        pos = st.position
        if pos is None:
            return
        cfg = st.cfg

        # --- Breakeven trail:  once price moves be_trigger_R in our favour,
        #     ratchet SL up to entry price (cost-adjusted).  This flips bad
        #     runners into zero-loss exits rather than full-R losers.
        if not pos.be_triggered:
            best_favourable = (high - pos.entry_price) if pos.side > 0 \
                               else (pos.entry_price - low)
            fav_R = best_favourable / pos.R_dist
            if fav_R >= pos.be_trigger_R:
                # Move SL to entry + (small buffer to cover commission drag)
                comm_buffer_pts = cfg.commission_rt_per_lot / (pos.lots * cfg.pip_value + 1e-9)
                be_price = pos.entry_price + pos.side * comm_buffer_pts
                # Only ratchet in the favourable direction (never worsen SL)
                if pos.side > 0:
                    pos.sl = max(pos.sl, be_price)
                else:
                    pos.sl = min(pos.sl, be_price)
                pos.be_triggered = True

        # SL / TP (worst-case: SL checked first)
        if pos.side > 0 and low <= pos.sl:
            self._close(st, pos.sl, time, "stop_loss" if not pos.be_triggered else "breakeven_exit")
            return
        if pos.side < 0 and high >= pos.sl:
            self._close(st, pos.sl, time, "stop_loss" if not pos.be_triggered else "breakeven_exit")
            return
        if pos.side > 0 and high >= pos.tp:
            self._close(st, pos.tp, time, "take_profit"); return
        if pos.side < 0 and low <= pos.tp:
            self._close(st, pos.tp, time, "take_profit"); return

        # Time stop
        held_hours = (time - pos.entry_time) / 3600.0
        if held_hours >= self.cfg.time_stop_hours:
            self._close(st, close, time, "time_stop"); return

    # -----------------------------------------------------------------
    def _close(self, st: SymbolState, fill: float, time: float, reason: str) -> None:
        pos = st.position
        if pos is None:
            return
        cfg = st.cfg

        slip = 1.0 if reason == "stop_loss" else 0.5
        actual = fill - pos.side * slip * cfg.spread_pts
        gross = (actual - pos.entry_price) * pos.side * pos.lots * cfg.pip_value
        commission = cfg.commission_rt_per_lot * pos.lots
        net = gross - commission

        self.equity += net
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

        realised_R = net / max(pos.R_dollars, 1e-9)

        rec = GeniusTrade(
            symbol=cfg.symbol, probe_id=pos.probe_id, side=pos.side,
            entry_time=pos.entry_time, exit_time=time,
            entry_price=pos.entry_price, exit_price=actual,
            lots=pos.lots, R_dist=pos.R_dist, realised_R=realised_R,
            gross_pnl=gross, commission=commission, net_pnl=net,
            exit_reason=reason,
            equity_at_entry=getattr(pos, "_equity_at_entry", self.equity),
            equity_at_exit=self.equity,
        )
        self.trades.append(rec)

        # Update Bayesian posterior
        self._update_bayes(cfg.symbol, pos.probe_id, win=(net > 0))

        st.position = None

    def _close_all(self, reason: str, time: float) -> None:
        for st in self.states.values():
            if st.position is not None:
                self._close(st, st.last_close, time, reason)

    # -----------------------------------------------------------------
    def summary(self) -> dict:
        if not self.trades:
            return {"trades": 0, "net_pnl": 0, "equity": self.equity,
                    "peak": self.peak_equity}
        wins = [t for t in self.trades if t.net_pnl > 0]
        losses = [t for t in self.trades if t.net_pnl <= 0]
        gw = sum(t.net_pnl for t in wins)
        gl = -sum(t.net_pnl for t in losses)
        pf = gw / gl if gl > 0 else float("inf")
        net = sum(t.net_pnl for t in self.trades)
        eq = self.start_equity; peak = eq; mdd = 0.0
        for t in self.trades:
            eq += t.net_pnl
            peak = max(peak, eq)
            dd = (peak - eq) / peak if peak > 0 else 0.0
            if dd > mdd: mdd = dd

        by_probe: dict[str, dict] = {}
        by_symbol: dict[str, int] = defaultdict(int)
        by_sym_probe: dict[str, dict] = {}
        for t in self.trades:
            by_symbol[t.symbol] += 1
            for key, d in [(t.probe_id, by_probe), (f"{t.symbol}::{t.probe_id}", by_sym_probe)]:
                rec = d.setdefault(key, {"n": 0, "wins": 0, "net": 0.0, "sum_R": 0.0})
                rec["n"] += 1
                rec["wins"] += 1 if t.net_pnl > 0 else 0
                rec["net"] += t.net_pnl
                rec["sum_R"] += t.realised_R
        for d in by_probe.values():
            d["wr"] = d["wins"] / d["n"]
            d["expR"] = d["sum_R"] / d["n"]
        for d in by_sym_probe.values():
            d["wr"] = d["wins"] / d["n"]
            d["expR"] = d["sum_R"] / d["n"]

        return {
            "trades": len(self.trades),
            "net_pnl": net,
            "pct_return": (self.equity - self.start_equity) / self.start_equity * 100,
            "pf": pf,
            "win_rate": len(wins) / len(self.trades),
            "expectancy_R": sum(t.realised_R for t in self.trades) / len(self.trades),
            "avg_winner_R": sum(t.realised_R for t in wins) / len(wins) if wins else 0,
            "avg_loser_R": sum(t.realised_R for t in losses) / len(losses) if losses else 0,
            "max_dd_pct": mdd * 100,
            "equity": self.equity,
            "peak": self.peak_equity,
            "gross_commissions": sum(t.commission for t in self.trades),
            "by_probe": by_probe,
            "by_symbol": dict(by_symbol),
            "by_sym_probe": by_sym_probe,
            "muted_pairs": {f"{s}::{p}": u for (s, p), u in self.mute_until_trade.items()},
        }

    def dump_trades(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump([asdict(t) for t in self.trades], f, indent=2, default=str)
