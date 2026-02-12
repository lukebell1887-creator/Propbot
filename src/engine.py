"""SHF Trading Engine v5.6.3 - Oil + Index Duo"""

import asyncio
import logging
import math
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import json
from pathlib import Path
import signal
import sys
import numpy as np

from src.risk.supervisor import RiskSupervisor, RiskAction, calculate_position_size
from src.risk.akad_risk import AKADRiskManager, DynamicAKAD
from src.execution.mt5_bridge import (
    MT5Bridge, OrderRequest, OrderType, Position, BridgeTimeoutError, ServerTimeInfo
)

# Try importing Rust modules — fall back gracefully
try:
    from shf_core import CointegrationEngine, KalmanSentinel, AKADRiskCalculator, CorrelationRiskMonitor
    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

# Try importing HMM (Numba JIT)
try:
    from src.strategies.hmm_regime import HMMRegimeDetector, create_regime_detector
    HMM_AVAILABLE = True
except ImportError:
    HMM_AVAILABLE = False

# Configure logging
Path('logs').mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('logs/trading.log')
    ]
)
logger = logging.getLogger(__name__)


# =============================================================================
# PAIR CONFIGURATION
# =============================================================================

@dataclass
class PairConfig:
    """Configuration for a trading pair."""
    name: str
    symbol_a: str       # Long leg (preferred name)
    symbol_b: str       # Short leg (preferred name)
    aliases_a: tuple = ()  # Alternative broker names for symbol A
    aliases_b: tuple = ()  # Alternative broker names for symbol B
    static_beta: float = 1.0
    beta_tolerance: float = 0.15
    pair_index: int = 0  # For CorrelationRiskMonitor
    max_spread_a: float = 50.0   # Max allowed spread (points) for symbol A
    max_spread_b: float = 50.0   # Max allowed spread (points) for symbol B
    hmm_min_hold: int = 100      # Per-pair HMM min regime hold (bars before regime can change)
    # Per-pair dwell parameters (oil needs raised dwell to eliminate bid-ask bounce)
    dwell_base: float = 60.0     # Base dwell seconds (60 for index, 1800 for oil)
    dwell_anchor: float = 0.3    # Hurst anchor value
    dwell_min: float = 30.0      # Floor seconds
    dwell_max: float = 300.0     # Ceiling seconds
    # Resolved at runtime (actual broker names)
    resolved_a: str = ""
    resolved_b: str = ""


# v5.6.3 DUO — Oil + Index (forex pairs dropped: costs eat the edge)
# Per-pair HMM hold + Per-pair dwell (physics-based from Hurst):
#   Index (H=0.585, trending): hmm=20, dwell=60s base (2-bar holds fine)
#   Oil (H~0.5, fast MR):     hmm=5,  dwell=1800s base (eliminate bid-ask bounce)
HOLY_TRIO: List[PairConfig] = [
    PairConfig(name="Index Spread",    symbol_a="US100",  symbol_b="DE40",   pair_index=0,
               aliases_a=("NAS100","USTEC","US100.cash","NAS100.cash","USTEC.cash"),
               aliases_b=("DAX40","GER40","DE40.cash","DAX40.cash","GER40.cash"),
               max_spread_a=200.0, max_spread_b=200.0,
               hmm_min_hold=20,    # HMM=20 — best from sweep
               dwell_base=60.0, dwell_anchor=0.3, dwell_min=30.0, dwell_max=300.0),
    PairConfig(name="Oil Spread",      symbol_a="XTIUSD", symbol_b="XBRUSD", pair_index=1,
               aliases_a=("XTIUSD","WTI","USOIL","CrudeOIL","USOILm","WTIm","XTIUSD.","OIL.WTI"),
               aliases_b=("XBRUSD","BRENT","UKOIL","BrentOIL","UKOILm","BRNm","XBRUSD.","OIL.BRENT"),
               max_spread_a=150.0, max_spread_b=150.0,
               hmm_min_hold=5,     # HMM=5 — best from sweep
               dwell_base=1800.0, dwell_anchor=0.3, dwell_min=900.0, dwell_max=9000.0),
]


# =============================================================================
# PAIR STATE
# =============================================================================

@dataclass
class PairState:
    """Live state for a trading pair."""
    config: PairConfig

    # Rust engines
    coint_engine: Optional[object] = None    # CointegrationEngine
    kalman_sentinel: Optional[object] = None  # KalmanSentinel

    # Signals (updated ONLY on M1 bar close — not every tick)
    position: int = 0       # +1 long spread, -1 short spread, 0 flat
    entry_z: float = 0.0    # Z at entry
    last_z: float = 0.0
    last_hurst: float = 0.5
    last_z_crit: float = 2.0
    last_exit_z: float = 0.5
    last_signal: int = 0    # from Rust: 1=long, -1=short, 0=flat

    # Price tracking (tick-level — updated every tick for execution/monitoring)
    last_price_a: float = 0.0
    last_price_b: float = 0.0
    last_spread: float = 0.0
    prev_spread: float = 0.0  # For correlation monitor (M1-bar-level)

    # M1 Bar Aggregation — CRITICAL: signals computed on M1 bars, NOT ticks
    # This matches the backtest which used M1 CSV data (one update per minute)
    current_bar_minute: int = -1   # Current M1 bar minute-of-day (0-1439), -1 = not started
    current_bar_epoch_min: int = -1  # Current bar epoch minute (unix_time // 60)
    bar_close_a: float = 0.0       # Latest close price of symbol A in current M1 bar
    bar_close_b: float = 0.0       # Latest close price of symbol B in current M1 bar
    m1_bar_count: int = 0          # Total completed M1 bars (for warmup tracking)
    last_bar_log_time: float = 0.0  # Wall time of last bar-close log (rate limit)

    # Positions
    ticket_a: int = 0
    ticket_b: int = 0

    # HMM regime filter
    hmm_detector: Optional[object] = None    # HMMRegimeDetector
    hmm_blocked: bool = False                # True when regime 2 (volatile)

    # Sentinel
    sentinel_aborted: bool = False
    sentinel_abort_count: int = 0

    # Entry tracking for actual P&L calculation
    entry_spread: float = 0.0   # Raw spread value at entry (for real win/loss)

    # Dwell / Cooldown timing
    entry_time: Optional[datetime] = None       # When position was opened
    last_close_time: Optional[datetime] = None   # When last position was closed

    # Stats
    total_trades: int = 0
    wins: int = 0
    losses: int = 0


# =============================================================================
# ENGINE
# =============================================================================

class TradingEngine:
    """
    v5.6 Pairs Trading Engine — Dynamic Z Entry + Dynamic Z Exit + Correlation Risk
    """

    # v5.6 Parameters
    WELFORD_SPAN = 100
    Z_BASE = 2.0
    GAMMA = 6.0
    HURST_WINDOW = 512
    EXIT_Z_BASE = 0.5
    EXIT_GAMMA = 2.0

    # AKAD Parameters
    AKAD_BASE_RISK = 0.0075   # 0.75% at DD=0%
    AKAD_DD_LAMBDA = 40.0

    # Ghost Stop
    GHOST_STOP_DAILY = 0.04   # 4% daily DD kill
    GHOST_STOP_MAX = 0.09     # 9% max DD kill

    # Correlation Risk
    CORR_WINDOW = 200

    # Dynamic Dwell (Hurst-adaptive minimum hold time)
    DWELL_BASE_SECONDS = 60.0     # Base dwell at H=0.3
    DWELL_HURST_ANCHOR = 0.3      # Hurst value where dwell = base
    DWELL_MIN_SECONDS = 30.0      # Floor (prop firm anti-scalp)
    DWELL_MAX_SECONDS = 300.0     # Ceiling (don't get stuck)

    # Rollover Lockout — block all new entries ±30 min around broker midnight
    # Markets are thin/volatile at open; spreads blow out, swaps charge, JPY
    # pairs especially dangerous in this window.
    ROLLOVER_LOCKOUT_MINUTES = 30  # Minutes before AND after broker 00:00

    # Cold-Start Warmup: block entries until enough M1 BARS are accumulated.
    # The Hurst R/S window is 512 bars; we need at least 200 for Welford to
    # stabilise (matching backtest `if count < 200: continue`).
    # Hurst defaults to 0.5 until 512 bars → safe base thresholds until then.
    MIN_WARMUP_BARS = 200    # M1 bars (~3.3 hours) before first trade allowed

    # Delta Staleness Guard (timezone-agnostic)
    STALE_FEED_TIMEOUT = 5.0  # seconds — if no new tick for this long, data is stale

    # Broker Time Sync interval (don't hammer every tick)
    BROKER_TIME_SYNC_INTERVAL = 60.0  # seconds between GET_SERVER_TIME calls

    # Engine
    TICK_INTERVAL = 0.1  # 100ms tick
    STATUS_LOG_INTERVAL = 300.0  # Log pair status every 5 minutes

    def __init__(self):
        # MT5 bridge
        self._bridge: Optional[MT5Bridge] = None

        # Pair states
        self._pairs: Dict[str, PairState] = {}

        # Risk components
        self._risk_supervisor: Optional[RiskSupervisor] = None
        self._dynamic_akad: Optional[DynamicAKAD] = None  # PRIMARY risk calculator
        self._akad_python: Optional[AKADRiskManager] = None  # Legacy fallback
        self._akad_rust: Optional[object] = None  # Rust legacy fallback
        self._corr_monitor: Optional[object] = None

        # Delta Staleness tracking: {symbol: (last_tick_time_msc, local_wall_time)}
        self._tick_tracker: Dict[str, Tuple[int, float]] = {}

        # Broker Time Sync state
        self._broker_gmt_offset: int = 0          # Seconds offset from GMT
        self._broker_time_synced: bool = False     # True after first successful sync
        self._last_broker_time_sync: float = 0.0   # Wall-clock of last sync

        # Daily DD tracking (prop firm rule: DD from start-of-BROKER-day balance)
        self._daily_start_balance = 0.0
        self._daily_start_broker_date: Optional[Tuple[int, int, int]] = None  # (Y, M, D) broker time

        # Control
        self._running = False
        self._shutdown_requested = False
        self._initial_balance = 0.0
        self._last_status_log: float = 0.0  # Wall-clock of last status log

        logger.info("SHF v5.6 Engine initialized")
        logger.info(f"  Rust available: {RUST_AVAILABLE}")
        logger.info(f"  HMM available: {HMM_AVAILABLE}")
        logger.info(f"  Dynamic Z: base={self.Z_BASE}, gamma={self.GAMMA}")
        logger.info(f"  Dynamic Exit Z: base={self.EXIT_Z_BASE}, gamma={self.EXIT_GAMMA}")
        logger.info(f"  Dynamic Dwell: base={self.DWELL_BASE_SECONDS}s, anchor_H={self.DWELL_HURST_ANCHOR}, "
                     f"range=[{self.DWELL_MIN_SECONDS}s, {self.DWELL_MAX_SECONDS}s]")
        logger.info(f"  AKAD: base={self.AKAD_BASE_RISK*100:.2f}%, lambda={self.AKAD_DD_LAMBDA}")
        logger.info(f"  M1 BAR MODE: signals computed on M1 bar close (not every tick)")
        logger.info(f"  Warmup: {self.MIN_WARMUP_BARS} M1 bars (~{self.MIN_WARMUP_BARS/60:.1f}h) before first trade")

    async def initialize(self) -> bool:
        """Initialize all components."""
        try:
            Path('logs').mkdir(exist_ok=True)
            Path('state').mkdir(exist_ok=True)

            # Connect MT5
            self._bridge = MT5Bridge()
            if not self._bridge.connect():
                logger.error("Failed to connect to MT5")
                return False

            account = self._bridge.get_account_info()
            self._initial_balance = account.balance
            logger.info(f"MT5 connected | Balance: {account.balance} {account.currency}")

            # Risk supervisor
            self._risk_supervisor = RiskSupervisor(
                initial_balance=account.balance,
                on_kill_all=self._emergency_close_all,
                on_alert=lambda a: logger.warning(f"Risk Alert: {a.message}")
            )

            # Dynamic AKAD — PRIMARY risk calculator (adaptive base from DD headroom + WR)
            self._dynamic_akad = DynamicAKAD(
                dd_lambda=self.AKAD_DD_LAMBDA,
                daily_dd_ceiling=self.GHOST_STOP_DAILY,
            )
            logger.info("Dynamic AKAD initialized (PRIMARY risk calculator)")

            # Legacy AKAD (Python fallback — kept for reference only)
            self._akad_python = AKADRiskManager(
                base_risk=self.AKAD_BASE_RISK,
                dd_lambda=self.AKAD_DD_LAMBDA,
            )

            # Legacy Rust AKAD (kept for reference only)
            if RUST_AVAILABLE:
                self._akad_rust = AKADRiskCalculator(
                    base_risk=self.AKAD_BASE_RISK,
                    dd_lambda=self.AKAD_DD_LAMBDA
                )
                logger.info("Rust AKADRiskCalculator initialized (legacy fallback)")

            # Correlation Risk Monitor (Rust)
            if RUST_AVAILABLE:
                self._corr_monitor = CorrelationRiskMonitor(window=self.CORR_WINDOW)
                logger.info(f"Rust CorrelationRiskMonitor initialized (window={self.CORR_WINDOW})")

            # FFI Contract Validation — fail fast if Rust binary is missing critical getters
            if RUST_AVAILABLE:
                _probe = CointegrationEngine(span=100, beta=1.0, dynamic_z=True, dynamic_exit=True)
                for attr in ('last_hurst', 'last_z_crit', 'last_exit_z', 'last_std', 'last_mean',
                             'last_z_score', 'last_spread', 'buffer_len'):
                    if not hasattr(_probe, attr):
                        raise SystemError(
                            f"FFI Contract Violation: CointegrationEngine missing '{attr}'. "
                            f"Recompile shf_core.pyd from rust_core/."
                        )
                del _probe
                logger.info("FFI contract validated — all Rust getters present")

            # Auto-resolve broker symbol names from EA's available symbols
            available = self._bridge.get_available_symbols()
            logger.info(f"EA streaming symbols: {available}")

            active_pairs = []
            for pair_cfg in HOLY_TRIO:
                resolved_a = self._bridge.resolve_symbol(pair_cfg.symbol_a, list(pair_cfg.aliases_a))
                resolved_b = self._bridge.resolve_symbol(pair_cfg.symbol_b, list(pair_cfg.aliases_b))

                if resolved_a and resolved_b:
                    # Update config with actual broker names
                    pair_cfg.resolved_a = resolved_a
                    pair_cfg.resolved_b = resolved_b
                    pair_cfg.symbol_a = resolved_a
                    pair_cfg.symbol_b = resolved_b
                    active_pairs.append(pair_cfg)
                    logger.info(f"  {pair_cfg.name}: {resolved_a} / {resolved_b} -- ACTIVE")
                else:
                    missing = []
                    if not resolved_a:
                        missing.append(f"{pair_cfg.symbol_a} (tried: {pair_cfg.aliases_a})")
                    if not resolved_b:
                        missing.append(f"{pair_cfg.symbol_b} (tried: {pair_cfg.aliases_b})")
                    logger.warning(f"  {pair_cfg.name}: SKIPPED -- missing: {', '.join(missing)}")

            if not active_pairs:
                logger.error("No tradeable pairs found! Check broker symbol names.")
                return False

            # Initialize pair states for active pairs only
            for pair_cfg in active_pairs:
                self._init_pair(pair_cfg)

            # Initial broker time sync + daily DD tracking
            self._sync_broker_time()
            self._daily_start_balance = account.balance
            self._daily_start_broker_date = self._get_broker_date()
            broker_now = self._get_broker_now()
            logger.info(
                f"Initial broker time sync: offset={self._broker_gmt_offset//3600:+d}h | "
                f"Broker date: {self._daily_start_broker_date} | "
                f"Broker time: {broker_now.strftime('%H:%M:%S')} | "
                f"Synced: {self._broker_time_synced}"
            )

            logger.info(f"Initialized {len(self._pairs)} pairs")

            # Pre-warm engines with historical M1 bars (instant readiness)
            self._prewarm_pairs()

            return True

        except Exception as e:
            logger.exception(f"Initialization failed: {e}")
            return False

    def _init_pair(self, cfg: PairConfig) -> None:
        """Initialize a pair with Rust engines."""
        state = PairState(config=cfg)

        if RUST_AVAILABLE:
            # CointegrationEngine with v5.6: Dynamic Entry Z + Dynamic Exit Z
            state.coint_engine = CointegrationEngine(
                span=self.WELFORD_SPAN,
                beta=cfg.static_beta,
                entry_z=self.Z_BASE,
                exit_z=self.EXIT_Z_BASE,
                z_base=self.Z_BASE,
                gamma=self.GAMMA,
                hurst_window=self.HURST_WINDOW,
                dynamic_z=True,
                exit_z_base=self.EXIT_Z_BASE,
                exit_gamma=self.EXIT_GAMMA,
                dynamic_exit=True,
            )
            logger.info(f"  {cfg.name}: Rust CointegrationEngine (dynamic_z=True, dynamic_exit_z=True)")

            # Kalman Sentinel
            state.kalman_sentinel = KalmanSentinel(
                static_beta=cfg.static_beta,
                beta_tolerance=cfg.beta_tolerance,
            )
            logger.info(f"  {cfg.name}: Rust KalmanSentinel (tol={cfg.beta_tolerance})")

        # HMM Volatility Filter (per pair, Numba JIT, per-pair min_regime_hold)
        if HMM_AVAILABLE:
            state.hmm_detector = create_regime_detector(
                n_regimes=3, lookback=100, min_regime_hold=cfg.hmm_min_hold
            )
            logger.info(f"  {cfg.name}: HMM 3-regime volatility filter (lookback=100, hold={cfg.hmm_min_hold})")

        self._pairs[cfg.name] = state

    # =========================================================================
    # HISTORICAL PRE-WARM (instant readiness — no 3h wait)
    # =========================================================================

    PREWARM_BARS = 768  # Request 768 M1 bars (fills Hurst 512 window + 256 extra)

    def _prewarm_pairs(self) -> None:
        """
        Fetch historical M1 bars from broker and replay them through the
        signal engines (CointegrationEngine, HMM, Kalman, CorrelationMonitor).

        This makes the bot immediately ready to trade on startup instead of
        waiting 3+ hours for live M1 bars to accumulate.

        The backtest processes M1 CSV data sequentially — this does exactly
        the same thing with broker history, producing identical engine state.
        """
        logger.info(f"PRE-WARM: Fetching {self.PREWARM_BARS} M1 bars per symbol from broker...")

        for name, state in self._pairs.items():
            cfg = state.config
            t_start = time.time()

            # Fetch M1 history for both legs
            bars_a = self._bridge.get_history(cfg.symbol_a, self.PREWARM_BARS)
            bars_b = self._bridge.get_history(cfg.symbol_b, self.PREWARM_BARS)

            if not bars_a or not bars_b:
                logger.warning(
                    f"PRE-WARM FAILED: {name} | A={len(bars_a)} B={len(bars_b)} bars | "
                    f"Will warm up from live data instead (~{self.MIN_WARMUP_BARS} min)"
                )
                continue

            # Use the shorter of the two series (they should be equal, but be safe)
            n_bars = min(len(bars_a), len(bars_b))
            if n_bars < 50:
                logger.warning(f"PRE-WARM: {name} | Only {n_bars} bars — too few, skipping")
                continue

            # Replay bars through all engines (oldest first — chronological)
            prev_spread = 0.0
            for i in range(n_bars):
                close_a = bars_a[i].get('c', 0.0)
                close_b = bars_b[i].get('c', 0.0)
                if close_a <= 0 or close_b <= 0:
                    continue

                # Feed to CointegrationEngine (Welford + Hurst + Z-score)
                if state.coint_engine is not None:
                    signal = state.coint_engine.update(close_a, close_b)
                    state.last_z = signal.z_score
                    state.last_signal = signal.signal
                    state.last_hurst = state.coint_engine.last_hurst
                    state.last_z_crit = state.coint_engine.last_z_crit
                    state.last_exit_z = state.coint_engine.last_exit_z
                    current_spread = signal.spread

                    # Feed to CorrelationRiskMonitor
                    if self._corr_monitor is not None and prev_spread != 0.0:
                        spread_return = current_spread - prev_spread
                        self._corr_monitor.push_return(cfg.pair_index, spread_return)

                    # Feed to HMM Volatility Filter
                    if state.hmm_detector is not None and prev_spread != 0.0:
                        spread_return = current_spread - prev_spread
                        state.hmm_detector.update(spread_return)
                        state.hmm_blocked = state.hmm_detector.is_blocked

                    # Feed to Kalman Sentinel
                    if state.kalman_sentinel is not None:
                        log_a = math.log(close_a) if close_a > 0 else 0.0
                        log_b = math.log(close_b) if close_b > 0 else 0.0
                        state.kalman_sentinel.update(log_a, log_b)

                    prev_spread = current_spread

            # Update bar tracking state
            state.last_spread = prev_spread
            state.m1_bar_count = n_bars
            state.last_price_a = bars_a[-1].get('c', 0.0)
            state.last_price_b = bars_b[-1].get('c', 0.0)

            # Set current bar epoch to NOW so live bar tracking starts cleanly
            state.current_bar_epoch_min = int(time.time()) // 60
            state.bar_close_a = state.last_price_a
            state.bar_close_b = state.last_price_b

            elapsed = time.time() - t_start
            buf = state.coint_engine.buffer_len if state.coint_engine else 0

            logger.info(
                f"PRE-WARM DONE: {name} | {n_bars} M1 bars replayed in {elapsed:.1f}s | "
                f"Buffer={buf} | Z={state.last_z:+.2f} H={state.last_hurst:.3f} "
                f"Zcrit={state.last_z_crit:.2f} | HMM={'BLOCKED' if state.hmm_blocked else 'OK'} | "
                f"READY TO TRADE"
            )

        logger.info("PRE-WARM COMPLETE: All pairs ready")

    # =========================================================================
    # MAIN LOOP
    # =========================================================================

    async def run(self) -> None:
        """Main trading loop — 100ms tick."""
        self._running = True
        logger.info("Starting v5.6 trading loop (100ms tick)...")

        try:
            while self._running and not self._shutdown_requested:
                await self._tick()
                await asyncio.sleep(self.TICK_INTERVAL)
        except Exception as e:
            logger.exception(f"Trading loop error: {e}")
        finally:
            await self._shutdown()

    async def _tick(self) -> None:
        """Single tick — process all pairs."""
        # Heartbeat — tolerant of brief EA restarts (chart timeframe changes etc.)
        if not self._bridge.heartbeat():
            self._hb_fail_count = getattr(self, '_hb_fail_count', 0) + 1
            if self._hb_fail_count <= 3:
                # Brief blip — wait it out (EA may be reinitializing)
                return
            logger.warning(f"MT5 heartbeat failed {self._hb_fail_count}x — reconnecting")
            self._bridge.disconnect()
            if not self._bridge.connect():
                return
            self._hb_fail_count = 0
            self._hb_grace_until = time.time() + 15.0  # 15s grace after reconnect
            return
        else:
            self._hb_fail_count = 0
            # Grace period after reconnect — skip processing until data flows
            if time.time() < getattr(self, '_hb_grace_until', 0):
                return

        # Get current account state
        account = self._bridge.get_account_info()

        # --- Broker Time Sync (rate-limited, every 60s) ---
        self._sync_broker_time()

        # --- Daily balance reset (prop firm daily DD — BROKER time, not UTC) ---
        broker_date = self._get_broker_date()
        if self._daily_start_broker_date is None or broker_date != self._daily_start_broker_date:
            self._daily_start_broker_date = broker_date
            self._daily_start_balance = account.balance
            broker_now = self._get_broker_now()
            logger.info(
                f"Daily balance reset (BROKER TIME): ${self._daily_start_balance:.2f} | "
                f"Broker date: {broker_date[0]}-{broker_date[1]:02d}-{broker_date[2]:02d} | "
                f"Broker time: {broker_now.strftime('%H:%M:%S')} | "
                f"GMT offset: {self._broker_gmt_offset//3600:+d}h"
            )

        # --- Ghost Stop: Daily DD (4%) from start-of-day balance ---
        daily_dd = max(0.0, (self._daily_start_balance - account.equity) / self._daily_start_balance) \
            if self._daily_start_balance > 0 else 0.0
        if daily_dd >= self.GHOST_STOP_DAILY:
            logger.critical(
                f"GHOST STOP (DAILY): DD={daily_dd*100:.2f}% >= {self.GHOST_STOP_DAILY*100}% "
                f"(start-of-day: ${self._daily_start_balance:.2f}, equity: ${account.equity:.2f})"
            )
            self._emergency_close_all(f"Daily ghost stop: {daily_dd*100:.2f}% DD")
            self._shutdown_requested = True
            return

        # --- Ghost Stop: Max DD (9%) from initial balance ---
        current_dd = max(0.0, (self._initial_balance - account.equity) / self._initial_balance) \
            if self._initial_balance > 0 else 0.0
        if current_dd >= self.GHOST_STOP_MAX:
            logger.critical(
                f"GHOST STOP (MAX): DD={current_dd*100:.2f}% >= {self.GHOST_STOP_MAX*100}% "
                f"(initial: ${self._initial_balance:.2f}, equity: ${account.equity:.2f})"
            )
            self._emergency_close_all(f"Max ghost stop: {current_dd*100:.2f}% DD")
            self._shutdown_requested = True
            return

        # --- RiskSupervisor check (consecutive losses, cooldown, etc.) ---
        if self._risk_supervisor:
            alert = self._risk_supervisor.update(account.equity)
            if alert and alert.action == RiskAction.KILL_ALL:
                self._shutdown_requested = True
                return
            if self._risk_supervisor.is_halted:
                return  # In consecutive-loss cooldown — skip this tick

        # Process each pair (pass daily_dd for Dynamic AKAD)
        for name, state in self._pairs.items():
            await self._process_pair(state, current_dd, daily_dd, account.balance)

        # --- Periodic Status Log (every 5 minutes) ---
        now_wall = time.time()
        if now_wall - self._last_status_log >= self.STATUS_LOG_INTERVAL:
            self._last_status_log = now_wall
            broker_now = self._get_broker_now()
            for name, state in self._pairs.items():
                hmm_regime = state.hmm_detector.current_regime if state.hmm_detector else "?"
                hmm_status = "BLOCKED" if state.hmm_blocked else "OK"
                pos_str = "FLAT" if state.position == 0 else ("LONG" if state.position > 0 else "SHORT")
                buf = state.coint_engine.buffer_len if state.coint_engine else 0
                warmup_pct = min(100, buf * 100 // self.MIN_WARMUP_BARS) if self.MIN_WARMUP_BARS > 0 else 100
                warmup_str = f"WARM {warmup_pct}%" if buf < self.MIN_WARMUP_BARS else "READY"
                logger.info(
                    f"STATUS | {name} | {pos_str} | Z={state.last_z:+.2f} Zcrit={state.last_z_crit:.2f} "
                    f"Zexit={state.last_exit_z:.3f} | H={state.last_hurst:.3f} | "
                    f"HMM={hmm_regime}({hmm_status}) | Sentinel={'ABORT' if state.sentinel_aborted else 'OK'} | "
                    f"M1Bars={buf}/{self.MIN_WARMUP_BARS}({warmup_str}) | "
                    f"Trades={state.total_trades} ({state.wins}W/{state.losses}L)"
                )
            logger.info(
                f"STATUS | Account: ${account.equity:.2f} | DD={current_dd*100:.2f}% daily={daily_dd*100:.2f}% | "
                f"Broker time: {broker_now.strftime('%H:%M')}"
            )

    async def _process_pair(self, state: PairState, current_dd: float, daily_dd: float, balance: float) -> None:
        """
        Process a single pair through the full v5.6 pipeline.

        CRITICAL FIX (v5.6.1): M1 Bar Aggregation
        ===========================================
        The backtests computed all signals on M1 (1-minute) bar close prices.
        The live engine receives ticks every ~100ms (10/second).

        Previously, every tick was fed to CointegrationEngine.update(), meaning:
          - Welford stats were computed on ~77 seconds of data (768 ticks) instead
            of ~768 minutes (12.8 hours) as in the backtest
          - Hurst exponent was wrong (tick-level H ≈ 0.4 vs M1-bar H ≈ 0.55)
          - Z_crit dropped to minimum 2.0 instead of 2.1–3.0
          - Trades fired every 1–2 minutes instead of ~10/day

        FIX: Aggregate tick prices into M1 bars. Only call coint_engine.update(),
        HMM, Kalman, and correlation monitor on M1 bar CLOSE — exactly matching
        the backtest cadence.

        Tick-level data is still used for: price monitoring, staleness checks,
        spread blowout detection, and execution.
        """
        cfg = state.config

        # =====================================================================
        # STEP 1: Get latest TICK prices (always — for execution & monitoring)
        # =====================================================================
        price_a = self._get_price(cfg.symbol_a)
        price_b = self._get_price(cfg.symbol_b)
        if price_a is None or price_b is None:
            return

        state.last_price_a = price_a
        state.last_price_b = price_b

        # =====================================================================
        # STEP 2: M1 Bar Aggregation — detect minute boundary
        # =====================================================================
        # Use wall-clock epoch minute as bar key (stable, monotonic)
        epoch_minute = int(time.time()) // 60

        if state.current_bar_epoch_min == -1:
            # Very first tick — initialise bar tracking, no signal yet
            state.current_bar_epoch_min = epoch_minute
            state.bar_close_a = price_a
            state.bar_close_b = price_b
            logger.info(f"M1 BAR INIT: {cfg.name} | First bar started | epoch_min={epoch_minute}")
            return

        if epoch_minute == state.current_bar_epoch_min:
            # Same minute — just update the close prices for this bar
            state.bar_close_a = price_a
            state.bar_close_b = price_b
            return  # NO signal processing until bar closes

        # =================================================================
        # NEW MINUTE — the previous M1 bar just CLOSED
        # =================================================================
        # Use the PREVIOUS bar's close prices for signal computation
        # (this is exactly what the backtest does with M1 CSV close prices)
        m1_close_a = state.bar_close_a
        m1_close_b = state.bar_close_b

        # Start the new bar
        state.current_bar_epoch_min = epoch_minute
        state.bar_close_a = price_a
        state.bar_close_b = price_b
        state.m1_bar_count += 1

        # Log bar close periodically (every 10 bars = every 10 min)
        now_wall = time.time()
        if now_wall - state.last_bar_log_time >= 600.0:  # every 10 min
            state.last_bar_log_time = now_wall
            logger.info(
                f"M1 BAR: {cfg.name} | bar #{state.m1_bar_count} closed | "
                f"A={m1_close_a:.5f} B={m1_close_b:.5f} | "
                f"Z={state.last_z:+.2f} H={state.last_hurst:.3f} Zcrit={state.last_z_crit:.2f}"
            )

        # =====================================================================
        # STEP 3: Feed M1 bar close to Rust CointegrationEngine
        # =====================================================================
        # Store previous spread for correlation monitor (M1-bar-level delta)
        state.prev_spread = state.last_spread

        if state.coint_engine is not None:
            signal = state.coint_engine.update(m1_close_a, m1_close_b)
            state.last_z = signal.z_score
            state.last_signal = signal.signal
            state.last_hurst = state.coint_engine.last_hurst
            state.last_z_crit = state.coint_engine.last_z_crit
            state.last_exit_z = state.coint_engine.last_exit_z
            state.last_spread = signal.spread
        else:
            return  # No engine, skip

        # =====================================================================
        # STEP 4: Feed M1-bar spread returns to CorrelationRiskMonitor
        # =====================================================================
        if self._corr_monitor is not None and state.prev_spread != 0.0:
            spread_return = state.last_spread - state.prev_spread
            self._corr_monitor.push_return(cfg.pair_index, spread_return)

        # =====================================================================
        # STEP 5: Kalman Sentinel check (M1-bar cadence)
        # =====================================================================
        if state.kalman_sentinel is not None:
            log_a = math.log(m1_close_a) if m1_close_a > 0 else 0.0
            log_b = math.log(m1_close_b) if m1_close_b > 0 else 0.0
            beta, should_abort = state.kalman_sentinel.update(log_a, log_b)

            if should_abort and not state.sentinel_aborted:
                state.sentinel_aborted = True
                state.sentinel_abort_count += 1
                logger.warning(
                    f"SENTINEL ABORT: {cfg.name} | beta={beta:.4f} | "
                    f"deviation={abs(beta - cfg.static_beta)*100:.1f}%"
                )
                # Close any open position for this pair
                if state.position != 0:
                    await self._close_spread(state, "Sentinel abort")
                return

            # Reset sentinel if beta returns to safe range
            if state.sentinel_aborted and not should_abort:
                state.sentinel_aborted = False
                logger.info(f"Sentinel cleared: {cfg.name} | beta={beta:.4f}")

        if state.sentinel_aborted:
            return  # Pair is blocked

        # =====================================================================
        # STEP 5b: HMM Volatility Filter (M1-bar cadence)
        # =====================================================================
        if state.hmm_detector is not None and state.prev_spread != 0.0:
            spread_return = state.last_spread - state.prev_spread
            regime = state.hmm_detector.update(spread_return)
            was_blocked = state.hmm_blocked
            state.hmm_blocked = state.hmm_detector.is_blocked

            # Log regime transitions
            if state.hmm_blocked and not was_blocked:
                logger.warning(
                    f"HMM BLOCK: {cfg.name} | Regime={regime} (volatile) | "
                    f"New entries blocked until regime clears"
                )
            elif not state.hmm_blocked and was_blocked:
                logger.info(
                    f"HMM CLEAR: {cfg.name} | Regime={regime} | "
                    f"Entries re-enabled"
                )

        # =====================================================================
        # STEP 6: Signal processing — entry/exit (only on M1 bar close)
        # =====================================================================
        if state.position == 0:
            # Check for entry (blocked if HMM says volatile regime)
            if state.last_signal != 0 and not state.hmm_blocked:
                await self._maybe_enter(state, current_dd, daily_dd, balance)
        else:
            # Exits ALWAYS allowed regardless of HMM regime
            await self._maybe_exit(state)

    # =========================================================================
    # DYNAMIC DWELL (Hurst-adaptive minimum hold time)
    # =========================================================================

    def _calculate_dynamic_dwell(self, hurst_value: float, cfg: PairConfig = None) -> float:
        """
        Calculate Hurst-adaptive minimum hold time (seconds).
        Uses PER-PAIR dwell parameters from PairConfig (v5.6.3).

        Formula: dwell = dwell_base * (H / dwell_anchor)
        Clamped to [dwell_min, dwell_max].

        Index (base=60):  H=0.5 -> 100s (2 bars)
        Oil (base=1800):  H=0.5 -> 3000s (50 bars) — eliminates bid-ask bounce
        """
        if cfg is not None:
            raw = cfg.dwell_base * (hurst_value / cfg.dwell_anchor)
            return max(cfg.dwell_min, min(cfg.dwell_max, raw))
        # Fallback to class-level defaults
        raw = self.DWELL_BASE_SECONDS * (hurst_value / self.DWELL_HURST_ANCHOR)
        return max(self.DWELL_MIN_SECONDS, min(self.DWELL_MAX_SECONDS, raw))

    # =========================================================================
    # ENTRY / EXIT
    # =========================================================================

    async def _maybe_enter(self, state: PairState, current_dd: float, daily_dd: float, balance: float) -> None:
        """Evaluate and potentially enter a spread trade."""
        cfg = state.config
        direction = state.last_signal  # 1=long spread, -1=short spread

        # Cold-Start Warmup: block entries until enough M1 bars accumulated
        # (buffer_len now counts M1 bars since we only feed bar closes)
        if state.coint_engine is not None:
            buf_len = state.coint_engine.buffer_len
            if buf_len < self.MIN_WARMUP_BARS:
                return  # Still warming up — need 200 M1 bars (~3.3h) for reliable stats

        # Rollover Lockout: block new entries ±5 min around broker midnight
        if self._is_rollover_lockout():
            return  # In rollover window — no new entries

        # Re-entry cooldown: block re-entry for dynamic dwell period after closing
        if state.last_close_time is not None:
            now = datetime.utcnow()
            cooldown_seconds = self._calculate_dynamic_dwell(state.last_hurst, cfg)
            elapsed = (now - state.last_close_time).total_seconds()
            if elapsed < cooldown_seconds:
                return  # Still in re-entry cooldown

        # Spread Blowout Filter: block entry if either leg's spread is too wide
        # (protects against rollover spreads, thin liquidity, news spikes)
        if not self._check_spread(cfg.symbol_a, cfg.max_spread_a):
            return  # Spread too wide on leg A
        if not self._check_spread(cfg.symbol_b, cfg.max_spread_b):
            return  # Spread too wide on leg B

        # Calculate risk using Dynamic AKAD (PRIMARY)
        if self._dynamic_akad is not None:
            risk = self._dynamic_akad.calculate_risk(total_dd=current_dd, daily_dd=daily_dd)
        elif self._akad_rust is not None:
            risk, dd_f, atr_f, exp_g = self._akad_rust.calculate_risk(current_dd)
        else:
            akad_state = self._akad_python.calculate_risk(current_dd=current_dd, symbols=[cfg.symbol_a])
            risk = akad_state.final_risk

        # Apply correlation risk multiplier
        corr_mult = 1.0
        if self._corr_monitor is not None:
            self._corr_monitor.compute_risk()
            corr_mult = self._corr_monitor.last_risk_multiplier
        final_risk = risk * corr_mult

        # Position sizing
        lots = max(0.01, round(balance * final_risk / 1000, 2))  # Simplified sizing

        logger.info(
            f"ENTRY {cfg.name} | Dir={'LONG' if direction > 0 else 'SHORT'} | "
            f"Z={state.last_z:.2f} Z_crit={state.last_z_crit:.2f} | "
            f"H={state.last_hurst:.3f} | Risk={final_risk*100:.3f}% | "
            f"CorrMult={corr_mult:.2f} | Lots={lots}"
        )

        # Calculate server-side hard stops (Huber 4.815σ catastrophe net)
        sl_a, tp_a, sl_b, tp_b = self._calculate_hard_stops(state, direction, lots)

        # Execute spread trade
        if direction > 0:
            # Long spread: buy A, sell B
            req_a = OrderRequest(cfg.symbol_a, OrderType.MARKET_BUY, lots, sl=sl_a, tp=tp_a)
            req_b = OrderRequest(cfg.symbol_b, OrderType.MARKET_SELL, lots, sl=sl_b, tp=tp_b)
        else:
            # Short spread: sell A, buy B
            req_a = OrderRequest(cfg.symbol_a, OrderType.MARKET_SELL, lots, sl=sl_a, tp=tp_a)
            req_b = OrderRequest(cfg.symbol_b, OrderType.MARKET_BUY, lots, sl=sl_b, tp=tp_b)

        try:
            result_a, result_b = self._bridge.execute_spread(req_a, req_b)
        except (BridgeTimeoutError, Exception) as e:
            # MT5 froze during execution — run 3-state reconciliation audit
            logger.critical(
                f"EXECUTION TIMEOUT: {cfg.name} | {e} | Initiating position audit..."
            )
            await self._reconcile_after_timeout(state, req_a, req_b, direction)
            return

        if result_a.success and result_b.success:
            state.position = direction
            state.entry_z = state.last_z
            state.entry_spread = state.last_spread  # Store raw spread for real P&L calc
            state.entry_time = datetime.utcnow()
            state.ticket_a = result_a.ticket
            state.ticket_b = result_b.ticket
            dwell = self._calculate_dynamic_dwell(state.last_hurst, cfg)
            logger.info(
                f"  FILLED: {cfg.name} spread entered | tickets={result_a.ticket},{result_b.ticket} | "
                f"Dwell={dwell:.0f}s (H={state.last_hurst:.3f}) | EntrySpread={state.entry_spread:.6f}"
            )
        else:
            err_a = result_a.error_message if not result_a.success else "OK"
            err_b = result_b.error_message if not result_b.success else "OK"
            logger.error(f"  REJECTED: Spread execution failed: A={err_a}, B={err_b}")
            # Set cooldown to prevent immediate retry (uses existing dwell mechanism)
            state.last_close_time = datetime.utcnow()

    async def _maybe_exit(self, state: PairState) -> None:
        """Check exit conditions using v5.6 dynamic exit Z + dynamic dwell enforcement."""
        cfg = state.config

        # --- Emergency exit: Z went 2.5x past entry (ALWAYS bypasses dwell) ---
        is_emergency = abs(state.last_z) > abs(state.entry_z) * 2.5
        if is_emergency:
            reason = f"Emergency: |Z|={abs(state.last_z):.2f} > 2.5×entry (dwell bypassed)"
            await self._close_spread(state, reason)
            return

        # --- Dynamic Dwell enforcement for normal exits ---
        if state.entry_time is not None:
            now = datetime.utcnow()
            hold_seconds = (now - state.entry_time).total_seconds()
            dwell_required = self._calculate_dynamic_dwell(state.last_hurst, cfg)
            if hold_seconds < dwell_required:
                return  # Still within minimum dwell — hold the position

        # --- Normal dynamic exit check — |Z| < Z_exit (Hurst-adaptive) ---
        should_exit = False
        reason = ""

        if state.position == 1 and state.last_z > -state.last_exit_z:
            should_exit = True
            reason = f"Z={state.last_z:.2f} > -{state.last_exit_z:.3f} (dynamic exit)"
        elif state.position == -1 and state.last_z < state.last_exit_z:
            should_exit = True
            reason = f"Z={state.last_z:.2f} < {state.last_exit_z:.3f} (dynamic exit)"

        if should_exit:
            await self._close_spread(state, reason)

    async def _close_spread(self, state: PairState, reason: str) -> None:
        """Close both legs of a spread position."""
        cfg = state.config

        # Query ACTUAL MT5 profit BEFORE closing (broker P&L includes spread costs)
        actual_profit = 0.0
        try:
            positions = self._bridge.get_positions()
            for pos in positions:
                if pos.ticket in (state.ticket_a, state.ticket_b):
                    actual_profit += pos.profit + pos.swap
        except Exception as e:
            logger.warning(f"Could not query MT5 profit before close: {e}")

        # Close both legs
        closed_a = self._bridge.close_position(state.ticket_a) if state.ticket_a else True
        closed_b = self._bridge.close_position(state.ticket_b) if state.ticket_b else True

        # Determine win/loss from ACTUAL MT5 profit (includes spreads, swaps, commissions)
        # Falls back to spread math only if MT5 query failed
        if actual_profit != 0.0:
            is_win = actual_profit > 0
        else:
            # Fallback: use spread math
            spread_pnl = (state.last_spread - state.entry_spread) * state.position
            actual_profit = spread_pnl  # For logging only
            is_win = spread_pnl > 0

        state.total_trades += 1
        if is_win:
            state.wins += 1
        else:
            state.losses += 1

        # Record to Dynamic AKAD (PRIMARY) + legacy AKAD
        if self._dynamic_akad is not None:
            self._dynamic_akad.record_trade(win=is_win)
        r_multiple = 0.49 if is_win else -1.0
        if self._akad_rust is not None:
            self._akad_rust.record_trade(r_multiple)
        self._akad_python.record_trade(r_multiple=r_multiple, is_win=is_win)

        # Record to RiskSupervisor (consecutive loss tracking + cooldown)
        if self._risk_supervisor:
            if is_win:
                self._risk_supervisor.record_win()
            else:
                alert = self._risk_supervisor.record_loss()
                if alert:
                    logger.warning(f"RiskSupervisor: {alert.message}")

        wr = state.wins / state.total_trades * 100 if state.total_trades > 0 else 0

        # Calculate hold duration and dwell for logging
        now = datetime.utcnow()
        hold_seconds = (now - state.entry_time).total_seconds() if state.entry_time else 0.0
        dwell_required = self._calculate_dynamic_dwell(state.last_hurst, cfg)

        logger.info(
            f"EXIT {cfg.name} | {'WIN' if is_win else 'LOSS'} | {reason} | "
            f"MT5 P&L=${actual_profit:+.2f} | Spread={state.entry_spread:.6f}->{state.last_spread:.6f} | "
            f"H={state.last_hurst:.3f} Z_exit={state.last_exit_z:.3f} | "
            f"Hold={hold_seconds:.1f}s Dwell={dwell_required:.0f}s | "
            f"Record: {state.wins}W/{state.losses}L ({wr:.0f}%)"
        )

        # Update timing state
        state.last_close_time = now
        state.entry_time = None
        state.position = 0
        state.entry_z = 0.0
        state.entry_spread = 0.0
        state.ticket_a = 0
        state.ticket_b = 0

    # =========================================================================
    # EXECUTION RECONCILIATION (3-State Audit after MT5 timeout)
    # =========================================================================

    RECONCILE_RETRIES = 3       # How many times to retry get_positions after timeout
    RECONCILE_RETRY_DELAY = 1.0  # Seconds between retries
    RECONCILE_RECENCY_WINDOW = 30.0  # Seconds — only match positions opened within this window

    async def _reconcile_after_timeout(
        self,
        state: PairState,
        req_a: OrderRequest,
        req_b: OrderRequest,
        direction: int,
    ) -> None:
        """
        3-State Reconciliation Audit after BridgeTimeoutError.

        Queries MT5 for open positions and determines what actually happened:
          - SCENARIO A: Both legs filled → track as open spread
          - SCENARIO B: Neither leg filled → safe to reset
          - SCENARIO C: One leg only (WIDOWMAKER) → emergency close the orphan

        Matches positions by symbol + direction + magic number + recency.
        """
        cfg = state.config

        # --- Step 1: Query open positions (retry up to 3 times) ---
        positions: List[Position] = []
        for attempt in range(1, self.RECONCILE_RETRIES + 1):
            try:
                positions = self._bridge.get_positions()
                logger.info(
                    f"RECONCILE: Got {len(positions)} positions (attempt {attempt}/{self.RECONCILE_RETRIES})"
                )
                break
            except Exception as e:
                logger.error(f"RECONCILE: get_positions attempt {attempt} failed: {e}")
                if attempt < self.RECONCILE_RETRIES:
                    await asyncio.sleep(self.RECONCILE_RETRY_DELAY)
        else:
            # All retries failed — MT5 is completely unresponsive
            logger.critical(
                f"RECONCILE FAILED: Cannot reach MT5 after {self.RECONCILE_RETRIES} attempts. "
                f"State unknown for {cfg.name}. Manual intervention required!"
            )
            return

        # --- Step 2: Find matching positions for our attempted orders ---
        now = datetime.utcnow()

        # Determine expected direction types
        if direction > 0:
            expected_type_a = "BUY"   # Long A
            expected_type_b = "SELL"  # Short B
        else:
            expected_type_a = "SELL"  # Short A
            expected_type_b = "BUY"   # Long B

        found_a: Optional[Position] = None
        found_b: Optional[Position] = None

        for pos in positions:
            # Check recency — only match positions opened in the last N seconds
            try:
                age = (now - pos.open_time).total_seconds()
            except:
                age = 999.0  # Can't parse time → skip

            if age > self.RECONCILE_RECENCY_WINDOW:
                continue

            # Match by symbol + direction + magic number
            if (pos.symbol == req_a.symbol and
                    pos.type == expected_type_a and
                    pos.magic == req_a.magic):
                found_a = pos

            if (pos.symbol == req_b.symbol and
                    pos.type == expected_type_b and
                    pos.magic == req_b.magic):
                found_b = pos

        # --- Step 3: 3-State Decision ---

        if found_a and found_b:
            # SCENARIO A: Both legs filled — trade is live
            state.position = direction
            state.entry_z = state.last_z
            state.entry_spread = state.last_spread  # For real P&L tracking
            state.entry_time = datetime.utcnow()
            state.ticket_a = found_a.ticket
            state.ticket_b = found_b.ticket
            logger.critical(
                f"RECONCILE SUCCESS: {cfg.name} — Both legs confirmed filled. "
                f"tickets={found_a.ticket},{found_b.ticket}. Tracking as OPEN."
            )

        elif not found_a and not found_b:
            # SCENARIO B: Neither leg filled — safe to reset
            logger.info(
                f"RECONCILE CLEAN: {cfg.name} — Neither leg found. "
                f"No fills detected. Safe to retry."
            )

        else:
            # SCENARIO C: WIDOWMAKER — One leg filled, other didn't
            orphan = found_a if found_a else found_b
            orphan_side = "A" if found_a else "B"
            missing_side = "B" if found_a else "A"

            logger.critical(
                f"WARNING -- WIDOWMAKER DETECTED: {cfg.name} | "
                f"Leg {orphan_side} FILLED (ticket={orphan.ticket}, "
                f"{orphan.symbol} {orphan.type} {orphan.lots} lots) | "
                f"Leg {missing_side} NOT FILLED | "
                f"EMERGENCY CLOSING ORPHAN..."
            )

            # Close the orphaned leg immediately
            try:
                closed = self._bridge.close_position(orphan.ticket)
                if closed:
                    logger.critical(
                        f"ORPHAN CLOSED: ticket={orphan.ticket} | {cfg.name} is now FLAT."
                    )
                else:
                    logger.critical(
                        f"ORPHAN CLOSE FAILED: ticket={orphan.ticket} | "
                        f"MANUAL INTERVENTION REQUIRED!"
                    )
            except Exception as e:
                logger.critical(
                    f"ORPHAN CLOSE ERROR: ticket={orphan.ticket} | {e} | "
                    f"MANUAL INTERVENTION REQUIRED!"
                )

    # =========================================================================
    # BROKER TIME SYNC
    # =========================================================================

    def _sync_broker_time(self) -> bool:
        """
        Fetch broker server time via GET_SERVER_TIME and cache the GMT offset.
        Called at startup and periodically (every BROKER_TIME_SYNC_INTERVAL seconds).
        Returns True if sync succeeded.
        """
        now_wall = time.time()
        # Rate-limit: don't call more often than BROKER_TIME_SYNC_INTERVAL
        if self._broker_time_synced and (now_wall - self._last_broker_time_sync) < self.BROKER_TIME_SYNC_INTERVAL:
            return True  # Already fresh

        try:
            st = self._bridge.get_server_time()
            if st is None:
                logger.warning("Broker time sync failed — get_server_time() returned None")
                return self._broker_time_synced  # Keep previous value if we had one
            self._broker_gmt_offset = st.gmt_offset_seconds
            self._broker_time_synced = True
            self._last_broker_time_sync = now_wall
            logger.debug(
                f"Broker time synced: {st.datetime_str} | "
                f"GMT offset={st.gmt_offset_seconds}s ({st.gmt_offset_seconds//3600:+d}h) | "
                f"dow={st.day_of_week}"
            )
            return True
        except Exception as e:
            logger.warning(f"Broker time sync error: {e}")
            return self._broker_time_synced

    def _get_broker_now(self) -> datetime:
        """
        Return current datetime in broker server time.
        Uses UTC + cached GMT offset.
        Falls back to UTC if time has never been synced.
        """
        from datetime import timedelta
        utc_now = datetime.utcnow()
        if self._broker_time_synced:
            return utc_now + timedelta(seconds=self._broker_gmt_offset)
        return utc_now  # Fallback (first tick before sync)

    def _get_broker_date(self) -> Tuple[int, int, int]:
        """Return (year, month, day) in broker server time."""
        bt = self._get_broker_now()
        return (bt.year, bt.month, bt.day)

    def _is_rollover_lockout(self) -> bool:
        """
        Check if we are within ±ROLLOVER_LOCKOUT_MINUTES of broker midnight (00:00).
        During this window, new entries are blocked to avoid rollover spread spikes
        and swap charges.

        Returns True if in lockout window.
        """
        bt = self._get_broker_now()
        minutes_since_midnight = bt.hour * 60 + bt.minute
        minutes_before_midnight = 1440 - minutes_since_midnight  # 1440 = 24*60

        in_lockout = (
            minutes_since_midnight < self.ROLLOVER_LOCKOUT_MINUTES or  # Just after midnight
            minutes_before_midnight < self.ROLLOVER_LOCKOUT_MINUTES    # Just before midnight
        )
        return in_lockout

    # =========================================================================
    # HELPERS
    # =========================================================================

    def _get_tick_data(self, symbol: str) -> Optional[object]:
        """
        Get latest tick for a symbol with Delta Staleness check.

        Staleness is timezone-agnostic: we track when (local wall clock) each
        symbol's tick.time_msc last changed. If it hasn't changed for
        STALE_FEED_TIMEOUT seconds, the feed is dead/stale → return None.

        Returns the raw tick object (bid, ask, time, etc.) or None if stale/error.
        """
        try:
            tick = self._bridge.get_quote(symbol)
            if tick is None:
                return None

            now_wall = time.time()

            # Extract broker tick timestamp (use time_msc if available, else time)
            tick_epoch = getattr(tick, 'time_msc', 0)
            if tick_epoch == 0:
                # Fallback: tick.time may be datetime or numeric
                tick_time = getattr(tick, 'time', 0)
                if isinstance(tick_time, datetime):
                    tick_epoch = int(tick_time.timestamp() * 1000)
                elif isinstance(tick_time, (int, float)):
                    tick_epoch = int(tick_time * 1000)
                else:
                    tick_epoch = int(time.time() * 1000)

            # Check if tick has actually updated since last call
            prev_epoch, prev_wall = self._tick_tracker.get(symbol, (0, now_wall))

            if tick_epoch != prev_epoch:
                # New tick arrived — update tracker
                self._tick_tracker[symbol] = (tick_epoch, now_wall)
            else:
                # Same tick as before — check how long since last real update
                time_since_update = now_wall - prev_wall
                if time_since_update > self.STALE_FEED_TIMEOUT:
                    logger.warning(
                        f"STALE FEED: {symbol} — no new tick for {time_since_update:.1f}s "
                        f"(threshold={self.STALE_FEED_TIMEOUT}s). Skipping."
                    )
                    return None

            return tick
        except Exception as e:
            logger.warning(f"Tick error for {symbol}: {e}")
            return None

    def _get_price(self, symbol: str) -> Optional[float]:
        """Get latest mid-price for a symbol (with staleness check)."""
        tick = self._get_tick_data(symbol)
        if tick is not None:
            return (tick.bid + tick.ask) / 2.0
        return None

    def _check_spread(self, symbol: str, max_spread: float) -> bool:
        """
        Check if current spread for a symbol is within acceptable limits.
        Uses staleness-aware _get_tick_data() (not raw get_quote) so that
        stale feeds also block entry.
        Returns True if spread is OK, False if too wide or stale.
        """
        tick = self._get_tick_data(symbol)
        if tick is None:
            return False  # Stale or no data — block entry
        current_spread = tick.ask - tick.bid
        if current_spread > max_spread:
            logger.warning(
                f"SPREAD BLOWOUT: {symbol} spread={current_spread:.1f} pts > "
                f"max={max_spread:.1f} pts. Blocking entry."
            )
            return False
        return True

    # Minimum stop distance per asset class (points from current price).
    # Must exceed broker's STOPS_LEVEL to avoid "Invalid stops" rejection.
    MIN_STOP_DISTANCE = {
        'INDEX': 500.0,
        'COMMODITY': 5.0,      # Oil: 500 points (XTIUSD ~$70, 5.0 = ~7%)    # Indices: 500 points minimum (NAS100, DAX40)
        'FOREX': 0.0050,   # Forex: 50 pips minimum (AUDUSD, EURUSD etc.)
        'FOREX_JPY': 0.500, # JPY pairs: 50 pips minimum (1 pip = 0.01 for JPY)
    }

    # Minimum Welford buffer length before we trust the sigma for hard stops
    MIN_BUFFER_FOR_STOPS = 200

    def _get_asset_class(self, symbol: str) -> str:
        """Determine asset class from symbol name (JPY pairs need different stop distances)."""
        jpy_symbols = ('EURJPY', 'CHFJPY', 'GBPJPY', 'USDJPY', 'AUDJPY', 'NZDJPY', 'CADJPY',
                       'EURJPYm', 'CHFJPYm', 'GBPJPYm', 'USDJPYm')
        if symbol in jpy_symbols:
            return 'FOREX_JPY'
        commodity_symbols = ('XTIUSD', 'XBRUSD', 'WTI', 'BRENT', 'USOIL', 'UKOIL',
                             'CrudeOIL', 'BrentOIL', 'USOILm', 'UKOILm', 'WTIm', 'BRNm')
        if symbol in commodity_symbols:
            return 'COMMODITY'
        forex_symbols = ('AUDUSD', 'NZDUSD', 'EURUSD', 'GBPUSD', 'USDCAD', 'USDCHF',
                         'AUDUSDm', 'NZDUSDm', 'EURUSDm', 'GBPUSDm')
        if symbol in forex_symbols:
            return 'FOREX'
        return 'INDEX'

    def _calculate_hard_stops(
        self, state: PairState, direction: int, lots: float
    ) -> Tuple[float, float, float, float]:
        """
        Calculate server-side hard stops for both legs (Huber 4.815-sigma safety net).

        The spread sigma from Welford is in LOG-space (ln(A) - beta*ln(B)).
        To convert to PRICE-space: dx = price * d(ln(x)), so:
          stop_dist_A = price_A * HUBER * spread_sigma * weight_A
          stop_dist_B = price_B * HUBER * spread_sigma * weight_B

        Also enforces per-asset-class minimum stop distances to avoid
        "Invalid stops" rejection from the broker (STOPS_LEVEL constraint).

        These are *catastrophe* stops only — normal exits are handled by
        the ghost stop + dynamic Z exit logic.

        Returns: (sl_a, tp_a, sl_b, tp_b) — returns (0,0,0,0) if not enough data.
        """
        HUBER_SIGMA = 4.815

        price_a = state.last_price_a
        price_b = state.last_price_b

        if price_a <= 0 or price_b <= 0:
            return (0.0, 0.0, 0.0, 0.0)

        # Get spread sigma from Welford (LOG-space)
        spread_sigma = 0.0
        buffer_len = 0
        if state.coint_engine is not None:
            try:
                spread_sigma = state.coint_engine.last_std
                buffer_len = state.coint_engine.buffer_len
            except AttributeError:
                spread_sigma = 0.0

        # Don't trust sigma until we have enough data points
        if buffer_len < self.MIN_BUFFER_FOR_STOPS or spread_sigma <= 0:
            # Use fallback: 2% of price as catastrophe stop
            fallback_pct = 0.02
            stop_dist_a = price_a * fallback_pct
            stop_dist_b = price_b * fallback_pct
            logger.debug(
                f"HARD STOPS (fallback 2%%): {state.config.name} | "
                f"buffer={buffer_len}/{self.MIN_BUFFER_FOR_STOPS} | "
                f"Using 2%% price distance"
            )
        else:
            # Convert log-space sigma to price-space:
            # d(ln(x)) = dx/x => dx = x * d(ln(x))
            # Spread risk split: A gets 60%, B gets 40%
            stop_dist_a = price_a * HUBER_SIGMA * spread_sigma * 0.6
            stop_dist_b = price_b * HUBER_SIGMA * spread_sigma * 0.4

        # Enforce minimum stop distances per asset class
        asset_class_a = self._get_asset_class(state.config.symbol_a)
        asset_class_b = self._get_asset_class(state.config.symbol_b)
        min_dist_a = self.MIN_STOP_DISTANCE.get(asset_class_a, 500.0)
        min_dist_b = self.MIN_STOP_DISTANCE.get(asset_class_b, 500.0)
        stop_dist_a = max(stop_dist_a, min_dist_a)
        stop_dist_b = max(stop_dist_b, min_dist_b)

        if direction > 0:
            # Long spread: buy A (SL below), sell B (SL above)
            sl_a = round(price_a - stop_dist_a, 5)
            tp_a = 0.0
            sl_b = round(price_b + stop_dist_b, 5)
            tp_b = 0.0
        else:
            # Short spread: sell A (SL above), buy B (SL below)
            sl_a = round(price_a + stop_dist_a, 5)
            tp_a = 0.0
            sl_b = round(price_b - stop_dist_b, 5)
            tp_b = 0.0

        logger.info(
            f"HARD STOPS: {state.config.name} | spread_sigma={spread_sigma:.5f} | "
            f"buf={buffer_len} | dist_A={stop_dist_a:.1f} dist_B={stop_dist_b:.1f} | "
            f"SL_A={sl_a:.2f} SL_B={sl_b:.2f} (Huber {HUBER_SIGMA}sig)"
        )
        return (sl_a, tp_a, sl_b, tp_b)

    def _emergency_close_all(self, reason: str) -> None:
        """Emergency close all positions."""
        logger.critical(f"EMERGENCY CLOSE ALL: {reason}")
        if self._bridge:
            closed = self._bridge.close_all_positions()
            logger.critical(f"Closed {closed} positions")
        for state in self._pairs.values():
            state.position = 0
            state.ticket_a = 0
            state.ticket_b = 0

    def _save_state(self) -> None:
        """Save engine state for recovery."""
        state = {
            'version': '5.6',
            'timestamp': datetime.utcnow().isoformat(),
            'pairs': {
                name: {
                    'position': s.position,
                    'entry_z': s.entry_z,
                    'last_hurst': s.last_hurst,
                    'last_z_crit': s.last_z_crit,
                    'last_exit_z': s.last_exit_z,
                    'sentinel_aborted': s.sentinel_aborted,
                    'total_trades': s.total_trades,
                    'wins': s.wins,
                    'losses': s.losses,
                }
                for name, s in self._pairs.items()
            }
        }
        with open('state/engine_state.json', 'w') as f:
            json.dump(state, f, indent=2)

    async def _shutdown(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down v5.6 engine...")
        self._running = False
        self._save_state()
        if self._bridge:
            self._bridge.disconnect()
        logger.info("Shutdown complete")


# =============================================================================
# ENTRY POINT
# =============================================================================

async def main():
    """Main entry point."""
    engine = TradingEngine()

    if await engine.initialize():
        await engine.run()
    else:
        logger.error("Engine initialization failed")
        sys.exit(1)


if __name__ == '__main__':
    asyncio.run(main())
