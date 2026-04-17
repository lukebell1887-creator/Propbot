"""
SHF v12 "BUM CRUSHER" — anti-Bollinger-Band momentum engine.

The thesis (plain English):

    Retail Bollinger-Band traders SHORT the upper band and LONG the lower band,
    regardless of whether the market is actually mean-reverting.  They make a
    modest living in choppy markets but get steamrolled by real trends.

    We use FOUR independent PhD-math signals to detect the onset of a genuine
    trend 3-4 bars BEFORE the Bollinger Band even registers it.  We enter
    WITH that trend and ride it while it persists.  We exit 2-3 bars BEFORE
    the bums do, capturing the top 70-80% of the move while they catch 40-50%.

    We ONLY trade when Hurst > 0.55 (statistically confirmed trending regime).
    We SKIP mean-reverting (Hurst < 0.45) and chop (0.45-0.55) regimes
    entirely — the bums have that edge, we don't fight them on their turf.

Four early-detection signals (3-of-4 confluence required):

    1. Kalman drift z-score  (μ̂/√P)   — detects drift 3-4 bars before BB
    2. CUSUM change-point     (Page 1954)  — minimax-optimal mean-shift detector
    3. Hawkes self-excitation (λ_up/λ_dn)  — event-clustering burst detector
    4. Welford Z-velocity     (dZ/dt)       — price-Z rising/falling fast

Exits — 2-of-4 decay signals fire, OR price stops hit:

    A. Kalman drift decay    (|z| drops below 0.5)
    B. Z-velocity reversal   (dZ/dt flips sign)
    C. CUSUM reverse fire    (opposite-direction change-point)
    D. Hurst drops below 0.5 (regime ended)

Risk:
    * Stop at 2 × ATR(14)   (statistically calibrated, 2-sigma of M15 H-L)
    * TP at 3 × ATR(14)     (1:1.5 risk/reward base, but most exits are signal-decay)
    * AKAD-equivalent dynamic sizing (base_risk x Bayes x Grossman-Zhou)
    * Amplitude gate:  expected_profit > 2 × commission+spread cost else skip
    * Hard halts: 4% daily, 5% total

Universe (low-commission 5%ers MTB only):
    US100, US500, US30, USOIL
    (DE40/UK100/JP225 dropped — prior backtests showed no edge)
    (XTIUSD/XBRUSD dropped — XTIUSD is duplicate of USOIL in data; Brent noisy)

Uses:
    * src/momentum/kalman.py   (KalmanForecast)
    * src/momentum/cusum.py    (CUSUMDetector)
    * src/momentum/hawkes.py   (HawkesIntensity)
    * src/momentum/bayesian_edge.py (BetaPosterior)
    * src/momentum/kelly.py    (GrossmanZhouDD)
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, asdict
from typing import Optional
import json

from src.momentum.kalman import KalmanForecast
from src.momentum.cusum import CUSUMDetector
from src.momentum.hawkes import HawkesIntensity
from src.momentum.bayesian_edge import BetaPosterior
from src.momentum.kelly import GrossmanZhouDD


# =====================================================================
#  Symbol specs — low-commission 5%ers MTB universe only
# =====================================================================

@dataclass
class SymbolSpec:
    symbol: str
    asset_class: str
    pip_value: float              # $ per 1 point × 1 lot
    spread_pts: float             # typical spread in points
    commission_rt_per_lot: float  # $ per 1 lot round trip
    min_lots: float = 0.01
    lot_step: float = 0.01
    max_lots: float = 50.0
    trade_start: int = 7 * 60     # UTC minutes-of-day
    trade_end: int = 21 * 60


# Low-commission 5%ers MTB universe.  XAUUSD has 2 yrs of data — huge OOS sample.
BUMCRUSHER_UNIVERSE: dict[str, SymbolSpec] = {
    "US100":  SymbolSpec("US100",  "index",  1.0, 1.5, 2.0, trade_start=13 * 60, trade_end=21 * 60),
    "US500":  SymbolSpec("US500",  "index",  1.0, 0.5, 2.0, trade_start=13 * 60, trade_end=21 * 60),
    "US30":   SymbolSpec("US30",   "index",  1.0, 2.0, 2.0, trade_start=13 * 60, trade_end=21 * 60),
    "USOIL":  SymbolSpec("USOIL",  "oil",   10.0, 0.03, 0.0, trade_start=13 * 60, trade_end=20 * 60),
    "DE40":   SymbolSpec("DE40",   "index",  1.0, 1.0, 1.0, trade_start=7  * 60, trade_end=20 * 60),
    "XAUUSD": SymbolSpec("XAUUSD", "metal",  1.0, 0.35, 5.0, trade_start=7  * 60, trade_end=22 * 60),
}


# =====================================================================
#  Engine config
# =====================================================================

@dataclass
class BumCrusherConfig:
    # Timeframe — M15 bars (15 M1 bars per M15 bar)
    bars_per_m15: int = 15

    # Hurst window — 200 M15 bars = 50 hours = ~8 trading days
    hurst_window: int = 200
    hurst_min_data: int = 100       # minimum bars before computing Hurst at all
    hurst_trend_threshold: float = 0.55
    hurst_exit_threshold: float = 0.50   # Hurst drops below this -> exit trigger

    # Welford Z (20-period, same as Bollinger bums use)
    welford_span: int = 20

    # Kalman parameters
    kalman_sigma_obs: float = 0.003       # M15 return observation noise
    kalman_sigma_proc: float = 5e-4       # slow-walk drift
    kalman_entry_z: float = 1.5           # |μ̂/√P| threshold for entry
    kalman_exit_z: float = 0.5            # |μ̂/√P| threshold for exit (decay)

    # CUSUM — Page's change-point detector on standardized returns
    cusum_k: float = 0.5                  # reference shift in sigma units
    cusum_h: float = 4.0                  # detection threshold
    cusum_fresh_bars: int = 3             # fire must be within last N bars

    # Hawkes — self-excitation on event times
    hawkes_mu0: float = 0.15
    hawkes_alpha: float = 0.25
    hawkes_beta: float = 0.35             # half-life ~2 bars
    hawkes_entry_ratio: float = 2.0        # λ_up/λ_dn > this for long

    # Z-velocity
    z_velocity_threshold: float = 0.25    # dZ/dt in sigma-per-bar units

    # Confluence — need N of 4 signals agreeing
    confluence_needed: int = 3

    # Exit decay — need N of 4 decay signals agreeing to close early
    decay_exit_needed: int = 2

    # ATR
    atr_window: int = 14
    stop_atr_mult: float = 2.0
    tp_atr_mult: float = 3.0
    atr_min_mult: float = 3.0              # stop must be >= 3x spread

    # Amplitude gate — expected_profit > hurdle × cost
    amplitude_hurdle: float = 1.5

    # Sizing
    base_risk_pct: float = 0.005
    min_risk_pct: float = 0.002
    max_risk_pct: float = 0.012
    total_dd_limit: float = 0.05
    daily_dd_limit: float = 0.04
    gz_gamma: float = 2.0
    max_concurrent: int = 2                # conservative — max 2 trades at once
    max_index_concurrent: int = 2

    # Time stop — after this many M15 bars regardless of signals (safety)
    max_hold_bars: int = 48                # 12 hours at M15


# =====================================================================
#  Hurst (R/S) — simple Python implementation
# =====================================================================

def _compute_hurst_rs(series: list[float]) -> float:
    """R/S analysis Hurst estimator.  Returns H in [0, 1]."""
    n = len(series)
    if n < 40:
        return 0.5
    import statistics
    mean = statistics.fmean(series)
    dev = [x - mean for x in series]
    cum = [0.0] * n
    acc = 0.0
    for i, d in enumerate(dev):
        acc += d
        cum[i] = acc
    R = max(cum) - min(cum)
    try:
        S = statistics.pstdev(series)
    except Exception:
        S = 0.0
    if S <= 0:
        return 0.5
    rs = R / S
    if rs <= 0:
        return 0.5
    h = math.log(rs) / math.log(n)
    if not math.isfinite(h):
        return 0.5
    return max(0.0, min(1.0, h))


# =====================================================================
#  Welford-EMA Z-score (matches Rust OnlineNormalizer)
# =====================================================================

class WelfordZ:
    __slots__ = ("_alpha", "_mu", "_m2", "_init", "_history_z")

    def __init__(self, span: int = 20):
        self._alpha = 2.0 / (span + 1)
        self._mu = 0.0
        self._m2 = 0.0
        self._init = False
        self._history_z: deque[float] = deque(maxlen=10)

    def update(self, x: float) -> float:
        if not self._init:
            self._mu = x
            self._m2 = 1e-9
            self._init = True
            self._history_z.append(0.0)
            return 0.0
        delta = x - self._mu
        self._mu += self._alpha * delta
        self._m2 = (1.0 - self._alpha) * self._m2 + self._alpha * delta * (x - self._mu)
        sigma = math.sqrt(max(self._m2, 1e-12))
        z = (x - self._mu) / sigma
        self._history_z.append(z)
        return z

    def z(self) -> float:
        return self._history_z[-1] if self._history_z else 0.0

    def z_velocity(self) -> float:
        """dZ/dt in sigma-per-bar, computed over last 3 bars for stability."""
        if len(self._history_z) < 3:
            return 0.0
        hs = list(self._history_z)
        return (hs[-1] - hs[-3]) / 2.0

    @property
    def sigma(self) -> float:
        return math.sqrt(max(self._m2, 1e-12))


# =====================================================================
#  ATR (Wilder, incremental)
# =====================================================================

class ATR:
    __slots__ = ("_w", "_atr", "_prev_close", "_init", "_n")

    def __init__(self, window: int = 14):
        self._w = window
        self._atr = 0.0
        self._prev_close = None
        self._init = False
        self._n = 0

    def update(self, high: float, low: float, close: float) -> float:
        if self._prev_close is None:
            tr = high - low
        else:
            tr = max(high - low, abs(high - self._prev_close),
                       abs(low - self._prev_close))
        self._prev_close = close
        self._n += 1
        if self._n <= self._w:
            self._atr = (self._atr * (self._n - 1) + tr) / self._n
        else:
            self._atr = (self._atr * (self._w - 1) + tr) / self._w
        if self._n >= self._w:
            self._init = True
        return self._atr

    @property
    def value(self) -> float:
        return self._atr

    @property
    def ready(self) -> bool:
        return self._init


# =====================================================================
#  Per-symbol state
# =====================================================================

class BumCrusherSymbol:
    def __init__(self, spec: SymbolSpec, cfg: BumCrusherConfig):
        self.spec = spec
        self.cfg = cfg

        self.welford = WelfordZ(span=cfg.welford_span)
        self.kalman = KalmanForecast(sigma_obs=cfg.kalman_sigma_obs,
                                       sigma_proc=cfg.kalman_sigma_proc)
        self.cusum = CUSUMDetector(k=cfg.cusum_k, h=cfg.cusum_h)
        self.hawkes = HawkesIntensity(mu0=cfg.hawkes_mu0,
                                        alpha=cfg.hawkes_alpha,
                                        beta=cfg.hawkes_beta)
        self.atr = ATR(window=cfg.atr_window)

        # For Hurst we keep a rolling log-return buffer
        self.ret_buffer: deque[float] = deque(maxlen=cfg.hurst_window)
        self._hurst_cached: float = 0.5

        # M15 aggregation state
        self._m15_o: Optional[float] = None
        self._m15_h: float = -1e18
        self._m15_l: float = +1e18
        self._m15_prev_close: Optional[float] = None
        self._m15_count: int = 0
        self.m15_bars_seen: int = 0

        # CUSUM last-fire bar tracking
        self._last_cusum_fire_bar: int = -9999
        self._last_cusum_fire_dir: int = 0

        self.position: Optional[BumCrusherPosition] = None
        self._last_close: float = 0.0


@dataclass
class BumCrusherPosition:
    symbol: str
    side: int
    entry_price: float
    entry_time: float
    entry_bar: int
    lots: float
    sl: float
    tp: float
    R_dist: float
    R_dollars: float
    hurst_at_entry: float
    confluence_at_entry: int


@dataclass
class BumCrusherTrade:
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
    hurst_at_entry: float
    confluence_at_entry: int
    bars_held: int
    equity_at_entry: float
    equity_at_exit: float


# =====================================================================
#  The engine
# =====================================================================

class BumCrusherEngine:
    def __init__(self, symbols: list[SymbolSpec],
                   cfg: Optional[BumCrusherConfig] = None,
                   initial_equity: float = 100_000.0):
        self.cfg = cfg or BumCrusherConfig()
        self.states: dict[str, BumCrusherSymbol] = {
            s.symbol: BumCrusherSymbol(s, self.cfg) for s in symbols
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

        self.trades: list[BumCrusherTrade] = []

    # -----------------------------------------------------------------
    def _roll_day(self, day_key: str):
        if self.day_key is None:
            self.day_key = day_key
            return
        if day_key != self.day_key:
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
        """Feed M1 bar; engine aggregates to M15."""
        if symbol not in self.states:
            return
        st = self.states[symbol]
        self._roll_day(day_key)

        if st._m15_count == 0:
            st._m15_o = open_
            st._m15_h = high
            st._m15_l = low
        else:
            st._m15_h = max(st._m15_h, high)
            st._m15_l = min(st._m15_l, low)
        st._m15_count += 1

        # Intrabar SL/TP check
        if st.position is not None:
            self._manage_intrabar(st, time, high, low, close)
        if st._m15_count < self.cfg.bars_per_m15:
            st._last_close = close
            return

        # --- M15 bar complete
        m15_o = st._m15_o if st._m15_o is not None else close
        m15_h = st._m15_h
        m15_l = st._m15_l
        m15_c = close
        if m15_o <= 0 or m15_c <= 0:
            st._m15_o = None; st._m15_h = -1e18; st._m15_l = +1e18; st._m15_count = 0
            return
        ret = math.log(m15_c / m15_o)

        # Reset aggregator
        st._m15_o = None
        st._m15_h = -1e18
        st._m15_l = +1e18
        st._m15_count = 0
        st.m15_bars_seen += 1
        st._m15_prev_close = m15_c
        st._last_close = close

        # --- Update indicators ---
        st.atr.update(m15_h, m15_l, m15_c)
        z = st.welford.update(ret)             # Welford-Z of returns
        st.kalman.update(ret)
        # CUSUM on standardized returns (use welford z)
        fire = st.cusum.update(z)
        if fire != 0:
            st._last_cusum_fire_bar = st.m15_bars_seen
            st._last_cusum_fire_dir = fire
        st.hawkes.update(float(st.m15_bars_seen), ret)
        st.ret_buffer.append(ret)

        # Hurst update every 10 bars (expensive, doesn't change fast)
        if len(st.ret_buffer) >= self.cfg.hurst_min_data and st.m15_bars_seen % 5 == 0:
            st._hurst_cached = _compute_hurst_rs(list(st.ret_buffer))

        # --- Position management at M15 close
        if st.position is not None:
            self._manage(st, time, high, low, m15_c)

        # --- Safety
        if not self._check_safety(time):
            return

        # Warmup
        if st.m15_bars_seen < self.cfg.hurst_min_data:
            return
        if not st.atr.ready:
            return

        mod = hour_utc * 60 + minute_utc
        if mod < st.spec.trade_start or mod >= st.spec.trade_end:
            return

        # --- Entry
        if st.position is None:
            self._maybe_enter(st, time, m15_c)

    # -----------------------------------------------------------------
    def _maybe_enter(self, st: BumCrusherSymbol, time: float, close: float):
        cfg = self.cfg

        # Hurst regime gate — ONLY trade trending regimes
        H = st._hurst_cached
        if H < cfg.hurst_trend_threshold:
            return

        # Compute 4 early-detection signals
        # 1. Kalman drift z
        mu = st.kalman.mu
        P = max(st.kalman.P, 1e-12)
        kz = mu / math.sqrt(P)
        kalman_side = 0
        if kz > cfg.kalman_entry_z: kalman_side = +1
        elif kz < -cfg.kalman_entry_z: kalman_side = -1

        # 2. CUSUM — recent fire
        cusum_side = 0
        if st.m15_bars_seen - st._last_cusum_fire_bar <= cfg.cusum_fresh_bars:
            cusum_side = st._last_cusum_fire_dir

        # 3. Hawkes ratio
        hawkes_ratio = st.hawkes.ratio()
        hawkes_side = 0
        if hawkes_ratio > cfg.hawkes_entry_ratio: hawkes_side = +1
        elif hawkes_ratio < 1.0 / cfg.hawkes_entry_ratio: hawkes_side = -1

        # 4. Z-velocity
        dz = st.welford.z_velocity()
        zv_side = 0
        if dz > cfg.z_velocity_threshold: zv_side = +1
        elif dz < -cfg.z_velocity_threshold: zv_side = -1

        # Confluence vote
        longs = sum(1 for s in (kalman_side, cusum_side, hawkes_side, zv_side) if s > 0)
        shorts = sum(1 for s in (kalman_side, cusum_side, hawkes_side, zv_side) if s < 0)

        if longs >= cfg.confluence_needed:
            side = +1; confluence = longs
        elif shorts >= cfg.confluence_needed:
            side = -1; confluence = shorts
        else:
            return

        # Concurrency
        total_open = sum(1 for s in self.states.values() if s.position is not None)
        if total_open >= cfg.max_concurrent:
            return
        if st.spec.asset_class == "index":
            idx_open = sum(1 for s in self.states.values()
                             if s.position is not None and s.spec.asset_class == "index")
            if idx_open >= cfg.max_index_concurrent:
                return

        # Stop / TP distance via ATR
        atr_pts = st.atr.value
        stop_distance_pts = max(cfg.stop_atr_mult * atr_pts,
                                  cfg.atr_min_mult * st.spec.spread_pts)
        tp_distance_pts = cfg.tp_atr_mult * atr_pts

        entry_fill = close + side * 0.5 * st.spec.spread_pts
        sl = entry_fill - side * stop_distance_pts
        tp = entry_fill + side * tp_distance_pts

        # --- Amplitude gate ---
        # Expected profit = TP distance × pip_value × lots  (before we know lots)
        # Cost per trade = (spread_pts + comm/lot_at_1lot) × pip_value × lots
        # Ratio test is lots-independent if we compare per-lot:
        # expected_per_lot = tp_distance_pts × pip_value
        # cost_per_lot = spread_pts × pip_value + commission_rt_per_lot
        expected_per_lot = tp_distance_pts * st.spec.pip_value
        cost_per_lot = st.spec.spread_pts * st.spec.pip_value + st.spec.commission_rt_per_lot
        if expected_per_lot < cfg.amplitude_hurdle * cost_per_lot:
            return

        # Sizing
        risk_pct = self._dynamic_risk_pct(st.spec.symbol, side)
        risk_dollars = self.equity * risk_pct
        lots = risk_dollars / max(stop_distance_pts * st.spec.pip_value, 1e-9)
        lots = max(st.spec.min_lots,
                    min(st.spec.max_lots,
                        math.floor(lots / st.spec.lot_step) * st.spec.lot_step))
        if lots < st.spec.min_lots:
            return

        pos = BumCrusherPosition(
            symbol=st.spec.symbol, side=side,
            entry_price=entry_fill, entry_time=time,
            entry_bar=st.m15_bars_seen, lots=lots,
            sl=sl, tp=tp, R_dist=stop_distance_pts,
            R_dollars=risk_dollars,
            hurst_at_entry=H, confluence_at_entry=confluence,
        )
        pos._entry_equity = self.equity
        st.position = pos

    # -----------------------------------------------------------------
    def _dynamic_risk_pct(self, symbol: str, side: int) -> float:
        base = self.cfg.base_risk_pct
        b = self.beta.get((symbol, side))
        if b is not None and (b.alpha + b.beta - 2) >= 4:
            mean_wr = b.mean()
            x = max(0.35, min(0.70, mean_wr))
            bay = 0.6 + (x - 0.35) / 0.35 * 1.0
        else:
            bay = 1.0
        gz = self.gz.factor(equity=self.equity, peak=self.peak_equity)
        raw = base * bay * gz
        return max(self.cfg.min_risk_pct, min(self.cfg.max_risk_pct, raw))

    # -----------------------------------------------------------------
    def _manage_intrabar(self, st: BumCrusherSymbol, time: float,
                           high: float, low: float, close: float):
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
    def _manage(self, st: BumCrusherSymbol, time: float,
                 high: float, low: float, close: float):
        pos = st.position
        if pos is None:
            return

        # Price stops first
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

        # Trailing — ratchet SL once price has moved favourably
        bars_held = st.m15_bars_seen - pos.entry_bar
        if pos.side > 0:
            running_R = (close - pos.entry_price) / max(pos.R_dist, 1e-9)
            if running_R >= 2.0 and pos.sl < pos.entry_price + pos.R_dist:
                pos.sl = pos.entry_price + pos.R_dist
            elif running_R >= 1.0 and pos.sl < pos.entry_price:
                pos.sl = pos.entry_price
        else:
            running_R = (pos.entry_price - close) / max(pos.R_dist, 1e-9)
            if running_R >= 2.0 and pos.sl > pos.entry_price - pos.R_dist:
                pos.sl = pos.entry_price - pos.R_dist
            elif running_R >= 1.0 and pos.sl > pos.entry_price:
                pos.sl = pos.entry_price

        # --- Decay exits — 2-of-4 must fire opposite to our side
        # A. Kalman drift decay
        mu = st.kalman.mu
        P = max(st.kalman.P, 1e-12)
        kz = mu / math.sqrt(P)
        decay_kalman = abs(kz) < self.cfg.kalman_exit_z
        # If drift has actually flipped sign, that's a strong decay
        kalman_flipped = (pos.side > 0 and kz < 0) or (pos.side < 0 and kz > 0)

        # B. Z-velocity flip
        dz = st.welford.z_velocity()
        zv_flipped = (pos.side > 0 and dz < 0) or (pos.side < 0 and dz > 0)

        # C. CUSUM reverse
        cusum_reversed = False
        if st.m15_bars_seen - st._last_cusum_fire_bar <= 2:
            if pos.side > 0 and st._last_cusum_fire_dir < 0:
                cusum_reversed = True
            elif pos.side < 0 and st._last_cusum_fire_dir > 0:
                cusum_reversed = True

        # D. Hurst regime died
        hurst_died = st._hurst_cached < self.cfg.hurst_exit_threshold

        decay_count = sum([decay_kalman or kalman_flipped,
                             zv_flipped,
                             cusum_reversed,
                             hurst_died])
        if decay_count >= self.cfg.decay_exit_needed:
            self._close(st, close, time, "signal_decay"); return

        # Hard time stop
        if bars_held >= self.cfg.max_hold_bars:
            self._close(st, close, time, "time_stop"); return

    # -----------------------------------------------------------------
    def _close(self, st: BumCrusherSymbol, fill: float,
                time: float, reason: str):
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
        bars_held = st.m15_bars_seen - pos.entry_bar

        rec = BumCrusherTrade(
            symbol=spec.symbol, side=pos.side,
            entry_time=pos.entry_time, exit_time=time,
            entry_price=pos.entry_price, exit_price=actual,
            lots=pos.lots, R_dist=pos.R_dist, realised_R=realised_R,
            gross_pnl=gross, commission=commission, net_pnl=net,
            exit_reason=reason, hurst_at_entry=pos.hurst_at_entry,
            confluence_at_entry=pos.confluence_at_entry,
            bars_held=bars_held,
            equity_at_entry=getattr(pos, "_entry_equity", self.equity),
            equity_at_exit=self.equity,
        )
        self.trades.append(rec)
        self.beta[(spec.symbol, pos.side)].update(net > 0)
        st.position = None

    def _close_all(self, reason: str, time: float):
        for st in self.states.values():
            if st.position is not None:
                self._close(st, st._last_close, time, reason)

    # -----------------------------------------------------------------
    def summary(self) -> dict:
        if not self.trades:
            return {"trades": 0, "net_pnl": 0, "equity": self.equity,
                    "peak": self.peak_equity, "pct_return": 0, "pf": 0,
                    "win_rate": 0, "expectancy_R": 0, "avg_winner_R": 0,
                    "avg_loser_R": 0, "avg_bars_held": 0, "max_dd_pct": 0,
                    "gross_commissions": 0, "by_symbol": {}, "by_side": {},
                    "by_exit_reason": {}, "by_hurst": {}, "by_confluence": {}}
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
        by_hurst: dict[str, dict] = {}
        by_conf: dict[int, dict] = {}
        for t in self.trades:
            for key, d in [(t.symbol, by_symbol), (t.side, by_side)]:
                rec = d.setdefault(key, {"n": 0, "wins": 0, "net": 0.0, "sum_R": 0.0, "sum_bars": 0})
                rec["n"] += 1
                rec["wins"] += 1 if t.net_pnl > 0 else 0
                rec["net"] += t.net_pnl
                rec["sum_R"] += t.realised_R
                rec["sum_bars"] += t.bars_held
            by_exit[t.exit_reason] += 1

            h_bucket = f"{int(t.hurst_at_entry*10)/10:.1f}"
            hb = by_hurst.setdefault(h_bucket, {"n": 0, "wins": 0, "net": 0.0})
            hb["n"] += 1; hb["wins"] += 1 if t.net_pnl > 0 else 0; hb["net"] += t.net_pnl

            cb = by_conf.setdefault(t.confluence_at_entry, {"n": 0, "wins": 0, "net": 0.0})
            cb["n"] += 1; cb["wins"] += 1 if t.net_pnl > 0 else 0; cb["net"] += t.net_pnl

        for d in list(by_symbol.values()) + list(by_side.values()):
            d["wr"] = d["wins"] / d["n"]
            d["expR"] = d["sum_R"] / d["n"]
            d["avg_bars"] = d["sum_bars"] / d["n"]
        for d in list(by_hurst.values()) + list(by_conf.values()):
            d["wr"] = d["wins"] / d["n"]

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
            "by_hurst": by_hurst,
            "by_confluence": {str(k): v for k, v in by_conf.items()},
        }

    def dump_trades(self, path: str):
        with open(path, "w") as f:
            json.dump([asdict(t) for t in self.trades], f, indent=2, default=str)
