"""
DynamicSizerV16 — composition-based per-trade sizer.

Replaces v14's fixed base_risk_pct with a dynamic recomputation on every trade:

    risk_pct  =  base_kelly  *  dd_throttle  *  vol_mult  *  regime_mult
               then clamped to [min_risk_pct, max_risk_pct]

where:

    base_kelly   Thorp-corrected fractional Kelly from the rolling R-history
                 per (symbol, side), Beta(1,1) prior, CVaR-capped.
                 Academic reference: Thorp 2006; Rockafellar-Uryasev 2000.

    dd_throttle  Grossman-Zhou smooth DD factor
                 (1 -> fresh peak, 0 -> at the max-DD barrier).  γ=2.
                 Reference: Grossman & Zhou 1993 Math. Finance 3(3).

    vol_mult     Inverse-volatility targeting.  Uses ATR as a robust proxy
                 for realized M5 vol, annualized to the 15 % target.
                 Clamped to [vol_floor_mult, vol_ceil_mult] to stop the
                 sizer from going nuclear on quiet days.

    regime_mult  (0.7 - 1.3) multiplier based on the three live regime
                 indicators we already compute: |Z|, Hurst, OU half-life.
                 Higher confidence setups  ->  more size.

All factors are bounded, so the product is bounded.  No "wild Kelly".
"""
from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from src.momentum.kelly import ThorpKelly, GrossmanZhouDD, CVaRCap


# =====================================================================
#  Config
# =====================================================================

@dataclass
class SizerConfig:
    # Kelly
    kelly_fractional:     float = 0.25    # 1/4 Kelly (institutional standard)
    min_trades_for_kelly: int   = 20
    kelly_lookback:       int   = 100     # rolling window of last-N R per key

    # Risk bounds (absolute, post-composition)
    min_risk_pct: float = 0.001           # 0.10 %
    max_risk_pct: float = 0.010           # 1.00 %   (5%ers-safe)

    # Drawdown throttle (Grossman-Zhou)
    dd_max:   float = 0.06                # 6 % envelope
    dd_gamma: float = 2.0

    # Vol target (annualized)
    target_ann_vol: float = 0.15          # 15 % ann realized vol = full size
    vol_floor_mult: float = 0.5
    vol_ceil_mult:  float = 1.5

    # CVaR (expected shortfall) cap
    cvar_alpha: float = 0.05              # 5 % tail
    cvar_cap:   float = 0.02              # ES <= 2 % of equity

    # Regime multiplier bounds
    regime_floor: float = 0.7
    regime_ceil:  float = 1.3

    # Fallback before enough Kelly data exists
    cold_start_risk_pct: float = 0.0025   # 0.25 % per trade


# =====================================================================
#  Sizer
# =====================================================================

class DynamicSizerV16:
    """
    Stateless with respect to the engine; state = per-key deque of realized R.
    Engine calls:
      * record_trade(symbol, side, realised_R)   after every close
      * compute_risk_pct(...)                     before every open
    """

    def __init__(self, cfg: Optional[SizerConfig] = None):
        self.cfg = cfg or SizerConfig()
        self.kelly = ThorpKelly(fractional=self.cfg.kelly_fractional)
        self.gz    = GrossmanZhouDD(max_dd=self.cfg.dd_max,
                                     gamma=self.cfg.dd_gamma)
        self.cvar  = CVaRCap(alpha=self.cfg.cvar_alpha, cap=self.cfg.cvar_cap)
        self._hist: dict[tuple[str, int], Deque[float]] = {}
        self.last_breakdown: dict = {}

    # -----------------------------------------------------------------
    def record_trade(self, symbol: str, side: int, realised_R: float) -> None:
        key = (symbol, side)
        dq = self._hist.get(key)
        if dq is None:
            dq = deque(maxlen=self.cfg.kelly_lookback)
            self._hist[key] = dq
        dq.append(float(realised_R))

    # -----------------------------------------------------------------
    def _kelly_fraction(self, symbol: str, side: int) -> float:
        """
        Returns Thorp-shrunk fractional Kelly (already CVaR-capped).
        If not enough data yet, returns self.cfg.cold_start_risk_pct.
        """
        key = (symbol, side)
        dq = self._hist.get(key)
        if dq is None or len(dq) < self.cfg.min_trades_for_kelly:
            return self.cfg.cold_start_risk_pct

        Rs = list(dq)
        wins = [r for r in Rs if r > 0]
        losses = [r for r in Rs if r <= 0]
        n = len(Rs)
        if not wins or not losses:
            return self.cfg.cold_start_risk_pct

        # Point estimates
        p = len(wins) / n
        win_R  =  sum(wins)   / len(wins)
        loss_R = -sum(losses) / len(losses)         # positive scalar

        # Posterior uncertainty on p  (Beta(wins+1, losses+1))
        a = len(wins) + 1.0
        b = len(losses) + 1.0
        p_var = a * b / ((a + b) ** 2 * (a + b + 1.0))

        # Mean and var of per-trade R
        mu_R  = sum(Rs) / n
        var_R = sum((r - mu_R) ** 2 for r in Rs) / max(n - 1, 1)
        sig_R = math.sqrt(max(var_R, 0.0))
        mu_R_var = var_R / n

        # Kelly (Thorp-shrunk)
        f = self.kelly.fraction(p=p, p_var=p_var,
                                 mu_R=mu_R, mu_R_var=mu_R_var,
                                 win_R=win_R, loss_R=loss_R)
        # CVaR cap
        cap_mult = self.cvar.factor(mu_R=mu_R, sigma_R=sig_R,
                                     candidate_risk_frac=f)
        return max(0.0, f * cap_mult)

    # -----------------------------------------------------------------
    @staticmethod
    def _vol_target_mult(realized_vol_ann: float, target_ann: float,
                         floor: float, ceil: float) -> float:
        if realized_vol_ann <= 0.0:
            return 1.0
        m = target_ann / realized_vol_ann
        return max(floor, min(ceil, m))

    # -----------------------------------------------------------------
    @staticmethod
    def _regime_mult(abs_z: float, hurst: float, halflife: float,
                     floor: float, ceil: float) -> float:
        # Z strength:  2.5σ -> 1.00,  5.0σ -> 1.30
        z_span = max(0.0, min(2.5, abs_z - 2.5))
        z_comp = 1.0 + 0.12 * z_span                # 0..0.30

        # Hurst:  lower than 0.5 = more mean-reverting = bigger size
        h_span = max(0.0, min(0.3, 0.50 - hurst))
        h_comp = 1.0 + 0.33 * h_span                # 0..0.10 at h=0.2

        # OU half-life:  <10 = 1.10,  10-25 = 1.00,  >25 = 0.90
        if halflife is None or not math.isfinite(halflife):
            hl_comp = 0.95
        elif halflife < 10.0:
            hl_comp = 1.10
        elif halflife < 25.0:
            hl_comp = 1.00
        else:
            hl_comp = 0.90

        m = z_comp * h_comp * hl_comp
        return max(floor, min(ceil, m))

    # -----------------------------------------------------------------
    def compute_risk_pct(self, *,
                         symbol: str,
                         side: int,
                         equity: float,
                         peak_equity: float,
                         realized_vol_ann: float,
                         abs_z: float,
                         hurst: float,
                         halflife: float,
                         base_risk_pct: float) -> float:
        """
        Returns the risk_pct to use for the NEXT open trade.
        Bounded to [min_risk_pct, max_risk_pct].
        """
        c = self.cfg
        kelly_f = self._kelly_fraction(symbol, side)
        candidate = kelly_f if kelly_f > 0.0 else base_risk_pct

        dd_mult     = self.gz.factor(equity=equity, peak=peak_equity)
        vol_mult    = self._vol_target_mult(realized_vol_ann, c.target_ann_vol,
                                             c.vol_floor_mult, c.vol_ceil_mult)
        regime_mult = self._regime_mult(abs_z, hurst, halflife,
                                         c.regime_floor, c.regime_ceil)

        risk_pct = candidate * dd_mult * vol_mult * regime_mult
        risk_pct = max(c.min_risk_pct, min(c.max_risk_pct, risk_pct))

        self.last_breakdown = dict(
            symbol=symbol, side=side,
            kelly=kelly_f, dd=dd_mult, vol=vol_mult, regime=regime_mult,
            risk_pct=risk_pct)
        return risk_pct
