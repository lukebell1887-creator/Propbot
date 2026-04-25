#!/usr/bin/env python3
"""
V30 LIVE LAUNCHER -- 4-pair ORB + Merton-GZ sizer + news rails + v25.1 ship config.

Defaults to DRY-RUN. Pass --live to place real orders.

    python Scripts\\run_v30_live.py                   # DRY-RUN (no orders)
    python Scripts\\run_v30_live.py --live            # LIVE

What's new in v30 vs v23 live:
    * base_risk_pct       0.00170   (★ was 0.00110 in v23)
    * NOCHASE_COOLDOWN_S  300.0     (★ NEW — cross-symbol queue-release filter)
    * SLIPPAGE TRACKER    every entry's intended_px vs fill_px in TICKS,
                          rolled up per-symbol + portfolio in heartbeat,
                          one JSON line per trade in
                          Results/v30_live_slippage.jsonl
    * magic               30000     (was 23000 — distinguishes v30 tickets)
    * comment             "SHF_v30" (was "SHF_v23")

Per Docs/V25_1_SHIP_RECOMMENDATION.md, the offline backtest projects
+62.9 % net P&L vs v23 with DD slightly improved (3.35 % → 3.16 %).
You will dry-run this on Monday before flipping --live.

If your broker uses DIFFERENT symbol names than the defaults, override:

    python Scripts\\run_v30_live.py ^
        --broker-names "DE40=DAX40,US30=US30,US500=SP500,XAUUSD=XAUUSD"

(Use Scripts\\probe_broker_symbols.py to auto-discover.)
"""
from __future__ import annotations
import argparse, logging, signal, sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from src.execution.mt5_bridge import MT5Bridge
from src.live.v30_live import (
    V30Live, V30LiveConfig,
    V30_SPECS, V30_BROKER_NAMES, SymbolSpec,
    V30_BROKER_TICK_SIZE, V30_BROKER_LOT_STEP, V30_BROKER_MIN_LOT,
)
from src.smartbb_engine import SMARTBB_UNIVERSE


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / f"v30_live_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
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


def parse_broker_names(arg: str) -> Dict[str, str]:
    """Parse '--broker-names' value into {INTERNAL: BROKER} dict."""
    out: Dict[str, str] = {}
    if not arg:
        return out
    for pair in arg.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise SystemExit(f"--broker-names: bad token '{pair}' (need INTERNAL=BROKER)")
        k, v = pair.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def build_specs(broker_names: Dict[str, str]) -> Dict[str, SymbolSpec]:
    """Rebuild SymbolSpec dict using the user-supplied broker-names override."""
    specs: Dict[str, SymbolSpec] = {}
    for sym, default_broker in V30_BROKER_NAMES.items():
        broker = broker_names.get(sym, default_broker)
        uni = SMARTBB_UNIVERSE.get(sym)
        if uni is None:
            raise SystemExit(f"internal symbol {sym} missing from SMARTBB_UNIVERSE")
        specs[sym] = SymbolSpec(
            internal=sym, broker=broker,
            tick_size=V30_BROKER_TICK_SIZE[sym],
            pip_value_per_lot=float(uni.pip_value),
            min_lot=V30_BROKER_MIN_LOT[sym],
            lot_step=V30_BROKER_LOT_STEP[sym],
        )
    return specs


def preflight_symbols(bridge: MT5Bridge, specs: Dict[str, SymbolSpec]) -> None:
    """
    Before warmup, probe each broker symbol with a tiny get_history(count=5).
    If ANY symbol returns 0 bars, abort with a clear actionable message.
    """
    print()
    print("PRE-FLIGHT: probing broker symbol mapping ...")
    bad: list = []
    for sym, spec in specs.items():
        try:
            bars = bridge.get_history(spec.broker, count=5)
        except Exception as e:
            bars = []
            print(f"  {sym:<7} -> {spec.broker:<15}   ERROR: {e}")
            bad.append((sym, spec.broker, str(e)))
            continue
        n = len(bars or [])
        status = "OK" if n > 0 else "NO BARS"
        print(f"  {sym:<7} -> {spec.broker:<15}   {status}  ({n} bars)")
        if n == 0:
            bad.append((sym, spec.broker, "CopyRates returned 0 bars"))

    if bad:
        print()
        print("=" * 72)
        print(" PRE-FLIGHT FAILED -- one or more symbols returned 0 bars.")
        print(" The broker does not expose them under those names, or the")
        print(" symbols are not in MT5 Market Watch on the VPS.")
        print("=" * 72)
        print(" FIX 1 (fastest):")
        print("   python Scripts\\probe_broker_symbols.py")
        print("   -> it will auto-discover the correct names and print the")
        print("      --broker-names flag to use.")
        print()
        print(" FIX 2: open MT5 on the VPS, View -> Market Watch -> right-click")
        print("   -> 'Show All', then restart the EA. Make sure the index")
        print("   symbols are visible in the list before re-running.")
        print()
        print(" Broken:")
        for sym, broker, err in bad:
            print(f"   {sym:<7} -> {broker:<15}  {err}")
        print("=" * 72)
        raise SystemExit(3)
    print("  -> all symbols OK\n")


def main() -> int:
    p = argparse.ArgumentParser(description="v30 live / dry-run launcher (v25.1 ship config)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5555)
    p.add_argument("--live", action="store_true", help="place real orders (default = DRY-RUN)")
    p.add_argument("--symbols", default="DE40,US30,XAUUSD,US500")
    p.add_argument("--broker-names", default="",
                   help='override broker symbol names, e.g. '
                        '"DE40=DAX40,US30=US30,US500=SP500,XAUUSD=XAUUSD"')
    # v30 SHIP DEFAULTS  ★
    p.add_argument("--risk", type=float, default=0.00170,
                   help="base risk pct (v30 ship: 0.00170 = 0.170 pct)")
    p.add_argument("--cap-mult", type=float, default=5.0)
    p.add_argument("--nochase-cooldown", type=float, default=300.0,
                   help="cross-symbol no-chase cooldown in seconds (0 to disable; "
                        "v30 ship: 300.0)")
    p.add_argument("--account-kill", type=float, default=0.08)
    p.add_argument("--daily-breaker", type=float, default=0.02)
    p.add_argument("--magic", type=int, default=30000)
    p.add_argument("--news-csv", default="data/news/tier1_2026.csv")
    p.add_argument("--log-dir", default="Results")
    p.add_argument("--heartbeat", type=float, default=60.0)
    p.add_argument("--skip-preflight", action="store_true",
                   help="skip the broker-symbol probe (not recommended)")
    args = p.parse_args()

    setup_logging(Path(args.log_dir))
    log = logging.getLogger("v30.launch")

    banner(f"V30 LIVE LAUNCHER   {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"  mode          : {'LIVE (real orders)' if args.live else 'DRY-RUN (no orders)'}")
    print(f"  symbols       : {args.symbols}")
    print(f"  base risk     : {args.risk*100:.3f}%   (cap = {args.cap_mult:.1f}x = {args.risk*args.cap_mult*100:.3f}%)   ★ v25.1 ship")
    print(f"  no-chase cd   : {args.nochase_cooldown:.0f} s   ★ v25.1 ship (cross-symbol)")
    print(f"  account kill  : {args.account_kill*100:.1f}% rolling DD -> close-all + halt")
    print(f"  daily breaker : {args.daily_breaker*100:.1f}% intraday DD -> halt new entries")
    print(f"  news CSV      : {args.news_csv}")
    print(f"  magic number  : {args.magic}")
    print(f"  bridge        : {args.host}:{args.port}")
    print(f"  log dir       : {args.log_dir}")
    print(f"  slippage log  : {args.log_dir}/v30_live_slippage.jsonl  (one JSON line per trade)")

    # Build specs with optional broker-name overrides
    broker_names = parse_broker_names(args.broker_names)
    specs = build_specs(broker_names)
    if broker_names:
        print(f"  broker names  : (override) " +
              ", ".join(f"{k}->{v.broker}" for k, v in specs.items()))
    else:
        print(f"  broker names  : (default) " +
              ", ".join(f"{k}->{v.broker}" for k, v in specs.items()))

    bridge = MT5Bridge(host=args.host, req_port=args.port)
    if not bridge.connect():
        log.error("MT5 bridge failed to connect -- is SHF_Bridge EA attached to an MT5 chart?")
        return 2

    # PRE-FLIGHT: verify every broker symbol returns bars before we start.
    if not args.skip_preflight:
        preflight_symbols(bridge, specs)

    cfg = V30LiveConfig(
        symbols=[s.strip() for s in args.symbols.split(",") if s.strip()],
        base_risk_pct=args.risk,
        cap_mult=args.cap_mult,
        nochase_cooldown_s=args.nochase_cooldown,
        account_kill_dd=args.account_kill,
        daily_breaker_dd=args.daily_breaker,
        magic=args.magic,
        news_csv=args.news_csv,
        heartbeat_sec=args.heartbeat,
        log_dir=args.log_dir,
    )

    runner = V30Live(bridge=bridge, cfg=cfg, dry_run=not args.live, specs=specs)

    banner("STARTING")
    rc = runner.start()
    if rc != 0:
        return rc

    def _handler(signum, frame):
        log.warning("Signal %s received -- stopping cleanly ...", signum)
        runner.stop()
    signal.signal(signal.SIGINT, _handler)
    try:
        signal.signal(signal.SIGTERM, _handler)
    except Exception:
        pass

    banner("MAIN LOOP")
    runner.run()

    banner("STOPPED -- bye.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
