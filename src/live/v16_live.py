"""
SmartBB v16  -  LIVE RUNNER on 5%ers MTB
=========================================

Thin subclass of V15Live that swaps the engine for SmartBBV16Engine and adds
a TradingCalendar gate before each live entry.

Everything else (symbol map, 8 % account kill-switch, broker patching,
dry-run mode) is inherited unchanged from V15Live.  The live v15 bot on the
VPS is NOT affected because this file is separate.

What you get live versus v15:
  * per-trade risk% computed from Thorp-Kelly + Grossman-Zhou DD +
    inverse-vol targeting + regime multiplier + CVaR cap (SmartBBV16Engine)
  * pre-entry blackout for weekends, daily rollover 20:58-22:02 UTC,
    US/EU market holidays, and (optional) red-news macro prints
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.execution.mt5_bridge import MT5Bridge
from src.smartbb_engine import SMARTBB_UNIVERSE, SymbolSpec
from src.smartbb_engine_v14 import (
    SmartBBV14Config, SymbolParams, _PositionV14,
)
from src.smartbb_engine_v16 import SmartBBV16Engine
from src.dynamic_sizer_v16 import DynamicSizerV16, SizerConfig
from src.trading_calendar import TradingCalendar
from src.live.v15_live import V15Live, BrokerLink

logger = logging.getLogger("smartbb.v16.live")


class V16Live(V15Live):
    """v16 live runner: v16 engine + calendar gate, everything else reused."""

    def __init__(
        self,
        bridge: MT5Bridge,
        internal_symbols: list[str],
        symbol_map: dict[str, str],
        per_symbol_params: dict[str, SymbolParams],
        cfg: Optional[SmartBBV14Config] = None,
        magic: int = 16000,
        comment: str = "SHF_v16",
        dry_run: bool = False,
        max_account_dd_pct: float = 0.08,
        equity_refresh_sec: float = 2.0,
        trade_log_path: Optional[Path] = None,
        # v16 extras
        sizer_cfg: Optional[SizerConfig] = None,
        calendar: Optional[TradingCalendar] = None,
        use_dynamic_sizing: bool = True,
        use_calendar: bool = True,
    ):
        # Build v16 helpers FIRST (engine ctor needs them)
        self.sizer = DynamicSizerV16(cfg=sizer_cfg or SizerConfig())
        self.calendar = calendar or TradingCalendar()
        self.use_dynamic_sizing = use_dynamic_sizing
        self.use_calendar = use_calendar
        self.blackout_counts: dict[str, int] = {}

        # Call V15Live parent ctor, then REPLACE the engine with v16.
        # We can't just hand a v16 engine to super().__init__() because V15Live
        # builds its own SmartBBV14Engine internally.  So: let it build v14,
        # then swap in v16 with identical params, then re-patch.
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

        # Swap engine for v16 (same symbols / params / cfg / start equity)
        initial_equity = self.engine.equity
        peak_equity = self.engine.peak_equity
        specs = [SMARTBB_UNIVERSE[s] for s in internal_symbols]
        self.engine = SmartBBV16Engine(
            symbols=specs,
            params=per_symbol_params,
            cfg=self.cfg,
            initial_equity=initial_equity,
            sizer=self.sizer,
            calendar=self.calendar,
            use_dynamic_sizing=use_dynamic_sizing,
            use_calendar=use_calendar,
        )
        self.engine.peak_equity = peak_equity
        self.engine.start_equity = initial_equity
        self._patch_engine()  # re-wire _live_maybe_enter / _live_close / _live_manage

        logger.info(
            f"V16Live initialised  |  dynamic_sizing={use_dynamic_sizing}  "
            f"calendar={use_calendar}  magic={magic}"
        )

    # =================================================================
    #  Calendar gate: wrap V15Live._live_maybe_enter
    # =================================================================
    def _live_maybe_enter(self, st, time_ts: float, close: float):
        if self.use_calendar:
            ts = datetime.utcfromtimestamp(time_ts)
            allowed, reason = self.calendar.can_enter(st.spec.symbol, ts)
            if not allowed:
                self.blackout_counts[reason] = \
                    self.blackout_counts.get(reason, 0) + 1
                return
        super()._live_maybe_enter(st, time_ts, close)

    # =================================================================
    #  Feed realised-R back into the sizer AFTER a live close completes
    # =================================================================
    def _live_close(self, st, fill: float, t: float, reason: str):
        n_before = len(self.engine.trades)
        super()._live_close(st, fill, t, reason)
        if self.use_dynamic_sizing and len(self.engine.trades) > n_before:
            tr = self.engine.trades[-1]
            self.sizer.record_trade(tr.symbol, tr.side, tr.realised_R)
