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
  * per-symbol LIVE TELEMETRY snapshot: Z-score, |Z|/trigger ratio,
    Hurst, OU half-life, current position -> written to heartbeat log
    AND Results/v16_live_telemetry.json every heartbeat_sec seconds
  * optional warm-up from broker M1 history (last ~5000 bars per symbol)
    so every indicator is hot from tick 1
"""
from __future__ import annotations

import json
import logging
import math
import statistics
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
        telemetry_path: Optional[Path] = None,
    ):
        # Build v16 helpers FIRST (engine ctor needs them)
        self.sizer = DynamicSizerV16(cfg=sizer_cfg or SizerConfig())
        self.calendar = calendar or TradingCalendar()
        self.use_dynamic_sizing = use_dynamic_sizing
        self.use_calendar = use_calendar
        self.blackout_counts: dict[str, int] = {}
        self.telemetry_path = telemetry_path

        # Call V15Live parent ctor, then REPLACE the engine with v16.
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

    # =================================================================
    #  LIVE TELEMETRY: what's each symbol actually thinking right now?
    # =================================================================
    def telemetry_snapshot(self) -> dict:
        """Build a per-symbol snapshot for heartbeat logging + JSON export.

        For each symbol we report:
          * last_price            current close
          * z                     Bollinger z-score (sign + magnitude)
          * abs_z                 absolute z-score
          * z_trigger             abs_z_q.value() if ready else NaN
          * dist_to_trigger_pct   (abs_z / z_trigger) * 100 %   (100% = about to fire)
          * hurst                 last rolling-Hurst estimate
          * hurst_trigger         hurst_q.value() if ready else NaN
          * ou_halflife_bars      current OU half-life (∞ if unstable)
          * m5_bars_seen          bars processed so far (warmup + live)
          * position              "LONG @ price / x lots", "SHORT @ ...", or "-"
          * ready                 True if all 3 quantile gates have min_samples
          * next_gate_blocking    first gate name that would reject right now
        """
        out = {
            "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "equity": round(self.engine.equity, 2),
            "peak_equity": round(self.engine.peak_equity, 2),
            "dd_pct": round(
                (self.engine.peak_equity - self.engine.equity)
                / max(1e-9, self.engine.peak_equity) * 100.0, 3,
            ),
            "trades": len(self.engine.trades),
            "blackouts": dict(self.blackout_counts),
            "symbols": {},
        }
        for sym, st in self.engine.states.items():
            # Last observed close
            last_close = st.close_buf[-1] if len(st.close_buf) > 0 else float("nan")

            # Compute z-score from the BB window on the close_buf tail.
            # The engine doesn't cache _abs_z so we recompute cheaply here.
            bb_n = self.cfg.bb_window if hasattr(self.cfg, "bb_window") else 200
            if len(st.close_buf) >= bb_n:
                tail = list(st.close_buf)[-bb_n:]
                mu = statistics.fmean(tail)
                sd = statistics.pstdev(tail)
                z = (last_close - mu) / sd if sd > 1e-9 else 0.0
            else:
                z = float("nan")
            abs_z = abs(z) if not math.isnan(z) else float("nan")

            # Quantile triggers (if warmed up)
            z_trig = st.abs_z_q.value() if st.abs_z_q.ready else float("nan")
            h_trig = st.hurst_q.value() if st.hurst_q.ready else float("nan")

            # Distance to trigger (Z gate): 1.0 = about to fire
            if (not math.isnan(abs_z)) and (not math.isnan(z_trig)) and z_trig > 0:
                dist = abs_z / z_trig
            else:
                dist = float("nan")

            # Position description
            pos = st.position
            if pos is None:
                pos_desc = "-"
            else:
                side_name = "LONG" if pos.side > 0 else "SHORT"
                pos_desc = (f"{side_name} @ {pos.entry_price:.5f} "
                            f"x{pos.lots:.2f} lots "
                            f"SL={pos.stop_price:.5f} TP={pos.tp_price:.5f}")

            # Which gate (if any) would block an entry at this instant?
            p = st.params
            next_block = None
            if st._hurst >= p.hurst_max_abs:
                next_block = "hurst_abs_max"
            elif st.hurst_q.ready and st._hurst > h_trig:
                next_block = "hurst_quantile"
            elif (not math.isnan(abs_z)) and abs_z < p.z_min_abs:
                next_block = "z_min_abs"
            elif (not math.isnan(abs_z)) and abs_z > p.z_max_abs:
                next_block = "z_max_abs"
            elif st.abs_z_q.ready and (not math.isnan(abs_z)) and abs_z < z_trig:
                next_block = "z_quantile"
            elif (getattr(p, "use_ou_gate", False)
                  and st._ou_halflife > p.ou_max_halflife):
                next_block = "ou_halflife"
            elif self.use_calendar:
                allowed, reason = self.calendar.can_enter(
                    sym, datetime.now(timezone.utc).replace(tzinfo=None),
                )
                if not allowed:
                    next_block = f"calendar:{reason}"

            ready = st.abs_z_q.ready and st.hurst_q.ready
            out["symbols"][sym] = {
                "last_price": round(last_close, 5),
                "z": round(z, 3) if not math.isnan(z) else None,
                "abs_z": round(abs_z, 3) if not math.isnan(abs_z) else None,
                "z_trigger": round(z_trig, 3) if not math.isnan(z_trig) else None,
                "dist_to_trigger_pct": (
                    round(dist * 100, 1) if not math.isnan(dist) else None
                ),
                "hurst": round(st._hurst, 3),
                "hurst_trigger": round(h_trig, 3) if not math.isnan(h_trig) else None,
                "ou_halflife_bars": (
                    round(st._ou_halflife, 1) if math.isfinite(st._ou_halflife) else None
                ),
                "m5_bars_seen": st.m5_bars,
                "ready": bool(ready),
                "next_gate_blocking": next_block,
                "position": pos_desc,
            }
        return out

    def _log_telemetry(self):
        """Log a pretty per-symbol table + write JSON for external dashboards."""
        snap = self.telemetry_snapshot()

        lines = [f"── TELEMETRY {snap['ts_utc']} ─────────────────────────"]
        lines.append(
            f"   eq=${snap['equity']:,.0f}  peak=${snap['peak_equity']:,.0f}  "
            f"dd={snap['dd_pct']:+.2f}%  trades={snap['trades']}  "
            f"blackouts={snap['blackouts'] or '{}'}"
        )
        lines.append(
            f"   {'SYM':<6}  {'PRICE':>10}  {'Z':>6}  {'|Z|/trig':>9}  "
            f"{'HURST':>5}  {'HL':>5}  {'READY':>5}  {'BLOCK':>20}  POS"
        )
        for sym, s in snap["symbols"].items():
            def _fmt(v, fmt="{}"):
                return "n/a" if v is None else fmt.format(v)
            lines.append(
                f"   {sym:<6}  {_fmt(s['last_price'], '{:>10.5f}')}  "
                f"{_fmt(s['z'], '{:>+6.2f}')}  "
                f"{_fmt(s['dist_to_trigger_pct'], '{:>8.0f}%')}  "
                f"{_fmt(s['hurst'], '{:>5.2f}')}  "
                f"{_fmt(s['ou_halflife_bars'], '{:>5.0f}')}  "
                f"{'YES' if s['ready'] else 'NO':>5}  "
                f"{(s['next_gate_blocking'] or '-'):>20}  "
                f"{s['position']}"
            )
        for ln in lines:
            logger.info(ln)

        if self.telemetry_path is not None:
            try:
                self.telemetry_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.telemetry_path, "w", encoding="utf-8") as f:
                    json.dump(snap, f, indent=2, default=str)
            except Exception as e:
                logger.warning(f"telemetry json write failed: {e}")

    # =================================================================
    #  Override run() to inject telemetry into the heartbeat loop
    # =================================================================
    def run(self, heartbeat_sec: float = 60.0):
        import time as _t
        self.subscribe()
        logger.info(
            f"SmartBB v16 LIVE  | symbols={list(self.internal_to_broker.keys())} | "
            f"magic={self.magic} | dry_run={self.dry_run} | "
            f"dynamic_sizing={self.use_dynamic_sizing} | calendar={self.use_calendar}"
        )
        # Emit a telemetry line immediately so the user can see state from tick 1
        try:
            self._log_telemetry()
        except Exception as e:
            logger.warning(f"initial telemetry failed: {e}")

        while not self._stop_event.is_set():
            _t.sleep(heartbeat_sec)
            # Standard v15 heartbeat (equity + stats)
            try:
                s = self.engine.summary()
                logger.info(
                    f"♥ eq=${s.get('equity', 0):,.0f} "
                    f"trades={s.get('trades', 0)} "
                    f"pnl=${s.get('net_pnl', 0):,.0f} "
                    f"wr={s.get('win_rate', 0)*100:.1f}% "
                    f"pf={s.get('pf', 0):.2f} "
                    f"max_dd={s.get('max_dd_pct', 0):.2f}%"
                )
            except Exception as e:
                logger.warning(f"heartbeat summary failed: {e}")

            # v16 telemetry
            try:
                self._log_telemetry()
            except Exception as e:
                logger.warning(f"telemetry failed: {e}")
