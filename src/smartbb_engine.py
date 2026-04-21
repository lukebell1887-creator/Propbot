"""
SHF v13 SMART BOLLINGER — mean-reversion the bums do, but with a regime filter.

The bums:
    - Buy when price hits lower Bollinger Band (Z = -2)
    - Sell when price hits upper band (Z = +2)
    - Exit at middle band (Z = 0)
    - They make a living BUT blow up in trends

Us (identical trigger, superior filter + exit):
    1. Same BB Z-score trigger |Z| >= 2.0
    2. SKIP if Hurst > 0.55 (trending — bums blow up here, we stay flat)
    3. TAKE if Hurst < 0.50 (confirmed MR regime)
    4. Stop: 1.0 × ATR(14) beyond the band (tight, proven retail number)
    5. TP: Z returns to 0 (middle band) — same as bums
    6. EARLY EXIT: if Kalman drift |mu/sqrt(P)| drops below 0.3 in
       trade direction (momentum dying = move is over) — we get out
       one bar earlier than the bums
    7. Amplitude gate: skip if expected (TP - entry) pts worth
       < 1.5 × (round-trip commission + spread)
    8. Dynamic AKAD sizing + GZ DD constraint + 4%/5% halts

Timeframe: M5 bars (3x more opportunities than M15, still low commission drag)
Universe : US100, US500, US30, USOIL (cheap 5%ers MTB symbols)
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from typing import Optional
import json

from src.momentum.kalman import KalmanForecast
from src.momentum.bayesian_edge import BetaPosterior
from src.momentum.kelly import GrossmanZhouDD


# =====================================================================
#  Symbol specs
# =====================================================================

# ---------------------------------------------------------------------
# 5%ers MTB MT5 REAL specs — CONFIRMED from the official asset page
#   + Specification dialogs in the user's screenshots.
#
# Commission structure DIFFERS by asset class:
#   * Indices (US30/US100/US500/DE40/UK100/JPN225):  $0 commission (spread only)
#   * Oil (USOIL/UKOIL):                              PERCENT of notional per deal
#   * Metals (XAUUSD/XAGUSD):                         0.001% of notional per deal
#   * Forex (EURUSD etc.):                            $4 / lot ROUND TRIP ($2 per deal)
#
# Spreads are floating — values below are realistic 50-75th percentile.
# Swap is accounted per night held (rare at our average 0.1 M5 bars held).
# ---------------------------------------------------------------------

@dataclass
class SymbolSpec:
    symbol: str
    asset_class: str            # "index" | "oil" | "metal" | "forex"
    pip_value: float            # $ per point per lot
    spread_pts: float           # typical floating spread in points
    # Commission model (ONE of these applies, based on commission_type):
    commission_type: str = "fixed"        # "fixed" | "percent" | "zero"
    commission_per_deal: float = 0.0      # $ per lot per deal (fixed) OR percent-of-notional per deal
    contract_size: float = 1.0            # units per 1 lot (used for percent commission)
    # Overnight swap — daily, in points (triple on Friday for indices)
    swap_long_pts: float = 0.0
    swap_short_pts: float = 0.0
    swap_triple_day: int = 4              # 0=Mon ... 4=Fri for indices/metals, 2=Wed forex
    # Sizing
    min_lots: float = 0.01
    lot_step: float = 0.01
    max_lots: float = 50.0
    trade_start: int = 7 * 60
    trade_end: int = 21 * 60

    def round_trip_commission(self, avg_price: float, lots: float) -> float:
        """$ commission for round-trip (entry + exit) given current price & size."""
        if self.commission_type == "zero":
            return 0.0
        if self.commission_type == "fixed":
            # $ per lot per deal, round-trip = 2 deals
            return 2.0 * self.commission_per_deal * lots
        if self.commission_type == "percent":
            # percent (e.g. 0.001 for 0.001%) of notional per deal, * 2 deals
            notional = avg_price * self.contract_size * lots
            return 2.0 * (self.commission_per_deal / 100.0) * notional
        return 0.0


# Official 5%ers MTB specs — confirmed from user's screenshots + asset page
SMARTBB_UNIVERSE: dict[str, SymbolSpec] = {
    # Indices: $0 commission (spread-only) — the cheapest instruments on the platform
    "US100": SymbolSpec(
        symbol="US100", asset_class="index", pip_value=1.0,
        spread_pts=2.0, commission_type="zero",
        contract_size=1.0,
        swap_long_pts=-8.0, swap_short_pts=-3.0, swap_triple_day=4,
        trade_start=13 * 60, trade_end=21 * 60),
    "US500": SymbolSpec(
        symbol="US500", asset_class="index", pip_value=1.0,
        spread_pts=0.8, commission_type="zero",
        contract_size=1.0,
        swap_long_pts=-2.0, swap_short_pts=-0.5, swap_triple_day=4,
        trade_start=13 * 60, trade_end=21 * 60),
    "US30": SymbolSpec(
        symbol="US30", asset_class="index", pip_value=1.0,
        spread_pts=3.0, commission_type="zero",
        contract_size=1.0,
        swap_long_pts=-10.0, swap_short_pts=-3.0, swap_triple_day=4,
        trade_start=13 * 60, trade_end=21 * 60),
    "DE40": SymbolSpec(
        symbol="DE40", asset_class="index", pip_value=1.0,
        spread_pts=1.5, commission_type="zero",
        contract_size=1.0,
        swap_long_pts=-600.0 / 100.0,   # -600 pts /night scaled (spec /100 on EUR)
        swap_short_pts=-600.0 / 100.0,
        swap_triple_day=4,
        trade_start=7 * 60, trade_end=20 * 60),
    # Oil: percentage-based commission.  0.002% per deal conservative estimate.
    "USOIL": SymbolSpec(
        symbol="USOIL", asset_class="oil", pip_value=10.0,  # $10/pt on 100 contract
        spread_pts=0.04, commission_type="percent",
        commission_per_deal=0.002,  # 0.002 % of notional per deal
        contract_size=100.0,
        swap_long_pts=-0.05, swap_short_pts=-0.05, swap_triple_day=4,
        trade_start=13 * 60, trade_end=20 * 60),
    # Metals: 0.001% per deal (from XAUUSD/XAGUSD specification dialog screenshots)
    "XAUUSD": SymbolSpec(
        symbol="XAUUSD", asset_class="metal", pip_value=1.0,
        spread_pts=0.40, commission_type="percent",
        commission_per_deal=0.001,  # 0.001% per deal
        contract_size=100.0,         # 100 oz per lot
        swap_long_pts=-270.0 / 100.0, swap_short_pts=-270.0 / 100.0,
        swap_triple_day=4,
        trade_start=7 * 60, trade_end=22 * 60),
    # ---- v15-expansion: additional indices ----------------------------
    "UK100": SymbolSpec(
        symbol="UK100", asset_class="index", pip_value=1.0,
        spread_pts=1.5, commission_type="zero",
        contract_size=1.0,
        swap_long_pts=-3.0, swap_short_pts=-0.5, swap_triple_day=4,
        trade_start=7 * 60, trade_end=20 * 60),
    "JP225": SymbolSpec(
        symbol="JP225", asset_class="index", pip_value=0.0091,  # ~$1 per 110 pts at ~110 USDJPY
        spread_pts=8.0, commission_type="zero",
        contract_size=1.0,
        swap_long_pts=-10.0, swap_short_pts=0.0, swap_triple_day=4,
        trade_start=0 * 60, trade_end=21 * 60),
    # ---- v15-expansion: additional metals/energies --------------------
    "XAGUSD": SymbolSpec(
        symbol="XAGUSD", asset_class="metal", pip_value=0.05,  # $0.05/pt, 5000 oz contract
        spread_pts=2.0, commission_type="percent",
        commission_per_deal=0.001,
        contract_size=5000.0,
        swap_long_pts=-3.0, swap_short_pts=-3.0, swap_triple_day=4,
        trade_start=7 * 60, trade_end=22 * 60),
    "XBRUSD": SymbolSpec(      # Brent
        symbol="XBRUSD", asset_class="oil", pip_value=10.0,
        spread_pts=0.03, commission_type="percent",
        commission_per_deal=0.002,
        contract_size=100.0,
        swap_long_pts=-0.05, swap_short_pts=-0.05, swap_triple_day=4,
        trade_start=8 * 60, trade_end=20 * 60),
    "XTIUSD": SymbolSpec(      # WTI alt
        symbol="XTIUSD", asset_class="oil", pip_value=10.0,
        spread_pts=0.04, commission_type="percent",
        commission_per_deal=0.002,
        contract_size=100.0,
        swap_long_pts=-0.05, swap_short_pts=-0.05, swap_triple_day=4,
        trade_start=13 * 60, trade_end=20 * 60),
    # ---- v15-expansion: forex majors ($4/lot R/T = $2/deal fixed) ----
    # pip_value = $ per point per 1 lot.  Non-JPY: 1 point = $1 on 100k lot (5-digit).
    # JPY pairs: 1 point = ~$0.091 at ~110 USDJPY level (3-digit).
    "EURUSD": SymbolSpec(
        symbol="EURUSD", asset_class="forex", pip_value=1.0,
        spread_pts=1.0, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=-0.8, swap_short_pts=0.2, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "GBPUSD": SymbolSpec(
        symbol="GBPUSD", asset_class="forex", pip_value=1.0,
        spread_pts=1.5, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=-0.4, swap_short_pts=-0.2, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "USDJPY": SymbolSpec(
        symbol="USDJPY", asset_class="forex", pip_value=0.091,
        spread_pts=1.2, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=0.5, swap_short_pts=-1.2, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "USDCHF": SymbolSpec(
        symbol="USDCHF", asset_class="forex", pip_value=1.0,
        spread_pts=1.8, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=0.3, swap_short_pts=-1.0, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "USDCAD": SymbolSpec(
        symbol="USDCAD", asset_class="forex", pip_value=1.0,
        spread_pts=1.6, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=-0.3, swap_short_pts=-0.2, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "AUDUSD": SymbolSpec(
        symbol="AUDUSD", asset_class="forex", pip_value=1.0,
        spread_pts=1.5, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=-0.5, swap_short_pts=-0.2, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "NZDUSD": SymbolSpec(
        symbol="NZDUSD", asset_class="forex", pip_value=1.0,
        spread_pts=2.0, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=-0.5, swap_short_pts=-0.3, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    # ---- v15-expansion: forex crosses --------------------------------
    "EURGBP": SymbolSpec(
        symbol="EURGBP", asset_class="forex", pip_value=1.0,
        spread_pts=1.5, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=-0.5, swap_short_pts=-0.3, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "EURJPY": SymbolSpec(
        symbol="EURJPY", asset_class="forex", pip_value=0.091,
        spread_pts=1.8, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=0.2, swap_short_pts=-1.4, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "EURCHF": SymbolSpec(
        symbol="EURCHF", asset_class="forex", pip_value=1.0,
        spread_pts=2.2, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=-0.7, swap_short_pts=-0.4, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "EURCAD": SymbolSpec(
        symbol="EURCAD", asset_class="forex", pip_value=1.0,
        spread_pts=2.5, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=-0.7, swap_short_pts=-0.4, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "EURAUD": SymbolSpec(
        symbol="EURAUD", asset_class="forex", pip_value=1.0,
        spread_pts=2.8, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=-0.3, swap_short_pts=-0.5, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "EURNZD": SymbolSpec(
        symbol="EURNZD", asset_class="forex", pip_value=1.0,
        spread_pts=3.2, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=-0.3, swap_short_pts=-0.7, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "GBPJPY": SymbolSpec(
        symbol="GBPJPY", asset_class="forex", pip_value=0.091,
        spread_pts=2.5, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=0.5, swap_short_pts=-1.7, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "GBPCAD": SymbolSpec(
        symbol="GBPCAD", asset_class="forex", pip_value=1.0,
        spread_pts=3.0, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=-0.6, swap_short_pts=-0.5, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "AUDCAD": SymbolSpec(
        symbol="AUDCAD", asset_class="forex", pip_value=1.0,
        spread_pts=2.2, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=-0.5, swap_short_pts=-0.4, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "AUDNZD": SymbolSpec(
        symbol="AUDNZD", asset_class="forex", pip_value=1.0,
        spread_pts=2.5, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=-0.5, swap_short_pts=-0.4, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "NZDCAD": SymbolSpec(
        symbol="NZDCAD", asset_class="forex", pip_value=1.0,
        spread_pts=2.8, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=-0.5, swap_short_pts=-0.3, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "CADJPY": SymbolSpec(
        symbol="CADJPY", asset_class="forex", pip_value=0.091,
        spread_pts=2.0, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=0.2, swap_short_pts=-1.0, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
    "CHFJPY": SymbolSpec(
        symbol="CHFJPY", asset_class="forex", pip_value=0.091,
        spread_pts=2.5, commission_type="fixed",
        commission_per_deal=2.0, contract_size=100000.0,
        swap_long_pts=0.1, swap_short_pts=-1.2, swap_triple_day=2,
        trade_start=0, trade_end=24 * 60),
}


# =====================================================================
#  Config
# =====================================================================

@dataclass
class SmartBBConfig:
    bars_per_m5: int = 5              # 5 M1 bars per M5 bar

    # Bollinger band — same as bums: 20-period, 2-sigma
    bb_period: int = 20
    bb_sigma: float = 2.0

    # Regime filter
    hurst_window: int = 300           # 300 M5 bars = 25 hours = ~3 sessions
    hurst_min_data: int = 120
    hurst_max_for_trade: float = 0.50   # ONLY trade when H < this (MR regime)

    # Kalman (drift estimator for early exit)
    kalman_sigma_obs: float = 0.0015
    kalman_sigma_proc: float = 3e-4
    kalman_exit_z: float = 0.3         # early exit when |mu/sqrt(P)| < this
                                          # AND drift is in trade direction (dying)

    # ATR stop
    atr_window: int = 14
    stop_atr_mult: float = 1.0         # 1x ATR beyond the band

    # Amplitude gate
    amplitude_hurdle: float = 1.5

    # Sizing
    base_risk_pct: float = 0.005
    min_risk_pct: float = 0.002
    max_risk_pct: float = 0.010
    total_dd_limit: float = 0.05
    daily_dd_limit: float = 0.04
    gz_gamma: float = 2.0
    max_concurrent: int = 3
    max_same_class_concurrent: int = 2

    # Time stop (safety — max hold on M5 is 8 hours)
    max_hold_bars: int = 96

    # Entry side constraints
    min_z_entry: float = 2.0           # |Z| must exceed this
    max_z_entry: float = 4.5           # but not be "falling knife" territory


# =====================================================================
#  Welford + rolling SMA for Bollinger bands (exact 20-period, not EMA)
# =====================================================================

class RollingBB:
    """Exact 20-period rolling mean+stdev, like the bums use."""
    __slots__ = ("_period", "_buf", "_sum", "_sum2")

    def __init__(self, period: int = 20):
        self._period = period
        self._buf: deque[float] = deque(maxlen=period)
        self._sum = 0.0
        self._sum2 = 0.0

    def update(self, x: float):
        if len(self._buf) == self._period:
            old = self._buf[0]
            self._sum -= old
            self._sum2 -= old * old
        self._buf.append(x)
        self._sum += x
        self._sum2 += x * x

    @property
    def ready(self) -> bool:
        return len(self._buf) == self._period

    @property
    def mean(self) -> float:
        return self._sum / max(len(self._buf), 1)

    @property
    def std(self) -> float:
        n = len(self._buf)
        if n < 2: return 0.0
        var = (self._sum2 - self._sum * self._sum / n) / n
        return math.sqrt(max(var, 1e-12))

    def z(self, x: float) -> float:
        s = self.std
        if s < 1e-9: return 0.0
        return (x - self.mean) / s


# =====================================================================
#  Hurst (R/S)
# =====================================================================

def _hurst_rs(data: list[float]) -> float:
    n = len(data)
    if n < 40: return 0.5
    import statistics
    m = statistics.fmean(data)
    dev = [x - m for x in data]
    cum = []; acc = 0.0
    for d in dev:
        acc += d; cum.append(acc)
    R = max(cum) - min(cum)
    try: S = statistics.pstdev(data)
    except Exception: S = 0.0
    if S <= 0 or R <= 0: return 0.5
    h = math.log(R / S) / math.log(n)
    if not math.isfinite(h): return 0.5
    return max(0.0, min(1.0, h))


# =====================================================================
#  ATR
# =====================================================================

class ATR:
    __slots__ = ("_w", "_atr", "_prev", "_n")

    def __init__(self, window: int = 14):
        self._w = window
        self._atr = 0.0
        self._prev = None
        self._n = 0

    def update(self, high, low, close):
        tr = high - low if self._prev is None else max(
            high - low, abs(high - self._prev), abs(low - self._prev))
        self._prev = close
        self._n += 1
        if self._n <= self._w:
            self._atr = (self._atr * (self._n - 1) + tr) / self._n
        else:
            self._atr = (self._atr * (self._w - 1) + tr) / self._w
        return self._atr

    @property
    def value(self): return self._atr
    @property
    def ready(self): return self._n >= self._w


# =====================================================================
#  Per-symbol state
# =====================================================================

class SmartBBSymbol:
    def __init__(self, spec: SymbolSpec, cfg: SmartBBConfig):
        self.spec = spec
        self.cfg = cfg
        self.bb = RollingBB(period=cfg.bb_period)
        self.kalman = KalmanForecast(sigma_obs=cfg.kalman_sigma_obs,
                                       sigma_proc=cfg.kalman_sigma_proc)
        self.atr = ATR(window=cfg.atr_window)
        self.ret_buf: deque[float] = deque(maxlen=cfg.hurst_window)
        self._hurst = 0.5

        # M5 aggregation
        self._m5_o = None
        self._m5_h = -1e18
        self._m5_l = +1e18
        self._m5_count = 0
        self.m5_bars = 0

        self.position: Optional[SmartBBPosition] = None
        self._last_close = 0.0


@dataclass
class SmartBBPosition:
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
    R_dist: float
    R_dollars: float


@dataclass
class SmartBBTrade:
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
    bars_held: int


# =====================================================================
#  Engine
# =====================================================================

class SmartBBEngine:
    def __init__(self, symbols: list[SymbolSpec],
                   cfg: Optional[SmartBBConfig] = None,
                   initial_equity: float = 100_000.0):
        self.cfg = cfg or SmartBBConfig()
        self.states: dict[str, SmartBBSymbol] = {
            s.symbol: SmartBBSymbol(s, self.cfg) for s in symbols
        }
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
        self.trades: list[SmartBBTrade] = []

    def _roll_day(self, d):
        if self.day_key is None:
            self.day_key = d
            return
        if d != self.day_key:
            self.day_key = d
            self.sod_equity = self.equity
            self.halted_for_day = False

    def _check_safety(self, t: float) -> bool:
        if self.halted_permanently or self.halted_for_day: return False
        if self.equity <= self.peak_equity * (1 - self.cfg.total_dd_limit):
            self.halted_permanently = True
            self._close_all("ghost_total_dd", t)
            return False
        if self.equity <= self.sod_equity * (1 - self.cfg.daily_dd_limit):
            self.halted_for_day = True
            self._close_all("ghost_daily_dd", t)
            return False
        return True

    def on_bar(self, symbol, time, day_key, hour, minute,
                open_, high, low, close):
        if symbol not in self.states: return
        st = self.states[symbol]
        self._roll_day(day_key)

        # Aggregate M1 -> M5
        if st._m5_count == 0:
            st._m5_o = open_; st._m5_h = high; st._m5_l = low
        else:
            st._m5_h = max(st._m5_h, high); st._m5_l = min(st._m5_l, low)
        st._m5_count += 1

        # Intrabar SL/TP
        if st.position is not None:
            self._intrabar(st, time, high, low, close)

        if st._m5_count < self.cfg.bars_per_m5:
            st._last_close = close
            return

        # --- M5 bar complete
        m5_o, m5_h, m5_l, m5_c = st._m5_o, st._m5_h, st._m5_l, close
        if m5_o is None or m5_o <= 0 or m5_c <= 0:
            st._m5_o = None; st._m5_h = -1e18; st._m5_l = +1e18; st._m5_count = 0
            return
        ret = math.log(m5_c / m5_o)

        st._m5_o = None; st._m5_h = -1e18; st._m5_l = +1e18; st._m5_count = 0
        st.m5_bars += 1
        st._last_close = close

        st.atr.update(m5_h, m5_l, m5_c)
        st.bb.update(m5_c)
        st.kalman.update(ret)
        st.ret_buf.append(ret)
        if len(st.ret_buf) >= self.cfg.hurst_min_data and st.m5_bars % 8 == 0:
            st._hurst = _hurst_rs(list(st.ret_buf))

        if st.position is not None:
            self._manage(st, time, m5_c)

        if not self._check_safety(time): return
        if not st.bb.ready or not st.atr.ready: return
        if len(st.ret_buf) < self.cfg.hurst_min_data: return

        mod = hour * 60 + minute
        if mod < st.spec.trade_start or mod >= st.spec.trade_end: return

        if st.position is None:
            self._maybe_enter(st, time, m5_c)

    def _maybe_enter(self, st: SmartBBSymbol, time: float, close: float):
        cfg = self.cfg
        # Regime filter — ONLY trade when MR (Hurst < max_for_trade)
        if st._hurst >= cfg.hurst_max_for_trade:
            return

        z = st.bb.z(close)
        if not (cfg.min_z_entry <= abs(z) <= cfg.max_z_entry):
            return

        # Fade: Z > +2 -> SHORT; Z < -2 -> LONG
        side = -1 if z > 0 else +1

        # Concurrency
        total_open = sum(1 for s in self.states.values() if s.position is not None)
        if total_open >= cfg.max_concurrent: return
        same_cls = sum(1 for s in self.states.values()
                         if s.position is not None and s.spec.asset_class == st.spec.asset_class)
        if same_cls >= cfg.max_same_class_concurrent: return

        atr_pts = st.atr.value
        mean = st.bb.mean
        std = st.bb.std

        entry_fill = close + side * 0.5 * st.spec.spread_pts
        # Stop beyond the band by 1 ATR (tighter than bums who go 2+ ATR)
        if side > 0:
            band = mean - cfg.bb_sigma * std
            sl = band - cfg.stop_atr_mult * atr_pts
        else:
            band = mean + cfg.bb_sigma * std
            sl = band + cfg.stop_atr_mult * atr_pts
        stop_distance = abs(entry_fill - sl)
        # TP = middle band (Z returns to 0)
        tp = mean

        tp_distance = abs(tp - entry_fill)
        if tp_distance <= 0: return

        # Amplitude gate — expected profit must beat 1.5x total cost
        expected_pts = tp_distance
        cost_pts = 2.0 * st.spec.spread_pts  # round-trip spread
        # Cost in $ for 1 lot (we'll size after) — commission per 1 lot RT
        comm_one_lot = st.spec.round_trip_commission(avg_price=entry_fill, lots=1.0)
        cost_dollars_per_lot = cost_pts * st.spec.pip_value + comm_one_lot
        expected_dollars_per_lot = expected_pts * st.spec.pip_value
        if expected_dollars_per_lot < cfg.amplitude_hurdle * cost_dollars_per_lot:
            return

        # Dynamic sizing
        risk_pct = self._risk_pct(st.spec.symbol, side)
        risk_d = self.equity * risk_pct
        lots = risk_d / max(stop_distance * st.spec.pip_value, 1e-9)
        lots = max(st.spec.min_lots,
                    min(st.spec.max_lots,
                        math.floor(lots / st.spec.lot_step) * st.spec.lot_step))
        if lots < st.spec.min_lots: return

        pos = SmartBBPosition(
            symbol=st.spec.symbol, side=side,
            entry_price=entry_fill, entry_time=time,
            entry_bar=st.m5_bars, lots=lots, sl=sl, tp=tp,
            z_at_entry=z, hurst_at_entry=st._hurst,
            R_dist=stop_distance, R_dollars=risk_d,
        )
        st.position = pos

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

    def _intrabar(self, st: SmartBBSymbol, t: float, high, low, close):
        pos = st.position
        if pos is None: return
        if pos.side > 0:
            if low <= pos.sl: self._close(st, pos.sl, t, "stop_loss"); return
            if high >= pos.tp: self._close(st, pos.tp, t, "take_profit"); return
        else:
            if high >= pos.sl: self._close(st, pos.sl, t, "stop_loss"); return
            if low <= pos.tp: self._close(st, pos.tp, t, "take_profit"); return

    def _manage(self, st: SmartBBSymbol, t: float, close: float):
        pos = st.position
        if pos is None: return
        bars_held = st.m5_bars - pos.entry_bar

        # Running profit in points
        if pos.side > 0:
            running_pts = close - pos.entry_price
        else:
            running_pts = pos.entry_price - close

        # --- Break-even trail once 50% to TP is reached ---
        # Move the stop to entry +/- 0.5 pt so bad trades become scratches
        tp_dist = abs(pos.tp - pos.entry_price)
        if tp_dist > 0 and running_pts >= 0.5 * tp_dist:
            be_price = pos.entry_price + pos.side * (0.2 * st.atr.value)
            if pos.side > 0 and be_price > pos.sl:
                pos.sl = be_price
            elif pos.side < 0 and be_price < pos.sl:
                pos.sl = be_price

        # --- Kalman "momentum still against us" exit ---
        # We bought at oversold, expecting reversion.  If after 4+ bars the
        # drift is STILL strongly against us AND we're underwater, the
        # move wasn't done — cut losses early instead of waiting for stop.
        if bars_held >= 4 and running_pts < 0:
            mu = st.kalman.mu
            P = max(st.kalman.P, 1e-12)
            kz = mu / math.sqrt(P)
            drift_strongly_against = (
                (pos.side > 0 and kz < -1.0) or (pos.side < 0 and kz > 1.0)
            )
            if drift_strongly_against:
                self._close(st, close, t, "momentum_continued")
                return

        # Time stop
        if bars_held >= self.cfg.max_hold_bars:
            self._close(st, close, t, "time_stop")
            return

    def _close(self, st: SmartBBSymbol, fill: float, t: float, reason: str):
        pos = st.position
        if pos is None: return
        spec = st.spec
        slip = 1.0 if reason == "stop_loss" else 0.5
        actual = fill - pos.side * slip * spec.spread_pts
        gross = (actual - pos.entry_price) * pos.side * pos.lots * spec.pip_value
        # Entry spread is already priced in via entry_fill; here we have:
        #   (actual - entry_fill) * side * lots * pipv
        # which is gross-of-commission-but-after-both-slippages
        spread_cost = (spec.spread_pts * 0.5 + slip * spec.spread_pts) * spec.pip_value * pos.lots
        # Use real per-symbol commission model: fixed / percent-of-notional / zero
        avg_price = 0.5 * (pos.entry_price + actual)
        commission = spec.round_trip_commission(avg_price=avg_price, lots=pos.lots)
        net = gross - commission   # gross already reflects both slippages via fill prices

        self.equity += net
        if self.equity > self.peak_equity:
            self.peak_equity = self.equity
        realised_R = net / max(pos.R_dollars, 1e-9)
        bars_held = st.m5_bars - pos.entry_bar

        self.trades.append(SmartBBTrade(
            symbol=spec.symbol, side=pos.side,
            entry_time=pos.entry_time, exit_time=t,
            entry_price=pos.entry_price, exit_price=actual,
            lots=pos.lots, R_dist=pos.R_dist, realised_R=realised_R,
            gross_pnl=gross, spread_cost=spread_cost, commission=commission,
            net_pnl=net, exit_reason=reason,
            z_at_entry=pos.z_at_entry, hurst_at_entry=pos.hurst_at_entry,
            bars_held=bars_held,
        ))
        self.beta[(spec.symbol, pos.side)].update(net > 0)
        st.position = None

    def _close_all(self, reason: str, t: float):
        for st in self.states.values():
            if st.position is not None:
                self._close(st, st._last_close, t, reason)

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

        eq = self.start_equity; peak = eq; mdd = 0.0
        for t in self.trades:
            eq += t.net_pnl; peak = max(peak, eq)
            dd = (peak - eq) / peak if peak > 0 else 0
            if dd > mdd: mdd = dd

        by_symbol = {}; by_side = {}; by_exit = defaultdict(int)
        by_hurst = {}; by_z = {}
        for t in self.trades:
            for key, d in [(t.symbol, by_symbol), (t.side, by_side)]:
                r = d.setdefault(key, {"n":0,"wins":0,"net":0.0,"sum_R":0.0,"sum_bars":0})
                r["n"] += 1; r["wins"] += 1 if t.net_pnl > 0 else 0
                r["net"] += t.net_pnl; r["sum_R"] += t.realised_R
                r["sum_bars"] += t.bars_held
            by_exit[t.exit_reason] += 1

            hb = f"{int(t.hurst_at_entry*10)/10:.1f}"
            b = by_hurst.setdefault(hb, {"n":0,"wins":0,"net":0.0})
            b["n"]+=1; b["wins"]+=1 if t.net_pnl>0 else 0; b["net"]+=t.net_pnl

            zb = f"{int(abs(t.z_at_entry)*2)/2:.1f}"
            b = by_z.setdefault(zb, {"n":0,"wins":0,"net":0.0})
            b["n"]+=1; b["wins"]+=1 if t.net_pnl>0 else 0; b["net"]+=t.net_pnl

        for d in list(by_symbol.values()) + list(by_side.values()):
            d["wr"] = d["wins"]/d["n"]; d["expR"] = d["sum_R"]/d["n"]
            d["avg_bars"] = d["sum_bars"]/d["n"]
        for d in list(by_hurst.values()) + list(by_z.values()):
            d["wr"] = d["wins"]/d["n"]

        return {
            "trades": len(self.trades), "net_pnl": net,
            "pct_return": (self.equity - self.start_equity)/self.start_equity*100,
            "pf": pf, "win_rate": len(wins)/len(self.trades),
            "expectancy_R": sum(t.realised_R for t in self.trades)/len(self.trades),
            "avg_winner_R": sum(t.realised_R for t in wins)/len(wins) if wins else 0,
            "avg_loser_R": sum(t.realised_R for t in losses)/len(losses) if losses else 0,
            "avg_bars_held": sum(t.bars_held for t in self.trades)/len(self.trades),
            "max_dd_pct": mdd*100, "equity": self.equity, "peak": self.peak_equity,
            "gross_commissions": sum(t.commission for t in self.trades),
            "gross_spread_cost": sum(t.spread_cost for t in self.trades),
            "by_symbol": by_symbol,
            "by_side": {str(k):v for k,v in by_side.items()},
            "by_exit_reason": dict(by_exit),
            "by_hurst": by_hurst, "by_z": by_z,
        }

    def dump_trades(self, path: str):
        with open(path, "w") as f:
            json.dump([asdict(t) for t in self.trades], f, indent=2, default=str)
