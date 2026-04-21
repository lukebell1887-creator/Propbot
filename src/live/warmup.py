"""
Warm-up loader for SmartBB v16 live
====================================

Before the first live tick, pull the last N M1 bars from the broker via
MT5Bridge.get_history() and stream them through engine.on_bar() so **all**
indicators are primed:

  * Bollinger 200-period (~3-4 hours of M5 data)
  * Hurst rolling 500 M5 (~40 hours)
  * OU half-life rolling 300 M5 (~25 hours)
  * abs-Z rolling quantile 1000 M5 (~83 hours)
  * Hurst rolling quantile 500 M5 (~40 hours)

Default 5000 M1 bars (~3.5 days) is enough for BB + Hurst to be ready
immediately.  Set --warmup-bars 7200 (~5 days) or 14000 (~10 days) for the
quantiles to also be fully populated from bar 1.

Also (optional): replay the realised-R history from a v16 backtest trades
JSON into the DynamicSizerV16 so Kelly has ≥ 20 per-(symbol,side) samples
on day 1 — eliminates the "Kelly needs 30+ live trades to activate" cold
start we documented in V16_RESULTS_HONEST.md.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.execution.mt5_bridge import MT5Bridge
from src.smartbb_engine_v14 import SmartBBV14Engine
from src.dynamic_sizer_v16 import DynamicSizerV16

logger = logging.getLogger("smartbb.v16.warmup")


def warmup_engine_from_broker(
    engine: SmartBBV14Engine,
    bridge: MT5Bridge,
    internal_to_broker: dict[str, str],
    bars_per_symbol: int = 5000,
    timeout_ms: int = 30000,
) -> dict[str, int]:
    """Pre-feed the engine with the last `bars_per_symbol` M1 bars per symbol.

    Returns {internal_symbol: bars_streamed}.
    """
    streamed: dict[str, int] = {}
    total = 0
    for internal, broker_sym in internal_to_broker.items():
        logger.info(f"warm-up  {internal} ({broker_sym}):  requesting "
                    f"{bars_per_symbol} M1 bars ...")
        try:
            bars = bridge.get_history(
                broker_sym, count=bars_per_symbol, timeout_ms=timeout_ms,
            )
        except Exception as e:
            logger.warning(f"warm-up  {internal}: get_history FAILED: {e}")
            streamed[internal] = 0
            continue

        if not bars:
            logger.warning(f"warm-up  {internal}: broker returned 0 bars "
                           f"(symbol listed but no history?)")
            streamed[internal] = 0
            continue

        # bars is oldest-first per bridge contract — perfect for on_bar()
        n = 0
        for b in bars:
            try:
                t  = float(b["t"])
                o  = float(b["o"]); h = float(b["h"])
                lo = float(b["l"]); c = float(b["c"])
            except (KeyError, TypeError, ValueError):
                continue
            dt = datetime.utcfromtimestamp(t)
            day_key = dt.strftime("%Y-%m-%d")
            engine.on_bar(
                symbol=internal, time=t, day_key=day_key,
                hour=dt.hour, minute=dt.minute,
                open_=o, high=h, low=lo, close=c,
            )
            n += 1
        streamed[internal] = n
        total += n
        logger.info(f"warm-up  {internal}:  streamed {n:,} bars  "
                    f"(covers ~{n/1440:.1f} trading days)")

    # Reset any "trades" accidentally materialised during warm-up
    # (with no real SL hits, the warm-up should not create any, but belt+braces)
    n_trades_pre = len(engine.trades)
    if n_trades_pre:
        logger.warning(f"warm-up  {n_trades_pre} synthetic trades generated "
                       f"during replay — CLEARING so live P&L starts at zero")
        engine.trades.clear()
    # Reset equity/peak curve to starting equity
    engine.equity = engine.start_equity
    engine.peak_equity = engine.start_equity
    engine.equity_curve.clear() if hasattr(engine, "equity_curve") else None

    logger.info(f"warm-up  COMPLETE  |  {len(streamed)} symbols  |  "
                f"{total:,} bars streamed  |  engine ready")
    return streamed


def warmup_sizer_from_backtest(
    sizer: DynamicSizerV16,
    trades_json: Path,
    max_per_side: int = 200,
) -> dict[str, int]:
    """Pre-load Kelly trade history from a recent backtest trade log.

    Each entry in the JSON must have `symbol`, `side` (or `dir`), and
    `realised_R` (or `R`).  We only load the MOST RECENT `max_per_side` so
    the Kelly estimate stays fresh.

    Returns {(sym,side): count_loaded}.
    """
    if not trades_json.exists():
        logger.warning(f"warm-up  sizer history file not found: {trades_json}")
        return {}

    with open(trades_json, encoding="utf-8") as f:
        trades = json.load(f)
    if not isinstance(trades, list):
        logger.warning(f"warm-up  sizer history malformed ({trades_json})")
        return {}

    # Collect per-(sym,side) in chronological order, then keep last N
    buckets: dict[str, list[float]] = {}
    for tr in trades:
        sym = tr.get("symbol")
        side = tr.get("side") or tr.get("dir") or 0
        r = tr.get("realised_R", tr.get("R"))
        if sym is None or r is None:
            continue
        try:
            side_n = int(side)
        except (TypeError, ValueError):
            continue
        if side_n not in (-1, 1):
            continue
        try:
            r_f = float(r)
        except (TypeError, ValueError):
            continue
        key = f"{sym}:{side_n}"
        buckets.setdefault(key, []).append(r_f)

    counts: dict[str, int] = {}
    for key, rs in buckets.items():
        # keep most recent max_per_side
        rs = rs[-max_per_side:]
        sym, side_str = key.split(":")
        side = int(side_str)
        for r in rs:
            sizer.record_trade(sym, side, r)
        counts[key] = len(rs)

    total = sum(counts.values())
    logger.info(f"warm-up  sizer  |  loaded {total} historical realised-R "
                f"values across {len(counts)} (symbol, side) buckets "
                f"from {trades_json.name}")
    for key, n in sorted(counts.items()):
        logger.info(f"                  {key:<12}  {n} trades")
    return counts
