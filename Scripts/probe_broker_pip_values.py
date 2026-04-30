"""
Scripts/probe_broker_pip_values.py
==================================

Print the live broker `trade_tick_value` and `trade_tick_size` for every
symbol the bot trades, scaled into the bot's $-per-pip-per-lot convention.

Use this BEFORE going live to confirm the EUR→USD conversion on DAX40 and
verify USD-quoted symbols (US30, US500, XAUUSD) still come out at ~$1/pt.

Run on the VPS (where MetaTrader5 terminal is running):

    cd C:\\PropBot
    python Scripts\\probe_broker_pip_values.py
        --broker-names DE40=DAX40,US30=US30,US500=SP500,XAUUSD=XAUUSD

Expected output on a USD-denominated 5ers / Eightcap account, today
(EURUSD ≈ 1.166, broker `trade_tick_size = 0.01`):

    ==============================================================================
      BROKER PIP-VALUE TABLE   source=broker_live
    ==============================================================================
        DE40    $/bot-tick/lot =   1.1664   ✓ broker-truth (was $1.00, +16.6% via FX)
        US30    $/bot-tick/lot =   1.0000   (USD-quoted, no FX needed)
        US500   $/bot-tick/lot =   1.0000   (USD-quoted, no FX needed)
        XAUUSD  $/bot-tick/lot =   1.0000   (USD-quoted, no FX needed)
    ==============================================================================

If a symbol fails the sanity gate (broker reports a value 100× off, etc.)
the line will say "(broker-truth, fallback was $1.00, +X.X%)" or it will
have been REJECTED and reverted to the hardcoded fallback.  Search the
log for the word REJECTED to see which.

Anything saying `source=fallback_*` means MetaTrader5 isn't reachable or
the package isn't installed; fix that BEFORE running live (see
Docs/V30_BROKER_PIP_VALUE_FIX.md).
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Make `src` importable when run from project root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.live.broker_specs import fetch_live_pip_values, log_pip_values_banner
from src.live.v30_live import V30_BROKER_NAMES, V30_BROKER_TICK_SIZE


def _parse_overrides(s: str) -> dict:
    """Parse 'BOT=BROKER,BOT2=BROKER2' into a dict."""
    out: dict = {}
    for chunk in (s or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise argparse.ArgumentTypeError(
                f"--broker-names entry '{chunk}' must look like BOT=BROKER")
        bot, brk = chunk.split("=", 1)
        out[bot.strip()] = brk.strip()
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--broker-names",
        type=_parse_overrides,
        default={},
        help=("Comma-separated overrides BOT=BROKER, e.g. "
              "'DE40=DAX40,US30=US30,US500=SP500,XAUUSD=XAUUSD'.  "
              "Required when broker symbol names differ from the "
              "defaults baked into V30_BROKER_NAMES."),
    )
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )

    bot_to_broker = dict(V30_BROKER_NAMES)
    bot_to_broker.update(args.broker_names)
    bot_tick_sizes = dict(V30_BROKER_TICK_SIZE)

    pip_values, source = fetch_live_pip_values(bot_to_broker, bot_tick_sizes)
    log_pip_values_banner(pip_values, source)

    if source.startswith("fallback"):
        print("EXIT 1 — broker fetch failed; the engine will run with hardcoded "
              "$1/pt and DAX40 will be under-sized by ~14%.")
        return 1
    if source == "broker_live_partial":
        print("EXIT 2 — at least one symbol failed the sanity gate and reverted "
              "to fallback. Search the log above for the word REJECTED.")
        return 2
    print("EXIT 0 — all values came from the live MT5 terminal and passed sanity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
