"""
SHF v16 - DYNAMIC KELLY + TRADING CALENDAR (on top of v14/v15 core)

What's new vs v15:
    * Per-trade risk% is computed dynamically on every entry by composing
      Thorp-Kelly (rolling-R history) x Grossman-Zhou (DD) x inverse-vol
      targeting x regime-strength multiplier x CVaR cap.  No more fixed
      base_risk_pct scalar.
    * Entries are gated by TradingCalendar (weekend / daily rollover /
      holiday / optional news).  OPEN positions are still managed during
      blackouts - safety first.
    * All of v15's per-symbol tuned thresholds (v15_ultimate_tuning.json)
      still apply unchanged.

This is a thin subclass of SmartBBV14Engine.  The live v15 bot does NOT
import from this file, so nothing in this module can affect live trading.
"""
from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

from src.smartbb_engine import SymbolSpec
from src.smartbb_engine_v14 import (
    SmartBBV14Config,
    SmartBBV14Engine,
    SymbolParams,
    _PositionV14,
    _SymbolStateV14,
)
from src.dynamic_sizer_v16 import DynamicSizerV16, SizerConfig
from src.trading_calendar import TradingCalendar, CalendarConfig


class SmartBBV16Engine(SmartBBV14Engine):
    """
    v16 engine = v14 engine + DynamicSizerV16 + TradingCalendar.

    Toggle either subsystem off with use_dynamic_sizing=False or
    use_calendar=False for ablation testing.
    """

    def __init__(
        self,
        symbols: list[SymbolSpec],
        params: Optional[dict[str, SymbolParams]] = None,
        cfg: Optional[SmartBBV14Config] = None,
        initial_equity: float = 100_000.0,
        sizer: Optional[DynamicSizerV16] = None,
        calendar: Optional[TradingCalendar] = None,
        use_dynamic_sizing: bool = True,
        use_calendar: bool = True,
    ):
        super().__init__(
            symbols=symbols, params=params, cfg=cfg,
            initial_equity=initial_equity,
        )
        self.sizer = sizer or DynamicSizerV16()
        self.calendar = calendar or TradingCalendar()
        self.use_dynamic_sizing = use_dynamic_sizing
        self.use_calendar = use_calendar

        # Telemetry
        self.blackout_counts: dict[str, int] = {}
        self.risk_breakdowns: list[dict] = []  # kept small - one per entry

    # =================================================================
    # Override 1: entry gate - calendar blackouts
    # =================================================================
    def _maybe_enter(self, st: _SymbolStateV14, time: float,
                       close: float) -> None:
        if self.use_calendar:
            ts = datetime.utcfromtimestamp(time)
            allowed, reason = self.calendar.can_enter(st.spec.symbol, ts)
            if not allowed:
                self.blackout_counts[reason] = \
                    self.blackout_counts.get(reason, 0) + 1
                return
        super()._maybe_enter(st, time, close)

    # =================================================================
    # Override 2: per-trade risk% - dynamic composition
    # =================================================================
    def _risk_pct(self, symbol: str, side: int) -> float:
        if not self.use_dynamic_sizing:
            return super()._risk_pct(symbol, side)

        st = self.states.get(symbol)
        if st is None:
            return super()._risk_pct(symbol, side)

        close = max(st._last_close, 1e-9)
        atr = max(st.atr.value, 1e-9)

        # Annualized realized vol proxy:
        # atr/close is ~1-sigma on an M5 bar, so sqrt(78 * 252) annualizes it
        # (78 M5 bars per trading day * 252 trading days).
        vol_per_m5 = atr / close
        realized_vol_ann = vol_per_m5 * math.sqrt(78.0 * 252.0)

        abs_z = abs(st.bb.z(close)) if st.bb.ready else 0.0
        hurst = getattr(st, "_hurst", 0.5)
        hl = getattr(st, "_ou_halflife", float("inf"))

        risk_pct = self.sizer.compute_risk_pct(
            symbol=symbol, side=side,
            equity=self.equity, peak_equity=self.peak_equity,
            realized_vol_ann=realized_vol_ann,
            abs_z=abs_z, hurst=hurst, halflife=hl,
            base_risk_pct=self.cfg.base_risk_pct,
        )

        # Keep a small trace (last 400 entries) for diagnostics
        if len(self.risk_breakdowns) < 400:
            self.risk_breakdowns.append(dict(self.sizer.last_breakdown))

        return risk_pct

    # =================================================================
    # Override 3: feed realized R back into the sizer after every close
    # =================================================================
    def _close(self, st: _SymbolStateV14, fill: float, t: float,
                reason: str) -> None:
        n_before = len(self.trades)
        super()._close(st, fill, t, reason)
        if len(self.trades) > n_before:
            tr = self.trades[-1]
            self.sizer.record_trade(tr.symbol, tr.side, tr.realised_R)

    # =================================================================
    # Summary + telemetry
    # =================================================================
    def summary(self) -> dict:
        s = super().summary()
        s["v16"] = {
            "use_dynamic_sizing": self.use_dynamic_sizing,
            "use_calendar": self.use_calendar,
            "blackout_counts": dict(self.blackout_counts),
            "sizer_config": vars(self.sizer.cfg),
            "n_risk_breakdowns_sampled": len(self.risk_breakdowns),
        }
        # Summary stats of risk breakdown
        if self.risk_breakdowns:
            rp = [b["risk_pct"] for b in self.risk_breakdowns]
            kp = [b["kelly"]    for b in self.risk_breakdowns]
            vp = [b["vol"]      for b in self.risk_breakdowns]
            gp = [b["dd"]       for b in self.risk_breakdowns]
            mp = [b["regime"]   for b in self.risk_breakdowns]
            def avg(x): return sum(x) / len(x) if x else 0.0
            s["v16"]["risk_pct_mean"] = avg(rp)
            s["v16"]["risk_pct_min"]  = min(rp) if rp else 0.0
            s["v16"]["risk_pct_max"]  = max(rp) if rp else 0.0
            s["v16"]["kelly_mean"]    = avg(kp)
            s["v16"]["vol_mean"]      = avg(vp)
            s["v16"]["dd_mean"]       = avg(gp)
            s["v16"]["regime_mean"]   = avg(mp)
        return s
