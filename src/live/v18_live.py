"""
SmartBB v18  —  LIVE RUNNER (Grossman-Zhou dynamic Kelly)

Thin subclass of V15Live that swaps the engine for SmartBBV18Engine.
Reuses V15Live's broker patching, 8% account kill, dry-run mode and
symbol map. V18 differences:

    * DynamicSizerV18 = Grossman-Zhou × Bayesian shrinkage × conviction
                         × SAFETY-ONLY 5%ers guard × 2% hard cap
    * TradingCalendar  = weekend / rollover / holiday blackout
    * Kelly warm-up    = seed per-bucket R history from a backtest trades
                         JSON so GZ fractions are hot from bar 1
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.execution.mt5_bridge import MT5Bridge
from src.smartbb_engine import SMARTBB_UNIVERSE
from src.smartbb_engine_v14 import SmartBBV14Config, SymbolParams
from src.smartbb_engine_v18 import SmartBBV18Engine
from src.dynamic_sizer_v18 import DynamicSizerV18, SizerV18Config
from src.trading_calendar import TradingCalendar
from src.live.v15_live import V15Live

logger = logging.getLogger("smartbb.v18.live")


class V18Live(V15Live):
    """v18 live runner: v18 engine + calendar gate, everything else reused."""

    def __init__(
        self,
        bridge: MT5Bridge,
        internal_symbols: list[str],
        symbol_map: dict[str, str],
        per_symbol_params: dict[str, SymbolParams],
        cfg: Optional[SmartBBV14Config] = None,
        magic: int = 18000,
        comment: str = "SHF_v18",
        dry_run: bool = False,
        max_account_dd_pct: float = 0.08,
        equity_refresh_sec: float = 2.0,
        trade_log_path: Optional[Path] = None,
        # v18 extras
        sizer_cfg: Optional[SizerV18Config] = None,
        calendar: Optional[TradingCalendar] = None,
        use_calendar: bool = True,
    ):
        # Build v18 helpers FIRST (engine ctor uses them)
        self.sizer = DynamicSizerV18(cfg=sizer_cfg or SizerV18Config())
        self.calendar = calendar or TradingCalendar()
        self.use_calendar = use_calendar

        # Call V15Live parent ctor, then REPLACE engine with v18
        super().__init__(
            bridge=bridge,
            internal_symbols=internal_symbols,
            symbol_map=symbol_map,
            per_symbol_params=per_symbol_params,
            cfg=cfg,
            magic=magic,
            comment=comment,
            dry_run=dry_run,
            max_account_dd_pct=max_account_dd_pct,
            equity_refresh_sec=equity_refresh_sec,
            trade_log_path=trade_log_path,
        )

        initial_equity = self.engine.equity
        peak_equity    = self.engine.peak_equity
        specs          = [SMARTBB_UNIVERSE[s] for s in internal_symbols]
        self.engine = SmartBBV18Engine(
            symbols=specs,
            params=per_symbol_params,
            cfg=self.cfg,
            initial_equity=initial_equity,
            sizer=self.sizer,
            calendar=self.calendar,
            use_calendar=use_calendar,
        )
        self.engine.peak_equity  = peak_equity
        self.engine.start_equity = initial_equity
        self._patch_engine()

        logger.info(
            f"V18Live initialised  |  Grossman-Zhou sizer  "
            f"calendar={use_calendar}  magic={magic}"
        )


def warmup_sizer_v18(sizer: DynamicSizerV18, trades_path: Path) -> int:
    """Seed per-(symbol, side) R history from a backtest trades JSON."""
    if not trades_path.exists():
        return 0
    try:
        trades = json.loads(trades_path.read_text())
    except Exception:
        return 0
    n = 0
    for t in trades:
        sym, side, R = t.get("symbol"), t.get("side"), t.get("realised_R")
        if sym is None or side is None or R is None:
            continue
        sizer.record_trade(sym, int(side), float(R))
        n += 1
    return n
