"""
MomentumEngine - SHF v7 single-leg directional momentum scalper.

Per-instrument state:
    * KalmanForecast   drift posterior
    * CUSUMDetector    change-point detector
    * HawkesIntensity  self-exciting burst signal
    * HMMRegimeDetector (reused from strategies/hmm_regime.py) - regime gate
    * EVTGarchStop     dynamic tail-aware stop
    * OptimalStopper   Shiryaev exit override

Shared-account state:
    * BayesianSizer    posterior-driven lot sizing + GZ DD + CVaR
    * Supervisor       ghost stops (unchanged, 1:1 with v5/v6)

Primary usage surface is `EngineConfig` + `MomentumEngine.on_bar(...)`.
Backtest scripts drive the engine bar-by-bar; the live engine drives it
from the MT5 bridge feed.

This module is intentionally free of MT5 / bridge imports so it can run
purely in a simulation harness (backtest, paper parity, Monte Carlo).
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

from src.momentum import (
    KalmanForecast, CUSUMDetector, HawkesIntensity, OptimalStopper,
    EVTGarchStop, BayesianSizer,
    ORBConfig, ORB_DEFAULTS, OpeningRangeTracker, NRFilter,
)
from src.momentum.sizer import SizingDecision
from src.strategies.hmm_regime import HMMRegimeDetector, create_regime_detector



logger = logging.getLogger(__name__)


# ======================================================================
#                          Per-symbol config
# ======================================================================

@dataclass
class SymbolConfig:
    symbol: str
    pip_value: float          # $ per 1.0 point per 1.0 lot  (NAS100=1.0, DAX=1.0, XAU=100.0)
    contract_size: float      # lots -> units (NAS100/DAX 1.0, XAU 100.0 oz)
    spread_pts: float         # realistic median spread in price units (NOT pips)
    commission_per_lot: float = 0.0         # commission-free basket default
    swap_long_pts_per_day: float = 0.0      # overnight swap in price units per lot
    swap_short_pts_per_day: float = 0.0
    min_lots: float = 0.01
    lot_step: float = 0.01
    max_lots: float = 50.0
    session_start_hour: int = 0             # UTC hours (inclusive)
    session_end_hour: int = 24              # UTC hours (exclusive)


# ======================================================================
#                          Engine-wide config
# ======================================================================

@dataclass
class EngineConfig:
    # Kalman
    kalman_sigma_obs: float = 5e-4
    kalman_sigma_proc: float = 1e-6
    kalman_tau: float = 1.96         # signal z threshold
    kalman_p_crit: float = 1e-3

    # CUSUM
    cusum_k: float = 0.5
    cusum_h: float = 5.0             # masterplan-tuned: 5-10 fires/day/symbol (high-quality)

    # Hawkes
    hawkes_mu0: float = 0.1
    hawkes_alpha: float = 0.4
    hawkes_beta: float = 1.0
    hawkes_threshold: float = 2.0

    # Optimal stopper
    optstop_r_min: float = 1.0

    # Stop quantiles
    entry_alpha: float = 0.005      # 99.5% survival
    trail_alpha: float = 0.10       # 90% survival

    # Sizer gates
    max_concurrent_positions: int = 3
    max_total_risk_frac: float = 0.03
    max_trades_per_symbol_per_day: int = 20
    max_trades_account_per_day: int = 40

    # Time stop
    max_hold_minutes: int = 90
    stale_r_threshold: float = 0.3

    # Standardisation window for CUSUM input
    standardise_window: int = 500

    # Avg R priors (for Kelly payout); updated by BayesianEdge over time
    avg_win_R: float = 1.7
    avg_loss_R: float = 1.0

    # TP ladder (in R-multiples)
    tp1_R: float = 1.0
    tp2_R: float = 2.0
    tp3_R: float = 3.5

    # Minimum stop distance as fraction of price (safety net, 5bp default)
    min_stop_pct: float = 5e-4

    # ---- Strategy mode (masterplan §6.4) -----------------------------------
    # False = "continuation" (classic CUSUM-momentum):
    #         enter in the direction of the CUSUM fire
    # True  = "fade"  (mean-reversion at intraday exhaustion):
    #         enter OPPOSITE the CUSUM fire, using Kalman drift OPPOSING the
    #         fire as the confluence filter.  Diagnostics on Q4-2025 / Q1-2026
    #         NAS100/DAX/XAU M1 showed that CUSUM fires on M1 index data are
    #         far more often exhaustion than continuation — Kalman-aligned
    #         entries averaged -0.22R, opposed entries averaged +0.03R,
    #         so fading with confluence is where the edge lives.
    fade_mode: bool = False

    # Only trade when |Kalman drift| > this threshold (bps per bar).
    # Forces us to wait for a strong, persistent drift before fading.
    min_abs_kalman_mu: float = 8e-5

    # Entry gate: require both Kalman AND Hawkes confluence?
    # Strict mode kills many marginal trades but raises win-rate substantially.
    require_strict_confluence: bool = True

    # ---- Strategy selector (v7.1) -----------------------------------------
    # "cusum" (legacy v7.0) = CUSUM-driven momentum, proven negative expectancy
    #                         on M1 indices/gold — kept for comparison only.
    # "orb"   (v7.1)         = Opening-Range Breakout at session open,
    #                         canonical Crabel/Zarattini intraday edge.
    strategy_mode: str = "cusum"

    # ---- ORB parameters (used only when strategy_mode == "orb") ------------
    orb_require_nr: bool = False        # require yesterday to be narrow-range
    orb_nr_lookback_n: int = 7          # NR7 by default (Crabel 1990)
    orb_require_kalman_agree: bool = True   # veto if drift opposes break direction
    orb_require_trend_regime: bool = True   # veto if HMM says chop (regime == 2)
    orb_tp1_R: float = 1.0              # TP1 in R-multiples (EVT-GARCH R, same as CUSUM)
    orb_tp2_R: float = 2.0              # TP2 in R-multiples
    orb_max_hold_minutes: int = 180     # session-open moves typically resolve in < 3h


# ======================================================================
#                          Position state
# ======================================================================


@dataclass
class Position:
    symbol: str
    side: int                 # +1 long, -1 short
    entry_price: float
    entry_time: float         # epoch seconds (or bar index)
    lots: float
    initial_sl: float
    initial_R: float          # |entry - SL| in price units
    sl: float
    tp1_hit: bool = False
    tp2_hit: bool = False
    remaining_fraction: float = 1.0
    max_favorable: float = 0.0   # highest (side-adjusted) price since entry
    realised_pnl: float = 0.0    # from partial closes


# ======================================================================
#                          Per-symbol runtime state
# ======================================================================

class InstrumentState:
    """Per-symbol kernel bundle + open position."""

    def __init__(self, cfg: SymbolConfig, eng: EngineConfig):
        self.cfg = cfg
        self.kalman = KalmanForecast(eng.kalman_sigma_obs, eng.kalman_sigma_proc)
        self.cusum = CUSUMDetector(eng.cusum_k, eng.cusum_h)
        self.hawkes = HawkesIntensity(eng.hawkes_mu0, eng.hawkes_alpha, eng.hawkes_beta)
        self.stop = EVTGarchStop()
        self.optstop = OptimalStopper(eng.optstop_r_min)
        self.hmm: HMMRegimeDetector = create_regime_detector(
            n_regimes=3, lookback=100, min_regime_hold=20
        )

        # Standardisation state for CUSUM
        self._std_buf: list[float] = []

        # Previous close for returns
        self._prev_close: Optional[float] = None
        self.last_close: float = 0.0

        # Open position
        self.position: Optional[Position] = None

        # Counts
        self.trades_today = 0

        # ORB / NR state (only populated when strategy_mode == "orb")
        orb_cfg = ORB_DEFAULTS.get(cfg.symbol)
        self.orb: Optional[OpeningRangeTracker] = (
            OpeningRangeTracker(orb_cfg) if orb_cfg is not None else None
        )
        self.nr = NRFilter(lookback=20)



# ======================================================================
#                          Trade log record
# ======================================================================

@dataclass
class TradeRecord:
    symbol: str
    side: int
    entry_time: float
    exit_time: float
    entry_price: float
    exit_price: float
    lots: float
    sl_price: float
    R_distance: float         # price-units
    realised_R: float
    gross_pnl: float
    commission: float
    spread_cost: float
    swap_cost: float
    net_pnl: float
    exit_reason: str
    # Decision breakdown
    risk_fraction: float
    conviction: float
    gz_factor: float
    cvar_factor: float
    kalman_mu: float
    kalman_P: float
    cusum_s: float
    hawkes_ratio: float
    regime: int
    equity_at_entry: float
    equity_at_exit: float


# ======================================================================
#                          Main engine
# ======================================================================

class MomentumEngine:
    """
    Pure-Python reference implementation of the v7 momentum scalper.

    Drives per-symbol InstrumentState on every M1 bar close.  Decision / lot
    sizing delegated to BayesianSizer.  No direct MT5 / socket imports - the
    caller supplies a fill-executor callback for live deployments, or the
    backtest harness calls `on_bar` in simulation.
    """

    def __init__(self,
                 symbols: list[SymbolConfig],
                 cfg: Optional[EngineConfig] = None,
                 initial_equity: float = 5000.0,
                 daily_dd_limit: float = 0.04,      # 4% default  (5%ers MTB: 5%)
                 total_dd_limit: float = 0.09,      # 9% default  (5%ers MTB: 6%)
                 rng_seed: int = 0):
        self.cfg = cfg or EngineConfig()
        self.states: dict[str, InstrumentState] = {
            sc.symbol: InstrumentState(sc, self.cfg) for sc in symbols
        }
        self.sizer = BayesianSizer(
            max_lots=max(sc.max_lots for sc in symbols),
            lot_step=min(sc.lot_step for sc in symbols),
            min_lots=min(sc.min_lots for sc in symbols),
        )
        # Override sizer GZ with matching DD limit
        from src.momentum.kelly import GrossmanZhouDD
        self.sizer._gz = GrossmanZhouDD(max_dd=total_dd_limit, gamma=2.0)

        self.equity = initial_equity
        self.start_equity = initial_equity
        self.peak_equity = initial_equity
        self.sod_equity = initial_equity                # start-of-day equity
        self.sizer.mark_equity(initial_equity)

        self.daily_dd_limit = daily_dd_limit
        self.total_dd_limit = total_dd_limit

        # State flags
        self.halted_for_day = False
        self.halted_permanently = False
        self.day_key: Optional[str] = None
        self.trades_account_today = 0

        # Trade log
        self.trades: list[TradeRecord] = []

    # ==================================================================
    #  Day roll helper
    # ==================================================================
    def _roll_day_if_needed(self, bar_day_key: str) -> None:
        if self.day_key is None:
            self.day_key = bar_day_key
            return
        if bar_day_key != self.day_key:
            # New day
            self.day_key = bar_day_key
            self.sod_equity = self.equity
            self.halted_for_day = False
            self.trades_account_today = 0
            for st in self.states.values():
                st.trades_today = 0

    # ==================================================================
    #  Account DD check (ghost stops)
    # ==================================================================
    def _check_ghost_stops(self, current_time: float) -> bool:
        """Returns True if trading is still allowed, False if halted."""
        if self.halted_permanently:
            return False
        if self.halted_for_day:
            return False
        # Permanent DD
        if self.peak_equity > 0 and \
           self.equity <= self.peak_equity * (1.0 - self.total_dd_limit):
            logger.warning("v7 GHOST: total DD hit - halting permanently")
            self.halted_permanently = True
            self._close_all_positions("ghost_total_dd", current_time)
            return False
        # Daily DD
        if self.sod_equity > 0 and \
           self.equity <= self.sod_equity * (1.0 - self.daily_dd_limit):
            logger.warning("v7 GHOST: daily DD hit - halting for day")
            self.halted_for_day = True
            self._close_all_positions("ghost_daily_dd", current_time)
            return False
        return True

    # ==================================================================
    #  Position management helpers
    # ==================================================================
    def _open_count(self) -> int:
        return sum(1 for s in self.states.values() if s.position is not None)

    def _open_risk_fraction(self) -> float:
        if self.equity <= 0:
            return 0.0
        total = 0.0
        for st in self.states.values():
            pos = st.position
            if pos is None:
                continue
            # risk $ = lots * R_distance * pip_value * remaining_fraction
            risk = (pos.lots * pos.initial_R * st.cfg.pip_value
                    * pos.remaining_fraction)
            total += risk
        return total / self.equity

    # ==================================================================
    #  Market bar processing
    # ==================================================================
    def on_bar(self, symbol: str, time: float, day_key: str,
               hour_utc: int,
               open_: float, high: float, low: float, close: float,
               minute_utc: int = 0) -> None:
        """
        Drive one M1 bar close for one instrument.

        `day_key` should be a sortable daily identifier (YYYY-MM-DD).
        `time` must be monotonically non-decreasing across calls.
        `minute_utc` is the minute-of-hour of the bar (0-59); required for
        ORB strategy mode where opening-range boundaries sit mid-hour
        (e.g. NY cash open 14:30 UTC).  Defaults to 0 so legacy callers
        still work in CUSUM mode (which only uses hour granularity).
        """
        if symbol not in self.states:
            return
        st = self.states[symbol]
        self._roll_day_if_needed(day_key)

        # --- update kernels -----------------------------------------
        if st._prev_close is None:
            st._prev_close = close
            st.last_close = close
            # ORB / NR trackers still need seeding on the very first bar
            if st.orb is not None:
                st.orb.update(day_key, hour_utc, minute_utc, high, low)
            st.nr.update(day_key, high, low)
            return
        ret = math.log(close / st._prev_close) if st._prev_close > 0 else 0.0
        st._prev_close = close
        st.last_close = close

        # Kalman
        st.kalman.update(ret)
        # Hawkes
        st.hawkes.update(time, ret)
        # GARCH-EVT
        st.stop.update(close, ret, high, low)
        # HMM regime (uses returns)
        regime = st.hmm.update(ret)

        # ORB / NR trackers (cheap O(1))
        if st.orb is not None:
            st.orb.update(day_key, hour_utc, minute_utc, high, low)
        st.nr.update(day_key, high, low)

        # Standardised return for CUSUM
        st._std_buf.append(ret)
        if len(st._std_buf) > self.cfg.standardise_window:
            st._std_buf.pop(0)
        if len(st._std_buf) < 30:
            return
        mu = sum(st._std_buf) / len(st._std_buf)
        var = sum((x - mu) ** 2 for x in st._std_buf) / len(st._std_buf)
        sigma = math.sqrt(max(var, 1e-18))
        z = (ret - mu) / sigma if sigma > 0 else 0.0
        cusum_fire = st.cusum.update(z)

        # --- manage open position first -----------------------------
        if st.position is not None:
            self._manage_position(st, time, high, low, close, regime,
                                  cusum_fire)

        # --- check ghost stops --------------------------------------
        if not self._check_ghost_stops(time):
            return

        # --- try to open new position -------------------------------
        if st.position is None:
            self._try_open(st, time, hour_utc, close, regime, cusum_fire,
                            minute_utc=minute_utc, bar_high=high, bar_low=low)


    # ==================================================================
    def _try_open(self, st: InstrumentState, time: float, hour_utc: int,
                  close: float, regime: int, cusum_fire: int,
                  minute_utc: int = 0,
                  bar_high: Optional[float] = None,
                  bar_low: Optional[float] = None) -> None:
        cfg = st.cfg
        if regime == 2:                                       # block choppy
            return
        # For CUSUM mode we keep the legacy per-symbol session guard; for
        # ORB mode the ORBConfig's (hour, minute, window) guard is stricter
        # so we skip the coarse per-hour guard and let ORB do the gating.
        if self.cfg.strategy_mode == "cusum":
            if hour_utc < cfg.session_start_hour or hour_utc >= cfg.session_end_hour:
                return
        if st.trades_today >= self.cfg.max_trades_per_symbol_per_day:
            return
        if self.trades_account_today >= self.cfg.max_trades_account_per_day:
            return
        if self._open_count() >= self.cfg.max_concurrent_positions:
            return
        if self._open_risk_fraction() >= self.cfg.max_total_risk_frac:
            return

        # ==========================================================
        #  ENTRY TRIGGER
        # ==========================================================
        #
        # Two strategies coexist here.  `strategy_mode == "cusum"` is the
        # legacy v7.0 CUSUM change-point trigger (kept for A/B comparison;
        # proven negative expectancy on M1 indices/gold).  `"orb"` is the
        # v7.1 Opening Range Breakout trigger — the documented edge.
        #
        side: int = 0
        entry_ref: float = close      # the reference price used for the fill
        trigger_source: str = ""

        if self.cfg.strategy_mode == "orb":
            # ---------- ORB path ----------
            if st.orb is None:
                return
            if not st.orb.in_trade_window(hour_utc, minute_utc):
                return
            if bar_high is None or bar_low is None:
                return
            breakout = st.orb.detect_breakout(bar_high, bar_low, close)
            if breakout == 0:
                return

            # Optional Narrow-Range filter (Crabel)
            if self.cfg.orb_require_nr:
                if not st.nr.is_prev_day_narrow(self.cfg.orb_nr_lookback_n):
                    return

            # Kalman-agree filter: drift must not oppose the break
            if self.cfg.orb_require_kalman_agree:
                kalman_agrees = (
                    (breakout > 0 and st.kalman.mu > 0) or
                    (breakout < 0 and st.kalman.mu < 0)
                )
                if not kalman_agrees:
                    return

            # Trend-regime filter (HMM regime 2 is chop; already vetoed above)
            # Reject regime == 1 too if caller wants trend-only?  We keep 1
            # (medium-trend) because the OR break itself is the trend proof.

            side = breakout
            # Entry reference: worst-case fill at OR edge.  If the bar closed
            # well beyond OR (gap), use the close; otherwise use the OR edge
            # itself (a stop-buy would have filled there).
            if side > 0:
                entry_ref = max(close, st.orb.or_high if st.orb.or_high else close)
            else:
                entry_ref = min(close, st.orb.or_low if st.orb.or_low else close)
            trigger_source = "orb"

        else:
            # ---------- CUSUM legacy path (unchanged) ----------
            if cusum_fire == 0:
                return

            if self.cfg.fade_mode:
                if cusum_fire > 0 and st.kalman.mu > -self.cfg.min_abs_kalman_mu:
                    return
                if cusum_fire < 0 and st.kalman.mu < self.cfg.min_abs_kalman_mu:
                    return
                side = -cusum_fire
            else:
                k_sig = st.kalman.signal(self.cfg.kalman_tau, self.cfg.kalman_p_crit)
                h_sig = st.hawkes.signal(self.cfg.hawkes_threshold)
                if k_sig != 0 and k_sig != cusum_fire:
                    return
                if h_sig != 0 and h_sig != cusum_fire:
                    return
                kalman_sign_agrees = (
                    (cusum_fire > 0 and st.kalman.mu > 0) or
                    (cusum_fire < 0 and st.kalman.mu < 0)
                )
                hawkes_agrees = (h_sig == cusum_fire)
                if self.cfg.require_strict_confluence:
                    if not (kalman_sign_agrees and hawkes_agrees):
                        return
                else:
                    if not (kalman_sign_agrees or hawkes_agrees):
                        return
                side = cusum_fire
            entry_ref = close
            trigger_source = "cusum"

        # ==========================================================
        #  STOP + SIZE  (common to both triggers)
        # ==========================================================

        # EVT-GARCH stop distance anchored on the entry reference
        stop_dist = st.stop.entry_stop_distance(entry_ref, regime, self.cfg.entry_alpha)
        stop_dist = max(stop_dist, entry_ref * self.cfg.min_stop_pct)

        # ORB may widen the stop to the opposite OR edge (stop sits on the
        # losing side of the opening range — a clean technical invalidation).
        if self.cfg.strategy_mode == "orb" and st.orb is not None:
            if side > 0 and st.orb.or_low is not None:
                or_stop_dist = max(0.0, entry_ref - st.orb.or_low)
                stop_dist = max(stop_dist, or_stop_dist)
            elif side < 0 and st.orb.or_high is not None:
                or_stop_dist = max(0.0, st.orb.or_high - entry_ref)
                stop_dist = max(stop_dist, or_stop_dist)

        sl = entry_ref - side * stop_dist

        # Conviction — recipe depends on trigger
        c_kalman = st.kalman.confidence()
        c_regime = 1.0 if regime == 0 else (0.7 if regime == 1 else 0.0)
        if self.cfg.strategy_mode == "orb" and st.orb is not None:
            # Break strength: how far beyond OR edge did this bar print?
            or_range = st.orb.or_range
            if side > 0:
                break_dist = max(0.0, bar_high - st.orb.or_high)
            else:
                break_dist = max(0.0, st.orb.or_low - bar_low)
            c_break = min(1.0, break_dist / or_range) if or_range > 0 else 0.5
            c_nr = 1.0 if st.nr.is_prev_day_narrow(7) else 0.5
            conv = 0.25 * (c_kalman + c_regime + c_break + c_nr)
        else:
            c_cusum = st.cusum.confidence()
            c_hawkes = st.hawkes.confidence()
            conv = 0.25 * (c_kalman + c_cusum + c_hawkes + c_regime)
        conv = max(0.15, min(1.0, conv))

        # Spread check (skip trap)
        if cfg.spread_pts > 1.5 * stop_dist:
            return

        # Size
        dec = self.sizer.decide(
            equity=self.equity,
            conviction=conv,
            stop_distance=stop_dist,
            pip_value=cfg.pip_value,
            avg_win_R=self.cfg.avg_win_R,
            avg_loss_R=self.cfg.avg_loss_R,
        )
        lots = max(cfg.min_lots, min(cfg.max_lots,
                                     math.floor(dec.lots / cfg.lot_step) * cfg.lot_step))
        if lots < cfg.min_lots:
            return

        # Entry slippage = 0.5 * spread (stop-buy fills aren't perfect)
        slip = 0.5 * cfg.spread_pts
        fill_price = entry_ref + side * slip

        pos = Position(
            symbol=cfg.symbol,
            side=side,
            entry_price=fill_price,
            entry_time=time,
            lots=lots,
            initial_sl=sl,
            initial_R=stop_dist,
            sl=sl,
            max_favorable=fill_price,
        )
        st.position = pos
        st.trades_today += 1
        self.trades_account_today += 1
        st.optstop.arm(st.kalman.mu)

        # Store decision context for exit-time trade record
        pos._entry_decision = dec                      # type: ignore[attr-defined]
        pos._entry_kalman_mu = st.kalman.mu            # type: ignore[attr-defined]
        pos._entry_kalman_P = st.kalman.P              # type: ignore[attr-defined]
        pos._entry_cusum_s = max(st.cusum.s_plus, st.cusum.s_minus)  # type: ignore[attr-defined]
        pos._entry_hawkes_ratio = st.hawkes.ratio()    # type: ignore[attr-defined]
        pos._entry_regime = regime                     # type: ignore[attr-defined]
        pos._entry_equity = self.equity                # type: ignore[attr-defined]
        pos._trigger_source = trigger_source           # type: ignore[attr-defined]


    # ==================================================================
    def _manage_position(self, st: InstrumentState, time: float,
                         high: float, low: float, close: float,
                         regime: int, cusum_fire: int) -> None:
        pos = st.position
        assert pos is not None
        cfg = st.cfg

        # Update max-favorable (in side-adjusted sense)
        if pos.side > 0:
            pos.max_favorable = max(pos.max_favorable, high)
        else:
            pos.max_favorable = min(pos.max_favorable, low)

        # ----- Hard SL check (first, worst case) -----
        if pos.side > 0 and low <= pos.sl:
            self._close_position(st, time, pos.sl, "stop_loss")
            return
        if pos.side < 0 and high >= pos.sl:
            self._close_position(st, time, pos.sl, "stop_loss")
            return

        # Current unrealised R-multiple (at close for management decisions)
        unreal_R = ((close - pos.entry_price) * pos.side) / pos.initial_R

        # ----- TP ladder -----
        if not pos.tp1_hit and unreal_R >= self.cfg.tp1_R:
            # Close 33% at the tp1 price (= entry + tp1_R * R)
            tp1_price = pos.entry_price + pos.side * self.cfg.tp1_R * pos.initial_R
            self._partial_close(st, pos, tp1_price, 1.0 / 3.0, time)
            pos.tp1_hit = True
            # Move SL to breakeven
            pos.sl = pos.entry_price

        if pos.tp1_hit and not pos.tp2_hit and unreal_R >= self.cfg.tp2_R:
            tp2_price = pos.entry_price + pos.side * self.cfg.tp2_R * pos.initial_R
            self._partial_close(st, pos, tp2_price, 1.0 / 3.0, time)
            pos.tp2_hit = True
            # Move SL to +1R
            pos.sl = pos.entry_price + pos.side * self.cfg.tp1_R * pos.initial_R

        # ----- Trailing stop (EVT-GARCH) after TP2 -----
        if pos.tp2_hit:
            trail_dist = st.stop.trail_distance(close, regime, self.cfg.trail_alpha)
            if pos.side > 0:
                new_sl = pos.max_favorable - trail_dist
                if new_sl > pos.sl:
                    pos.sl = new_sl
            else:
                new_sl = pos.max_favorable + trail_dist
                if new_sl < pos.sl:
                    pos.sl = new_sl

        # ----- Optimal-stopping override (Shiryaev) -----
        # Only active AFTER tp1 has been booked — otherwise the Kalman drift
        # posterior's high-frequency sign flips would exit pre-maturely before
        # the ladder has had a chance to print +1R.
        if pos.tp1_hit and st.optstop.should_exit(st.kalman.mu, unreal_R):
            self._close_position(st, time, close, "optstop")
            return

        # ----- Opposite CUSUM fire -> close only with confirmation -----
        # Early diagnostic on MTB data showed 188 naked "cusum_flip" exits
        # averaging -0.20R, crushing PF despite +1.17R optstop exits.  The fix
        # is to demand that Kalman drift ALSO opposes the position — i.e. the
        # same 2-of-3 confluence we use for entry — before we kill a trade.
        # If we're already past TP1 the trailing stop handles it.
        if (
            cusum_fire != 0
            and cusum_fire != pos.side
            and not pos.tp1_hit
        ):
            kalman_opposes = (
                (pos.side > 0 and st.kalman.mu < 0) or
                (pos.side < 0 and st.kalman.mu > 0)
            )
            # Only exit on confirmed opposite momentum AND non-trivial loss
            if kalman_opposes and unreal_R < -0.3:
                self._close_position(st, time, close, "cusum_flip")
                return

        # ----- Time stop -----
        if (time - pos.entry_time) / 60.0 >= self.cfg.max_hold_minutes \
                and unreal_R < self.cfg.stale_r_threshold:
            self._close_position(st, time, close, "time_stop")
            return

    # ==================================================================
    def _partial_close(self, st: InstrumentState, pos: Position,
                       fill_price: float, fraction: float, time: float) -> None:
        """Close `fraction` of remaining_fraction of the position."""
        cfg = st.cfg
        close_frac = fraction       # fraction of the ORIGINAL position
        if close_frac <= 0:
            return
        actual_lots = pos.lots * close_frac
        # Slippage: half-spread
        fill_price = fill_price - pos.side * 0.5 * cfg.spread_pts
        gross = (fill_price - pos.entry_price) * pos.side * actual_lots * cfg.pip_value
        # Commission per side
        commission = cfg.commission_per_lot * actual_lots
        # Spread cost already baked into entry/exit slippage; count it once here
        spread_cost = cfg.spread_pts * actual_lots * cfg.pip_value
        pos.realised_pnl += gross - commission
        pos.remaining_fraction -= close_frac
        # Equity kept tracked separately via full close; for partials, update equity now:
        self.equity += gross - commission
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
            self.sizer.mark_equity(self.equity)

    # ==================================================================
    def _close_position(self, st: InstrumentState, time: float,
                         fill_price: float, reason: str) -> None:
        pos = st.position
        if pos is None:
            return
        cfg = st.cfg
        # For stop-out reason: exit slippage = 1.0 * spread (conservative)
        slip_mult = 1.0 if reason == "stop_loss" else 0.5
        exit_slip = slip_mult * cfg.spread_pts
        actual_fill = fill_price - pos.side * exit_slip

        actual_lots = pos.lots * pos.remaining_fraction
        gross = (actual_fill - pos.entry_price) * pos.side * actual_lots * cfg.pip_value
        commission = cfg.commission_per_lot * actual_lots
        # Swap: approximate as swap_pts * days_held * lots
        hold_days = max(0.0, (time - pos.entry_time) / 86400.0)
        swap_rate = cfg.swap_long_pts_per_day if pos.side > 0 else cfg.swap_short_pts_per_day
        swap_cost = swap_rate * hold_days * pos.lots * cfg.pip_value
        # Spread cost already in slippage accounting
        spread_cost = cfg.spread_pts * actual_lots * cfg.pip_value

        net = gross + pos.realised_pnl - commission - swap_cost
        self.equity += gross - commission - swap_cost

        if self.equity > self.peak_equity:
            self.peak_equity = self.equity

        # realised R = total PnL in R-units
        total_risk_dollars = pos.initial_R * pos.lots * cfg.pip_value
        realised_R = (gross + pos.realised_pnl - commission - swap_cost) / max(
            total_risk_dollars, 1e-9)

        # Record
        dec: SizingDecision = getattr(pos, "_entry_decision", None)
        rec = TradeRecord(
            symbol=cfg.symbol,
            side=pos.side,
            entry_time=pos.entry_time,
            exit_time=time,
            entry_price=pos.entry_price,
            exit_price=actual_fill,
            lots=pos.lots,
            sl_price=pos.initial_sl,
            R_distance=pos.initial_R,
            realised_R=realised_R,
            gross_pnl=gross + pos.realised_pnl,
            commission=commission,
            spread_cost=spread_cost,
            swap_cost=swap_cost,
            net_pnl=net,
            exit_reason=reason,
            risk_fraction=dec.risk_fraction if dec else 0.0,
            conviction=dec.conviction if dec else 0.0,
            gz_factor=dec.gz_factor if dec else 0.0,
            cvar_factor=dec.cvar_factor if dec else 0.0,
            kalman_mu=getattr(pos, "_entry_kalman_mu", 0.0),
            kalman_P=getattr(pos, "_entry_kalman_P", 0.0),
            cusum_s=getattr(pos, "_entry_cusum_s", 0.0),
            hawkes_ratio=getattr(pos, "_entry_hawkes_ratio", 0.0),
            regime=getattr(pos, "_entry_regime", 0),
            equity_at_entry=getattr(pos, "_entry_equity", self.equity),
            equity_at_exit=self.equity,
        )
        self.trades.append(rec)
        self.sizer.record_trade(realised_R, self.equity)
        st.position = None
        st.optstop.disarm()

    # ==================================================================
    def _close_all_positions(self, reason: str, time: float) -> None:
        for st in self.states.values():
            if st.position is not None:
                # force-close at last close price
                self._close_position(st, time, st.last_close, reason)

    # ==================================================================
    #  Summary helpers (for backtest reporting)
    # ==================================================================
    def summary(self) -> dict:
        if not self.trades:
            return {"trades": 0, "net_pnl": 0, "pf": 0, "win_rate": 0,
                    "equity": self.equity, "peak": self.peak_equity}
        wins = [t for t in self.trades if t.net_pnl > 0]
        losses = [t for t in self.trades if t.net_pnl <= 0]
        gross_win = sum(t.net_pnl for t in wins)
        gross_loss = -sum(t.net_pnl for t in losses)
        pf = gross_win / gross_loss if gross_loss > 0 else float("inf")
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
        return {
            "trades": len(self.trades),
            "net_pnl": net,
            "pct_return": (self.equity - self.start_equity) / self.start_equity * 100,
            "pf": pf,
            "win_rate": len(wins) / len(self.trades),
            "avg_winner_R": (sum(t.realised_R for t in wins) / len(wins)) if wins else 0,
            "avg_loser_R": (sum(t.realised_R for t in losses) / len(losses)) if losses else 0,
            "expectancy_R": sum(t.realised_R for t in self.trades) / len(self.trades),
            "max_dd_pct": mdd * 100,
            "equity": self.equity,
            "peak": self.peak_equity,
            "gross_costs": sum(t.commission + t.swap_cost + t.spread_cost
                               for t in self.trades),
            "by_symbol": {
                sym: sum(1 for t in self.trades if t.symbol == sym)
                for sym in self.states
            },
        }

    def dump_trades(self, path: str) -> None:
        rows = [asdict(t) for t in self.trades]
        with open(path, "w") as f:
            json.dump(rows, f, indent=2, default=str)
