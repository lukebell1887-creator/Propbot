"""
SmartBB v15 — LIVE RUNNER on 5%ers MTB
======================================

Wraps `SmartBBV14Engine` (the engine our v15 walk-forward optimizer used)
so the SAME strategy that produced the backtest runs on a live MT5 broker.

Key v15-specific upgrades over v13 live runner:
  1. Loads **per-symbol** tuned params from `Results/v15_ultimate_tuning.json`
     (US30, US100, US500, DE40, XAUUSD each have distinct z_quantile / tp_frac /
     stop_atr_mult found by the optimizer).
  2. **Symbol mapping** (internal ↔ broker) so the engine keeps its clean
     names (US100) while the MT5 bridge uses broker names (NAS100).
  3. **8 % account-level kill-switch** on top of engine's 4 % daily / 5 %
     total halts — gives 2 % safety margin under 5%ers' 10 % blow rule.
  4. **Dry-run mode** — bot decides but sends no orders, for Phase A.

Architecture
------------
    MT5 terminal <--TCP--> MT5Bridge <--calls--> V15Live <--> SmartBBV14Engine
                   (broker names)                (internal)
"""
from __future__ import annotations

import json
import logging
import math
import signal
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Callable

from src.execution.mt5_bridge import (
    MT5Bridge, OrderRequest, OrderType, BarData,
)
from src.smartbb_engine import SMARTBB_UNIVERSE, SymbolSpec
from src.smartbb_engine_v14 import (
    SmartBBV14Engine, SmartBBV14Config, SymbolParams,
    _PositionV14, params_from_dict,
)

logger = logging.getLogger("smartbb.v15.live")


# =====================================================================
#  Per-symbol v15 config loader
# =====================================================================

def load_v15_params(
    json_path: Path = Path("Results/v15_ultimate_tuning.json"),
    tier: str = "TIER1",
    symbols: Optional[list[str]] = None,
) -> dict[str, SymbolParams]:
    """Load per-symbol best_params from the v15 optimizer's JSON output.

    Parameters
    ----------
    json_path : path to the tuning JSON
    tier      : only load symbols whose tier matches (default TIER1)
    symbols   : optional list of symbols to restrict to
    """
    with open(json_path, encoding="utf-8") as f:
        data = json.load(f)
    results = data.get("results", {})
    out: dict[str, SymbolParams] = {}
    for sym, r in results.items():
        if tier and r.get("tier") != tier:
            continue
        if symbols and sym not in symbols:
            continue
        params_dict = r.get("best_params")
        if not params_dict:
            continue
        out[sym] = params_from_dict(params_dict)
    return out


# =====================================================================
#  Broker-linked position
# =====================================================================

@dataclass
class BrokerLink:
    ticket: int
    internal_symbol: str
    broker_symbol: str
    side: int
    lots: float
    entry_fill: float


# =====================================================================
#  v15 Live runner
# =====================================================================

class V15Live:
    """Live runner: per-symbol v15 params + symbol_map + 8 % account kill-switch."""

    TIMEFRAME = "M1"  # engine aggregates M1→M5 internally

    def __init__(
        self,
        bridge: MT5Bridge,
        internal_symbols: list[str],               # engine-internal names (US30, US100, …)
        symbol_map: dict[str, str],                # internal → broker (US100 → NAS100)
        per_symbol_params: dict[str, SymbolParams],
        cfg: Optional[SmartBBV14Config] = None,
        magic: int = 15000,
        comment: str = "SHF_v15",
        dry_run: bool = False,
        max_account_dd_pct: float = 0.08,          # hard kill at 8 %
        equity_refresh_sec: float = 2.0,
        trade_log_path: Optional[Path] = None,
    ):
        self.bridge = bridge
        self.cfg = cfg or SmartBBV14Config()
        self.magic = magic
        self.comment = comment
        self.dry_run = dry_run
        self.max_account_dd_pct = max_account_dd_pct
        self.equity_refresh_sec = equity_refresh_sec
        self.trade_log_path = trade_log_path

        # Build engine with per-symbol params
        specs = [SMARTBB_UNIVERSE[s] for s in internal_symbols]
        acct = bridge.get_account_info()
        initial_equity = max(acct.balance, 1.0)
        logger.info(
            f"Live broker equity: ${initial_equity:,.2f} "
            f"(server={acct.server}, currency={acct.currency}, leverage=1:{acct.leverage})"
        )
        self.engine = SmartBBV14Engine(
            symbols=specs,
            params=per_symbol_params,
            cfg=self.cfg,
            initial_equity=initial_equity,
        )
        self.engine.start_equity = initial_equity

        # Symbol mapping (both directions)
        self.internal_to_broker = dict(symbol_map)
        self.broker_to_internal = {v: k for k, v in symbol_map.items()}

        # Specs keyed by internal name (what the engine uses)
        self._specs_internal: dict[str, SymbolSpec] = {s.symbol: s for s in specs}

        # Patch engine's three action methods
        self._patch_engine()

        # State
        self._links: dict[str, BrokerLink] = {}   # keyed by INTERNAL symbol
        self._last_equity_sync: float = 0.0
        self._stop_event = threading.Event()
        self._live_trades: list[dict] = []
        self._kill_fired = False

    # -----------------------------------------------------------------
    def _patch_engine(self):
        """Intercept the three order-touching engine methods to route live."""
        eng = self.engine
        self._orig_close = eng._close
        self._orig_manage = eng._manage
        self._orig_maybe_enter = eng._maybe_enter
        eng._maybe_enter = self._live_maybe_enter
        eng._close = self._live_close
        eng._manage = self._live_manage

    # -----------------------------------------------------------------
    #  LIVE ENTRY (replays engine's _maybe_enter but routes to broker)
    # -----------------------------------------------------------------
    def _live_maybe_enter(self, st, time_ts: float, close: float):
        cfg = self.cfg
        p = st.params

        # Gate 1: Hurst
        if st._hurst >= p.hurst_max_abs:
            return
        if st.hurst_q.ready and st._hurst > st.hurst_q.value():
            return

        # Gate 2: |Z|
        z = st.bb.z(close)
        abs_z = abs(z)
        if not (p.z_min_abs <= abs_z <= p.z_max_abs):
            return
        if st.abs_z_q.ready and abs_z < st.abs_z_q.value():
            return

        # Gate 3: OU half-life
        halflife = st._ou_halflife
        if p.use_ou_gate:
            if not math.isfinite(halflife) or halflife > p.ou_max_halflife:
                return

        side = -1 if z > 0 else +1

        # Concurrency
        total_open = len(self._links)
        if total_open >= cfg.max_concurrent:
            return
        same_cls = sum(
            1 for sym in self._links
            if self._specs_internal[sym].asset_class == st.spec.asset_class
        )
        if same_cls >= cfg.max_same_class_concurrent:
            return
        if st.spec.symbol in self._links:
            return

        # Sizing
        atr_pts = st.atr.value
        mean = st.bb.mean
        std = st.bb.std
        entry_fill = close + side * 0.5 * st.spec.spread_pts
        if side > 0:
            band = mean - cfg.bb_sigma * std
            sl = band - p.stop_atr_mult * atr_pts
        else:
            band = mean + cfg.bb_sigma * std
            sl = band + p.stop_atr_mult * atr_pts
        stop_distance = abs(entry_fill - sl)

        tp_raw = mean
        tp = entry_fill + side * p.tp_frac * abs(tp_raw - entry_fill)
        tp_distance = abs(tp - entry_fill)
        if tp_distance <= 0 or stop_distance <= 0:
            return

        # Amplitude gate (cost discipline)
        expected_pts = tp_distance
        cost_pts = 2.0 * st.spec.spread_pts
        comm_one = st.spec.round_trip_commission(avg_price=entry_fill, lots=1.0)
        cost_d_per_lot = cost_pts * st.spec.pip_value + comm_one
        expected_d_per_lot = expected_pts * st.spec.pip_value
        if expected_d_per_lot < cfg.amplitude_hurdle * cost_d_per_lot:
            return

        # AKAD sizing × per-symbol risk_multiplier
        risk_pct = self.engine._risk_pct(st.spec.symbol, side) * p.risk_multiplier
        risk_pct = max(cfg.min_risk_pct, min(cfg.max_risk_pct, risk_pct))
        risk_d = self.engine.equity * risk_pct
        lots = risk_d / max(stop_distance * st.spec.pip_value, 1e-9)
        lots = max(st.spec.min_lots,
                   min(st.spec.max_lots,
                       math.floor(lots / st.spec.lot_step) * st.spec.lot_step))
        if lots < st.spec.min_lots:
            return

        if p.use_ou_gate and math.isfinite(halflife):
            time_stop_bars_abs = st.m5_bars + min(
                int(p.time_stop_ou_mult * halflife) + 1, p.time_stop_max)
        else:
            time_stop_bars_abs = st.m5_bars + p.time_stop_max

        # ---- ROUTE TO BROKER ---------------------------------------
        broker_sym = self.internal_to_broker.get(st.spec.symbol, st.spec.symbol)
        order_type = OrderType.MARKET_BUY if side > 0 else OrderType.MARKET_SELL
        req = OrderRequest(
            symbol=broker_sym,
            order_type=order_type,
            lots=lots, price=0.0,
            sl=sl, tp=tp,
            deviation=20,
            magic=self.magic,
            comment=self.comment,
        )
        logger.info(
            f"SIGNAL   {st.spec.symbol}->{broker_sym} {'LONG' if side>0 else 'SHORT'} "
            f"Z={z:+.2f} H={st._hurst:.2f} HL={halflife:.1f} lots={lots:.2f} "
            f"entry~{entry_fill:.5f} sl={sl:.5f} tp={tp:.5f} risk=${risk_d:.0f}"
        )
        if self.dry_run:
            logger.warning(f"DRY_RUN — NOT sending order on {broker_sym}")
            return

        try:
            result = self.bridge.send_order(req)
        except Exception as e:
            logger.error(f"Order send error on {broker_sym}: {e}")
            return
        if not result.success:
            logger.error(f"Order REJECTED on {broker_sym}: {result.error_message}")
            return

        real_fill = result.price or entry_fill
        real_lots = result.lots or lots

        # Record position in engine state using REAL fill price
        pos = _PositionV14(
            symbol=st.spec.symbol, side=side,
            entry_price=real_fill, entry_time=time_ts,
            entry_bar=st.m5_bars, lots=real_lots,
            sl=sl, tp=tp,
            z_at_entry=z, hurst_at_entry=st._hurst,
            halflife_at_entry=halflife if math.isfinite(halflife) else -1.0,
            R_dist=abs(real_fill - sl), R_dollars=risk_d,
            time_stop_bars=time_stop_bars_abs,
        )
        st.position = pos
        if p.use_optimal_stop:
            st.optimal_stop.arm(side)
        self._links[st.spec.symbol] = BrokerLink(
            ticket=result.ticket,
            internal_symbol=st.spec.symbol,
            broker_symbol=broker_sym,
            side=side, lots=real_lots, entry_fill=real_fill,
        )
        logger.info(
            f"OPENED   {st.spec.symbol}->{broker_sym} ticket={result.ticket} "
            f"fill={real_fill:.5f}"
        )

    # -----------------------------------------------------------------
    #  LIVE MANAGE (delegates to engine; propagates SL changes to broker)
    # -----------------------------------------------------------------
    def _live_manage(self, st, t: float, close: float):
        pos = st.position
        if pos is None:
            return
        old_sl = pos.sl
        self._orig_manage(st, t, close)
        if st.position is None:
            return
        if abs(st.position.sl - old_sl) > 1e-9:
            link = self._links.get(st.spec.symbol)
            if link and not self.dry_run:
                try:
                    self.bridge.modify_position(link.ticket, sl=st.position.sl)
                    logger.info(
                        f"SL_MOVE  {link.internal_symbol} ticket={link.ticket} "
                        f"sl={old_sl:.5f}->{st.position.sl:.5f}"
                    )
                except Exception as e:
                    logger.error(f"SL modify failed on {link.broker_symbol}: {e}")

    # -----------------------------------------------------------------
    #  LIVE CLOSE (sends close to broker first, then books locally)
    # -----------------------------------------------------------------
    def _live_close(self, st, fill: float, t: float, reason: str):
        pos = st.position
        if pos is None:
            return
        link = self._links.get(st.spec.symbol)
        actual_fill = fill
        if link and not self.dry_run:
            try:
                self.bridge.close_position(link.ticket)
                time.sleep(0.2)
                positions = self.bridge.get_positions(symbol=link.broker_symbol)
                still_open = any(p.ticket == link.ticket for p in positions)
                if still_open:
                    logger.error(
                        f"Position {link.ticket} on {link.broker_symbol} "
                        f"did not close — retrying"
                    )
                    self.bridge.close_position(link.ticket)
                quotes = self.bridge._quotes.get(link.broker_symbol, {})
                if pos.side > 0:
                    actual_fill = quotes.get("bid", fill) or fill
                else:
                    actual_fill = quotes.get("ask", fill) or fill
            except Exception as e:
                logger.error(f"Close failed on {link.broker_symbol}: {e}")
        logger.info(
            f"CLOSED   {st.spec.symbol} side={'L' if pos.side>0 else 'S'} "
            f"reason={reason} fill={actual_fill:.5f}"
        )
        # Delegate to original engine close for book-keeping
        self._orig_close(st, actual_fill, t, reason)
        self._links.pop(st.spec.symbol, None)
        # Persist live trade row
        if self.engine.trades:
            last = self.engine.trades[-1]
            row = {
                "symbol": last.symbol, "side": last.side,
                "entry_time": datetime.fromtimestamp(last.entry_time, tz=timezone.utc).isoformat(),
                "exit_time": datetime.fromtimestamp(last.exit_time, tz=timezone.utc).isoformat(),
                "entry": last.entry_price, "exit": last.exit_price,
                "lots": last.lots, "gross_pnl": last.gross_pnl,
                "commission": last.commission, "net_pnl": last.net_pnl,
                "realised_R": last.realised_R, "reason": last.exit_reason,
                "bars_held": last.bars_held,
                "z_at_entry": last.z_at_entry,
                "hurst_at_entry": last.hurst_at_entry,
            }
            self._live_trades.append(row)
            self._persist_trade(row)

    # -----------------------------------------------------------------
    #  Equity sync + 8 % account-level kill-switch
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
                # 8 % account-level kill-switch
                if not self._kill_fired and self.engine.peak_equity > 0:
                    acct_dd = (self.engine.peak_equity - acct.equity) / self.engine.peak_equity
                    if acct_dd >= self.max_account_dd_pct:
                        logger.warning(
                            f"🚨 ACCOUNT DD {acct_dd*100:.1f}% >= "
                            f"{self.max_account_dd_pct*100:.1f}% KILL-SWITCH — "
                            f"closing ALL positions and halting permanently."
                        )
                        self._kill_fired = True
                        self.engine.halted_permanently = True
                        if not self.dry_run:
                            try:
                                self.bridge.close_all_positions()
                            except Exception as e:
                                logger.error(f"close_all on kill failed: {e}")
                        self._links.clear()
        except Exception as e:
            logger.debug(f"equity sync skipped: {e}")

    # -----------------------------------------------------------------
    #  Bar callback — entry from the TCP bridge
    # -----------------------------------------------------------------
    def on_live_bar(self, bar: BarData):
        self._sync_equity()
        # Translate broker → internal symbol
        internal = self.broker_to_internal.get(bar.symbol, bar.symbol)
        if internal not in self._specs_internal:
            return
        t = bar.time if isinstance(bar.time, datetime) else datetime.utcnow()
        self.engine.on_bar(
            symbol=internal,
            time=t.timestamp(),
            day_key=t.strftime("%Y-%m-%d"),
            hour=t.hour, minute=t.minute,
            open_=bar.open, high=bar.high,
            low=bar.low, close=bar.close,
        )
        # Reconcile engine halts with broker
        if self.engine.halted_permanently or self.engine.halted_for_day:
            if self._links and not self.dry_run:
                logger.warning("Engine halt detected — closing all on broker")
                try:
                    self.bridge.close_all_positions()
                except Exception as e:
                    logger.error(f"close_all failed: {e}")
                self._links.clear()

    # -----------------------------------------------------------------
    #  Lifecycle
    # -----------------------------------------------------------------
    def subscribe(self):
        for internal, broker_sym in self.internal_to_broker.items():
            if internal not in self._specs_internal:
                continue
            self.bridge.subscribe_bars(broker_sym, self.TIMEFRAME, self.on_live_bar)
            logger.info(f"Subscribed  {broker_sym} (internal={internal}) {self.TIMEFRAME}")

    def run(self, heartbeat_sec: float = 60.0):
        self.subscribe()
        logger.info("=" * 78)
        logger.info(
            f"SmartBB v15 LIVE  | symbols={list(self.internal_to_broker.keys())} | "
            f"magic={self.magic} | dry_run={self.dry_run} | "
            f"account_kill={self.max_account_dd_pct*100:.0f}%"
        )
        logger.info("=" * 78)

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
            pf = s.get("pf", 0)
            pf_str = f"{pf:.2f}" if pf != float("inf") else "inf"
            logger.info(
                f"HEARTBEAT  equity=${self.engine.equity:,.2f} "
                f"peak=${self.engine.peak_equity:,.2f} "
                f"trades={s['trades']} wr={s.get('win_rate',0)*100:.1f}% "
                f"pf={pf_str} open={len(self._links)}"
            )
            if self.engine.halted_permanently:
                logger.warning("Engine halted permanently — exiting live loop")
                break

    def stop(self):
        self._stop_event.set()

    # -----------------------------------------------------------------
    def _persist_trade(self, row: dict):
        if self.trade_log_path is None:
            return
        try:
            self.trade_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.trade_log_path, "a") as f:
                f.write(json.dumps(row, default=str) + "\n")
        except Exception as e:
            logger.debug(f"trade log write skipped: {e}")
