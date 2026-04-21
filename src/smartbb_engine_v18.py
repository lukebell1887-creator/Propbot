"""
SmartBBV18Engine — v14 core + Grossman-Zhou dynamic sizer (v18 stack).

What's NEW vs v16:
    * The sizer is DynamicSizerV18 (Grossman-Zhou × Bayesian shrinkage ×
      conviction × SAFETY-ONLY 5%ers guard × 2 % hard cap).
    * The outer FiversRiskGuard is NOT needed anymore — the guard is baked
      into the sizer itself, and it does nothing in the green zone (no more
      pre-emptive haircuts).
    * TradingCalendar blackouts are preserved (weekend / rollover / holiday).

This engine is used by Scripts/backtest_v18.py and, once validated, by
Scripts/run_v18_live.py via the same V16Live runner (which already speaks
the `_risk_pct` protocol).
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.smartbb_engine import SymbolSpec
from src.smartbb_engine_v14 import (
    SmartBBV14Config, SmartBBV14Engine, SymbolParams, _SymbolStateV14,
)
from src.dynamic_sizer_v18 import DynamicSizerV18, SizerV18Config
from src.trading_calendar import TradingCalendar


class SmartBBV18Engine(SmartBBV14Engine):
    def __init__(
        self,
        symbols: list[SymbolSpec],
        params: Optional[dict[str, SymbolParams]] = None,
        cfg: Optional[SmartBBV14Config] = None,
        initial_equity: float = 100_000.0,
        sizer: Optional[DynamicSizerV18] = None,
        calendar: Optional[TradingCalendar] = None,
        use_calendar: bool = True,
    ):
        super().__init__(
            symbols=symbols, params=params, cfg=cfg,
            initial_equity=initial_equity,
        )
        self.sizer = sizer or DynamicSizerV18()
        self.calendar = calendar or TradingCalendar()
        self.use_calendar = use_calendar

        # Telemetry
        self.blackout_counts: dict[str, int] = {}
        self.risk_breakdowns: list[dict] = []      # keep only last 400

    # -----------------------------------------------------------------
    #  Entry gate — calendar blackouts
    # -----------------------------------------------------------------
    def _maybe_enter(self, st: _SymbolStateV14, time: float,
                       close: float) -> None:
        st._last_bar_time = time
        if self.use_calendar:
            ts = datetime.utcfromtimestamp(time)
            allowed, reason = self.calendar.can_enter(st.spec.symbol, ts)
            if not allowed:
                self.blackout_counts[reason] = \
                    self.blackout_counts.get(reason, 0) + 1
                return
        super()._maybe_enter(st, time, close)

    # -----------------------------------------------------------------
    #  Risk %  — Grossman-Zhou pipeline
    # -----------------------------------------------------------------
    def _risk_pct(self, symbol: str, side: int) -> float:
        st = self.states.get(symbol)
        if st is None:
            return super()._risk_pct(symbol, side)

        close = max(st._last_close, 1e-9)
        abs_z = abs(st.bb.z(close)) if st.bb.ready else 0.0
        hurst = getattr(st, "_hurst", 0.5)

        # Day key for daily-DD rollover logic inside the sizer
        bar_t = getattr(st, "_last_bar_time", None)
        if bar_t is None:
            day_key = datetime.utcnow().strftime("%Y-%m-%d")
        else:
            day_key = datetime.utcfromtimestamp(bar_t).strftime("%Y-%m-%d")

        risk_pct = self.sizer.compute_risk_pct(
            symbol=symbol, side=side,
            equity=self.equity,
            day_key=day_key,
            abs_z=abs_z, hurst=hurst,
        )

        if len(self.risk_breakdowns) < 400:
            self.risk_breakdowns.append(dict(self.sizer.last_breakdown))

        # Guard halted? Book it so telemetry shows it
        br = self.sizer.last_breakdown
        if risk_pct <= 0.0:
            phase = br.get("phase", "unknown")
            reason = (
                "fivers_guard_total_halt" if phase == "total_halt"
                else "fivers_guard_daily_halt" if phase == "daily_halt"
                else "losing_bucket_skip" if br.get("source") == "losing_bucket_killed"
                else "sizer_zero"
            )
            self.blackout_counts[reason] = \
                self.blackout_counts.get(reason, 0) + 1

        return risk_pct

    # -----------------------------------------------------------------
    #  Feed realised R back into the sizer after every close
    # -----------------------------------------------------------------
    def _close(self, st: _SymbolStateV14, fill: float, t: float,
                reason: str) -> None:
        n_before = len(self.trades)
        super()._close(st, fill, t, reason)
        if len(self.trades) > n_before:
            tr = self.trades[-1]
            self.sizer.record_trade(tr.symbol, tr.side, tr.realised_R)

    # -----------------------------------------------------------------
    #  Summary
    # -----------------------------------------------------------------
    def summary(self) -> dict:
        s = super().summary()
        rb = self.risk_breakdowns
        v18: dict = {
            "use_calendar": self.use_calendar,
            "blackout_counts": dict(self.blackout_counts),
            "sizer_config": vars(self.sizer.cfg),
            "n_risk_breakdowns_sampled": len(rb),
        }
        if rb:
            def avg(key): return sum(b.get(key, 0.0) for b in rb) / len(rb)
            rp = [b["risk_pct"] for b in rb]
            v18["risk_pct_mean"]   = avg("risk_pct")
            v18["risk_pct_min"]    = min(rp)
            v18["risk_pct_max"]    = max(rp)
            v18["f_base_mean"]     = avg("f_base")
            v18["shrink_mean"]     = avg("shrink")
            v18["conviction_mean"] = avg("conviction")
            v18["guard_mean"]      = avg("guard")
            # How many times each source was used
            v18["source_counts"] = {}
            for b in rb:
                src = b.get("source", "unknown")
                v18["source_counts"][src] = v18["source_counts"].get(src, 0) + 1
        s["v18"] = v18
        return s
