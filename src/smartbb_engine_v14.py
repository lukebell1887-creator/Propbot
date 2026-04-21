"""
SHF v14 PhD-ADAPTIVE SMART BOLLINGER
=====================================

v14 is v13's architecture with every hand-picked scalar replaced by either
(a) a per-symbol override (via `SymbolParams`), or (b) a self-calibrating
rolling-quantile threshold.

What is identical to v13 (so your live bot can share infra):
    - M1 -> M5 aggregation
    - Intrabar SL/TP honouring
    - Break-even trail mechanic
    - Per-symbol REAL 5%ers MTB cost model (`SymbolSpec`)
    - AKAD sizing (base × Bayesian WR × Grossman-Zhou DD)
    - 4 % daily / 5 % total DD halts
    - Concurrency caps

What is NEW in v14:
    U1  adaptive |Z| entry gate (rolling-quantile + absolute rails)
    U2  adaptive Hurst gate       (rolling-quantile + absolute ceiling)
    U3  OU half-life gate + dynamic time-stop
    U4  optimal-stopping exit    (Gaussian-drift completion probability)
    U5  per-symbol SymbolParams overrides
    U6  per-symbol allowed_hours mask (hour-of-day filter)
    U7  bootstrap helper for CIs (in the backtest script, not here)

Import-compatible entry point:
    SmartBBV14Engine(symbols=[SMARTBB_UNIVERSE['US100'], ...],
                      params=<per-symbol SymbolParams>,
                      cfg=<engine-wide SmartBBV14Config>,
                      initial_equity=100_000)
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, asdict, field
from typing import Optional, Deque, FrozenSet
import json

from src.momentum.kalman import KalmanForecast
from src.momentum.bayesian_edge import BetaPosterior
from src.momentum.kelly import GrossmanZhouDD
from src.momentum.rolling_quantile import RollingQuantile
from src.momentum.ou_halflife import fit_ou
from src.momentum.optimal_stop_v14 import OptimalStopV14

# Reuse v13's per-symbol cost table — single source of truth.
from src.smartbb_engine import (
    SymbolSpec, SMARTBB_UNIVERSE,
    RollingBB, ATR, _hurst_rs,
)


# =====================================================================
#  Per-symbol overridable parameters
# =====================================================================

@dataclass
class SymbolParams:
    """
    Per-symbol v14 overrides. Anything left at default uses the engine-wide
    default. The walk-forward optimizer writes one of these per kept symbol.
    """
    # Entry gates -------------------------------------------------------
    z_quantile: float = 0.99
    z_quantile_window: int = 500
    z_min_abs: float = 2.5
    z_max_abs: float = 5.0

    hurst_quantile: float = 0.30
    hurst_quantile_window: int = 200
    hurst_max_abs: float = 0.55

    use_ou_gate: bool = True
    ou_window: int = 200
    ou_max_halflife: float = 30.0

    # Exits -------------------------------------------------------------
    stop_atr_mult: float = 1.0
    tp_frac: float = 1.0         # 1.0 = middle band, 0.5 = halfway
    breakeven_trigger_frac: float = 0.5
    breakeven_atr_offset: float = 0.2

    use_optimal_stop: bool = True
    optimal_stop_threshold: float = 0.40
    optimal_stop_min_bars: int = 3
    time_stop_max: int = 96
    time_stop_ou_mult: float = 2.0   # T_stop = min(ou_mult × halflife, time_stop_max)

    # Session / hour mask ----------------------------------------------
    # If None, fall back to SymbolSpec.trade_start/trade_end window
    allowed_hours: Optional[FrozenSet[int]] = None

    # Sizing -----------------------------------------------------------
    risk_multiplier: float = 1.0


# =====================================================================
#  Engine-wide config (things not tuned per-symbol)
# =====================================================================

@dataclass
class SmartBBV14Config:
    bars_per_m5: int = 5

    # Bollinger (not tuned — retail canonical; stability guarantee)
    bb_period: int = 20
    bb_sigma: float = 2.0

    # Hurst rolling window (same as v13)
    hurst_window: int = 300
    hurst_min_data: int = 120

    # Kalman (identical to v13)
    kalman_sigma_obs: float = 0.0015
    kalman_sigma_proc: float = 3e-4

    # ATR
    atr_window: int = 14

    # Amplitude gate (cost discipline — kept from v13)
    amplitude_hurdle: float = 1.5

    # Sizing envelope
    base_risk_pct: float = 0.005
    min_risk_pct: float = 0.002
    max_risk_pct: float = 0.010
    total_dd_limit: float = 0.05
    daily_dd_limit: float = 0.04
    gz_gamma: float = 2.0
    max_concurrent: int = 3
    max_same_class_concurrent: int = 2

    # v15 COMMISSION-STRESS KNOB
    # Extra $/lot added to every round-trip (simulates hidden slippage, bad
    # fills, or a future fee hike). Default 0 = honest, pure spec cost model.
    # Used by the v15 optimizer to verify edge survives +$1/lot, +$2/lot etc.
    extra_cost_per_lot: float = 0.0

    # Default SymbolParams for symbols without an explicit override
    default_params: SymbolParams = field(default_factory=SymbolParams)


# =====================================================================
#  Per-symbol state
# =====================================================================

class _SymbolStateV14:
    def __init__(self, spec: SymbolSpec, cfg: SmartBBV14Config,
                  params: SymbolParams):
        self.spec = spec
        self.cfg = cfg
        self.params = params

        self.bb = RollingBB(period=cfg.bb_period)
        self.kalman = KalmanForecast(sigma_obs=cfg.kalman_sigma_obs,
                                       sigma_proc=cfg.kalman_sigma_proc)
        self.atr = ATR(window=cfg.atr_window)

        self.ret_buf: Deque[float] = deque(maxlen=cfg.hurst_window)
        self._hurst = 0.5

        # Rolling quantile trackers (U1, U2)
        self.abs_z_q = RollingQuantile(
            q=params.z_quantile,
            window=params.z_quantile_window,
            min_samples=max(50, params.z_quantile_window // 4),
        )
        self.hurst_q = RollingQuantile(
            q=params.hurst_quantile,
            window=params.hurst_quantile_window,
            min_samples=max(20, params.hurst_quantile_window // 4),
        )

        # Closes buffer for OU fit (U3) — we only recompute every N bars to save CPU
        self.close_buf: Deque[float] = deque(maxlen=params.ou_window)
        self._ou_halflife: float = math.inf
        self._ou_last_calc_bar: int = -1

        # M5 aggregation
        self._m5_o: Optional[float] = None
        self._m5_h: float = -1e18
        self._m5_l: float = +1e18
        self._m5_count: int = 0
        self.m5_bars: int = 0

        # Open position
        self.position: Optional["_PositionV14"] = None
        self.optimal_stop = OptimalStopV14(
            threshold=params.optimal_stop_threshold,
            min_bars=params.optimal_stop_min_bars,
            sigma_obs=cfg.kalman_sigma_obs,
        )
        self._last_close: float = 0.0


# =====================================================================
#  Position + Trade records
# =====================================================================

@dataclass
class _PositionV14:
    symbol: str
    side: int
    entry_price: float
    entry_time: float
    entry_bar: int
    lots: float
    sl: float
    tp: float
    z_at_entry: float
    hurst_at_entry: float
    halflife_at_entry: float
    R_dist: float
    R_dollars: float
    time_stop_bars: int     # absolute M5-bar index at which time stop fires


@dataclass
class _TradeV14:
    symbol: str
    side: int
    entry_time: float
    exit_time: float
    entry_price: float
    exit_price: float
    lots: float
    R_dist: float
    realised_R: float
    gross_pnl: float
    spread_cost: float
    commission: float
    net_pnl: float
    exit_reason: str
    z_at_entry: float
    hurst_at_entry: float
    halflife_at_entry: float
    bars_held: int


# =====================================================================
#  Engine
# =====================================================================

class SmartBBV14Engine:

    def __init__(
        self,
        symbols: list[SymbolSpec],
        params: Optional[dict[str, SymbolParams]] = None,
        cfg: Optional[SmartBBV14Config] = None,
        initial_equity: float = 100_000.0,
    ):
        self.cfg = cfg or SmartBBV14Config()
        params = params or {}
        self.states: dict[str, _SymbolStateV14] = {}
        for spec in symbols:
            p = params.get(spec.symbol, self.cfg.default_params)
            self.states[spec.symbol] = _SymbolStateV14(spec, self.cfg, p)

        self.beta: dict[tuple[str, int], BetaPosterior] = defaultdict(
            lambda: BetaPosterior(alpha=1.0, beta=1.0))
        self.gz = GrossmanZhouDD(max_dd=self.cfg.total_dd_limit,
                                   gamma=self.cfg.gz_gamma)
        self.equity = initial_equity
        self.start_equity = initial_equity
        self.peak_equity = initial_equity
        self.sod_equity = initial_equity
        self.day_key: Optional[str] = None
        self.halted_for_day = False
        self.halted_permanently = False
        self.trades: list[_TradeV14] = []

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
    def _check_safety(self, t: float) -> bool:
        if self.halted_permanently or self.halted_for_day:
            return False
        if self.equity <= self.peak_equity * (1 - self.cfg.total_dd_limit):
            self.halted_permanently = True
            self._close_all("ghost_total_dd", t)
            return False
        if self.equity <= self.sod_equity * (1 - self.cfg.daily_dd_limit):
            self.halted_for_day = True
            self._close_all("ghost_daily_dd", t)
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

        # M1 -> M5 aggregation
        if st._m5_count == 0:
            st._m5_o = open_
            st._m5_h = high
            st._m5_l = low
        else:
            st._m5_h = max(st._m5_h, high)
            st._m5_l = min(st._m5_l, low)
        st._m5_count += 1

        # Intrabar SL/TP (every M1 tick)
        if st.position is not None:
            self._intrabar(st, time, high, low, close)

        if st._m5_count < self.cfg.bars_per_m5:
            st._last_close = close
            return

        # --- M5 bar complete ---
        m5_o = st._m5_o
        m5_h = st._m5_h
        m5_l = st._m5_l
        m5_c = close
        if m5_o is None or m5_o <= 0 or m5_c <= 0:
            st._m5_o = None
            st._m5_h = -1e18
            st._m5_l = +1e18
            st._m5_count = 0
            return
        ret = math.log(m5_c / m5_o)

        st._m5_o = None
        st._m5_h = -1e18
        st._m5_l = +1e18
        st._m5_count = 0
        st.m5_bars += 1
        st._last_close = close

        st.atr.update(m5_h, m5_l, m5_c)
        st.bb.update(m5_c)
        st.kalman.update(ret)
        st.ret_buf.append(ret)
        st.close_buf.append(m5_c)

        # Update adaptive-threshold feeds
        if st.bb.ready:
            st.abs_z_q.update(abs(st.bb.z(m5_c)))

        # Hurst recomputed every 8 bars (same as v13)
        if len(st.ret_buf) >= self.cfg.hurst_min_data and st.m5_bars % 8 == 0:
            st._hurst = _hurst_rs(list(st.ret_buf))
            st.hurst_q.update(st._hurst)

        # OU half-life recomputed every 16 bars (fit is O(W) — keep cheap)
        if (st.params.use_ou_gate
                and len(st.close_buf) >= max(30, st.params.ou_window // 2)
                and (st.m5_bars - st._ou_last_calc_bar) >= 16):
            _, _, hl = fit_ou(list(st.close_buf))
            st._ou_halflife = hl
            st._ou_last_calc_bar = st.m5_bars

        # Manage any open position using v14 logic (optimal-stop / time)
        if st.position is not None:
            self._manage(st, time, m5_c)

        # Safety halts
        if not self._check_safety(time):
            return

        if not st.bb.ready or not st.atr.ready:
            return
        if len(st.ret_buf) < self.cfg.hurst_min_data:
            return

        # Hour-of-day filter
        if not self._hour_ok(st, hour, minute):
            return

        if st.position is None:
            self._maybe_enter(st, time, m5_c)

    # ------------------------------------------------------------------
    def _hour_ok(self, st: _SymbolStateV14, hour: int, minute: int) -> bool:
        p = st.params
        if p.allowed_hours is not None:
            return hour in p.allowed_hours
        # Fallback: use spec's trade_start / trade_end (like v13)
        mod = hour * 60 + minute
        return st.spec.trade_start <= mod < st.spec.trade_end

    # ------------------------------------------------------------------
    def _maybe_enter(self, st: _SymbolStateV14, time: float, close: float) -> None:
        cfg = self.cfg
        p = st.params

        # Hurst gate (U2): trade only when regime is reverting
        #   - Must be at or below rolling-q threshold for THIS symbol
        #   - AND must be below the absolute safety ceiling
        hurst = st._hurst
        if hurst >= p.hurst_max_abs:
            return
        if st.hurst_q.ready:
            if hurst > st.hurst_q.value():
                return

        z = st.bb.z(close)
        abs_z = abs(z)

        # Z gate (U1): trade only on extreme |Z|
        #   - Must exceed rolling-q threshold
        #   - AND in absolute safety range [z_min_abs, z_max_abs]
        if not (p.z_min_abs <= abs_z <= p.z_max_abs):
            return
        if st.abs_z_q.ready:
            if abs_z < st.abs_z_q.value():
                return

        # OU gate (U3): reversion speed must be reasonable
        halflife = st._ou_halflife
        if p.use_ou_gate:
            if not math.isfinite(halflife) or halflife > p.ou_max_halflife:
                return

        side = -1 if z > 0 else +1

        # Concurrency checks
        total_open = sum(1 for s in self.states.values() if s.position is not None)
        if total_open >= cfg.max_concurrent:
            return
        same_cls = sum(1 for s in self.states.values()
                         if s.position is not None and s.spec.asset_class == st.spec.asset_class)
        if same_cls >= cfg.max_same_class_concurrent:
            return

        atr_pts = st.atr.value
        mean = st.bb.mean
        std = st.bb.std

        entry_fill = close + side * 0.5 * st.spec.spread_pts

        # Stop beyond the band by stop_atr_mult * ATR
        if side > 0:
            band = mean - cfg.bb_sigma * std
            sl = band - p.stop_atr_mult * atr_pts
        else:
            band = mean + cfg.bb_sigma * std
            sl = band + p.stop_atr_mult * atr_pts
        stop_distance = abs(entry_fill - sl)

        # TP: fraction of the way to the middle band
        tp_raw = mean
        tp = entry_fill + side * p.tp_frac * abs(tp_raw - entry_fill)
        tp_distance = abs(tp - entry_fill)
        if tp_distance <= 0 or stop_distance <= 0:
            return

        # Amplitude gate (cost discipline — kept from v13)
        expected_pts = tp_distance
        cost_pts = 2.0 * st.spec.spread_pts
        comm_one_lot = st.spec.round_trip_commission(avg_price=entry_fill, lots=1.0)
        cost_dollars_per_lot = cost_pts * st.spec.pip_value + comm_one_lot
        expected_dollars_per_lot = expected_pts * st.spec.pip_value
        if expected_dollars_per_lot < cfg.amplitude_hurdle * cost_dollars_per_lot:
            return

        # Sizing: AKAD with per-symbol risk_multiplier
        risk_pct = self._risk_pct(st.spec.symbol, side) * p.risk_multiplier
        risk_pct = max(cfg.min_risk_pct, min(cfg.max_risk_pct, risk_pct))
        risk_d = self.equity * risk_pct
        lots = risk_d / max(stop_distance * st.spec.pip_value, 1e-9)
        lots = max(st.spec.min_lots,
                    min(st.spec.max_lots,
                        math.floor(lots / st.spec.lot_step) * st.spec.lot_step))
        if lots < st.spec.min_lots:
            return

        # Dynamic time stop: min(ou_mult * halflife, time_stop_max)
        if p.use_ou_gate and math.isfinite(halflife):
            time_stop_bars_abs = st.m5_bars + min(
                int(p.time_stop_ou_mult * halflife) + 1, p.time_stop_max)
        else:
            time_stop_bars_abs = st.m5_bars + p.time_stop_max

        pos = _PositionV14(
            symbol=st.spec.symbol, side=side,
            entry_price=entry_fill, entry_time=time,
            entry_bar=st.m5_bars, lots=lots, sl=sl, tp=tp,
            z_at_entry=z, hurst_at_entry=hurst,
            halflife_at_entry=halflife if math.isfinite(halflife) else -1.0,
            R_dist=stop_distance, R_dollars=risk_d,
            time_stop_bars=time_stop_bars_abs,
        )
        st.position = pos
        if p.use_optimal_stop:
            st.optimal_stop.arm(side)

    # ------------------------------------------------------------------
    def _risk_pct(self, symbol: str, side: int) -> float:
        base = self.cfg.base_risk_pct
        b = self.beta.get((symbol, side))
        if b is not None and (b.alpha + b.beta - 2) >= 6:
            wr = b.mean()
            x = max(0.40, min(0.75, wr))
            bay = 0.6 + (x - 0.40) / 0.35 * 1.0
        else:
            bay = 1.0
        gz = self.gz.factor(equity=self.equity, peak=self.peak_equity)
        raw = base * bay * gz
        return max(self.cfg.min_risk_pct, min(self.cfg.max_risk_pct, raw))

    # ------------------------------------------------------------------
    def _intrabar(self, st: _SymbolStateV14, t: float,
                   high: float, low: float, close: float) -> None:
        pos = st.position
        if pos is None:
            return
        if pos.side > 0:
            if low <= pos.sl:
                self._close(st, pos.sl, t, "stop_loss")
                return
            if high >= pos.tp:
                self._close(st, pos.tp, t, "take_profit")
                return
        else:
            if high >= pos.sl:
                self._close(st, pos.sl, t, "stop_loss")
                return
            if low <= pos.tp:
                self._close(st, pos.tp, t, "take_profit")
                return

    # ------------------------------------------------------------------
    def _manage(self, st: _SymbolStateV14, t: float, close: float) -> None:
        pos = st.position
        if pos is None:
            return
        p = st.params
        bars_held = st.m5_bars - pos.entry_bar

        # Running points
        if pos.side > 0:
            running_pts = close - pos.entry_price
        else:
            running_pts = pos.entry_price - close

        # Break-even trail (identical mechanic to v13, but with per-symbol params)
        tp_dist = abs(pos.tp - pos.entry_price)
        if tp_dist > 0 and running_pts >= p.breakeven_trigger_frac * tp_dist:
            be_price = pos.entry_price + pos.side * (p.breakeven_atr_offset * st.atr.value)
            if pos.side > 0 and be_price > pos.sl:
                pos.sl = be_price
            elif pos.side < 0 and be_price < pos.sl:
                pos.sl = be_price

        # U4: optimal-stopping exit
        if p.use_optimal_stop and st.optimal_stop.armed and bars_held >= p.optimal_stop_min_bars:
            bars_remaining = max(pos.time_stop_bars - st.m5_bars, 1)
            # Rotate distances so they are "toward-TP" and "toward-SL" from current close
            tp_log_dist = math.log(max(pos.tp, 1e-9) / max(close, 1e-9))
            sl_log_dist = math.log(max(pos.sl, 1e-9) / max(close, 1e-9))
            if pos.side > 0:
                d_tp = max(tp_log_dist, 0.0)         # TP is above close -> positive log
                d_sl = max(-sl_log_dist, 0.0)        # SL is below close -> -log is positive
            else:
                d_tp = max(-tp_log_dist, 0.0)        # TP is below close -> -log positive
                d_sl = max(sl_log_dist, 0.0)         # SL is above close -> +log positive
            should_exit, p_win = st.optimal_stop.should_exit(
                mu_hat=st.kalman.mu,
                post_var=st.kalman.P,
                dist_tp=d_tp, dist_sl=d_sl,
                bars_held=bars_held,
                bars_remaining=bars_remaining,
            )
            if should_exit:
                self._close(st, close, t, "optimal_stop")
                return

        # Dynamic time stop (U3)
        if st.m5_bars >= pos.time_stop_bars:
            self._close(st, close, t, "time_stop")
            return

    # ------------------------------------------------------------------
    def _close(self, st: _SymbolStateV14, fill: float, t: float, reason: str) -> None:
        pos = st.position
        if pos is None:
            return
        spec = st.spec
        slip = 1.0 if reason == "stop_loss" else 0.5
        actual = fill - pos.side * slip * spec.spread_pts
        gross = (actual - pos.entry_price) * pos.side * pos.lots * spec.pip_value
        spread_cost = (spec.spread_pts * 0.5 + slip * spec.spread_pts) * spec.pip_value * pos.lots
        avg_price = 0.5 * (pos.entry_price + actual)
        commission = spec.round_trip_commission(avg_price=avg_price, lots=pos.lots)
        # v15 commission-stress: optional extra $/lot (default 0)
        if self.cfg.extra_cost_per_lot > 0:
            commission += self.cfg.extra_cost_per_lot * pos.lots
        net = gross - commission

        self.equity += net
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
        realised_R = net / max(pos.R_dollars, 1e-9)
        bars_held = st.m5_bars - pos.entry_bar

        self.trades.append(_TradeV14(
            symbol=spec.symbol, side=pos.side,
            entry_time=pos.entry_time, exit_time=t,
            entry_price=pos.entry_price, exit_price=actual,
            lots=pos.lots, R_dist=pos.R_dist, realised_R=realised_R,
            gross_pnl=gross, spread_cost=spread_cost, commission=commission,
            net_pnl=net, exit_reason=reason,
            z_at_entry=pos.z_at_entry,
            hurst_at_entry=pos.hurst_at_entry,
            halflife_at_entry=pos.halflife_at_entry,
            bars_held=bars_held,
        ))
        self.beta[(spec.symbol, pos.side)].update(net > 0)
        st.position = None
        st.optimal_stop.disarm()

    # ------------------------------------------------------------------
    def _close_all(self, reason: str, t: float) -> None:
        for st in self.states.values():
            if st.position is not None:
                self._close(st, st._last_close, t, reason)

    # ------------------------------------------------------------------
    def summary(self) -> dict:
        if not self.trades:
            return {"trades": 0, "net_pnl": 0, "equity": self.equity,
                    "pct_return": 0, "pf": 0, "win_rate": 0,
                    "expectancy_R": 0, "avg_winner_R": 0, "avg_loser_R": 0,
                    "avg_bars_held": 0, "max_dd_pct": 0,
                    "gross_commissions": 0, "gross_spread_cost": 0,
                    "by_symbol": {}, "by_side": {}, "by_exit_reason": {},
                    "by_hurst": {}, "by_z": {}}
        wins = [t for t in self.trades if t.net_pnl > 0]
        losses = [t for t in self.trades if t.net_pnl <= 0]
        gw = sum(t.net_pnl for t in wins)
        gl = -sum(t.net_pnl for t in losses)
        pf = gw / gl if gl > 0 else float("inf")
        net = sum(t.net_pnl for t in self.trades)

        eq = self.start_equity
        peak = eq
        mdd = 0.0
        for t in self.trades:
            eq += t.net_pnl
            peak = max(peak, eq)
            dd = (peak - eq) / peak if peak > 0 else 0.0
            if dd > mdd:
                mdd = dd

        by_symbol: dict = {}
        by_side: dict = {}
        by_exit: dict = defaultdict(int)
        by_hurst: dict = {}
        by_z: dict = {}
        for t in self.trades:
            for key, d in [(t.symbol, by_symbol), (t.side, by_side)]:
                r = d.setdefault(key, {"n": 0, "wins": 0, "net": 0.0,
                                          "sum_R": 0.0, "sum_bars": 0})
                r["n"] += 1
                r["wins"] += 1 if t.net_pnl > 0 else 0
                r["net"] += t.net_pnl
                r["sum_R"] += t.realised_R
                r["sum_bars"] += t.bars_held
            by_exit[t.exit_reason] += 1

            hb = f"{int(t.hurst_at_entry * 10) / 10:.1f}"
            b = by_hurst.setdefault(hb, {"n": 0, "wins": 0, "net": 0.0})
            b["n"] += 1
            b["wins"] += 1 if t.net_pnl > 0 else 0
            b["net"] += t.net_pnl

            zb = f"{int(abs(t.z_at_entry) * 2) / 2:.1f}"
            b = by_z.setdefault(zb, {"n": 0, "wins": 0, "net": 0.0})
            b["n"] += 1
            b["wins"] += 1 if t.net_pnl > 0 else 0
            b["net"] += t.net_pnl

        for d in list(by_symbol.values()) + list(by_side.values()):
            d["wr"] = d["wins"] / d["n"]
            d["expR"] = d["sum_R"] / d["n"]
            d["avg_bars"] = d["sum_bars"] / d["n"]
        for d in list(by_hurst.values()) + list(by_z.values()):
            d["wr"] = d["wins"] / d["n"]

        return {
            "trades": len(self.trades),
            "net_pnl": net,
            "pct_return": (self.equity - self.start_equity) / self.start_equity * 100.0,
            "pf": pf,
            "win_rate": len(wins) / len(self.trades),
            "expectancy_R": sum(t.realised_R for t in self.trades) / len(self.trades),
            "avg_winner_R": sum(t.realised_R for t in wins) / len(wins) if wins else 0.0,
            "avg_loser_R": sum(t.realised_R for t in losses) / len(losses) if losses else 0.0,
            "avg_bars_held": sum(t.bars_held for t in self.trades) / len(self.trades),
            "max_dd_pct": mdd * 100.0,
            "equity": self.equity,
            "peak": self.peak_equity,
            "gross_commissions": sum(t.commission for t in self.trades),
            "gross_spread_cost": sum(t.spread_cost for t in self.trades),
            "by_symbol": by_symbol,
            "by_side": {str(k): v for k, v in by_side.items()},
            "by_exit_reason": dict(by_exit),
            "by_hurst": by_hurst,
            "by_z": by_z,
        }

    # ------------------------------------------------------------------
    def dump_trades(self, path: str) -> None:
        with open(path, "w") as f:
            json.dump([asdict(t) for t in self.trades], f, indent=2, default=str)


# =====================================================================
#  Convenience: params serialization (JSON-friendly)
# =====================================================================

def params_to_dict(p: SymbolParams) -> dict:
    d = asdict(p)
    if p.allowed_hours is not None:
        d["allowed_hours"] = sorted(p.allowed_hours)
    return d


def params_from_dict(d: dict) -> SymbolParams:
    h = d.get("allowed_hours")
    if isinstance(h, (list, tuple)):
        h = frozenset(int(x) for x in h)
    elif h is None:
        pass
    else:
        h = frozenset(h)
    return SymbolParams(
        z_quantile=float(d.get("z_quantile", 0.99)),
        z_quantile_window=int(d.get("z_quantile_window", 500)),
        z_min_abs=float(d.get("z_min_abs", 2.5)),
        z_max_abs=float(d.get("z_max_abs", 5.0)),
        hurst_quantile=float(d.get("hurst_quantile", 0.30)),
        hurst_quantile_window=int(d.get("hurst_quantile_window", 200)),
        hurst_max_abs=float(d.get("hurst_max_abs", 0.55)),
        use_ou_gate=bool(d.get("use_ou_gate", True)),
        ou_window=int(d.get("ou_window", 200)),
        ou_max_halflife=float(d.get("ou_max_halflife", 30.0)),
        stop_atr_mult=float(d.get("stop_atr_mult", 1.0)),
        tp_frac=float(d.get("tp_frac", 1.0)),
        breakeven_trigger_frac=float(d.get("breakeven_trigger_frac", 0.5)),
        breakeven_atr_offset=float(d.get("breakeven_atr_offset", 0.2)),
        use_optimal_stop=bool(d.get("use_optimal_stop", True)),
        optimal_stop_threshold=float(d.get("optimal_stop_threshold", 0.40)),
        optimal_stop_min_bars=int(d.get("optimal_stop_min_bars", 3)),
        time_stop_max=int(d.get("time_stop_max", 96)),
        time_stop_ou_mult=float(d.get("time_stop_ou_mult", 2.0)),
        allowed_hours=h,
        risk_multiplier=float(d.get("risk_multiplier", 1.0)),
    )
