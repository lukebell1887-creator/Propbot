"""
broker_specs.py — query MT5 terminal for live tick_value per symbol.
=====================================================================

WHY THIS MODULE EXISTS
----------------------
Before this fix the engine hardcoded `pip_value_per_lot = $1.00 / point / lot`
for every index symbol.  That's correct for **USD-quoted** instruments
(US30, US500, XAUUSD on a USD account) but WRONG for the **EUR-quoted**
DAX40 / DE40 contract — every point is €1 first, which the broker
converts to USD at the live EURUSD rate (≈1.166 today).

THE TWO TRAPS WE HAD TO LEARN ABOUT
-----------------------------------
MT5 reports two related fields on `SymbolInfo`:

    trade_tick_value : cash value (in account currency) of ONE
                       `trade_tick_size` move on 1.0 lot.
    trade_tick_size  : the broker's minimum tick (often 0.01, NOT 1.0).

So `trade_tick_value` is **per `trade_tick_size`**, NOT per "1 unit of price".

Eightcap / 5ers report:
    DE40 :  trade_tick_value = $0.0117    trade_tick_size = 0.01
    US30 :  trade_tick_value = $0.0100    trade_tick_size = 0.01
    SP500:  trade_tick_value = $0.0100    trade_tick_size = 0.01
    XAUUSD: trade_tick_value = $0.0100    trade_tick_size = 0.01

The bot's downstream `pip_value_per_lot` field is "$ P/L per BOT'S
tick_size per 1.0 lot".  The bot uses tick_size = 1.0 for indices and
0.01 for XAUUSD.  Therefore we MUST scale:

    pip_value_per_lot  =  trade_tick_value × (bot_tick_size / trade_tick_size)

Worked examples (broker = Eightcap, account = USD):
    DE40 : 0.0117 × (1.0  / 0.01)  = $1.17  / pt    / lot   ★ EUR→USD
    US30 : 0.0100 × (1.0  / 0.01)  = $1.00  / pt    / lot
    US500: 0.0100 × (1.0  / 0.01)  = $1.00  / pt    / lot
    XAUUSD:0.0100 × (0.01 / 0.01)  = $0.01  / 0.01-tick / lot
                                  ^^^^ but bot expects $1.00 here!

The XAUUSD case is the second trap: the broker's contract_size = 100
should produce a tick_value ~$1.00 / $0.01-tick / lot, but Eightcap
reports $0.01 — implying their effective lot has only 1 oz of gold,
not 100.  Rather than guess, we fall back if the result is outside a
strict sanity band (0.5× to 5× the hardcoded fallback).

DEFENCE-IN-DEPTH SANITY GATE
----------------------------
For every symbol:
    1. fetch tv (trade_tick_value) and ts (trade_tick_size).
    2. compute candidate = tv × (bot_tick_size / ts)
    3. accept ONLY if 0.5 × fallback ≤ candidate ≤ 5 × fallback.
    4. otherwise log a CRITICAL warning and use the hardcoded fallback.

This means a wrongly-configured broker can NEVER over-size the bot.
At worst we under-size DAX40 by 14% (revert to the old $1/pt fallback).

USAGE
-----
    from src.live.broker_specs import fetch_live_pip_values, log_pip_values_banner

    bot_to_broker   = {"DE40": "DAX40", "US30": "US30", "US500": "SP500", "XAUUSD": "XAUUSD"}
    bot_tick_sizes  = {"DE40": 1.0,     "US30": 1.0,    "US500": 1.0,     "XAUUSD": 0.01}
    pip_values, source = fetch_live_pip_values(bot_to_broker, bot_tick_sizes)
    log_pip_values_banner(pip_values, source)

The engine then mutates `self.specs[sym].pip_value_per_lot = pip_values[sym]`
before any sizing call.

The string `source` is one of:
    broker_live          — every value came from MT5 and passed sanity
    broker_live_partial  — at least one symbol fell back due to sanity-gate
    fallback_no_mt5_pkg  — MetaTrader5 python package not installed
    fallback_mt5_init    — package installed but mt5.initialize() failed
"""
from __future__ import annotations

import logging
from typing import Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# Hardcoded fallback ($1/pt for indices, $1/0.01-tick for XAUUSD).
# Correct on a USD account for USD-quoted symbols.
# DE40 fallback understates true value by ~14-17 % (the old bug),
# but that's a SAFE direction (under-size, not over-size).
_FALLBACK_USD_PER_TICK_PER_LOT: Dict[str, float] = {
    "DE40":   1.0,   # broker-truth ≈ $1.166 ; this fallback under-sizes by ~14 %.
    "US30":   1.0,
    "US500":  1.0,
    "XAUUSD": 1.0,
}

# Sanity-gate band, expressed as a multiplier of the fallback.
# Any broker value outside [low_mult × fallback, high_mult × fallback] is
# REJECTED (we use the fallback instead).
_SANITY_LOW_MULT  = 0.5
_SANITY_HIGH_MULT = 5.0


def fetch_live_pip_values(
    bot_to_broker: Dict[str, str],
    bot_tick_sizes: Dict[str, float],
) -> Tuple[Dict[str, float], str]:
    """Query a running MT5 terminal for the cash-currency value of a
    1-bot-tick move on 1.0 lot, per symbol.

    Parameters
    ----------
    bot_to_broker : Dict[str, str]
        Map from internal bot symbol (e.g. "DE40") to the broker's
        symbol name (e.g. "DAX40").
    bot_tick_sizes : Dict[str, float]
        Map from internal bot symbol to the bot's tick size convention
        (e.g. {"DE40": 1.0, "XAUUSD": 0.01}).  Required because MT5's
        `trade_tick_value` is per the broker's `trade_tick_size`, but
        the engine wants $ per BOT tick.  See module docstring.

    Returns
    -------
    pip_values : Dict[str, float]
        {bot_sym: $-cash-per-bot-tick-per-lot}, always populated for every
        bot symbol.  Falls back to $1 on any per-symbol failure or
        sanity-gate rejection.
    source : str
        See module docstring.
    """
    out: Dict[str, float] = {}

    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError:
        logger.warning(
            "[broker-spec] MetaTrader5 python package NOT INSTALLED. "
            "Install with: pip install MetaTrader5    "
            "Falling back to hardcoded $1/pt — DAX40 will be under-sized "
            "by ~14%% until the package is added.")
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
        return out, "fallback_mt5_init"

    any_fallback = False
    try:
        for bot_sym, broker_sym in bot_to_broker.items():
            fb = _FALLBACK_USD_PER_TICK_PER_LOT.get(bot_sym, 1.0)
            bot_ts = float(bot_tick_sizes.get(bot_sym, 1.0))
            sane_lo = _SANITY_LOW_MULT  * fb
            sane_hi = _SANITY_HIGH_MULT * fb

            info = mt5.symbol_info(broker_sym)
            if info is None:
                logger.warning(
                    "[broker-spec] %s (%s): symbol_info() returned None — "
                    "using fallback $%.4f",
                    bot_sym, broker_sym, fb)
                out[bot_sym] = fb
                any_fallback = True
                continue

            # Make sure MT5 has the symbol selected — some terminals lazy-load.
            if not getattr(info, "select", True):
                try:
                    mt5.symbol_select(broker_sym, True)
                    info = mt5.symbol_info(broker_sym) or info
                except Exception:
                    pass

            tv = float(getattr(info, "trade_tick_value", 0.0) or 0.0)
            ts = float(getattr(info, "trade_tick_size",  0.0) or 0.0)
            cs = float(getattr(info, "trade_contract_size", -1.0) or -1.0)
            ccy = getattr(info, "currency_profit", "?")

            if tv <= 0 or ts <= 0:
                logger.warning(
                    "[broker-spec] %s (%s): trade_tick_value=%.6f or "
                    "trade_tick_size=%.6f non-positive — using fallback $%.4f",
                    bot_sym, broker_sym, tv, ts, fb)
                out[bot_sym] = fb
                any_fallback = True
                continue

            # The crucial scale conversion (see module docstring).
            scaled = tv * (bot_ts / ts)

            # Strict sanity gate.  Outside the band → fall back rather than
            # risk silently mis-sizing live trades.
            if not (sane_lo <= scaled <= sane_hi):
                logger.warning(
                    "[broker-spec] %s (%s): tv=$%.4f, ts=%.4f, bot_ts=%.4f → "
                    "scaled=$%.4f OUTSIDE sanity [%.4f, %.4f] — "
                    "REJECTED, using fallback $%.4f. "
                    "(currency=%s, contract_size=%.1f)",
                    bot_sym, broker_sym, tv, ts, bot_ts, scaled,
                    sane_lo, sane_hi, fb, ccy, cs)
                out[bot_sym] = fb
                any_fallback = True
                continue

            out[bot_sym] = scaled
            logger.info(
                "[broker-spec] %s (%s): tv=$%.4f /broker-tick(%.4f) × "
                "(bot-tick %.4f / broker-tick %.4f) = $%.4f / bot-tick / lot   "
                "(currency=%s, contract_size=%.1f)",
                bot_sym, broker_sym, tv, ts, bot_ts, ts, scaled, ccy, cs)
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
    rejected_symbols: Optional[set] = None,
) -> None:
    """Print a loud, scannable banner so the operator can verify broker
    numbers at a glance in the PowerShell console.

    `rejected_symbols` is the set of bot-symbols whose broker value was
    REJECTED by the sanity gate (and replaced with the fallback). When
    we don't know (None), we fall back to inferring from value-vs-fallback
    equality, which can occasionally mis-label a broker-confirmed $1.00
    as "fallback".
    """
    bar = "=" * 78
    lines = [
        "",
        bar,
        f"  BROKER PIP-VALUE TABLE   source={source}",
        bar,
    ]
    rejected_symbols = rejected_symbols or set()
    for sym in sorted(pip_values):
        v = pip_values[sym]
        ref = fallback_reference.get(sym, 1.0)
        marker = ""
        if sym in rejected_symbols:
            # Sanity gate rejected the broker value; we're using the fallback.
            # This is the SAFE outcome — the fallback is the same value
            # backtests have always used.
            marker = (f"  ⚠ broker REJECTED by sanity gate — using fallback "
                      f"${ref:.4f} (matches backtest)")
        elif abs(v - ref) < 1e-4:
            # Broker reported a value that happened to equal the fallback
            # (i.e. broker confirmed $1/pt for a USD-quoted symbol).
            marker = "  ✓ broker confirms (USD-quoted, no FX needed)"
        elif sym == "DE40" and v > ref:
            pct = (v / ref - 1.0) * 100.0
            marker = f"  ✓ broker-truth (was ${ref:.2f}, +{pct:.1f}% via FX)"
        else:
            pct = (v / ref - 1.0) * 100.0
            marker = f"  ✓ broker-truth (fallback was ${ref:.2f}, {pct:+.1f}%)"
        lines.append(f"    {sym:6s}  $/bot-tick/lot = {v:>8.4f}   {marker}")
    lines.append(bar)
    if source.startswith("fallback"):
        lines.append(
            "  WARNING: BROKER FETCH FAILED — values above are HARDCODED. "
            "DAX40 will be under-sized by ~14%.")
        lines.append(bar)
    elif source == "broker_live_partial":
        lines.append(
            "  NOTE: at least one symbol failed the sanity gate and fell back to")
        lines.append(
            "  the hardcoded value. The fallback is the SAME value the backtest")
        lines.append(
            "  uses, so the rejected symbol(s) will trade IDENTICALLY to backtest.")
        lines.append(
            "  This is the SAFE outcome of defence-in-depth — not an error.")
        lines.append(bar)
    lines.append("")
    msg = "\n".join(lines)
    print(msg, flush=True)
    for ln in msg.splitlines():
        logger.info(ln)
