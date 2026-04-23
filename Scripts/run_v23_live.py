#!/usr/bin/env python3
"""
V23 LIVE LAUNCHER — 4-pair ORB + Merton-GZ sizer + news rails.

Defaults to DRY-RUN. Pass --live to place real orders.

    python Scripts\run_v23_live.py                    # DRY-RUN (no orders)
    python Scripts\run_v23_live.py --live             # LIVE
"""
from __future__ import annotations
import argparse, logging, signal, sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from src.execution.mt5_bridge import MT5Bridge
from src.live.v23_live import V23Live, V23LiveConfig


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / f"v23_live_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
    fmt = "%(asctime)s  %(levelname)-5s  %(name)s  %(message)s"
    logging.basicConfig(
        level=logging.INFO, format=fmt,
        handlers=[
            logging.FileHandler(logfile, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def banner(title: str) -> None:
    print("=" * 100)
    print(f"  {title}")
    print("=" * 100)


def main() -> int:
    p = argparse.ArgumentParser(description="v23 live / dry-run launcher")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5555)
    p.add_argument("--live", action="store_true", help="place real orders (default = DRY-RUN)")
    p.add_argument("--symbols", default="DE40,US30,XAUUSD,US500")
    p.add_argument("--risk", type=float, default=0.00110, help="base risk %% (0.00110 = 0.110%%)")
    p.add_argument("--cap-mult", type=float, default=3.0)
    p.add_argument("--account-kill", type=float, default=0.08)
    p.add_argument("--daily-breaker", type=float, default=0.02)
    p.add_argument("--magic", type=int, default=23000)
    p.add_argument("--news-csv", default="data/news/tier1_2026.csv")
    p.add_argument("--log-dir", default="Results")
    p.add_argument("--heartbeat", type=float, default=60.0)
    args = p.parse_args()

    setup_logging(Path(args.log_dir))
    log = logging.getLogger("v23.launch")

    banner(f"V23 LIVE LAUNCHER   {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"  mode          : {'🔴 LIVE (real orders)' if args.live else '🟡 DRY-RUN (no orders)'}")
    print(f"  symbols       : {args.symbols}")
    print(f"  base risk     : {args.risk*100:.3f}%   (cap = {args.cap_mult:.1f}× = {args.risk*args.cap_mult*100:.3f}%)")
    print(f"  account kill  : {args.account_kill*100:.1f}% rolling DD → close-all + halt")
    print(f"  daily breaker : {args.daily_breaker*100:.1f}% intraday DD → halt new entries")
    print(f"  news CSV      : {args.news_csv}")
    print(f"  magic number  : {args.magic}")
    print(f"  bridge        : {args.host}:{args.port}")
    print(f"  log dir       : {args.log_dir}")

    bridge = MT5Bridge(host=args.host, req_port=args.port)
    if not bridge.connect():
        log.error("❌ MT5 bridge failed to connect — is SHF_Bridge EA attached to an MT5 chart?")
        return 2

    cfg = V23LiveConfig(
        symbols=[s.strip() for s in args.symbols.split(",") if s.strip()],
        base_risk_pct=args.risk,
        cap_mult=args.cap_mult,
        account_kill_dd=args.account_kill,
        daily_breaker_dd=args.daily_breaker,
        magic=args.magic,
        news_csv=args.news_csv,
        heartbeat_sec=args.heartbeat,
        log_dir=args.log_dir,
    )

    runner = V23Live(bridge=bridge, cfg=cfg, dry_run=not args.live)

    banner("STARTING")
    rc = runner.start()
    if rc != 0:
        return rc

    def _handler(signum, frame):
        log.warning("Signal %s received — stopping cleanly ...", signum)
        runner.stop()
    signal.signal(signal.SIGINT, _handler)
    try:
        signal.signal(signal.SIGTERM, _handler)
    except Exception:
        pass

    banner("MAIN LOOP")
    runner.run()

    banner("STOPPED — bye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
