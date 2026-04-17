#!/usr/bin/env python3
"""
SHF v13 SMART BOLLINGER — Live runner entry point

Usage
-----
    python Scripts/run_live_smartbb.py                    # Production
    python Scripts/run_live_smartbb.py --dry-run          # Paper (no orders)
    python Scripts/run_live_smartbb.py --risk 0.003       # Conservative risk
    python Scripts/run_live_smartbb.py --z-min 3.3        # Only high-quality setups
    python Scripts/run_live_smartbb.py \\
        --symbols US100 US500 US30 DE40                   # Drop USOIL

Required before running
-----------------------
1. MT5 terminal running with `SHF_Bridge.mq5` attached to any chart.
2. EA input `InpHost = 127.0.0.1`, `InpPort = 5555` (or the VPS's IP).
3. EA "AutoTrading" button green.
4. Each symbol in the --symbols list must be in MT5 Market Watch.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.execution.mt5_bridge import MT5Bridge            # noqa: E402
from src.live.smartbb_live import build_default           # noqa: E402


def setup_logging(level: str, logfile: Path):
    logfile.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)-5s %(name)-18s %(message)s"
    handlers = [
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(logfile, encoding="utf-8"),
    ]
    logging.basicConfig(level=level.upper(), format=fmt, handlers=handlers)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0",
                     help="Bind host for Python TCP server (EA connects to this)")
    ap.add_argument("--port", type=int, default=5555)
    ap.add_argument("--symbols", nargs="+",
                     default=["US100", "US500", "US30", "DE40", "USOIL"],
                     help="Universe to trade (must be in MT5 Market Watch)")
    ap.add_argument("--risk", type=float, default=0.003,
                     help="Base risk %% per trade (default 0.003 = 0.3 %%, "
                          "conservative for go-live)")
    ap.add_argument("--z-min", type=float, default=3.0,
                     help="Minimum |Z| for entry (3.0 default, 3.3 for "
                          "high-quality only)")
    ap.add_argument("--z-max", type=float, default=4.5)
    ap.add_argument("--hurst-max", type=float, default=0.50)
    ap.add_argument("--magic", type=int, default=13000,
                     help="Magic number for v13 orders (so it can be "
                          "distinguished from manual trades)")
    ap.add_argument("--dry-run", action="store_true",
                     help="Run strategy but send NO orders (paper mode)")
    ap.add_argument("--log-level", default="INFO",
                     choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    ap.add_argument("--log-file",
                     default=str(ROOT / "Results" / "live_smartbb.log"))
    args = ap.parse_args()

    setup_logging(args.log_level, Path(args.log_file))
    log = logging.getLogger("run_live")

    log.info("=" * 72)
    log.info("SHF v13 SMART BOLLINGER — LIVE")
    log.info("=" * 72)
    log.info(f"Host: {args.host}:{args.port} | Symbols: {args.symbols}")
    log.info(f"Risk: {args.risk*100:.2f}%/trade | Z in [{args.z_min}, {args.z_max}] | "
              f"Hurst max: {args.hurst_max}")
    log.info(f"Magic: {args.magic} | Dry-run: {args.dry_run}")
    log.info("=" * 72)

    # 1. Connect to MT5 EA
    bridge = MT5Bridge(req_port=args.port, host=args.host)
    log.info("Starting TCP bridge — waiting for EA to connect...")
    if not bridge.connect():
        log.error("FATAL: Failed to establish bridge with MT5 EA.")
        log.error("  - Is MT5 running?")
        log.error("  - Is SHF_Bridge.mq5 attached to a chart?")
        log.error("  - Is AutoTrading enabled (green button)?")
        log.error(f"  - Is EA InpHost pointing at this machine ({args.host})?")
        log.error(f"  - Is EA InpPort set to {args.port}?")
        return 1

    log.info("Bridge connected. Quick sanity check...")
    acct = bridge.get_account_info()
    log.info(f"  Account: {acct.server} | Balance: ${acct.balance:,.2f} | "
              f"Equity: ${acct.equity:,.2f} | Leverage: 1:{acct.leverage}")

    if acct.balance < 1000:
        log.error(f"Account balance ${acct.balance} too low — aborting.")
        return 1

    # 2. Build the live strategy
    live = build_default(
        bridge=bridge,
        symbols=args.symbols,
        cfg_overrides={
            "base_risk_pct": args.risk,
            "min_z_entry": args.z_min,
            "max_z_entry": args.z_max,
            "hurst_max_for_trade": args.hurst_max,
        },
        dry_run=args.dry_run,
    )
    live.magic = args.magic

    # 3. Run
    try:
        live.run(heartbeat_sec=60.0)
    except KeyboardInterrupt:
        log.warning("Interrupted — closing all positions and shutting down")
    finally:
        if not args.dry_run:
            try:
                bridge.close_all_positions()
            except Exception as e:
                log.error(f"close_all failed: {e}")
        bridge.disconnect()
        log.info("Live runner stopped cleanly.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
