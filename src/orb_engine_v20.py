"""
ORB v20 — Opening Range Breakout with honest 5%ers cost modelling.

Design principles (from Docs/V20_MASTER_PLAN_BEAT_THE_BUMS.md):

  - ENTRY  = stop order at OR-high (long) or OR-low (short). First breakout
             of the session only. Fill at level + half-spread slippage
             (realistic market-order slippage assumption).
  - SL     = opposite side of the OR (OR-mirror stop).
             R_dist = OR_range
  - TP1    = entry + side * (tp1_range_mult * OR_range)  -> close 50% of lots
  - TP2    = entry + side * (tp2_range_mult * OR_range)  -> close 25% of lots
  - TRAIL  = last 25% trails at `trail_atr_mult × ATR(14)` from peak.
  - NO same-bar exit: once filled, we don't also check SL/TP on the same bar.
  - SAFETY : hard time-stop at end of trade_window_minutes if still open.
  - FILTER : optional NR7 filter on the prior day's range.
  - COSTS  : full round-trip spread + commission via SymbolSpec.
  - SIZING : simple fixed fraction of equity (0.5% risk) — proves edge first;
             the dynamic Kelly + guard layer plugs in unchanged later.

Usage:

    from src.orb_engine_v20 import ORBEngineV20, ORBEngineConfig
    eng = ORBEngineV20(symbols=[SMARTBB_UNIVERSE['US30'], ...],
                       cfg=ORBEngineConfig(),
                       initial_equity=100_000)
    for (t, sym, o, h, l, c) in merged_m1:
        eng.on_bar(sym, t.timestamp(), t.strftime('%Y-%m-%d'),
                   t.hour, t.minute, o, h, l, c)
    print(eng.summary())
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, List

from src.smartbb_engine import SymbolSpec, ATR
from src.momentum.orb import OpeningRangeTracker, NRFilter, ORB_DEFAULTS, ORBConfig


# =====================================================================
#  Engine Config
# =====================================================================

@dataclass
class ORBEngineConfig:
    """Global (not per-symbol) engine knobs."""
    # Sizing envelope (fixed fraction; sizer plugs in later)
    risk_pct: float = 0.005      # 0.5% of equity per trade
    min_lots: float = 0.01
    max_lots: float = 50.0

    # Safety rails (mirror 5%ers limits)
    daily_dd_limit: float = 0.04      # 4% daily
    total_dd_limit: float = 0.08      # 8% total

    # TP ladder fractions (must sum <= 1.0)
    tp1_close_frac: float = 0.50
    tp2_close_frac: float = 0.25
    # Remaining (0.25) trails.

    # Trail stop (for the runner) in ATR multiples of the most recent favourable peak
    trail_atr_mult: float = 0.8

    # Cost-awareness amplitude hurdle — expected TP1 move must clear
    # amp_hurdle × round-trip cost (spread + commission)
    amp_hurdle: float = 2.5

    # Optional NR-7 filter: only take trades when yesterday's range was
    # the narrowest of last 7 days (Crabel 1990).
    require_nr7: bool = False
    nr_lookback: int = 7

    # ATR lookback for trail-stop and diagnostics
    atr_window: int = 14

    # Fallback time-stop in minutes from breakout (session safety net)
    time_stop_minutes: int = 180

    # Per-symbol ORB configs override this default
    default_orb: ORBConfig = field(default_factory=lambda: ORBConfig(
        or_start_hour=14, or_start_minute=30,
        or_minutes=15, trade_window_minutes=90,
        tp1_range_mult=1.0, tp2_range_mult=2.0,
    ))


# =====================================================================
#  Per-symbol state
# =====================================================================

class _ORBState:
    def __init__(self, spec: SymbolSpec, cfg: ORBEngineConfig,
                  orb_cfg: ORBConfig):
        self.spec = spec
        self.cfg = cfg
        self.orb = OpeningRangeTracker(orb_cfg)
        self.nr = NRFilter(lookback=cfg.nr_lookback + 2)
        self.atr = ATR(window=cfg.atr_window)
        self.position: Optional[_ORBPosition] = None
        self._last_close: float = 0.0
        self._m5_h = -1e18
        self._m5_l = +1e18
        self._m5_c = 0.0
        self._m5_count = 0


# =====================================================================
#  Position / Trade records
# =====================================================================

@dataclass
class _ORBPosition:
    symbol: str
    side: int
    entry_price: float
    entry_time: float
    entry_minute: int       # minute-of-day at fill
    entry_day: str
    lots_total: float       # original size
    lots_open: float        # remaining open
    sl: float
    tp1: float
    tp2: float
    R_dist: float           # |entry - sl|, initial risk distance
    or_range: float
    tp1_hit: bool = False
    tp2_hit: bool = False
    peak_favourable: float = 0.0   # max favourable excursion price (for trail)


@dataclass
class _ORBTrade:
    symbol: str
    side: int
    entry_time: float
    exit_time: float
    entry_price: float
    exit_price: float
    lots: float                 # size of THIS partial exit
    realised_R: float           # pnl in R units (of initial R_dist)
    gross_pnl: float
    spread_cost: float
    commission: float
    net_pnl: float
    exit_reason: str            # "tp1" | "tp2" | "trail" | "stop" | "time_stop" | "session_close"
    or_range: float


# =====================================================================
#  Engine
# =====================================================================

class ORBEngineV20:

    def __init__(
        self,
        symbols: List[SymbolSpec],
        cfg: Optional[ORBEngineConfig] = None,
        orb_configs: Optional[Dict[str, ORBConfig]] = None,
        initial_equity: float = 100_000.0,
    ):
        self.cfg = cfg or ORBEngineConfig()
        orb_configs = orb_configs or {}
        self.states: Dict[str, _ORBState] = {}
        for spec in symbols:
            orb_cfg = orb_configs.get(spec.symbol,
                                        ORB_DEFAULTS.get(spec.symbol, self.cfg.default_orb))
            self.states[spec.symbol] = _ORBState(spec, self.cfg, orb_cfg)
        self.equity = initial_equity
        self.start_equity = initial_equity
        self.peak_equity = initial_equity
        self.sod_equity = initial_equity
        self.day_key: Optional[str] = None
        self.halted_for_day = False
        self.halted_permanently = False
        self.trades: List[_ORBTrade] = []

    # ------------------------------------------------------------------
    def _roll_day(self, d: str) -> None:
        if self.day_key is None:
            self.day_key = d
            return
        if d != self.day_key:
            self.day_key = d
            self.sod_equity = self.equity
            self.halted_for_day = False

    # ------------------------------------------------------------------
    def _safety_ok(self) -> bool:
        if self.halted_permanently or self.halted_for_day:
            return False
        if self.equity <= self.peak_equity * (1 - self.cfg.total_dd_limit):
            self.halted_permanently = True
            return False
        if self.equity <= self.sod_equity * (1 - self.cfg.daily_dd_limit):
            self.halted_for_day = True
            return False
        return True

    # ------------------------------------------------------------------
    def on_bar(self, symbol: str, time: float, day_key: str,
                hour: int, minute: int,
                open_: float, high: float, low: float, close: float) -> None:
        if symbol not in self.states:
            return
        st = self.states[symbol]
        self._roll_day(day_key)

        # --- Update ORB + NR trackers ------------------------------------
        st.orb.update(day_key, hour, minute, high, low)
        st.nr.update(day_key, high, low)

        # --- Keep a 5-minute ATR tracker (used for trail) ---------------
        if st._m5_count == 0:
            st._m5_h = high
            st._m5_l = low
        else:
            st._m5_h = max(st._m5_h, high)
            st._m5_l = min(st._m5_l, low)
        st._m5_c = close
        st._m5_count += 1
        if st._m5_count >= 5:
            st.atr.update(st._m5_h, st._m5_l, st._m5_c)
            st._m5_h = -1e18
            st._m5_l = +1e18
            st._m5_count = 0

        st._last_close = close

        # --- Manage any open position (intrabar) -------------------------
        if st.position is not None:
            # Only manage if this bar is AFTER the entry bar (no same-bar
            # cheating — we already used the entry bar to fill).
            if not (st.position.entry_day == day_key
                    and st.position.entry_minute == hour * 60 + minute):
                self._intrabar(st, time, day_key, hour, minute, high, low, close)

        # --- Maybe enter? -----------------------------------------------
        if not self._safety_ok():
            return
        if st.position is None:
            self._maybe_enter(st, time, day_key, hour, minute, high, low, close)

    # ------------------------------------------------------------------
    def _maybe_enter(self, st: _ORBState, time: float, day_key: str,
                      hour: int, minute: int,
                      high: float, low: float, close: float) -> None:
        # Session-hours gate
        mod = hour * 60 + minute
        if not (st.spec.trade_start <= mod < st.spec.trade_end):
            return

        # Must be in the trade window
        if not st.orb.in_trade_window(hour, minute):
            return

        # Optional NR7 filter
        if self.cfg.require_nr7 and not st.nr.is_prev_day_narrow(self.cfg.nr_lookback):
            return

        # Detect breakout (uses intrabar H/L)
        sig = st.orb.detect_breakout(high, low, close)
        if sig == 0:
            return

        orb_cfg = st.orb.cfg
        or_high = st.orb.or_high
        or_low = st.orb.or_low
        or_range = st.orb.or_range
        if or_high is None or or_low is None or or_range <= 0.0:
            return

        # Entry price: realistic stop-order fill at the OR level plus half-spread
        # slippage in the adverse direction (stop order gets filled at or below the
        # trigger for longs, or above for shorts — so we pay half-spread).
        if sig > 0:
            entry = or_high + 0.5 * st.spec.spread_pts
            sl = or_low
        else:
            entry = or_low - 0.5 * st.spec.spread_pts
            sl = or_high

        # SL must be on the correct (loss) side — by construction for OR-mirror,
        # but guard anyway
        if sig > 0 and sl >= entry:
            return
        if sig < 0 and sl <= entry:
            return

        R_dist = abs(entry - sl)

        tp1 = entry + sig * orb_cfg.tp1_range_mult * or_range
        tp2 = entry + sig * orb_cfg.tp2_range_mult * or_range

        # Amplitude-vs-cost gate: expected TP1 $move must clear amp_hurdle × cost
        # (measured at 1 lot, to keep it size-independent)
        tp1_dollars_per_lot = abs(tp1 - entry) * st.spec.pip_value
        cost_pts_per_lot = 2.0 * st.spec.spread_pts * st.spec.pip_value
        comm_per_lot = st.spec.round_trip_commission(avg_price=entry, lots=1.0)
        cost_dollars_per_lot = cost_pts_per_lot + comm_per_lot
        if tp1_dollars_per_lot < self.cfg.amp_hurdle * cost_dollars_per_lot:
            return  # edge too thin vs costs — skip

        # Sizing: fixed risk_pct of equity
        risk_d = self.equity * self.cfg.risk_pct
        denom = max(R_dist * st.spec.pip_value, 1e-9)
        lots = risk_d / denom
        lots = max(self.cfg.min_lots,
                   min(self.cfg.max_lots,
                       math.floor(lots / st.spec.lot_step) * st.spec.lot_step))
        if lots < st.spec.min_lots:
            return

        st.position = _ORBPosition(
            symbol=st.spec.symbol, side=sig,
            entry_price=entry, entry_time=time,
            entry_minute=mod, entry_day=day_key,
            lots_total=lots, lots_open=lots,
            sl=sl, tp1=tp1, tp2=tp2,
            R_dist=R_dist, or_range=or_range,
            peak_favourable=entry,
        )

    # ------------------------------------------------------------------
    def _intrabar(self, st: _ORBState, time: float, day_key: str,
                   hour: int, minute: int,
                   high: float, low: float, close: float) -> None:
        pos = st.position
        if pos is None:
            return
        mod = hour * 60 + minute
        minutes_since_entry = (mod - pos.entry_minute) + \
                              (1440 if day_key != pos.entry_day else 0)

        # Track peak-favourable for trail
        if pos.side > 0:
            pos.peak_favourable = max(pos.peak_favourable, high)
        else:
            pos.peak_favourable = min(pos.peak_favourable, low)

        # --- Stop-loss first (hard rule: risk control before profit-taking) ---
        if pos.side > 0 and low <= pos.sl:
            self._close_remaining(st, pos.sl, time, "stop")
            return
        if pos.side < 0 and high >= pos.sl:
            self._close_remaining(st, pos.sl, time, "stop")
            return

        # --- TP1 ---
        if not pos.tp1_hit:
            if (pos.side > 0 and high >= pos.tp1) or (pos.side < 0 and low <= pos.tp1):
                lots_to_close = pos.lots_total * self.cfg.tp1_close_frac
                lots_to_close = min(lots_to_close, pos.lots_open)
                if lots_to_close >= st.spec.min_lots:
                    self._close_partial(st, pos.tp1, time, "tp1", lots_to_close)
                pos.tp1_hit = True
                # Move SL to break-even after TP1
                pos.sl = pos.entry_price

        # --- TP2 ---
        if not pos.tp2_hit and pos.lots_open > 0:
            if (pos.side > 0 and high >= pos.tp2) or (pos.side < 0 and low <= pos.tp2):
                lots_to_close = pos.lots_total * self.cfg.tp2_close_frac
                lots_to_close = min(lots_to_close, pos.lots_open)
                if lots_to_close >= st.spec.min_lots:
                    self._close_partial(st, pos.tp2, time, "tp2", lots_to_close)
                pos.tp2_hit = True

        # --- Trail runner (after TP2) ---
        if pos.tp2_hit and pos.lots_open > 0 and st.atr.ready:
            trail_offset = self.cfg.trail_atr_mult * st.atr.value
            if pos.side > 0:
                trail_sl = pos.peak_favourable - trail_offset
                if trail_sl > pos.sl:
                    pos.sl = trail_sl
            else:
                trail_sl = pos.peak_favourable + trail_offset
                if trail_sl < pos.sl:
                    pos.sl = trail_sl

        # --- Time stop ---
        if minutes_since_entry >= self.cfg.time_stop_minutes and pos.lots_open > 0:
            self._close_remaining(st, close, time, "time_stop")
            return

        # --- End of session / ORB trade window ---
        # Exit any residual runner at session close to avoid overnight risk
        orb_cfg = st.orb.cfg
        session_close_m = (orb_cfg.or_start_hour * 60 + orb_cfg.or_start_minute
                            + orb_cfg.or_minutes + orb_cfg.trade_window_minutes
                            + 60)  # +60min grace for runner
        if (day_key == pos.entry_day and mod >= session_close_m
                and pos.lots_open > 0):
            self._close_remaining(st, close, time, "session_close")

    # ------------------------------------------------------------------
    def _close_partial(self, st: _ORBState, fill_price: float, t: float,
                        reason: str, lots: float) -> None:
        pos = st.position
        if pos is None or lots <= 0:
            return
        spec = st.spec
        # Slippage assumption:
        #   - "tp1" / "tp2" → limit orders in MT5 (no slip beyond spread)
        #   - "stop" / "time_stop" / "session_close" → market orders (1pt slip)
        slip_pts = 1.0 if reason in ("stop", "time_stop", "session_close") else 0.0
        actual = fill_price - pos.side * slip_pts
        gross = (actual - pos.entry_price) * pos.side * lots * spec.pip_value
        spread_cost = (spec.spread_pts * 0.5 + 0.5 * spec.spread_pts) \
                      * spec.pip_value * lots
        avg_price = 0.5 * (pos.entry_price + actual)
        commission = spec.round_trip_commission(avg_price=avg_price, lots=lots)
        net = gross - commission
        self.equity += net
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
        realised_R = net / max(pos.R_dist * pos.lots_total * spec.pip_value, 1e-9)
        self.trades.append(_ORBTrade(
            symbol=spec.symbol, side=pos.side,
            entry_time=pos.entry_time, exit_time=t,
            entry_price=pos.entry_price, exit_price=actual,
            lots=lots, realised_R=realised_R,
            gross_pnl=gross, spread_cost=spread_cost,
            commission=commission, net_pnl=net,
            exit_reason=reason, or_range=pos.or_range,
        ))
        pos.lots_open -= lots
        if pos.lots_open < st.spec.min_lots * 0.5:
            st.position = None

    # ------------------------------------------------------------------
    def _close_remaining(self, st: _ORBState, fill_price: float, t: float,
                          reason: str) -> None:
        pos = st.position
        if pos is None:
            return
        lots = pos.lots_open
        if lots > 0:
            self._close_partial(st, fill_price, t, reason, lots)
        st.position = None

    # ------------------------------------------------------------------
    def summary(self) -> dict:
        trades = self.trades
        if not trades:
            return {"trades": 0, "net_pnl": 0.0, "equity": self.equity,
                    "pct_return": 0.0, "pf": 0.0, "win_rate": 0.0,
                    "max_dd_pct": 0.0, "by_symbol": {}, "by_exit": {}}
        wins = [t for t in trades if t.net_pnl > 0]
        losses = [t for t in trades if t.net_pnl <= 0]
        gw = sum(t.net_pnl for t in wins)
        gl = -sum(t.net_pnl for t in losses)
        pf = gw / gl if gl > 0 else float("inf")
        net = sum(t.net_pnl for t in trades)

        # Group partial exits by (symbol, entry_time) → count distinct trades
        entries = set((t.symbol, t.entry_time) for t in trades)
        n_entries = len(entries)

        eq = self.start_equity
        peak = eq
        mdd = 0.0
        for t in trades:
            eq += t.net_pnl
            peak = max(peak, eq)
            dd = (peak - eq) / peak if peak > 0 else 0.0
            mdd = max(mdd, dd)

        by_symbol, by_exit = {}, defaultdict(lambda: {"n": 0, "net": 0.0})
        for t in trades:
            sym = by_symbol.setdefault(t.symbol, {"n": 0, "wins": 0, "net": 0.0})
            sym["n"] += 1
            sym["wins"] += 1 if t.net_pnl > 0 else 0
            sym["net"] += t.net_pnl
            by_exit[t.exit_reason]["n"] += 1
            by_exit[t.exit_reason]["net"] += t.net_pnl
        for s in by_symbol.values():
            s["wr"] = s["wins"] / s["n"] if s["n"] > 0 else 0.0

        return {
            "trades": len(trades),
            "entries": n_entries,
            "net_pnl": net,
            "pct_return": (self.equity - self.start_equity) / self.start_equity * 100.0,
            "pf": pf,
            "win_rate": len(wins) / len(trades),
            "avg_win_pnl":  (gw / len(wins))   if wins   else 0.0,
            "avg_loss_pnl": (-gl / len(losses)) if losses else 0.0,
            "max_dd_pct": mdd * 100.0,
            "equity": self.equity,
            "peak": self.peak_equity,
            "gross_commissions": sum(t.commission for t in trades),
            "gross_spread_cost": sum(t.spread_cost for t in trades),
            "by_symbol": by_symbol,
            "by_exit": {k: dict(v) for k, v in by_exit.items()},
        }

    # ------------------------------------------------------------------
    def dump_trades(self, path: str) -> None:
        import json as _json
        with open(path, "w") as f:
            _json.dump([asdict(t) for t in self.trades], f, indent=2, default=str)
