"""
SHF v9 "Apex" — OR-breakout with statistically validated filters.

Built from deep_dive_orb_stratified evidence (Results/deep_dive_orb.txt):

    Symbol   Filter                              R:R     WR    n
    XAUUSD   OR-pct>0.75 AND |gap|<0.3*OR        1:1.5   50%   16   → +0.25R
    DE40     OR-pct>0.50 AND OR-pct<0.75         1:1.5   58.8% 17   → +0.38R
    DE40     up_gap > 0.3*OR                     1:1     57.1% 28   → +0.14R
    DE40     dn_gap > 0.3*OR                     1:1     56.2% 16   → +0.12R

Expected blended expectancy: ~+0.20R per trade.
Expected 3-month trade count: ~40-60 resolvable setups.

This is NOT another micro-scalper.  Wide stops (1R = full OR width)
make retail spread a trivial fraction of R, so costs can't kill us.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from collections import deque, defaultdict
from typing import Optional
import json

from src.momentum.kelly import GrossmanZhouDD


# =====================================================================
#  OR windows + per-symbol filter rules (calibrated from stratified study)
# =====================================================================

ORB_WINDOW_UTC = {
    "US100":  (14 * 60 + 30, 15 * 60),          # 14:30-15:00 UTC (30-min OR)
    "DE40":   (8 * 60,  8 * 60 + 30),             # 08:00-08:30 UTC (30-min OR)
    "XAUUSD": (14 * 60 + 30, 15 * 60 + 30),     # 14:30-15:30 UTC (60-min OR)
}

TRADE_END_UTC = {   # stop placing new entries after this minute-of-day
    "US100":  21 * 60,
    "DE40":   16 * 60,
    "XAUUSD": 21 * 60,
}


@dataclass(frozen=True)
class SymbolFilter:
    symbol: str
    or_pct_min: float              # require OR percentile >= this
    or_pct_max: float = 1.01       # (exclusive upper)
    abs_gap_to_or_max: float = 999.0   # require |gap|/OR < this
    required_gap_sign: int = 0      # 0 = any, +1 = up, -1 = down
    required_gap_min_abs: float = 0.0  # require |gap|/OR > this
    tp_mult: float = 1.5
    sl_mult: float = 1.0
    allow_direction: int = 0        # 0 = both ways, +1 = long only, -1 = short only


# Filters KEPT after live-engine calibration (v9.1):
#   only gap-breakout survived; wide-OR and medium-OR lost money live
#   (stratified study over-fit to 6-hour-hold assumption).
SYMBOL_FILTERS: list[SymbolFilter] = [
    # DE40 up-gap — 68.8% WR, +0.33R expectancy (CORE EDGE)
    SymbolFilter(
        symbol="DE40",
        or_pct_min=0.0,
        required_gap_sign=+1,
        required_gap_min_abs=0.25,   # slight relax from 0.30 for more volume
        tp_mult=1.0, sl_mult=1.0,
        allow_direction=+1,
    ),
    # (DE40 down-gap dropped: 31% WR, -$353 net in v9.1 test — loser)

    # DE40 up-gap wider — 50% WR at 1.5R (expansion variant)
    SymbolFilter(
        symbol="DE40",
        or_pct_min=0.0,
        required_gap_sign=+1,
        required_gap_min_abs=0.50,
        tp_mult=1.5, sl_mult=1.0,
        allow_direction=+1,
    ),
    # US100 up-gap — 57% WR at 1R (new from stratified study)
    SymbolFilter(
        symbol="US100",
        or_pct_min=0.0,
        required_gap_sign=+1,
        required_gap_min_abs=0.30,
        tp_mult=1.0, sl_mult=1.0,
        allow_direction=+1,
    ),
]


# =====================================================================
#  Engine config
# =====================================================================

@dataclass
class ApexConfig:
    # Risk
    risk_per_trade: float = 0.010      # 1.0% per trade
    max_concurrent: int = 3
    max_concurrent_per_symbol: int = 1

    # Trade management (disable to match stratified-study results)
    breakeven_at_R: float = 999.0      # disabled by default; 0.8 = classic BE-at-80%
    trail_from_R: float = 999.0        # disabled; 1.2 = trail after 120%
    trail_amount_R: float = 0.5

    # Time-stop (stratified study used 6-hour window)
    time_stop_hours: float = 6.0

    # Prop safety
    daily_dd_limit: float = 0.04
    total_dd_limit: float = 0.05


@dataclass
class SymbolConfig:
    symbol: str
    pip_value: float
    contract_size: float = 1.0
    spread_pts: float = 0.0
    commission_per_lot: float = 0.0
    min_lots: float = 0.01
    lot_step: float = 0.01
    max_lots: float = 50.0


# =====================================================================
#  Position + Trade
# =====================================================================

@dataclass
class ApexPosition:
    symbol: str
    filter_name: str
    side: int
    entry_price: float
    entry_time: float
    lots: float
    sl: float
    tp: float
    R_distance: float              # |entry - original_sl|
    original_sl: float
    be_moved: bool = False
    trail_active: bool = False
    high_water_R: float = 0.0


@dataclass
class ApexTrade:
    symbol: str
    filter_name: str
    side: int
    entry_time: float
    exit_time: float
    entry_price: float
    exit_price: float
    lots: float
    sl_price: float
    tp_price: float
    R_distance: float
    realised_R: float
    gross_pnl: float
    commission: float
    net_pnl: float
    exit_reason: str
    or_pct: float
    gap_to_or: float
    equity_at_entry: float
    equity_at_exit: float


# =====================================================================
#  Per-symbol state
# =====================================================================

class SymbolState:
    def __init__(self, cfg: SymbolConfig):
        self.cfg = cfg
        self.bars: deque = deque(maxlen=60)

        self.or_hi: Optional[float] = None
        self.or_lo: Optional[float] = None
        self.or_range: Optional[float] = None
        self.or_locked: bool = False
        self.today_opened: bool = False
        self.prev_day_close: Optional[float] = None
        self.day_open_price: Optional[float] = None
        self.last_close: float = 0.0

        self.past_or_ranges: deque = deque(maxlen=20)

        # Active position
        self.position: Optional[ApexPosition] = None
        # Track filter_names used today (prevent double-fire)
        self.filters_used_today: set[str] = set()

        # Pending OCO stop-orders for today (set after OR locks + filters pass)
        self.pending_long_entry: Optional[float] = None
        self.pending_short_entry: Optional[float] = None
        self.pending_tp_mult: float = 1.5
        self.pending_sl_mult: float = 1.0
        self.pending_filter_name: str = ""
        self.pending_allow_direction: int = 0

        # Metadata for trade record
        self.pending_or_pct: float = 0.0
        self.pending_gap_to_or: float = 0.0


# =====================================================================
#  Engine
# =====================================================================

class ApexEngine:
    def __init__(self,
                  symbols: list[SymbolConfig],
                  cfg: Optional[ApexConfig] = None,
                  initial_equity: float = 5000.0):
        self.cfg = cfg or ApexConfig()
        self.states: dict[str, SymbolState] = {sc.symbol: SymbolState(sc) for sc in symbols}

        self.equity = initial_equity
        self.start_equity = initial_equity
        self.peak_equity = initial_equity
        self.sod_equity = initial_equity

        self.day_key: Optional[str] = None
        self.halted_for_day = False
        self.halted_permanently = False

        self.trades: list[ApexTrade] = []

    # -----------------------------------------------------------------
    def _roll_day(self, day_key: str) -> None:
        if self.day_key is None:
            self.day_key = day_key
            return
        if day_key != self.day_key:
            self.day_key = day_key
            self.sod_equity = self.equity
            self.halted_for_day = False
            for st in self.states.values():
                # Archive OR range
                if st.or_range is not None:
                    st.past_or_ranges.append(st.or_range)
                # Save previous close
                st.prev_day_close = st.last_close
                # Reset day state
                st.or_hi = st.or_lo = st.or_range = None
                st.or_locked = False
                st.today_opened = False
                st.day_open_price = None
                st.filters_used_today.clear()
                st.pending_long_entry = None
                st.pending_short_entry = None
                st.pending_filter_name = ""

    # -----------------------------------------------------------------
    def _check_ghost_stops(self, time: float) -> bool:
        if self.halted_permanently:
            return False
        if self.halted_for_day:
            return False
        if self.peak_equity > 0 and \
           self.equity <= self.peak_equity * (1.0 - self.cfg.total_dd_limit):
            self.halted_permanently = True
            self._close_all("ghost_total_dd", time)
            return False
        if self.sod_equity > 0 and \
           self.equity <= self.sod_equity * (1.0 - self.cfg.daily_dd_limit):
            self.halted_for_day = True
            self._close_all("ghost_daily_dd", time)
            return False
        return True

    # -----------------------------------------------------------------
    def _percentile_rank(self, value: float, past: list[float]) -> float:
        if not past:
            return 0.5
        below = sum(1 for v in past if v < value)
        return below / len(past)

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

        st.bars.append({"t": time, "o": open_, "h": high, "l": low, "c": close})
        st.last_close = close

        mod = hour_utc * 60 + minute_utc
        or_start, or_end = ORB_WINDOW_UTC.get(symbol, (0, 0))
        trade_end = TRADE_END_UTC.get(symbol, 24 * 60)

        # Update OR tracker
        if or_start <= mod < or_end:
            st.or_hi = high if st.or_hi is None else max(st.or_hi, high)
            st.or_lo = low if st.or_lo is None else min(st.or_lo, low)

        # At OR close, lock + evaluate filters
        if not st.or_locked and mod >= or_end and st.or_hi is not None:
            st.or_range = st.or_hi - st.or_lo
            st.or_locked = True
            self._evaluate_filters(st, symbol)

        # Manage any open position
        if st.position is not None:
            self._manage(st, time, high, low, close)

        # Ghost stops
        if not self._check_ghost_stops(time):
            return

        # Check for pending OCO stop-entry triggers (only in trading window)
        if st.or_locked and st.position is None and mod < trade_end \
                and (st.pending_long_entry is not None or st.pending_short_entry is not None):
            self._check_oco_triggers(st, time, high, low, close)

    # -----------------------------------------------------------------
    def _evaluate_filters(self, st: SymbolState, symbol: str) -> None:
        """After OR locks, check which filters trigger and set pending OCO."""
        if st.or_range is None or st.or_range <= 0:
            return
        or_pct = self._percentile_rank(st.or_range, list(st.past_or_ranges))
        gap = (st.day_open_price - st.prev_day_close) if st.prev_day_close else 0.0
        gap_to_or = gap / st.or_range if st.or_range > 0 else 0.0

        # Walk filters applicable to this symbol, take the FIRST one that matches
        # (order in SYMBOL_FILTERS = priority)
        for f in SYMBOL_FILTERS:
            if f.symbol != symbol:
                continue
            if f.or_pct_min > or_pct:
                continue
            if f.or_pct_max <= or_pct:
                continue
            if abs(gap_to_or) > f.abs_gap_to_or_max:
                continue
            if f.required_gap_sign != 0:
                if f.required_gap_sign > 0 and gap_to_or < f.required_gap_min_abs:
                    continue
                if f.required_gap_sign < 0 and gap_to_or > -f.required_gap_min_abs:
                    continue
            # Match!
            filter_id = f"{symbol}_{f.or_pct_min:.2f}_{f.required_gap_sign}"
            if filter_id in st.filters_used_today:
                continue
            st.filters_used_today.add(filter_id)
            # Set pending OCO stop-orders
            if f.allow_direction >= 0:
                st.pending_long_entry = st.or_hi
            if f.allow_direction <= 0:
                st.pending_short_entry = st.or_lo
            st.pending_tp_mult = f.tp_mult
            st.pending_sl_mult = f.sl_mult
            st.pending_filter_name = filter_id
            st.pending_allow_direction = f.allow_direction
            st.pending_or_pct = or_pct
            st.pending_gap_to_or = gap_to_or
            return   # first match only

    # -----------------------------------------------------------------
    def _check_oco_triggers(self, st: SymbolState, time: float,
                              high: float, low: float, close: float) -> None:
        # Respect global concurrency
        total_open = sum(1 for s in self.states.values() if s.position is not None)
        if total_open >= self.cfg.max_concurrent:
            return

        side = 0
        entry = 0.0
        if st.pending_long_entry is not None and high >= st.pending_long_entry:
            side = +1
            entry = st.pending_long_entry
        elif st.pending_short_entry is not None and low <= st.pending_short_entry:
            side = -1
            entry = st.pending_short_entry

        if side == 0:
            return

        # Both sides disabled after first trigger
        st.pending_long_entry = None
        st.pending_short_entry = None

        # Open trade
        self._open_trade(st, time, side, entry)

    # -----------------------------------------------------------------
    def _open_trade(self, st: SymbolState, time: float, side: int, entry: float) -> None:
        cfg = st.cfg
        if st.or_range is None or st.or_range <= 0:
            return
        sl_dist = st.or_range * st.pending_sl_mult
        tp_dist = st.or_range * st.pending_tp_mult

        # Apply entry slippage (half spread)
        entry_fill = entry + side * 0.5 * cfg.spread_pts

        sl = entry_fill - side * sl_dist
        tp = entry_fill + side * tp_dist

        # Sizing: risk_per_trade% of equity divided by SL distance
        risk_dollars = self.equity * self.cfg.risk_per_trade
        lots = risk_dollars / (sl_dist * cfg.pip_value)
        lots = max(cfg.min_lots,
                   min(cfg.max_lots,
                       math.floor(lots / cfg.lot_step) * cfg.lot_step))
        if lots < cfg.min_lots:
            return

        pos = ApexPosition(
            symbol=cfg.symbol, filter_name=st.pending_filter_name, side=side,
            entry_price=entry_fill, entry_time=time, lots=lots,
            sl=sl, tp=tp, R_distance=sl_dist, original_sl=sl,
        )
        pos._or_pct = st.pending_or_pct              # type: ignore[attr-defined]
        pos._gap_to_or = st.pending_gap_to_or        # type: ignore[attr-defined]
        pos._equity_at_entry = self.equity           # type: ignore[attr-defined]
        st.position = pos

    # -----------------------------------------------------------------
    def _manage(self, st: SymbolState, time: float,
                 high: float, low: float, close: float) -> None:
        pos = st.position
        if pos is None:
            return
        cfg = st.cfg

        # Update high-water R
        if pos.side > 0:
            current_R = (close - pos.entry_price) / pos.R_distance
        else:
            current_R = (pos.entry_price - close) / pos.R_distance
        pos.high_water_R = max(pos.high_water_R, current_R)

        # Breakeven move
        if not pos.be_moved and pos.high_water_R >= self.cfg.breakeven_at_R:
            if pos.side > 0:
                pos.sl = max(pos.sl, pos.entry_price)
            else:
                pos.sl = min(pos.sl, pos.entry_price)
            pos.be_moved = True

        # Trailing
        if pos.high_water_R >= self.cfg.trail_from_R:
            trail_dist = self.cfg.trail_amount_R * pos.R_distance
            if pos.side > 0:
                trail_sl = close - trail_dist
                if trail_sl > pos.sl:
                    pos.sl = trail_sl
            else:
                trail_sl = close + trail_dist
                if trail_sl < pos.sl:
                    pos.sl = trail_sl

        # SL / TP hits (worst-case order: SL first)
        if pos.side > 0 and low <= pos.sl:
            self._close(st, pos.sl, time, "stop_loss" if pos.sl <= pos.original_sl else "trailed_out")
            return
        if pos.side < 0 and high >= pos.sl:
            self._close(st, pos.sl, time, "stop_loss" if pos.sl >= pos.original_sl else "trailed_out")
            return
        if pos.side > 0 and high >= pos.tp:
            self._close(st, pos.tp, time, "take_profit")
            return
        if pos.side < 0 and low <= pos.tp:
            self._close(st, pos.tp, time, "take_profit")
            return

        # Time-stop
        if self.cfg.time_stop_hours > 0:
            held_hours = (time - pos.entry_time) / 3600.0
            if held_hours >= self.cfg.time_stop_hours:
                self._close(st, close, time, "time_stop")
                return

    # -----------------------------------------------------------------
    def _close(self, st: SymbolState, fill: float, time: float, reason: str) -> None:
        pos = st.position
        if pos is None:
            return
        cfg = st.cfg
        # Exit slippage
        slip_mult = 1.0 if reason == "stop_loss" else 0.5
        actual = fill - pos.side * slip_mult * cfg.spread_pts
        gross = (actual - pos.entry_price) * pos.side * pos.lots * cfg.pip_value
        commission = cfg.commission_per_lot * pos.lots
        net = gross - commission
        self.equity += net
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

        risk_dollars = pos.R_distance * pos.lots * cfg.pip_value
        realised_R = net / max(risk_dollars, 1e-9)

        rec = ApexTrade(
            symbol=cfg.symbol, filter_name=pos.filter_name, side=pos.side,
            entry_time=pos.entry_time, exit_time=time,
            entry_price=pos.entry_price, exit_price=actual, lots=pos.lots,
            sl_price=pos.sl, tp_price=pos.tp,
            R_distance=pos.R_distance, realised_R=realised_R,
            gross_pnl=gross, commission=commission, net_pnl=net,
            exit_reason=reason,
            or_pct=getattr(pos, "_or_pct", 0.0),
            gap_to_or=getattr(pos, "_gap_to_or", 0.0),
            equity_at_entry=getattr(pos, "_equity_at_entry", self.equity),
            equity_at_exit=self.equity,
        )
        self.trades.append(rec)
        st.position = None

    def _close_all(self, reason: str, time: float) -> None:
        for st in self.states.values():
            if st.position is not None:
                self._close(st, st.last_close, time, reason)

    # -----------------------------------------------------------------
    def summary(self) -> dict:
        if not self.trades:
            return {"trades": 0, "net_pnl": 0, "equity": self.equity,
                    "peak": self.peak_equity, "by_filter": {}, "by_symbol": {}}
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
        by_filter = {}
        for t in self.trades:
            d = by_filter.setdefault(t.filter_name, {"n": 0, "wins": 0, "net": 0.0, "sum_R": 0.0})
            d["n"] += 1
            d["wins"] += 1 if t.net_pnl > 0 else 0
            d["net"] += t.net_pnl
            d["sum_R"] += t.realised_R
        for d in by_filter.values():
            d["wr"] = d["wins"] / d["n"]
            d["expR"] = d["sum_R"] / d["n"]
        by_symbol = defaultdict(int)
        for t in self.trades:
            by_symbol[t.symbol] += 1
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
            "gross_costs": sum(t.commission for t in self.trades),
            "by_filter": by_filter,
            "by_symbol": dict(by_symbol),
        }

    def dump_trades(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump([asdict(t) for t in self.trades], f, indent=2, default=str)
