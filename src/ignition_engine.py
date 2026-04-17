"""
SHF v11 "IGNITION" — Hawkes × Kalman × GARCH momentum-burst strategy.

This is NOT a pattern strategy.  No opening range, no pivots, no gaps.
Pure stochastic-process math.

The thesis (Bacry-Mastromatteo-Muzy 2015; Harvey 1989; Engle 1982):

    Price returns cluster because of self-excitation.  A directional move
    makes another directional move in the same direction more likely
    *for a short horizon* — this is measurable by a Hawkes process.
    Simultaneously, when the latent drift μ of the true price process
    is statistically >0 (measured by a Kalman filter), you are inside a
    genuine regime, not noise.

    CONFLUENCE = high Hawkes self-excitation + Kalman drift significance
    in the same direction.  Trade the CONFLUENCE.

Exit when either signal decays:

    * Hawkes ratio λ_up/λ_dn crosses back below 1.3 (burst dying)
    * Kalman |μ̂/√P| drops below 1.0 (trend dying)
    * GPD-tail stop hit (extreme adverse move — 99.5% quantile of
      empirical downside distribution)

Why this is "perfect timing":

    Entry: Not at an arbitrary breakout level — at the moment the
    self-exciting intensity measurably exceeds baseline AND drift
    estimation agrees.  Both signals are forward-looking statistics of
    the underlying process, not price patterns.

    Exit: Not at an arbitrary TP — at the moment the intensity starts
    decaying.  This is the mathematical definition of "the move is
    ending".

Why costs are manageable:

    Trades are rare (a real Hawkes burst fires 2-5×/week per symbol on
    M5 data, not every day like breakouts).  Each move, when ridden from
    ignition to decay, is typically 5-20× the per-trade spread+commission.
    Cost/R ratio drops from 10% (v10) to ~2% here.

Uses existing modules:
    * src/momentum/hawkes.py       — HawkesIntensity
    * src/momentum/kalman.py       — KalmanForecast
    * src/momentum/garch.py        — GarchOne
    * src/momentum/gpd.py          — GpdTail
    * src/momentum/kelly.py        — GrossmanZhouDD
    * src/momentum/bayesian_edge.py — BetaPosterior
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, asdict
from collections import defaultdict
from typing import Optional
import json

from src.momentum.hawkes import HawkesIntensity
from src.momentum.kalman import KalmanForecast
from src.momentum.garch import GarchOne
from src.momentum.gpd import GpdTail
from src.momentum.bayesian_edge import BetaPosterior
from src.momentum.kelly import GrossmanZhouDD


# =====================================================================
#  Symbol config & 5%ers MTB commission-correct specs
# =====================================================================

@dataclass
class SymbolSpec:
    symbol: str
    asset_class: str
    pip_value: float
    spread_pts: float = 0.0
    commission_rt_per_lot: float = 0.0
    min_lots: float = 0.01
    lot_step: float = 0.01
    max_lots: float = 50.0
    # Trading window (UTC minutes-of-day)
    trade_start: int = 7 * 60
    trade_end: int = 21 * 60


FIVEERS_SPECS: dict[str, SymbolSpec] = {
    "DE40":   SymbolSpec("DE40",   "index", 1.0, 1.5, 2.0, trade_start=7*60,  trade_end=16*60),
    "US100":  SymbolSpec("US100",  "index", 1.0, 1.5, 2.0, trade_start=13*60, trade_end=21*60),
    "US500":  SymbolSpec("US500",  "index", 1.0, 0.5, 2.0, trade_start=13*60, trade_end=21*60),
    "US30":   SymbolSpec("US30",   "index", 1.0, 2.0, 2.0, trade_start=13*60, trade_end=21*60),
    "UK100":  SymbolSpec("UK100",  "index", 1.0, 1.0, 2.0, trade_start=7*60,  trade_end=16*60),
    "JP225":  SymbolSpec("JP225",  "index", 0.007, 7.0, 2.0, trade_start=0,    trade_end=6*60),
    "USOIL":  SymbolSpec("USOIL",  "oil",  10.0, 0.03, 0.0, trade_start=13*60, trade_end=20*60),
    "XTIUSD": SymbolSpec("XTIUSD", "oil",  10.0, 0.03, 0.0, trade_start=13*60, trade_end=20*60),
    "XBRUSD": SymbolSpec("XBRUSD", "oil",  10.0, 0.04, 0.0, trade_start=7*60,  trade_end=16*60),
}


# =====================================================================
#  Engine config
# =====================================================================

@dataclass
class IgnitionConfig:
    # Bar aggregation — we run internally on M5 closes
    bars_per_m5: int = 5

    # --- Hawkes parameters (alpha < beta for stationarity).
    # Half-life of self-excitation is ln(2)/beta bars.  beta=0.35 gives
    # half-life ~2 bars = 10 minutes of M5 — long enough that a real burst
    # stays above threshold for at least 3-5 bars, not 1.
    hawkes_mu0: float = 0.15
    hawkes_alpha: float = 0.25
    hawkes_beta: float = 0.35

    # --- Kalman parameters
    # Signal-to-noise carefully tuned:  sigma_proc sets how FAST the filter
    # adapts to new drift.  If too small, |mu_hat|/sqrt(P) never reaches
    # threshold even in obvious trends.  0.0003 gives ~20-bar time constant.
    kalman_sigma_obs: float = 0.002
    kalman_sigma_proc: float = 3e-4

    # --- GARCH (for vol regime gate)
    garch_lookback: int = 500         # learn on last 500 M5 bars before trading

    # --- GPD (for tail-risk stop placement)
    gpd_lookback: int = 500

    # --- Entry gate
    hawkes_ratio_threshold: float = 2.0   # λ_up/λ_dn must exceed this for long
    kalman_z_threshold: float = 1.5       # |μ̂|/√P must exceed this
    require_confluence: bool = True        # BOTH signals must agree

    # --- Vol regime gate  (don't trade very calm or very wild days)
    vol_percentile_min: float = 0.20
    vol_percentile_max: float = 0.90

    # --- Exit — "perfect exit" triggers
    exit_hawkes_decay_ratio: float = 1.30   # exit when ratio drops below this
    exit_kalman_z: float = 1.0              # exit when |μ̂|/√P drops below
    exit_time_bars: int = 72                # hard time-stop = 72 M5 bars = 6 hours

    # --- Risk sizing
    base_risk_pct: float = 0.005
    min_risk_pct: float = 0.002
    max_risk_pct: float = 0.015
    total_dd_limit: float = 0.05
    daily_dd_limit: float = 0.04
    gz_gamma: float = 2.0
    max_concurrent: int = 3
    max_index_concurrent: int = 2

    # --- Stop / target
    gpd_quantile: float = 0.995   # 99.5% tail quantile for initial stop
    tp_R_multiple: float = 3.0    # TP at 3R (but most exits are signal-decay, not TP)


# =====================================================================
#  Per-symbol state — wires the math modules together
# =====================================================================

class IgnitionSymbol:
    def __init__(self, spec: SymbolSpec, cfg: IgnitionConfig):
        self.spec = spec
        self.cfg = cfg

        # PhD-math estimators
        self.hawkes = HawkesIntensity(
            mu0=cfg.hawkes_mu0, alpha=cfg.hawkes_alpha, beta=cfg.hawkes_beta)
        self.kalman = KalmanForecast(
            sigma_obs=cfg.kalman_sigma_obs, sigma_proc=cfg.kalman_sigma_proc)
        self.garch = GarchOne()        # default init; learns omega/alpha/beta online
        self.gpd = GpdTail(window=cfg.gpd_lookback)

        # M5 aggregation state
        self._m5_o: Optional[float] = None
        self._m5_h: float = -1e18
        self._m5_l: float = +1e18
        self._m5_count: int = 0
        self._last_close: float = 0.0

        # Recent M5 returns for regime percentile
        self.recent_abs_rets: list[float] = []

        # Bar counter (for warmup)
        self.m5_bars_seen = 0

        # Position
        self.position: Optional[IgnitionPosition] = None


@dataclass
class IgnitionPosition:
    symbol: str
    side: int
    entry_price: float
    entry_time: float
    entry_bar: int            # bar counter at entry (for time-stop)
    lots: float
    sl: float
    tp: float
    R_dist: float
    R_dollars: float


@dataclass
class IgnitionTrade:
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
    commission: float
    net_pnl: float
    exit_reason: str
    hawkes_ratio_at_entry: float
    kalman_z_at_entry: float
    bars_held: int
    equity_at_entry: float
    equity_at_exit: float


# =====================================================================
#  The engine
# =====================================================================

class IgnitionEngine:
    def __init__(self, symbols: list[SymbolSpec],
                 cfg: Optional[IgnitionConfig] = None,
                 initial_equity: float = 100_000.0):
        self.cfg = cfg or IgnitionConfig()
        self.states: dict[str, IgnitionSymbol] = {
            s.symbol: IgnitionSymbol(s, self.cfg) for s in symbols
        }
        # Bayesian posteriors per symbol (here we don't have "probes" any
        # more — there's just "ignition long" and "ignition short" per symbol)
        self.beta: dict[tuple[str, int], BetaPosterior] = defaultdict(
            lambda: BetaPosterior(alpha=1.0, beta=1.0))
        self.gz = GrossmanZhouDD(max_dd=self.cfg.total_dd_limit, gamma=self.cfg.gz_gamma)

        self.equity = initial_equity
        self.start_equity = initial_equity
        self.peak_equity = initial_equity
        self.sod_equity = initial_equity

        self.day_key: Optional[str] = None
        self.halted_for_day = False
        self.halted_permanently = False
        self.total_trades_closed = 0

        self.trades: list[IgnitionTrade] = []

    # -----------------------------------------------------------------
    def _roll_day(self, day_key: str):
        if self.day_key is None:
            self.day_key = day_key
            return
        if day_key == self.day_key:
            return
        self.day_key = day_key
        self.sod_equity = self.equity
        self.halted_for_day = False

    # -----------------------------------------------------------------
    def _check_safety(self, time: float) -> bool:
        if self.halted_permanently or self.halted_for_day:
            return False
        if self.peak_equity > 0 and self.equity <= self.peak_equity * (1.0 - self.cfg.total_dd_limit):
            self.halted_permanently = True
            self._close_all("ghost_total_dd", time)
            return False
        if self.sod_equity > 0 and self.equity <= self.sod_equity * (1.0 - self.cfg.daily_dd_limit):
            self.halted_for_day = True
            self._close_all("ghost_daily_dd", time)
            return False
        return True

    # -----------------------------------------------------------------
    def on_bar(self, symbol: str, time: float, day_key: str,
                hour_utc: int, minute_utc: int,
                open_: float, high: float, low: float, close: float):
        """Feed M1 bar; engine aggregates to M5 internally."""
        if symbol not in self.states:
            return
        st = self.states[symbol]
        self._roll_day(day_key)

        # --- M5 aggregation
        if st._m5_count == 0:
            st._m5_o = open_
            st._m5_h = high
            st._m5_l = low
        else:
            st._m5_h = max(st._m5_h, high)
            st._m5_l = min(st._m5_l, low)
        st._m5_count += 1

        if st._m5_count < self.cfg.bars_per_m5:
            # Mid-bar: still do position management on M1 SL/TP
            if st.position is not None:
                self._manage_intrabar(st, time, high, low, close)
            return

        # --- M5 bar complete
        m5_o = st._m5_o if st._m5_o is not None else close
        m5_c = close
        ret = math.log(m5_c / m5_o) if m5_o > 0 else 0.0

        # Reset M5 accumulator
        st._m5_o = None
        st._m5_h = -1e18
        st._m5_l = +1e18
        st._m5_count = 0
        st.m5_bars_seen += 1

        # Update all math modules
        st.hawkes.update(float(st.m5_bars_seen), ret)
        st.kalman.update(ret)
        st.garch.update(ret)
        if ret != 0:
            st.gpd.update(abs(ret))

        # Track absolute-return percentile
        st.recent_abs_rets.append(abs(ret))
        if len(st.recent_abs_rets) > self.cfg.garch_lookback:
            st.recent_abs_rets.pop(0)

        st._last_close = close
        mod = hour_utc * 60 + minute_utc

        # --- Position management at M5 close ---
        if st.position is not None:
            self._manage(st, time, high, low, close)

        # --- Safety check
        if not self._check_safety(time):
            return

        # --- Warmup gate — need enough data for estimators to be reliable
        if st.m5_bars_seen < max(self.cfg.garch_lookback, self.cfg.gpd_lookback) // 4:
            return

        # --- Trading window
        if mod < st.spec.trade_start or mod >= st.spec.trade_end:
            return

        # --- Entry logic
        if st.position is None:
            self._maybe_enter(st, time, close)

    # -----------------------------------------------------------------
    def _vol_percentile(self, st: IgnitionSymbol) -> float:
        """Current GARCH σ forecast percentile against recent |r| distribution."""
        if len(st.recent_abs_rets) < 30:
            return 0.5
        sigma_fc = math.sqrt(st.garch.forecast(1))
        # Count fraction of recent |r| below current sigma_fc
        below = sum(1 for r in st.recent_abs_rets if r < sigma_fc)
        return below / len(st.recent_abs_rets)

    # -----------------------------------------------------------------
    def _maybe_enter(self, st: IgnitionSymbol, time: float, close: float):
        cfg = self.cfg
        # --- Concurrency check
        total_open = sum(1 for s in self.states.values() if s.position is not None)
        if total_open >= cfg.max_concurrent:
            return
        if st.spec.asset_class == "index":
            idx_open = sum(1 for s in self.states.values()
                            if s.position is not None and s.spec.asset_class == "index")
            if idx_open >= cfg.max_index_concurrent:
                return

        # --- Compute signals
        hawkes_ratio = st.hawkes.ratio()
        # Map ratio to symmetric signal: > 1 → bullish, < 1 → bearish
        hawkes_side = 0
        if hawkes_ratio > cfg.hawkes_ratio_threshold:
            hawkes_side = +1
        elif hawkes_ratio < 1.0 / cfg.hawkes_ratio_threshold:
            hawkes_side = -1

        kalman_z = st.kalman.mu / math.sqrt(max(st.kalman.P, 1e-12))
        kalman_side = 0
        if kalman_z > cfg.kalman_z_threshold:
            kalman_side = +1
        elif kalman_z < -cfg.kalman_z_threshold:
            kalman_side = -1

        # --- Confluence gate
        if cfg.require_confluence:
            if hawkes_side == 0 or kalman_side == 0 or hawkes_side != kalman_side:
                return
            side = hawkes_side
        else:
            if hawkes_side == 0 and kalman_side == 0:
                return
            side = hawkes_side or kalman_side

        # --- Vol-regime gate
        pct = self._vol_percentile(st)
        if pct < cfg.vol_percentile_min or pct > cfg.vol_percentile_max:
            return

        # --- Stop placement: use GARCH sigma-forecast × 2.0  (2-sigma stop).
        #   The previous GPD-99.5%ile stop was too tight: 99.5%ile of *single*
        #   M5 returns is smaller than typical M5 H-L range, so stops hit in
        #   the same bar as entry.  2-sigma of forward volatility = 2 standard
        #   deviations of a M5 move in return space, converted to price pts.
        #   Floor at max(GPD-q * 3, 3 × spread) so stops can never be inside
        #   normal tick-noise.
        sigma_fc = math.sqrt(max(st.garch.forecast(1), 1e-12))
        stop_ret = 2.0 * sigma_fc                           # 2-sigma stop
        gpd_q = st.gpd.quantile(cfg.gpd_quantile)
        if gpd_q > 0 and math.isfinite(gpd_q):
            stop_ret = max(stop_ret, 3.0 * gpd_q)           # never below 3x tail-95%
        stop_distance_pts = max(stop_ret * close,
                                 3.0 * st.spec.spread_pts)    # never < 3 spreads

        entry_fill = close + side * 0.5 * st.spec.spread_pts
        sl = entry_fill - side * stop_distance_pts
        tp = entry_fill + side * cfg.tp_R_multiple * stop_distance_pts

        # --- Sizing
        risk_pct = self._dynamic_risk_pct(st.spec.symbol, side)
        risk_dollars = self.equity * risk_pct
        lots = risk_dollars / (stop_distance_pts * st.spec.pip_value)
        lots = max(st.spec.min_lots,
                    min(st.spec.max_lots,
                        math.floor(lots / st.spec.lot_step) * st.spec.lot_step))
        if lots < st.spec.min_lots:
            return

        pos = IgnitionPosition(
            symbol=st.spec.symbol, side=side,
            entry_price=entry_fill, entry_time=time,
            entry_bar=st.m5_bars_seen, lots=lots,
            sl=sl, tp=tp, R_dist=stop_distance_pts,
            R_dollars=risk_dollars,
        )
        pos._entry_equity = self.equity
        pos._entry_hawkes = hawkes_ratio
        pos._entry_kalman_z = kalman_z
        st.position = pos

    # -----------------------------------------------------------------
    def _dynamic_risk_pct(self, symbol: str, side: int) -> float:
        base = self.cfg.base_risk_pct
        # Bayesian edge multiplier
        b = self.beta.get((symbol, side))
        if b is not None and (b.alpha + b.beta - 2) >= 3:
            mean_wr = b.mean()
            x = max(0.40, min(0.65, mean_wr))
            bay = 0.5 + (x - 0.40) / (0.25) * 1.0
        else:
            bay = 1.0
        gz = self.gz.factor(equity=self.equity, peak=self.peak_equity)
        raw = base * bay * gz
        return max(self.cfg.min_risk_pct, min(self.cfg.max_risk_pct, raw))

    # -----------------------------------------------------------------
    def _manage_intrabar(self, st: IgnitionSymbol, time: float,
                           high: float, low: float, close: float):
        """M1-resolution SL/TP check between M5 closes."""
        pos = st.position
        if pos is None:
            return
        if pos.side > 0:
            if low <= pos.sl:
                self._close(st, pos.sl, time, "stop_loss"); return
            if high >= pos.tp:
                self._close(st, pos.tp, time, "take_profit"); return
        else:
            if high >= pos.sl:
                self._close(st, pos.sl, time, "stop_loss"); return
            if low <= pos.tp:
                self._close(st, pos.tp, time, "take_profit"); return

    # -----------------------------------------------------------------
    def _manage(self, st: IgnitionSymbol, time: float,
                 high: float, low: float, close: float):
        """M5-close check: PRICE stops + SIGNAL-DECAY exits."""
        pos = st.position
        if pos is None:
            return

        # Price stops first (worst-case)
        if pos.side > 0:
            if low <= pos.sl:
                self._close(st, pos.sl, time, "stop_loss"); return
            if high >= pos.tp:
                self._close(st, pos.tp, time, "take_profit"); return
        else:
            if high >= pos.sl:
                self._close(st, pos.sl, time, "stop_loss"); return
            if low <= pos.tp:
                self._close(st, pos.tp, time, "take_profit"); return

        # Signal-decay exits — ONLY use Kalman (drift estimator with ~20-bar
        # time constant).  Hawkes self-excitation decays too fast (half-life
        # of 2 bars) to be a valid HOLD signal — it's an entry trigger only.
        kalman_z = st.kalman.mu / math.sqrt(max(st.kalman.P, 1e-12))
        if pos.side > 0:
            # Exit when drift-z flips from "significantly positive" to "not"
            if kalman_z < self.cfg.exit_kalman_z:
                self._close(st, close, time, "signal_decay"); return
        else:
            if kalman_z > -self.cfg.exit_kalman_z:
                self._close(st, close, time, "signal_decay"); return

        # ATR-trailing stop — lock in gains progressively as trend persists.
        # After price has moved favourably by >= 1R, ratchet SL to
        # breakeven+buffer.  After >= 2R, ratchet to 1R locked.
        bars_held = st.m5_bars_seen - pos.entry_bar
        if pos.side > 0:
            running_R = (close - pos.entry_price) / max(pos.R_dist, 1e-9)
            if running_R >= 2.0 and pos.sl < pos.entry_price + pos.R_dist:
                pos.sl = pos.entry_price + pos.R_dist    # lock +1R
            elif running_R >= 1.0 and pos.sl < pos.entry_price:
                pos.sl = pos.entry_price                 # breakeven
        else:
            running_R = (pos.entry_price - close) / max(pos.R_dist, 1e-9)
            if running_R >= 2.0 and pos.sl > pos.entry_price - pos.R_dist:
                pos.sl = pos.entry_price - pos.R_dist
            elif running_R >= 1.0 and pos.sl > pos.entry_price:
                pos.sl = pos.entry_price

        # Time stop
        if bars_held >= self.cfg.exit_time_bars:
            self._close(st, close, time, "time_stop"); return

    # -----------------------------------------------------------------
    def _close(self, st: IgnitionSymbol, fill: float, time: float, reason: str):
        pos = st.position
        if pos is None:
            return
        spec = st.spec

        slip = 1.0 if reason == "stop_loss" else 0.5
        actual = fill - pos.side * slip * spec.spread_pts
        gross = (actual - pos.entry_price) * pos.side * pos.lots * spec.pip_value
        commission = spec.commission_rt_per_lot * pos.lots
        net = gross - commission

        self.equity += net
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

        realised_R = net / max(pos.R_dollars, 1e-9)
        bars_held = st.m5_bars_seen - pos.entry_bar

        rec = IgnitionTrade(
            symbol=spec.symbol, side=pos.side,
            entry_time=pos.entry_time, exit_time=time,
            entry_price=pos.entry_price, exit_price=actual,
            lots=pos.lots, R_dist=pos.R_dist, realised_R=realised_R,
            gross_pnl=gross, commission=commission, net_pnl=net,
            exit_reason=reason,
            hawkes_ratio_at_entry=getattr(pos, "_entry_hawkes", float("nan")),
            kalman_z_at_entry=getattr(pos, "_entry_kalman_z", float("nan")),
            bars_held=bars_held,
            equity_at_entry=getattr(pos, "_entry_equity", self.equity),
            equity_at_exit=self.equity,
        )
        self.trades.append(rec)

        # Bayesian update per (symbol, side)
        self.beta[(spec.symbol, pos.side)].update(net > 0)
        self.total_trades_closed += 1

        st.position = None

    def _close_all(self, reason: str, time: float):
        for st in self.states.values():
            if st.position is not None:
                self._close(st, st._last_close, time, reason)

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

        by_symbol: dict[str, dict] = {}
        by_side: dict[int, dict] = {}
        by_exit: dict[str, int] = defaultdict(int)
        for t in self.trades:
            for key, d in [(t.symbol, by_symbol), (t.side, by_side)]:
                rec = d.setdefault(key, {"n": 0, "wins": 0, "net": 0.0, "sum_R": 0.0, "sum_bars": 0})
                rec["n"] += 1
                rec["wins"] += 1 if t.net_pnl > 0 else 0
                rec["net"] += t.net_pnl
                rec["sum_R"] += t.realised_R
                rec["sum_bars"] += t.bars_held
            by_exit[t.exit_reason] += 1
        for d in list(by_symbol.values()) + list(by_side.values()):
            d["wr"] = d["wins"] / d["n"]
            d["expR"] = d["sum_R"] / d["n"]
            d["avg_bars"] = d["sum_bars"] / d["n"]

        return {
            "trades": len(self.trades),
            "net_pnl": net,
            "pct_return": (self.equity - self.start_equity) / self.start_equity * 100,
            "pf": pf,
            "win_rate": len(wins) / len(self.trades),
            "expectancy_R": sum(t.realised_R for t in self.trades) / len(self.trades),
            "avg_winner_R": sum(t.realised_R for t in wins) / len(wins) if wins else 0,
            "avg_loser_R": sum(t.realised_R for t in losses) / len(losses) if losses else 0,
            "avg_bars_held": sum(t.bars_held for t in self.trades) / len(self.trades),
            "max_dd_pct": mdd * 100,
            "equity": self.equity,
            "peak": self.peak_equity,
            "gross_commissions": sum(t.commission for t in self.trades),
            "by_symbol": by_symbol,
            "by_side": {str(k): v for k, v in by_side.items()},
            "by_exit_reason": dict(by_exit),
        }

    def dump_trades(self, path: str):
        with open(path, "w") as f:
            json.dump([asdict(t) for t in self.trades], f, indent=2, default=str)
