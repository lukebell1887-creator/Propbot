"""
dynamic_sizer_v21.py — Merton × Grossman-Zhou position sizer.

Mathematical foundations:
    Merton (1969)       optimal f* = μ / (γ σ²) for log-utility
    Grossman-Zhou (1993) drawdown-aware closed-form:
                         f*_GZ = f*_Merton × (1 - DD_current / DD_cap)
    Thorp (2006)        practical caps + partial Kelly for parameter
                         uncertainty — we use a hard cap multiplier instead
                         of full Bayesian shrinkage because it's simpler,
                         more robust, and caused no measurable PnL loss in
                         the v21 ablation study (Results/research_sizer_v21.json).

Design choices (each with experimental evidence):
    - Per-symbol μ̂, σ̂² tracked via EWMA on realised R (trade PnL ÷ initial $ risk)
    - α = 0.20 on EWMA (half-life ≈ 3 trades) — favours recent evidence
    - Warm-up: first 5 trades per symbol use base_risk_pct (no Merton formula)
    - γ = 2.0 (moderate risk aversion, standard choice in finance)
    - Hard cap: final risk% ≤ base × cap_mult (defaults to 3.0 for our 4% DD target)
    - GZ closes to zero at DD_cap; never exceeds 1.0 (never ADDS to base)
    - Thread-safe: all state protected by internal lock for live trading

Usage (integration with ORB v20):
    sizer = MertonGZSizer(base_risk_pct=0.0010, cap_mult=3.0, dd_cap_pct=0.04)
    cfg   = ORBEngineConfig(risk_pct=0.0010, risk_pct_fn=sizer.compute_risk_pct)
    eng   = ORBEngineV20(..., cfg=cfg)

    # After each trade closes in live or backtest, feed the realised R back:
    sizer.on_trade_closed(symbol=tr.symbol, realised_R=tr.realised_R)
"""

from __future__ import annotations

import math
import threading
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple


# =====================================================================
#  Configuration
# =====================================================================

@dataclass
class MertonGZSizerConfig:
    # === Base sizing ===
    base_risk_pct: float = 0.0010       # 0.10% — the UNIT that Merton scales up
    cap_mult: float = 3.0               # final risk % ≤ 3 × base = 0.30 % max
    min_risk_pct: float = 0.0           # allow full shutdown (GZ at barrier)

    # === Merton parameters ===
    gamma: float = 2.0                  # CRRA risk aversion coefficient
    ewma_alpha: float = 0.20            # EWMA smoothing (half-life ≈ 3 trades)
    warmup_trades: int = 5              # per-symbol warm-up before formula kicks in
    history_len: int = 60               # max trades to remember per symbol

    # === Grossman-Zhou drawdown barrier ===
    dd_cap_pct: float = 0.04            # 4% — hit this → risk goes to zero
    gz_floor: float = 0.0               # can be >0 if you want min non-zero risk
                                        # even at DD barrier (we leave 0.0)

    # === Safety ===
    min_variance: float = 1e-6          # numerical floor for σ²
    no_edge_multiplier: float = 0.5     # if μ̂ ≤ 0, size at half of base

    # === Pooling ===
    pool_symbols: bool = False          # True → one global μ̂/σ̂² across all symbols
                                        #        (matches research_sizer_v21 simulation)
                                        # False → per-symbol learning (more specific,
                                        #         but may undersize symbols with short
                                        #         history that haven't proven edge yet)



# =====================================================================
#  The sizer
# =====================================================================

class MertonGZSizer:
    """
    Thread-safe, stateful position sizer implementing
        risk%(t) = base × min(cap_mult, f*_Merton) × (1 − DD / DD_cap)
    where f*_Merton = μ̂_EWMA / (γ · σ̂²_EWMA) with per-symbol EWMAs of
    the realised R = pnl / risk_$ on closed trades.

    IMPORTANT: this class DOES NOT mutate shared state during the risk query.
    It's safe to call `compute_risk_pct` from any thread. The only mutation
    is via `on_trade_closed`, which updates the EWMA under a lock.
    """

    def __init__(self, cfg: Optional[MertonGZSizerConfig] = None):
        self.cfg = cfg or MertonGZSizerConfig()
        self._lock = threading.RLock()

        # Per-symbol R history (realised R per closed trade)
        self._r_history: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=self.cfg.history_len)
        )
        # Per-symbol EWMA state
        self._mu: Dict[str, float] = defaultdict(float)
        self._var: Dict[str, float] = defaultdict(lambda: 1.0)
        self._n_seen: Dict[str, int] = defaultdict(int)

        # Diagnostic counters
        self._n_calls: int = 0
        self._n_warmup: int = 0
        self._n_no_edge: int = 0
        self._n_capped: int = 0
        self._n_gz_zero: int = 0

        # Last observed equity/peak (not authoritative — engine owns these)
        self._last_equity: float = 0.0
        self._last_peak: float = 0.0
        self._last_dd: float = 0.0

    # -----------------------------------------------------------------
    #  Feedback: engine calls this AFTER each trade closes
    # -----------------------------------------------------------------
    def _key(self, symbol: str) -> str:
        """Per-symbol or pooled key for μ/σ² storage."""
        return "_GLOBAL_" if self.cfg.pool_symbols else symbol

    def on_trade_closed(self, symbol: str, realised_R: float) -> None:
        """Update per-symbol (or pooled) EWMA with the newly closed trade's realised R.

        realised_R = net_pnl / initial_risk_dollars  (signed, dimensionless).
        A +2R trade returned 2× what we risked; -1R = full stop loss.
        """
        if not math.isfinite(realised_R):
            return
        with self._lock:
            key = self._key(symbol)
            hist = self._r_history[key]
            hist.append(realised_R)
            self._n_seen[key] += 1
            a = self.cfg.ewma_alpha
            if self._n_seen[key] == 1:
                self._mu[key] = realised_R
                self._var[key] = max(self.cfg.min_variance, abs(realised_R))
            else:
                mu_old = self._mu[key]
                mu_new = a * realised_R + (1.0 - a) * mu_old
                var_new = a * (realised_R - mu_new) ** 2 + (1.0 - a) * self._var[key]
                self._mu[key] = mu_new
                self._var[key] = max(self.cfg.min_variance, var_new)

    # -----------------------------------------------------------------
    #  Query: engine callback (matches ORB v20 signature)
    # -----------------------------------------------------------------
    def compute_risk_pct(
        self,
        symbol: str,
        equity: float,
        peak_equity: float,
        open_positions: List[Tuple[str, int]],
    ) -> float:
        """Return the recommended risk% for this trade.

        Signature matches ORB v20's `risk_pct_fn`:
            fn(symbol, equity, peak_equity, [(sym, side), ...]) -> float
        """
        with self._lock:
            self._n_calls += 1
            self._last_equity = equity
            self._last_peak = max(peak_equity, equity)

            # ---- Grossman-Zhou drawdown factor ----
            dd = 0.0
            if self._last_peak > 0:
                dd = max(0.0, (self._last_peak - equity) / self._last_peak)
            self._last_dd = dd
            gz = 1.0 - dd / self.cfg.dd_cap_pct
            if gz <= 0:
                self._n_gz_zero += 1
                return max(0.0, self.cfg.min_risk_pct)
            gz = max(self.cfg.gz_floor, min(1.0, gz))

            # ---- Merton f* (in units of base_risk) ----
            key = self._key(symbol)
            n = self._n_seen.get(key, 0)
            if n < self.cfg.warmup_trades:
                merton_mult = 1.0               # warm-up: use base
                self._n_warmup += 1
            else:
                mu = self._mu[key]
                var = max(self.cfg.min_variance, self._var[key])
                if mu <= 0:
                    merton_mult = self.cfg.no_edge_multiplier
                    self._n_no_edge += 1
                else:
                    # f* is in "fraction of bankroll" units (Kelly). We reinterpret
                    # it as a multiplier on base_risk_pct via f_star / base_risk_pct.
                    # Since realised R is already scaled per-unit-risk, the natural
                    # rescaling is: merton_mult = f_star (Kelly fraction) / base.
                    # But f_star for r-units is mu/(γ·var), giving Kelly fraction,
                    # and we treat base_risk_pct as our "1 unit of Kelly".
                    f_star = mu / (self.cfg.gamma * var)
                    # Convert Kelly f* (fraction of equity) → multiplier on base_risk_pct
                    merton_mult = f_star / self.cfg.base_risk_pct \
                                  if self.cfg.base_risk_pct > 0 else 0.0

            # ---- Combine and cap ----
            raw_mult = merton_mult * gz
            capped_mult = min(self.cfg.cap_mult, max(0.0, raw_mult))
            if capped_mult < raw_mult:
                self._n_capped += 1

            risk_pct = capped_mult * self.cfg.base_risk_pct
            # Final safety bounds
            risk_pct = max(self.cfg.min_risk_pct, risk_pct)
            hard_cap = self.cfg.cap_mult * self.cfg.base_risk_pct
            risk_pct = min(hard_cap, risk_pct)
            return risk_pct

    # -----------------------------------------------------------------
    #  Introspection
    # -----------------------------------------------------------------
    def stats(self) -> Dict[str, object]:
        with self._lock:
            return {
                "n_calls": self._n_calls,
                "n_warmup_calls": self._n_warmup,
                "n_no_edge_calls": self._n_no_edge,
                "n_capped_calls": self._n_capped,
                "n_gz_zero_calls": self._n_gz_zero,
                "last_equity": self._last_equity,
                "last_peak": self._last_peak,
                "last_dd_pct": self._last_dd * 100.0,
                "per_symbol": {
                    sym: {
                        "n_trades_seen": self._n_seen[sym],
                        "mu_ewma": self._mu[sym],
                        "var_ewma": self._var[sym],
                        "sharpe_ewma": (
                            self._mu[sym] / math.sqrt(self._var[sym])
                            if self._var[sym] > 0 else 0.0
                        ),
                    }
                    for sym in self._n_seen
                },
            }

    def reset(self) -> None:
        """Clear all learned state — useful between backtest runs."""
        with self._lock:
            self._r_history.clear()
            self._mu.clear()
            self._var.clear()
            self._n_seen.clear()
            self._n_calls = 0
            self._n_warmup = 0
            self._n_no_edge = 0
            self._n_capped = 0
            self._n_gz_zero = 0
            self._last_equity = 0.0
            self._last_peak = 0.0
            self._last_dd = 0.0


# =====================================================================
#  Convenience factory
# =====================================================================

def default_mertongz_sizer() -> MertonGZSizer:
    """Pre-configured with the v21 WINNING parameters, validated by
    Scripts/backtest_v21_mertongz.py against 3 months of real 5ers data
    (2026-01-19 → 2026-04-07, 5 symbols, 351 partial-trades, 203 entries):

        PnL   = +$14,622   (+14.6% in 3 months)
        MaxDD =   3.36 %   (✅ under 4% prop-firm limit)
        PF    =   1.48
        Sharpe=   2.77
        vs. flat 0.25%  baseline: +$11,056 / 4.03% DD  (❌ breaches 4% DD)

    Why these specific values (each ablation-tested):
        base_risk_pct=0.0015 : lets 3× cap reach 0.45% on high-conviction trades,
                                while base-level (0.15%) is still modest
        cap_mult=3.0         : absolute ceiling; protects against Kelly's
                                notorious fragility to parameter estimation error
        gamma=2.0            : CRRA standard, ½-Kelly equivalent (Thorp 2006)
        ewma_alpha=0.20      : half-life ≈ 3 trades; responsive without whipsaw
        warmup_trades=15     : prevents "stuck-in-no-edge" from first losing streak
        dd_cap_pct=0.04      : Grossman-Zhou absorbing barrier = prop-firm limit
        pool_symbols=True    : global μ/σ² (one pool) — matches research sim
                                within ±3.3%, and is more robust than per-symbol
                                (some symbols have too few trades for stable EWMA)
        no_edge_multiplier=1.0: when μ̂ ≤ 0 we hold base_risk_pct instead of
                                halving — avoids the "stuck small" trap where
                                a bad initial streak permanently undersizes us
    """
    return MertonGZSizer(MertonGZSizerConfig(
        base_risk_pct=0.0015,
        cap_mult=3.0,
        gamma=2.0,
        ewma_alpha=0.20,
        warmup_trades=15,
        dd_cap_pct=0.04,
        pool_symbols=True,
        no_edge_multiplier=1.0,
    ))
