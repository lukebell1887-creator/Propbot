"""
broker_specs.py — query MT5 terminal for live tick_value per symbol.
=====================================================================

WHY THIS MODULE EXISTS
----------------------
Before this fix the engine hardcoded `pip_value_per_lot = $1.00 / point / lot`
for every index symbol.  That is correct for **USD-quoted** instruments
(US30, US500, XAUUSD) — they pay $1 per index point per 1.0 lot regardless
of account currency.

It is **wrong** for **EUR-quoted** instruments (DAX40 / DE40 / GER40) when
the trading account is denominated in USD.  For those, every point of
P/L is `€1` first, which the broker then converts to USD at the live
EURUSD rate (≈1.166 today).  So the *true* pip value is ≈ $1.166, not $1.00.

Concrete example from 2026-04-30:
    Trade: DAX40 BUY 11.5 lots, SL hit at -76.04 pts.
    Bot's planned risk_$  = 76.04 × 11.5 × $1.00 =   $874.46  (planned ≈ $853 incl tick math)
    Broker's real charge  = 76.04 × 11.5 × $1.166 = $1,019.51  ★ matches statement
This module fetches `SymbolInfo.trade_tick_value` from the running MT5
terminal — MT5 already does the FX conversion internally and returns the
**cash value in account currency** of a 1-tick move on 1.0 lot.  That
gives us broker-truth without ever hard-coding an FX rate.

USAGE
-----
    from src.live.broker_specs import fetch_live_pip_values, log_pip_values_banner

    bot_to_broker = {"DE40": "DAX40", "US30": "US30", "US500": "SP500", "XAUUSD": "XAUUSD"}
    pip_values, source = fetch_live_pip_values(bot_to_broker)
    log_pip_values_banner(pip_values, source)

The engine then mutates `self.specs[sym].pip_value_per_lot = pip_values[sym]`
before any sizing call.

FAIL-SAFE BEHAVIOUR
-------------------
If `MetaTrader5` is not installed, or `mt5.initialize()` fails, or any
single symbol's tick_value comes back missing/insane, we **never crash**.
We log a loud WARNING and return the hardcoded fallback.  The bot still
trades — it just sizes DAX40 the old way until you fix the environment.

A boolean `source == "broker_live"` flag lets the preflight check #14
flag any run that is silently falling back to hardcoded values.
"""
from __future__ import annotations

import logging
from typing import Dict, Tuple

logger = logging.getLogger(__name__)


# Hardcoded fallback ($1/pt for indices, $1/tick for XAUUSD).
# CORRECT for US30, US500, XAUUSD on a USD account.
# WRONG for DE40 on a USD account (true value ≈ $1.166 = €1 × EURUSD).
# The fallback is intentionally conservative: if we can't talk to MT5,
# we'd rather under-reach edge than over-size DAX40.
_FALLBACK_USD_PER_TICK_PER_LOT: Dict[str, float] = {
    "DE40":   1.0,   # broker-truth ≈ $1.166; this fallback under-sizes DE40 by ~14%.
    "US30":   1.0,
    "US500":  1.0,
    "XAUUSD": 1.0,
}


def fetch_live_pip_values(
    bot_to_broker: Dict[str, str],
) -> Tuple[Dict[str, float], str]:
    """Query a running MT5 terminal for the cash-currency value of a
    1-tick move on 1.0 lot, per symbol.

    Parameters
    ----------
    bot_to_broker : Dict[str, str]
        Map from internal bot symbol (e.g. "DE40") to the broker's
        symbol name (e.g. "DAX40").  The bot symbol is used as the key
        in the returned dict — broker names vary across feeds.

    Returns
    -------
    pip_values : Dict[str, float]
        {bot_sym: $-cash-per-tick-per-lot}, always populated for every
        bot symbol (falls back to $1.00 on per-symbol failure).
    source : str
        One of:
          - "broker_live"            — every value came from MT5 successfully
          - "broker_live_partial"    — at least one symbol fell back to hardcoded
          - "fallback_no_mt5_pkg"    — MetaTrader5 python package not installed
          - "fallback_mt5_init_fail" — package present but mt5.initialize() failed
    """
    out: Dict[str, float] = {}

    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError:
        logger.warning(
            "[broker-spec] MetaTrader5 python package NOT INSTALLED. "
            "Install with: pip install MetaTrader5    "
            "Falling back to hardcoded $1/pt — DAX40 will be under-sized "
            "by ~14% until the package is added.")
        for bot_sym in bot_to_broker:
            out[bot_sym] = _FALLBACK_USD_PER_TICK_PER_LOT.get(bot_sym, 1.0)
        return out, "fallback_no_mt5_pkg"

    if not mt5.initialize():
        last_err = mt5.last_error() if hasattr(mt5, "last_error") else "?"
        logger.warning(
            "[broker-spec] mt5.initialize() failed: %s    "
            "Is the MT5 terminal running and 'Allow algorithmic trading' on? "
            "Falling back to hardcoded $1/pt.", last_err)
        for bot_sym in bot_to_broker:
            out[bot_sym] = _FALLBACK_USD_PER_TICK_PER_LOT.get(bot_sym, 1.0)
        return out, "fallback_mt5_init_fail"

    any_fallback = False
    try:
        for bot_sym, broker_sym in bot_to_broker.items():
            info = mt5.symbol_info(broker_sym)
            if info is None:
                logger.warning(
                    "[broker-spec] %s (%s): symbol_info() returned None — "
                    "using fallback $%.4f",
                    bot_sym, broker_sym,
                    _FALLBACK_USD_PER_TICK_PER_LOT.get(bot_sym, 1.0))
                out[bot_sym] = _FALLBACK_USD_PER_TICK_PER_LOT.get(bot_sym, 1.0)
                any_fallback = True
                continue

            # `trade_tick_value` is the CASH value in ACCOUNT CURRENCY of a
            # 1-tick move on 1.0 lot.  MT5 already does the FX conversion.
            tv = float(getattr(info, "trade_tick_value", 0.0) or 0.0)

            # Sanity bounds — if the broker returns something ludicrous
            # (e.g. 0.0 because the symbol isn't selected), refuse it.
            if not (0.001 < tv < 100.0):
                logger.warning(
                    "[broker-spec] %s (%s): trade_tick_value=%.6f outside "
                    "sanity bounds (0.001, 100) — using fallback $%.4f",
                    bot_sym, broker_sym, tv,
                    _FALLBACK_USD_PER_TICK_PER_LOT.get(bot_sym, 1.0))
                out[bot_sym] = _FALLBACK_USD_PER_TICK_PER_LOT.get(bot_sym, 1.0)
                any_fallback = True
                continue

            out[bot_sym] = tv
            logger.info(
                "[broker-spec] %s (%s): trade_tick_value = $%.4f / tick / lot   "
                "(account_currency=%s, contract_size=%.1f)",
                bot_sym, broker_sym, tv,
                getattr(info, "currency_profit", "?"),
                getattr(info, "trade_contract_size", -1.0))
    finally:
        try:
            mt5.shutdown()
        except Exception:
            pass

    return out, ("broker_live_partial" if any_fallback else "broker_live")


def log_pip_values_banner(
    pip_values: Dict[str, float],
    source: str,
    *,
    fallback_reference: Dict[str, float] = _FALLBACK_USD_PER_TICK_PER_LOT,
) -> None:
    """Print a loud, scannable banner so the operator can verify broker
    numbers at a glance in the PowerShell console.

    Output looks like:

        ==============================================================
          BROKER PIP-VALUE TABLE   source=broker_live
        ==============================================================
            DE40    $/pt/lot =   1.1664   ✓ EUR→USD applied (broker truth)
            US30    $/pt/lot =   1.0000
            US500   $/pt/lot =   1.0000
            XAUUSD  $/pt/lot =   1.0000
        ==============================================================
    """
    bar = "=" * 78
    lines = [
        "",
        bar,
        f"  BROKER PIP-VALUE TABLE   source={source}",
        bar,
    ]
    for sym in sorted(pip_values):
        v = pip_values[sym]
        ref = fallback_reference.get(sym, 1.0)
        marker = ""
        if abs(v - ref) < 1e-4:
            # Same as fallback. For DE40 this is suspicious; for USD-quoted
            # symbols (US30/US500/XAUUSD) it's the expected value.
            if sym == "DE40":
                marker = "  WARN: still $1.00 — likely fallback, no FX correction"
            else:
                marker = "  (USD-quoted, no FX needed)"
        elif sym == "DE40" and v > ref:
            pct = (v / ref - 1.0) * 100.0
            marker = f"  ✓ broker-truth (was ${ref:.2f}, +{pct:.1f}% via FX)"
        else:
            marker = f"  (fallback was ${ref:.2f})"
        lines.append(f"    {sym:6s}  $/pt/lot = {v:>8.4f}   {marker}")
    lines.append(bar)
    if source.startswith("fallback"):
        lines.append(
            "  WARNING: BROKER FETCH FAILED — values above are HARDCODED. "
            "DAX40 will be under-sized by ~14%.")
        lines.append(bar)
    lines.append("")
    msg = "\n".join(lines)
    print(msg, flush=True)
    for ln in msg.splitlines():
        logger.info(ln)
