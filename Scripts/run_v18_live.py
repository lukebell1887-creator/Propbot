"""
SmartBB v18  —  LIVE LAUNCHER  (Grossman-Zhou dynamic Kelly)

No flags.  No knobs.  The single blessed v18 config.

    python Scripts\\run_v18_live.py            # DRY-RUN (no orders)
    python Scripts\\run_v18_live.py --live     # LIVE trading

Kelly warm-up automatically seeds per-(symbol, side) R history from
Results/v17_final_100000_3m_trades.json, so Grossman-Zhou is active
from bar 1.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.execution.mt5_bridge import MT5Bridge
from src.live.v15_live import load_v15_params
from src.live.v18_live import V18Live, warmup_sizer_v18
from src.live.warmup import warmup_engine_from_broker
from src.smartbb_engine_v14 import SmartBBV14Config
from src.dynamic_sizer_v18 import SizerV18Config
from src.trading_calendar import TradingCalendar

from Scripts.run_v15_live import (
    FIVEERS_SYMBOL_MAP, TIER1_SYMBOLS, setup_logging, banner,
)

# v18 default universe — XAUUSD REMOVED (negative bucket, high carry risk,
# thin sample: only 5 trades in 186-trade OOS, and the ones it had were
# either losses or marginal wins).  If you really want gold back in,
# override with:  python Scripts\run_v18_live.py --symbols DE40,US30,US100,US500,XAUUSD
V18_DEFAULT_SYMBOLS = ["DE40", "US30", "US100", "US500"]


def main():
    p = argparse.ArgumentParser(description="SmartBB v18 live launcher")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5555)
    p.add_argument("--tuning", default="Results/v15_ultimate_tuning.json")
    p.add_argument("--live", action="store_true")
    p.add_argument("--account-kill", type=float, default=0.08)
    p.add_argument("--magic",        type=int,   default=18000)
    p.add_argument("--symbols",      default=",".join(V18_DEFAULT_SYMBOLS))
    p.add_argument("--log-dir",      default="Results")
    p.add_argument("--warmup-bars",  type=int,   default=5000)
    p.add_argument("--warmup-sizer-from", type=str,
                    default="Results/v17_final_100000_3m_trades.json")
    p.add_argument("--heartbeat-sec", type=float, default=60.0)
    args = p.parse_args()

    setup_logging(Path(args.log_dir))
    log = logging.getLogger("smartbb.v18.launch")

    banner(f"SmartBB v18  LIVE LAUNCHER   "
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"  mode      = {'🔴 LIVE' if args.live else '🟡 DRY-RUN'}")
    print(f"  sizer     = Grossman-Zhou × Bayesian × conviction × safety-only")
    print(f"  hard cap  = 2.00 % per trade")

    bridge = MT5Bridge(host=args.host, req_port=args.port)
    if not bridge.connect():
        log.error("❌ MT5 bridge failed — is SHF_Bridge EA attached?")
        return 2

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    params = load_v15_params(Path(args.tuning), tier="TIER1", symbols=syms)

    sizer_cfg = SizerV18Config(
        alpha_cap             = 0.10,
        kelly_fractional      = 0.50,
        min_trades_for_bucket = 20,
        min_risk_pct          = 0.0020,
        max_risk_pct          = 0.0200,
        cold_start_risk_pct   = 0.0050,
        daily_cap_usd         = 4_000.0,
        total_cap_usd         = 10_000.0,
        daily_safety_frac     = 0.75,
        total_safety_frac     = 0.70,
        kill_losing_buckets   = True,
    )

    banner("ENGINE STARTING")
    runner = V18Live(
        bridge=bridge,
        internal_symbols=syms,
        symbol_map={k: v for k, v in FIVEERS_SYMBOL_MAP.items() if k in syms},
        per_symbol_params=params,
        cfg=SmartBBV14Config(),
        magic=args.magic,
        comment="SHF_v18",
        dry_run=not args.live,
        max_account_dd_pct=args.account_kill,
        trade_log_path=Path(args.log_dir) / "v18_live_trades.jsonl",
        sizer_cfg=sizer_cfg,
        calendar=TradingCalendar(),
        use_calendar=True,
        telemetry_path=Path(args.log_dir) / "v18_live_telemetry.json",
    )

    # WARM-UP A: Kelly history
    if args.warmup_sizer_from:
        banner("WARM-UP A/B  Kelly trade history")
        hist = Path(args.warmup_sizer_from)
        if hist.exists():
            n = warmup_sizer_v18(runner.sizer, hist)
            print(f"  ✅ loaded {n} historical R-values into sizer from {hist.name}")
        else:
            print(f"  ⚠️ {hist} not found — will start cold and back-fill from live")

    # WARM-UP B: indicator history
    if args.warmup_bars > 0:
        banner(f"WARM-UP B/B  Engine indicators ({args.warmup_bars} M1 bars/symbol)")
        sym_map = {k: v for k, v in FIVEERS_SYMBOL_MAP.items() if k in syms}
        streamed = warmup_engine_from_broker(
            engine=runner.engine, bridge=bridge,
            internal_to_broker=sym_map, bars_per_symbol=args.warmup_bars,
        )
        for s, n in streamed.items():
            print(f"     {s:<7} {n:>6,} bars")

    mode = "🔴 LIVE" if args.live else "🟡 DRY-RUN"
    log.info(f"{mode}  starting v18 runner — Ctrl-C to stop cleanly")
    try:
        runner.run(heartbeat_sec=args.heartbeat_sec)
    except KeyboardInterrupt:
        log.warning("KeyboardInterrupt — shutting down gracefully")
    finally:
        runner.stop()
        bridge.disconnect()
        try:
            s = runner.engine.summary()
            banner("FINAL SESSION SUMMARY")
            log.info(f"trades={s['trades']}  pnl=${s['net_pnl']:,.2f}  "
                      f"wr={s.get('win_rate',0)*100:.1f}%  "
                      f"pf={s.get('pf',0):.2f}  max_dd={s.get('max_dd_pct',0):.2f}%")
            bc = s.get("v18", {}).get("blackout_counts", {})
            if bc:
                log.info(f"blackouts  {bc}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
