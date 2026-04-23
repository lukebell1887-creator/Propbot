#!/usr/bin/env python3
"""
PROBE_BROKER_SYMBOLS  --  discover the REAL names your broker uses for
DE40 / US30 / US500 / XAUUSD.

Why this exists:
    The live bot defaults to {DE40.cash, US30.cash, US500.cash, XAUUSD}
    -- those are 5ers "FivePercentOnline-Real" conventions. Other 5ers
    servers (or challenge accounts behind a white-label) can expose the
    same instruments under DAX40, GER40, DJ30, SPX500, etc. If the name
    is wrong, MT5 CopyRates returns 0 bars and the bot aborts at warmup.

What it does:
    For every internal symbol, tries a list of known alias candidates
    via the SAME bridge the live bot uses. The FIRST alias that returns
    > 0 M1 bars wins. Prints a ready-to-paste --broker-names flag.

Run it ONCE on the VPS, after the EA is attached:

    python Scripts\\probe_broker_symbols.py

Then copy the printed --broker-names line into your GO_LIVE_V23.ps1
(or pass it directly to run_v23_live.py).
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from src.execution.mt5_bridge import MT5Bridge


# Aliases for each internal symbol -- ordered "most common first". The
# preferred 5ers names are listed first so we converge on the default
# when possible.
CANDIDATES: Dict[str, List[str]] = {
    "DE40":   ["DE40.cash", "DE40", "DAX40", "GER40", "GER40.cash",
               "DE40.", "DAX40.cash", "DAX"],
    "US30":   ["US30.cash", "US30", "DJ30", "WS30", "DJIA",
               "US30.", "DJ30.cash"],
    "US500":  ["US500.cash", "US500", "SP500", "SPX500", "SPX",
               "US500.", "SPX500.cash"],
    "XAUUSD": ["XAUUSD", "XAUUSD.cash", "XAU/USD", "XAUUSD.",
               "GOLD", "XAUUSDm"],
}


def probe_one(bridge: MT5Bridge, internal: str,
              candidates: List[str]) -> Optional[str]:
    """Return first alias that returns > 0 bars, or None."""
    print(f"\n[{internal}]")
    for name in candidates:
        try:
            bars = bridge.get_history(name, count=5)
            n = len(bars or [])
        except Exception as e:
            print(f"    {name:<15}  ERR: {e}")
            continue
        status = "OK" if n > 0 else "no bars"
        print(f"    {name:<15}  {status}  ({n})")
        if n > 0:
            return name
    return None


def main() -> int:
    p = argparse.ArgumentParser(description="probe broker for actual symbol names")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5555)
    args = p.parse_args()

    print("=" * 72)
    print(" PROBE_BROKER_SYMBOLS  --  auto-discover broker symbol mapping")
    print("=" * 72)
    print(f"  bridge : {args.host}:{args.port}")

    bridge = MT5Bridge(host=args.host, req_port=args.port)
    if not bridge.connect():
        print("ERROR: MT5 bridge failed to connect -- is SHF_Bridge EA attached?")
        return 2

    # Warm up the bridge with a trivial call
    try:
        ai = bridge.get_account_info()
        if ai:
            print(f"  account : login={ai.login}  equity=${ai.equity:,.2f}  server={ai.server}")
    except Exception as e:
        print(f"  account : (could not fetch: {e})")

    resolved: Dict[str, str] = {}
    missing: List[str] = []
    for internal, cands in CANDIDATES.items():
        hit = probe_one(bridge, internal, cands)
        if hit:
            resolved[internal] = hit
        else:
            missing.append(internal)

    print()
    print("=" * 72)
    print(" RESULT")
    print("=" * 72)
    for internal, broker in resolved.items():
        marker = " " if broker == CANDIDATES[internal][0] else "*"  # * = non-default
        print(f"  {marker} {internal:<7} -> {broker}")
    if missing:
        print()
        print(" MISSING (no candidate worked):")
        for m in missing:
            print(f"   {m}  -- tried: {', '.join(CANDIDATES[m])}")
        print()
        print(" Open MT5 on the VPS, View -> Market Watch -> right-click")
        print(" 'Show All', then re-run this probe. If still missing, the")
        print(" broker does not offer that symbol on this account.")
        return 3

    # Build the --broker-names flag
    flag = ",".join(f"{k}={v}" for k, v in resolved.items())
    print()
    print(" Copy this into your launcher:")
    print()
    print(f'   --broker-names "{flag}"')
    print()
    print(" Or for run_v23_live.py directly:")
    print()
    print(f'   python Scripts\\run_v23_live.py --broker-names "{flag}"')
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
