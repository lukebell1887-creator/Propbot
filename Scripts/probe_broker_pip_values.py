"""
Scripts/probe_broker_pip_values.py
==================================

Print the live broker `trade_tick_value` for every symbol the bot trades.
Use this BEFORE going live to confirm the EUR→USD conversion on DAX40 and
verify USD-quoted symbols (US30, US500, XAUUSD) are still ~$1/pt.

Run on the VPS (where MetaTrader5 terminal is running):

    cd C:\\PropBot
    python Scripts\\probe_broker_pip_values.py

Expected output on a USD-denominated 5ers account, today (EURUSD ≈ 1.166):

    ==============================================================================
      BROKER PIP-VALUE TABLE   source=broker_live
    ==============================================================================
        DE40    $/pt/lot =   1.1664   ✓ broker-truth (was $1.00, +16.6% via FX)
        US30    $/pt/lot =   1.0000   (USD-quoted, no FX needed)
        US500   $/pt/lot =   1.0000   (USD-quoted, no FX needed)
        XAUUSD  $/pt/lot =   1.0000   (USD-quoted, no FX needed)
    ==============================================================================

Anything saying "fallback_*" means MetaTrader5 isn't reachable; fix that
BEFORE running live (see Docs/V30_BROKER_PIP_VALUE_FIX.md).
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Make `src` importable when run from project root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.live.broker_specs import fetch_live_pip_values, log_pip_values_banner
from src.live.v30_live import V30_BROKER_NAMES


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
    )
    pip_values, source = fetch_live_pip_values(V30_BROKER_NAMES)
    log_pip_values_banner(pip_values, source)

    if source.startswith("fallback"):
        print("EXIT 1 — broker fetch failed; the engine will run with hardcoded "
              "$1/pt and DAX40 will be under-sized by ~14%.")
        return 1
    print("EXIT 0 — all values came from the live MT5 terminal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
