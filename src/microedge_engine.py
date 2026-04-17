"""
SHF v8 Microedge Engine — evidence-based multi-edge scalper.

Plugs the 18 surviving market_dna edges (src/edge_registry.py) into a
clean, simple bar-driven engine.  Reuses BayesianSizer + GZ DD from v7.

Key differences vs v7 momentum_engine.py:
    * No CUSUM trigger (proven negative expectancy).
    * No EVT-GARCH stops — stops are evidence-based MAE-q25 from data.
    * No optimal-stopper — 15-min hard time stop is sufficient.
    * No Kalman-confluence vetos — edges already passed holdout.
    * Triggers are pure data (autocorr lag, 1σ fade) — no smoothing.

Trade frequency target: 10-15 trades/day across 3 symbols.
Per-trade edge: ~0.05R (autocorr 0.06 ≈ 53/47 WR at R:R 1:1).
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, asdict
from collections import deque
from typing import Optional

from src.momentum.sizer import BayesianSizer
from src.momentum.kelly import GrossmanZhouDD
from src.edge_registry import (
    EDGES, EdgeSpec, INSTRUMENT_RR_BPS, ORB_WINDOW_UTC, edges_for_symbol_hour,
)


# ====================================================================
#  Per-symbol config (subset of v7 SymbolConfig — only what we need)
# ====================================================================

@dataclass
class SymbolConfig:
    symbol: str
    pip_value: float
    contract_size: float
    spread_pts: float
    commission_per_lot: float = 0.0
    swap_long_pts_per_day: float = 0.0
    swap_short_pts_per_day: float = 0.0
    min_lots: float = 0.01
    lot_step: float = 0.01
    max_lots: float = 50.0


# ====================================================================
#  Engine config
# ====================================================================

@dataclass
class EngineConfig:
    # Sizing
    base_risk_per_trade: float = 0.005      # 0.5% account at conviction=1.0
    min_lots_floor: float = 0.01

    # Conviction recipe: scale on edge effect-size
    conviction_scale: float = 12.0          # eff=0.05 -> 0.6, eff=0.10 -> 1.2 (clamped)
    conviction_min: float = 0.20
    conviction_max: float = 1.20

    # Concurrency caps
    max_concurrent_total: int = 5
    max_concurrent_per_symbol: int = 2
    max_trades_per_symbol_per_day: int = 30
    max_trades_account_per_day: int = 60

    # Risk caps
    max_total_risk_frac: float = 0.025      # 2.5% account at risk simultaneously

    # Time stop (hard) — most edges are 1-5 min lag; 15-30 min lets play out
    max_hold_minutes: int = 20

    # OR-range volatility filter: today's OR must be >= percentile of last N days
    or_filter_lookback_days: int = 20
    or_filter_min_percentile: float = 0.40   # require above 40th percentile

    # Spread filter: spread must be < this fraction of stop distance
    max_spread_to_stop: float = 0.30

    # Per-edge auto-disable on live underperformance (Bayesian shrinkage)
    edge_min_trades_before_disable: int = 30
    edge_disable_pf_threshold: float = 0.85


# ====================================================================
#  Position
# ====================================================================

@dataclass
class Position:
    symbol: str
    edge_name: str
    side: int                # +1 long, -1 short
    entry_price: float
    entry_time: float
    lots: float
    sl: float
    tp: float
    R_distance: float        # |entry - sl| in price units
    realised_pnl: float = 0.0


@dataclass
class TradeRecord:
    edge_name: str
    symbol: str
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
    spread_cost: float
    net_pnl: float
    exit_reason: str
    conviction: float
    or_factor: float
    equity_at_entry: float
    equity_at_exit: float


# ====================================================================
#  Per-symbol state
# ====================================================================

class SymbolState:
    def __init__(self, cfg: SymbolConfig):
        self.cfg = cfg
        # Rolling bar buffer for lag lookups (we need up to lag=20)
        self.bars: deque = deque(maxlen=120)   # ~2h of M1 bars
        self.last_close: float = 0.0

        # OR tracker (per current day)
        self.or_today_high: Optional[float] = None
        self.or_today_low: Optional[float] = None
        self.or_today_range: Optional[float] = None
        self.or_today_locked: bool = False
        self.current_day_key: Optional[str] = None

        # Rolling history of past OR ranges (for percentile filter)
        self.past_or_ranges: deque = deque(maxlen=20)

        # 5-min rolling for fade1sigma
        self.last5_open: Optional[float] = None
        self.last5_close: Optional[float] = None
        self.last5_start_min: int = -1
        self.recent_5m_returns: deque = deque(maxlen=200)   # for sigma estimate

        # Open positions (can be > 1 if different edges fire)
        self.positions: list[Position] = []

        # Daily counters
        self.trades_today = 0


# ====================================================================
#  Engine
# ====================================================================

class MicroedgeEngine:
    def __init__(self,
                  symbols: list[SymbolConfig],
                  cfg: Optional[EngineConfig] = None,
                  initial_equity: float = 5000.0,
                  daily_dd_limit: float = 0.04,
                  total_dd_limit: float = 0.05):
        self.cfg = cfg or EngineConfig()
        self.states: dict[str, SymbolState] = {sc.symbol: SymbolState(sc) for sc in symbols}

        self.equity = initial_equity
        self.start_equity = initial_equity
        self.peak_equity = initial_equity
        self.sod_equity = initial_equity
        self.daily_dd_limit = daily_dd_limit
        self.total_dd_limit = total_dd_limit

        max_max_lots = max(sc.max_lots for sc in symbols)
        min_lot_step = min(sc.lot_step for sc in symbols)
        min_min_lots = min(sc.min_lots for sc in symbols)
        self.sizer = BayesianSizer(
            max_lots=max_max_lots, lot_step=min_lot_step, min_lots=min_min_lots,
        )
        # Replace GZ DD with the prop-firm total DD
        self.sizer._gz = GrossmanZhouDD(max_dd=total_dd_limit, gamma=2.0)
        self.sizer.mark_equity(initial_equity)

        # Account state
        self.day_key: Optional[str] = None
        self.halted_for_day: bool = False
        self.halted_permanently: bool = False
        self.trades_account_today: int = 0

        # Trade log
        self.trades: list[TradeRecord] = []

        # Per-edge live posterior (Beta) — wins/losses
        self.edge_stats: dict[str, dict] = {
            e.name: {"trades": 0, "wins": 0, "gross_win_R": 0.0,
                     "gross_loss_R": 0.0, "disabled": False}
            for e in EDGES
        }

    # ----- Day roll ----------------------------------------------------
    def _roll_day_if_needed(self, day_key: str) -> None:
        if self.day_key is None:
            self.day_key = day_key
            return
        if day_key != self.day_key:
            self.day_key = day_key
            self.sod_equity = self.equity
            self.halted_for_day = False
            self.trades_account_today = 0
            for st in self.states.values():
                st.trades_today = 0
                # lock yesterday's OR range into history; reset today
                if st.or_today_range is not None:
                    st.past_or_ranges.append(st.or_today_range)
                st.or_today_high = None
                st.or_today_low = None
                st.or_today_range = None
                st.or_today_locked = False
                st.current_day_key = day_key

    # ----- Ghost stops -------------------------------------------------
    def _check_ghost_stops(self, time: float) -> bool:
        if self.halted_permanently:
            return False
        if self.halted_for_day:
            return False
        if self.peak_equity > 0 and \
           self.equity <= self.peak_equity * (1.0 - self.total_dd_limit):
            self.halted_permanently = True
            self._close_all("ghost_total_dd", time)
            return False
        if self.sod_equity > 0 and \
           self.equity <= self.sod_equity * (1.0 - self.daily_dd_limit):
            self.halted_for_day = True
            self._close_all("ghost_daily_dd", time)
            return False
        return True

    # ----- Open-position helpers --------------------------------------
    def _open_count_total(self) -> int:
        return sum(len(s.positions) for s in self.states.values())

    def _open_count_symbol(self, sym: str) -> int:
        return len(self.states[sym].positions)

    def _open_risk_frac(self) -> float:
        if self.equity <= 0:
            return 0.0
        total = 0.0
        for st in self.states.values():
            for pos in st.positions:
                total += pos.lots * pos.R_distance * st.cfg.pip_value
        return total / self.equity

    def _has_open_for_edge(self, edge_name: str) -> bool:
        for st in self.states.values():
            for pos in st.positions:
                if pos.edge_name == edge_name:
                    return True
        return False

    # ==================================================================
    #  on_bar — main entrypoint
    # ==================================================================
    def on_bar(self, symbol: str, time: float, day_key: str,
                hour_utc: int, minute_utc: int,
                open_: float, high: float, low: float, close: float) -> None:
        if symbol not in self.states:
            return
        st = self.states[symbol]
        self._roll_day_if_needed(day_key)
        st.current_day_key = day_key

        # Append bar to rolling buffer
        bar = {"t": time, "o": open_, "h": high, "l": low, "c": close,
                "hour": hour_utc, "minute": minute_utc}
        st.bars.append(bar)
        st.last_close = close

        # ----- update OR tracker -----
        or_start, or_end = ORB_WINDOW_UTC.get(symbol, (0, 0))
        mod = hour_utc * 60 + minute_utc
        if or_start <= mod < or_end:
            st.or_today_high = high if st.or_today_high is None else max(st.or_today_high, high)
            st.or_today_low = low if st.or_today_low is None else min(st.or_today_low, low)
        elif mod >= or_end and not st.or_today_locked and st.or_today_high is not None:
            st.or_today_range = st.or_today_high - st.or_today_low
            st.or_today_locked = True

        # ----- update 5-min rolling for fade1sigma -----
        if minute_utc % 5 == 0:
            # close prior 5-min agg
            if st.last5_open is not None and st.last5_open > 0:
                ret5 = math.log(st.last5_close / st.last5_open) if st.last5_close else 0.0
                st.recent_5m_returns.append(ret5)
            st.last5_open = open_
            st.last5_close = close
            st.last5_start_min = mod
        else:
            if st.last5_close is not None:
                st.last5_close = close

        # ----- Manage open positions on this symbol -----
        for pos in list(st.positions):
            self._manage(st, pos, time, high, low, close)

        # ----- Ghost stops -----
        if not self._check_ghost_stops(time):
            return

        # ----- Try to open new positions for this symbol+hour -----
        candidate_edges = edges_for_symbol_hour(symbol, hour_utc)
        for edge in candidate_edges:
            if self.edge_stats[edge.name]["disabled"]:
                continue
            if self._has_open_for_edge(edge.name):
                continue
            self._try_open(st, edge, time, hour_utc, minute_utc, close)

    # ==================================================================
    def _try_open(self, st: SymbolState, edge: EdgeSpec, time: float,
                   hour_utc: int, minute_utc: int, close: float) -> None:
        cfg = st.cfg
        # Caps
        if st.trades_today >= self.cfg.max_trades_per_symbol_per_day:
            return
        if self.trades_account_today >= self.cfg.max_trades_account_per_day:
            return
        if self._open_count_total() >= self.cfg.max_concurrent_total:
            return
        if self._open_count_symbol(cfg.symbol) >= self.cfg.max_concurrent_per_symbol:
            return
        if self._open_risk_frac() >= self.cfg.max_total_risk_frac:
            return

        # ----- Compute trigger & side -----
        side = self._compute_side(st, edge)
        if side == 0:
            return

        # ----- OR-volatility filter -----
        or_factor = 1.0
        if edge.requires_or_filter:
            if not st.or_today_locked or st.or_today_range is None:
                # OR not yet formed — skip rather than guess
                return
            if len(st.past_or_ranges) < 5:
                or_factor = 1.0   # not enough history yet
            else:
                sorted_past = sorted(st.past_or_ranges)
                k = int(self.cfg.or_filter_min_percentile * len(sorted_past))
                threshold = sorted_past[max(0, k)]
                if st.or_today_range < threshold:
                    return
                # Compute a sizing scaler: where in distribution is today's OR?
                # 50th percentile -> 1.0; 90th -> 1.5; 10th -> 0.5 (clamped)
                rank = sum(1 for r in sorted_past if r <= st.or_today_range) / len(sorted_past)
                or_factor = 0.5 + rank   # in [0.5, 1.5]

        # ----- Stop / TP from instrument R:R table -----
        rr = INSTRUMENT_RR_BPS[cfg.symbol]
        stop_dist = close * rr["stop_bps"] / 1e4
        tp_dist = close * rr["tp_bps"] / 1e4

        # ----- Spread filter -----
        if cfg.spread_pts > self.cfg.max_spread_to_stop * stop_dist:
            return

        # ----- Conviction & sizing -----
        conv = edge.effect_size * self.cfg.conviction_scale
        conv = max(self.cfg.conviction_min, min(self.cfg.conviction_max, conv))
        conv *= or_factor
        conv = min(self.cfg.conviction_max * 1.5, conv)   # absolute cap

        # Bayesian sizer (uses Kelly + GZ DD + CVaR floor)
        dec = self.sizer.decide(
            equity=self.equity,
            conviction=conv,
            stop_distance=stop_dist,
            pip_value=cfg.pip_value,
            avg_win_R=1.0,           # we measured target R:R ≈ 1.0
            avg_loss_R=1.0,
        )

        # Apply per-trade base risk override (sizer may be too aggressive)
        max_risk_dollars = self.equity * self.cfg.base_risk_per_trade * conv
        max_lots_by_risk = max_risk_dollars / (stop_dist * cfg.pip_value)
        lots = min(dec.lots, max_lots_by_risk)
        lots = max(cfg.min_lots,
                   min(cfg.max_lots,
                       math.floor(lots / cfg.lot_step) * cfg.lot_step))
        if lots < cfg.min_lots:
            return

        # Entry slippage: half spread
        slip = 0.5 * cfg.spread_pts
        fill = close + side * slip
        sl = fill - side * stop_dist
        tp = fill + side * tp_dist

        pos = Position(
            symbol=cfg.symbol, edge_name=edge.name, side=side,
            entry_price=fill, entry_time=time, lots=lots,
            sl=sl, tp=tp, R_distance=stop_dist,
        )
        # Stash decision context for trade record
        pos._conviction = conv          # type: ignore[attr-defined]
        pos._or_factor = or_factor      # type: ignore[attr-defined]
        pos._equity_at_entry = self.equity  # type: ignore[attr-defined]

        st.positions.append(pos)
        st.trades_today += 1
        self.trades_account_today += 1

    # ==================================================================
    def _compute_side(self, st: SymbolState, edge: EdgeSpec) -> int:
        if edge.kind == "autocorr":
            # Need at least lag_min+1 bars in buffer
            if len(st.bars) < edge.lag_min + 1:
                return 0
            # Bar at index -1 is current; lag bar is at -1-lag_min
            # But we need the RETURN at that lag bar's CLOSE vs prev close
            lag_idx = -1 - edge.lag_min
            if -lag_idx > len(st.bars):
                return 0
            lag_bar = st.bars[lag_idx]
            prev_bar = st.bars[lag_idx - 1] if -lag_idx + 1 <= len(st.bars) else None
            if prev_bar is None:
                return 0
            if prev_bar["c"] <= 0:
                return 0
            lag_ret = math.log(lag_bar["c"] / prev_bar["c"])
            if abs(lag_ret) < 1e-9:
                return 0
            trigger_dir = 1 if lag_ret > 0 else -1
            # sign: +1 momentum -> trade in trigger direction
            #       -1 reversal  -> trade opposite
            return trigger_dir * edge.sign

        elif edge.kind == "fade1sigma":
            # Need at least 30 5-min returns for sigma
            if len(st.recent_5m_returns) < 30:
                return 0
            # Look at most recent completed 5-min return
            if len(st.recent_5m_returns) == 0:
                return 0
            last_5m = st.recent_5m_returns[-1]
            sigma = (sum(r * r for r in st.recent_5m_returns) / len(st.recent_5m_returns)) ** 0.5
            if abs(last_5m) < 1.0 * sigma:
                return 0
            trigger_dir = 1 if last_5m > 0 else -1
            return -trigger_dir   # always fade
        return 0

    # ==================================================================
    def _manage(self, st: SymbolState, pos: Position, time: float,
                 high: float, low: float, close: float) -> None:
        # Hard SL
        if pos.side > 0 and low <= pos.sl:
            self._close(st, pos, pos.sl, time, "stop_loss")
            return
        if pos.side < 0 and high >= pos.sl:
            self._close(st, pos, pos.sl, time, "stop_loss")
            return
        # Hard TP
        if pos.side > 0 and high >= pos.tp:
            self._close(st, pos, pos.tp, time, "take_profit")
            return
        if pos.side < 0 and low <= pos.tp:
            self._close(st, pos, pos.tp, time, "take_profit")
            return
        # Time stop
        elapsed_min = (time - pos.entry_time) / 60.0
        if elapsed_min >= self.cfg.max_hold_minutes:
            self._close(st, pos, close, time, "time_stop")
            return

    # ==================================================================
    def _close(self, st: SymbolState, pos: Position,
                fill_price: float, time: float, reason: str) -> None:
        cfg = st.cfg
        # Exit slippage: full spread on stop-out, half on TP / time
        slip_mult = 1.0 if reason == "stop_loss" else 0.5
        actual_fill = fill_price - pos.side * slip_mult * cfg.spread_pts

        gross = (actual_fill - pos.entry_price) * pos.side * pos.lots * cfg.pip_value
        commission = cfg.commission_per_lot * pos.lots
        spread_cost = cfg.spread_pts * pos.lots * cfg.pip_value
        net = gross - commission

        self.equity += net
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
            self.sizer.mark_equity(self.equity)

        risk_dollars = pos.R_distance * pos.lots * cfg.pip_value
        realised_R = net / max(risk_dollars, 1e-9)

        rec = TradeRecord(
            edge_name=pos.edge_name,
            symbol=cfg.symbol,
            side=pos.side,
            entry_time=pos.entry_time,
            exit_time=time,
            entry_price=pos.entry_price,
            exit_price=actual_fill,
            lots=pos.lots,
            sl_price=pos.sl,
            tp_price=pos.tp,
            R_distance=pos.R_distance,
            realised_R=realised_R,
            gross_pnl=gross,
            commission=commission,
            spread_cost=spread_cost,
            net_pnl=net,
            exit_reason=reason,
            conviction=getattr(pos, "_conviction", 0.0),
            or_factor=getattr(pos, "_or_factor", 1.0),
            equity_at_entry=getattr(pos, "_equity_at_entry", self.equity),
            equity_at_exit=self.equity,
        )
        self.trades.append(rec)
        self.sizer.record_trade(realised_R, self.equity)

        # Update edge posterior + maybe disable
        es = self.edge_stats[pos.edge_name]
        es["trades"] += 1
        if net > 0:
            es["wins"] += 1
            es["gross_win_R"] += realised_R
        else:
            es["gross_loss_R"] -= realised_R
        if es["trades"] >= self.cfg.edge_min_trades_before_disable:
            pf = es["gross_win_R"] / es["gross_loss_R"] if es["gross_loss_R"] > 0 else float("inf")
            if pf < self.cfg.edge_disable_pf_threshold:
                es["disabled"] = True

        # Remove position from state
        if pos in st.positions:
            st.positions.remove(pos)

    # ==================================================================
    def _close_all(self, reason: str, time: float) -> None:
        for st in self.states.values():
            for pos in list(st.positions):
                self._close(st, pos, st.last_close, time, reason)

    # ==================================================================
    #  Reporting
    # ==================================================================
    def summary(self) -> dict:
        if not self.trades:
            return {"trades": 0, "net_pnl": 0, "pf": 0, "win_rate": 0,
                    "equity": self.equity, "peak": self.peak_equity,
                    "by_edge": {}, "by_symbol": {}}
        wins = [t for t in self.trades if t.net_pnl > 0]
        losses = [t for t in self.trades if t.net_pnl <= 0]
        gw = sum(t.net_pnl for t in wins)
        gl = -sum(t.net_pnl for t in losses)
        pf = gw / gl if gl > 0 else float("inf")
        net = sum(t.net_pnl for t in self.trades)
        # Max DD
        eq = self.start_equity
        peak = eq
        mdd = 0.0
        for t in self.trades:
            eq += t.net_pnl
            peak = max(peak, eq)
            dd = (peak - eq) / peak if peak > 0 else 0.0
            if dd > mdd:
                mdd = dd
        # Per-edge breakdown
        by_edge: dict[str, dict] = {}
        for t in self.trades:
            d = by_edge.setdefault(t.edge_name, {
                "n": 0, "wins": 0, "net_pnl": 0.0, "sum_R": 0.0,
            })
            d["n"] += 1
            d["wins"] += 1 if t.net_pnl > 0 else 0
            d["net_pnl"] += t.net_pnl
            d["sum_R"] += t.realised_R
        for d in by_edge.values():
            d["wr"] = d["wins"] / d["n"] if d["n"] > 0 else 0
            d["expectancy_R"] = d["sum_R"] / d["n"] if d["n"] > 0 else 0
        # Per-symbol
        by_sym: dict[str, int] = {}
        for t in self.trades:
            by_sym[t.symbol] = by_sym.get(t.symbol, 0) + 1
        return {
            "trades": len(self.trades),
            "net_pnl": net,
            "pct_return": (self.equity - self.start_equity) / self.start_equity * 100,
            "pf": pf,
            "win_rate": len(wins) / len(self.trades),
            "avg_winner_R": sum(t.realised_R for t in wins) / len(wins) if wins else 0,
            "avg_loser_R": sum(t.realised_R for t in losses) / len(losses) if losses else 0,
            "expectancy_R": sum(t.realised_R for t in self.trades) / len(self.trades),
            "max_dd_pct": mdd * 100,
            "equity": self.equity,
            "peak": self.peak_equity,
            "gross_costs": sum(t.commission for t in self.trades),
            "by_symbol": by_sym,
            "by_edge": by_edge,
            "disabled_edges": [n for n, s in self.edge_stats.items() if s["disabled"]],
        }

    def dump_trades(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump([asdict(t) for t in self.trades], f, indent=2, default=str)
