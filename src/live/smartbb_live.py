"""
SHF v13 SMART BOLLINGER — Live runner on 5%ers MTB

Wires `SmartBBEngine` to the MT5 TCP bridge so the same strategy
that produced +12.86 % in backtest runs on a live broker account.

Architecture
------------
    MT5 terminal <--TCP--> mt5_bridge.MT5Bridge <--calls--> SmartBBLive
                                                                │
                                                                ▼
                                                        SmartBBEngine
                                                        (v13 strategy)

The live driver does **four** things the backtest doesn't:

1.  Pulls live M1 bars from the EA stream and forwards them to
    `engine.on_bar(...)` — identical code path to the backtest.

2.  Overrides `_maybe_enter`, `_close`, `_manage` so that
    * entry   → `bridge.send_order(...)` (market, with slippage cap)
    * exit    → `bridge.close_position(ticket)`
    * BE trail→ `bridge.modify_position(ticket, sl=...)`

3.  Syncs equity from broker `AccountInfo` every bar, so the
    AKAD sizer and GZ DD cap use the *real* account value.

4.  Implements a **ghost-halt reconciler** — if the engine halts
    (4 % daily DD or 5 % total DD), it calls `bridge.close_all_positions()`
    and stops trading for the rest of the day / permanently.

The engine maths, the Hurst regime filter, Kalman exit, break-even trail,
amplitude gate and AKAD sizing are 100 % unchanged from `smartbb_engine.py`.
"""

from __future__ import annotations

import logging
import math
import signal
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.execution.mt5_bridge import (
    MT5Bridge, OrderRequest, OrderType, BarData,
)
from src.smartbb_engine import (
    SmartBBEngine, SmartBBConfig, SmartBBSymbol, SmartBBPosition,
    SmartBBTrade, SMARTBB_UNIVERSE, SymbolSpec,
)

logger = logging.getLogger("smartbb.live")


# =====================================================================
# Broker-tracked position: maps engine position <-> MT5 ticket
# =====================================================================

@dataclass
class BrokerLink:
    ticket: int
    symbol: str
    side: int
    lots: float
    entry_fill: float


# =====================================================================
# Live engine
# =====================================================================

class SmartBBLive:
    """Live runner that orchestrates the engine against a live broker."""

    # 1 M5 bar = 5 M1 bars.  We use M1 streams and aggregate inside engine.
    TIMEFRAME = "M1"

    def __init__(
        self,
        bridge: MT5Bridge,
        symbols: list[SymbolSpec],
        cfg: Optional[SmartBBConfig] = None,
        magic: int = 13000,
        comment: str = "SHF_v13",
        dry_run: bool = False,
        equity_refresh_sec: float = 2.0,
        trade_log_path: Optional[Path] = None,
    ):
        self.bridge = bridge
        self.cfg = cfg or SmartBBConfig()
        self.magic = magic
        self.comment = comment
        self.dry_run = dry_run
        self.equity_refresh_sec = equity_refresh_sec
        self.trade_log_path = trade_log_path

        # 1. Pull initial equity from broker
        acct = bridge.get_account_info()
        initial_equity = max(acct.balance, 1.0)
        logger.info(
            f"Starting equity from broker: ${initial_equity:,.2f} "
            f"(server={acct.server}, currency={acct.currency})"
        )

        # 2. Construct engine with live equity
        self.engine = SmartBBEngine(
            symbols=symbols, cfg=self.cfg, initial_equity=initial_equity,
        )
        self.engine.start_equity = initial_equity

        # 3. Patch engine's three order-touching methods to route through broker
        self._patch_engine()

        # 4. State
        self._symbol_specs: dict[str, SymbolSpec] = {s.symbol: s for s in symbols}
        self._links: dict[str, BrokerLink] = {}     # symbol -> broker ticket
        self._last_equity_sync: float = 0.0
        self._stop_event = threading.Event()
        self._live_trades: list[dict] = []

    # -----------------------------------------------------------------
    # Engine patching
    # -----------------------------------------------------------------

    def _patch_engine(self):
        """Monkey-patch the engine's trade-action methods so they route live."""
        eng = self.engine

        # Save originals for internal state updates (beta posterior etc.)
        self._orig_close = eng._close
        self._orig_maybe_enter = eng._maybe_enter
        self._orig_manage = eng._manage

        eng._maybe_enter = self._live_maybe_enter
        eng._close = self._live_close
        eng._manage = self._live_manage

    # -----------------------------------------------------------------
    # Live entry
    # -----------------------------------------------------------------

    def _live_maybe_enter(self, st: SmartBBSymbol, time_ts: float, close: float):
        """
        Replicate the engine's entry logic EXACTLY, but:
        * before creating SmartBBPosition, fire an ORDER_SEND through bridge
        * only record the position once we have a real broker ticket + fill
        """
        cfg = self.cfg
        if st._hurst >= cfg.hurst_max_for_trade:
            return
        z = st.bb.z(close)
        if not (cfg.min_z_entry <= abs(z) <= cfg.max_z_entry):
            return
        side = -1 if z > 0 else +1

        # Concurrency
        total_open = len(self._links)
        if total_open >= cfg.max_concurrent:
            return
        same_cls = sum(
            1 for sym, lnk in self._links.items()
            if self._symbol_specs[sym].asset_class == st.spec.asset_class
        )
        if same_cls >= cfg.max_same_class_concurrent:
            return
        if st.spec.symbol in self._links:
            return  # already open on this symbol

        atr_pts = st.atr.value
        mean = st.bb.mean
        std = st.bb.std

        entry_fill = close + side * 0.5 * st.spec.spread_pts
        if side > 0:
            band = mean - cfg.bb_sigma * std
            sl = band - cfg.stop_atr_mult * atr_pts
        else:
            band = mean + cfg.bb_sigma * std
            sl = band + cfg.stop_atr_mult * atr_pts
        stop_distance = abs(entry_fill - sl)
        tp = mean
        tp_distance = abs(tp - entry_fill)
        if tp_distance <= 0:
            return

        # Amplitude gate
        cost_pts = 2.0 * st.spec.spread_pts
        comm_one_lot = st.spec.round_trip_commission(avg_price=entry_fill, lots=1.0)
        cost_dollars = cost_pts * st.spec.pip_value + comm_one_lot
        expected_dollars = tp_distance * st.spec.pip_value
        if expected_dollars < cfg.amplitude_hurdle * cost_dollars:
            return

        # Sizing
        risk_pct = self.engine._risk_pct(st.spec.symbol, side)
        risk_d = self.engine.equity * risk_pct
        lots = risk_d / max(stop_distance * st.spec.pip_value, 1e-9)
        lots = max(
            st.spec.min_lots,
            min(st.spec.max_lots,
                math.floor(lots / st.spec.lot_step) * st.spec.lot_step)
        )
        if lots < st.spec.min_lots:
            return

        # --- ROUTE TO BROKER -----------------------------------------
        order_type = OrderType.MARKET_BUY if side > 0 else OrderType.MARKET_SELL
        req = OrderRequest(
            symbol=st.spec.symbol,
            order_type=order_type,
            lots=lots,
            price=0.0,              # 0 => market fill at bridge
            sl=sl,
            tp=tp,
            deviation=20,
            magic=self.magic,
            comment=self.comment,
        )

        logger.info(
            f"SIGNAL   {st.spec.symbol} {'LONG' if side>0 else 'SHORT'} "
            f"Z={z:+.2f} H={st._hurst:.2f} lots={lots:.2f} "
            f"entry~{entry_fill:.5f} sl={sl:.5f} tp={tp:.5f} risk=${risk_d:.0f}"
        )

        if self.dry_run:
            logger.warning("DRY_RUN — not sending order")
            return

        try:
            result = self.bridge.send_order(req)
        except Exception as e:
            logger.error(f"Order send error: {e}")
            return
        if not result.success:
            logger.error(f"Order rejected: {result.error_message}")
            return

        real_fill = result.price or entry_fill
        real_lots = result.lots or lots

        # Record position in engine using REAL fill price
        pos = SmartBBPosition(
            symbol=st.spec.symbol, side=side,
            entry_price=real_fill, entry_time=time_ts,
            entry_bar=st.m5_bars, lots=real_lots,
            sl=sl, tp=tp,
            z_at_entry=z, hurst_at_entry=st._hurst,
            R_dist=abs(real_fill - sl), R_dollars=risk_d,
        )
        st.position = pos
        self._links[st.spec.symbol] = BrokerLink(
            ticket=result.ticket, symbol=st.spec.symbol,
            side=side, lots=real_lots, entry_fill=real_fill,
        )
        logger.info(
            f"OPENED   {st.spec.symbol} ticket={result.ticket} fill={real_fill:.5f}"
        )

    # -----------------------------------------------------------------
    # Live manage — SL updates for break-even trail
    # -----------------------------------------------------------------

    def _live_manage(self, st: SmartBBSymbol, t: float, close: float):
        pos = st.position
        if pos is None:
            return
        old_sl = pos.sl
        # Delegate to original for all the maths (BE trail, Kalman exit, time stop)
        self._orig_manage(st, t, close)
        # If the engine closed the position via _close (which we patched),
        # it's gone — nothing to modify.
        if st.position is None:
            return
        # If the SL was moved, propagate to broker
        if abs(st.position.sl - old_sl) > 1e-9:
            link = self._links.get(st.spec.symbol)
            if link and not self.dry_run:
                try:
                    self.bridge.modify_position(link.ticket, sl=st.position.sl)
                    logger.info(
                        f"SL_MOVE  {st.spec.symbol} ticket={link.ticket} "
                        f"sl={old_sl:.5f}->{st.position.sl:.5f}"
                    )
                except Exception as e:
                    logger.error(f"SL modify failed: {e}")

    # -----------------------------------------------------------------
    # Live close
    # -----------------------------------------------------------------

    def _live_close(self, st: SmartBBSymbol, fill: float, t: float, reason: str):
        pos = st.position
        if pos is None:
            return

        # Close on broker FIRST, then record locally (with real fill)
        link = self._links.get(st.spec.symbol)
        actual_fill = fill
        if link and not self.dry_run:
            try:
                self.bridge.close_position(link.ticket)
                # Wait a moment for broker to confirm close + push updated quote
                time.sleep(0.2)
                positions = self.bridge.get_positions(symbol=st.spec.symbol)
                still_open = any(p.ticket == link.ticket for p in positions)
                if still_open:
                    logger.error(
                        f"Position {link.ticket} did not close on broker — retrying"
                    )
                    self.bridge.close_position(link.ticket)
                # Use latest quote as exit fill estimate
                quotes = self.bridge._quotes.get(st.spec.symbol, {})
                if pos.side > 0:
                    actual_fill = quotes.get("bid", fill) or fill
                else:
                    actual_fill = quotes.get("ask", fill) or fill
            except Exception as e:
                logger.error(f"Close failed: {e}")

        logger.info(
            f"CLOSED   {st.spec.symbol} side={'L' if pos.side>0 else 'S'} "
            f"reason={reason} fill={actual_fill:.5f}"
        )

        # Delegate to original to record trade + update beta posterior + equity
        self._orig_close(st, actual_fill, t, reason)
        self._links.pop(st.spec.symbol, None)

        # Record rich live trade row
        if self.engine.trades:
            last = self.engine.trades[-1]
            row = {
                "symbol": last.symbol,
                "side": last.side,
                "entry_time": datetime.fromtimestamp(last.entry_time, tz=timezone.utc).isoformat(),
                "exit_time": datetime.fromtimestamp(last.exit_time, tz=timezone.utc).isoformat(),
                "entry": last.entry_price, "exit": last.exit_price,
                "lots": last.lots, "gross_pnl": last.gross_pnl,
                "commission": last.commission, "net_pnl": last.net_pnl,
                "realised_R": last.realised_R, "reason": last.exit_reason,
                "bars_held": last.bars_held,
            }
            self._live_trades.append(row)
            self._persist_trade(row)

    # -----------------------------------------------------------------
    # Equity sync
    # -----------------------------------------------------------------

    def _sync_equity(self):
        now = time.time()
        if now - self._last_equity_sync < self.equity_refresh_sec:
            return
        self._last_equity_sync = now
        try:
            acct = self.bridge.get_account_info()
            if acct.equity > 0:
                self.engine.equity = acct.equity
                if acct.equity > self.engine.peak_equity:
                    self.engine.peak_equity = acct.equity
        except Exception as e:
            logger.debug(f"equity sync skipped: {e}")

    # -----------------------------------------------------------------
    # Bar callback — this is where live feeds hit the strategy
    # -----------------------------------------------------------------

    def on_live_bar(self, bar: BarData):
        """Forward a live M1 bar to the engine."""
        self._sync_equity()
        t = bar.time if isinstance(bar.time, datetime) else datetime.utcnow()
        self.engine.on_bar(
            symbol=bar.symbol,
            time=t.timestamp(),
            day_key=t.strftime("%Y-%m-%d"),
            hour=t.hour,
            minute=t.minute,
            open_=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
        )

        # Reconcile halts with broker: if engine halted, force CLOSE_ALL
        if self.engine.halted_permanently or self.engine.halted_for_day:
            if self._links and not self.dry_run:
                logger.warning(
                    "GHOST HALT triggered — closing all positions on broker"
                )
                try:
                    self.bridge.close_all_positions()
                except Exception as e:
                    logger.error(f"close_all failed: {e}")
                self._links.clear()

    # -----------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------

    def subscribe(self):
        for sym in self._symbol_specs:
            self.bridge.subscribe_bars(sym, self.TIMEFRAME, self.on_live_bar)
            logger.info(f"Subscribed to {sym} {self.TIMEFRAME} bars")

    def run(self, heartbeat_sec: float = 30.0):
        self.subscribe()
        logger.info("=" * 72)
        logger.info(
            f"SmartBB v13 LIVE  | symbols={list(self._symbol_specs.keys())} | "
            f"magic={self.magic} | dry_run={self.dry_run}"
        )
        logger.info("=" * 72)

        def _sig(*_):
            logger.warning("SIGINT — shutting down")
            self._stop_event.set()

        try:
            signal.signal(signal.SIGINT, _sig)
        except Exception:
            pass

        while not self._stop_event.is_set():
            time.sleep(heartbeat_sec)
            s = self.engine.summary()
            logger.info(
                f"STATUS equity=${self.engine.equity:,.2f} peak=${self.engine.peak_equity:,.2f} "
                f"trades={s['trades']} wr={s.get('win_rate',0)*100:.1f}% "
                f"pf={s.get('pf',0):.2f} open={len(self._links)}"
            )
            if self.engine.halted_permanently:
                logger.warning("Engine halted permanently — exiting live loop")
                break

    def stop(self):
        self._stop_event.set()

    # -----------------------------------------------------------------
    # Trade log persistence
    # -----------------------------------------------------------------

    def _persist_trade(self, row: dict):
        if self.trade_log_path is None:
            return
        import json
        try:
            self.trade_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.trade_log_path, "a") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except Exception as e:
            logger.debug(f"trade log write skipped: {e}")


# =====================================================================
# Convenience: build with the default v13 universe
# =====================================================================

def build_default(
    bridge: MT5Bridge,
    symbols: Optional[list[str]] = None,
    cfg_overrides: Optional[dict] = None,
    dry_run: bool = False,
) -> SmartBBLive:
    symbols = symbols or ["US100", "US500", "US30", "DE40", "USOIL"]
    specs = [SMARTBB_UNIVERSE[s] for s in symbols]
    cfg = SmartBBConfig()
    if cfg_overrides:
        for k, v in cfg_overrides.items():
            setattr(cfg, k, v)
    return SmartBBLive(
        bridge=bridge, symbols=specs, cfg=cfg, dry_run=dry_run,
        trade_log_path=Path("Results/live_smartbb_trades.jsonl"),
    )
