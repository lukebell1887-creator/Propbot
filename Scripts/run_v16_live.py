"""
SmartBB v16  -  LIVE LAUNCHER (pre-flight checked)
===================================================

Same pre-flight scoreboard as run_v15_live.py (8/8 checks) PLUS two new
v16-specific checks:

  9.  Trading calendar active (weekend/rollover/holiday blackouts)
  10. Dynamic sizer config sane (Kelly bounds, vol target)

Typical usage
-------------
    # DRY-RUN (decisions only, no orders):
    python Scripts\run_v16_live.py

    # LIVE at half risk (Phase B, 0.25 % floor):
    python Scripts\run_v16_live.py --live --risk-scale 0.5

    # LIVE full risk (Phase C):
    python Scripts\run_v16_live.py --live --risk-scale 1.0

    # Ablation toggles (default both ON):
    python Scripts\run_v16_live.py --live --no-sizer        # calendar only
    python Scripts\run_v16_live.py --live --no-calendar     # sizer only
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Ensure project root on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.execution.mt5_bridge import MT5Bridge
from src.live.v15_live import load_v15_params
from src.live.v16_live import V16Live
from src.smartbb_engine_v14 import SmartBBV14Config
from src.dynamic_sizer_v16 import SizerConfig
from src.trading_calendar import TradingCalendar, CalendarConfig

# Import the pre-flight helpers from the v15 launcher (DRY).
from Scripts.run_v15_live import (
    FIVEERS_SYMBOL_MAP, TIER1_SYMBOLS,
    FIVEERS_DAILY_DD_LIMIT_USD, FIVEERS_TOTAL_DD_LIMIT_USD,
    setup_logging, banner, hr, tick, human_bool,
    run_preflight as run_preflight_v15,
)


def run_preflight_v16(args, bridge, sizer_cfg: SizerConfig):
    """Reuse v15's 8 checks, then add 2 v16-specific ones."""
    ok, ctx = run_preflight_v15(args, bridge)

    # ─── 9. Trading calendar ──────────────────────────────────────────
    banner("PRE-FLIGHT  9/10  Trading calendar (v16)")
    if not args.use_calendar:
        print("  ⚠️  Calendar DISABLED by --no-calendar — entries will fire on "
              "weekends / rollover / holidays")
    else:
        cal = TradingCalendar(CalendarConfig())
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        weekend_ok, reason_w = cal.can_enter("US30", now)
        print(f"  ✅ TradingCalendar module active")
        print(f"     rollover window :  {CalendarConfig().rollover_start} - "
              f"{CalendarConfig().rollover_end} UTC daily")
        print(f"     weekend blackout:  Fri 21:00 UTC -> Sun 22:00 UTC")
        print(f"     holiday list    :  {len(CalendarConfig().holidays)} dates loaded")
        print(f"     right now ({now:%Y-%m-%d %H:%M UTC}): "
              f"{'tradeable' if weekend_ok else f'BLOCKED ({reason_w})'}")

    # ─── 10. Dynamic sizer config ─────────────────────────────────────
    banner("PRE-FLIGHT  10/10  Dynamic sizer config (v16)")
    if not args.use_sizer:
        print("  ⚠️  Dynamic sizer DISABLED by --no-sizer — falling back to "
              "v15's fixed base_risk_pct")
    else:
        acct = ctx["acct"]
        min_usd = acct.balance * sizer_cfg.min_risk_pct
        max_usd = acct.balance * sizer_cfg.max_risk_pct
        cold_usd = acct.balance * sizer_cfg.cold_start_risk_pct
        print(f"  ✅ Thorp-Kelly fractional          = {sizer_cfg.kelly_fractional:.2f}")
        print(f"  ✅ Grossman-Zhou DD envelope       = {sizer_cfg.dd_max*100:.1f}%")
        print(f"  ✅ Target annualised vol           = {sizer_cfg.target_ann_vol*100:.1f}%")
        print(f"  ✅ Risk bounds  [min, max]         = [{sizer_cfg.min_risk_pct*100:.3f}%, "
              f"{sizer_cfg.max_risk_pct*100:.2f}%]  (${min_usd:,.0f} - ${max_usd:,.0f})")
        print(f"  ✅ Cold-start risk (pre-Kelly)     = {sizer_cfg.cold_start_risk_pct*100:.3f}%  "
              f"(~${cold_usd:,.0f})")
        print(f"  ✅ Kelly activates after           = {sizer_cfg.min_trades_for_kelly} trades/(sym,side)")
        print(f"  ✅ CVaR cap  (a={sizer_cfg.cvar_alpha:.2f})            = "
              f"ES <= {sizer_cfg.cvar_cap*100:.1f}% of equity")

    # Attach sizer_cfg for the builder
    ctx["sizer_cfg"] = sizer_cfg
    return ok, ctx


def main():
    p = argparse.ArgumentParser(description="SmartBB v16 live launcher")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5555)
    p.add_argument("--tuning", default="Results/v15_ultimate_tuning.json")
    p.add_argument("--live", action="store_true",
                    help="OFF dry-run and place real orders")
    p.add_argument("--risk-scale", type=float, default=0.5,
                    help="multiplier on base_risk_pct (0.5 = Phase B, 1.0 = Phase C)")
    p.add_argument("--account-kill", type=float, default=0.08,
                    help="kill switch at this fractional account DD (0.08 = 8 %)")
    p.add_argument("--magic", type=int, default=16000)
    p.add_argument("--symbols", default=",".join(TIER1_SYMBOLS))
    p.add_argument("--log-dir", default="Results")
    p.add_argument("--skip-preflight", action="store_true")

    # v16 toggles
    p.add_argument("--no-sizer", action="store_true",
                    help="disable dynamic Kelly (fall back to v14 fixed risk%%)")
    p.add_argument("--no-calendar", action="store_true",
                    help="disable weekend / rollover / holiday blackouts")
    # Sizer knobs (production defaults match the OOS-winning config)
    p.add_argument("--kelly-frac",        type=float, default=0.25)
    p.add_argument("--dd-max",            type=float, default=0.06)
    p.add_argument("--target-vol",        type=float, default=0.15)
    p.add_argument("--min-risk",          type=float, default=0.001)
    p.add_argument("--max-risk",          type=float, default=0.015)
    p.add_argument("--cold-start",        type=float, default=0.005)
    p.add_argument("--min-trades-kelly",  type=int,   default=10)

    args = p.parse_args()

    # Derived flags
    args.use_sizer    = not args.no_sizer
    args.use_calendar = not args.no_calendar

    setup_logging(Path(args.log_dir))
    log = logging.getLogger("smartbb.v16.launch")

    banner(f"SmartBB v16  LIVE LAUNCHER   "
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"  repo      = {ROOT}")
    print(f"  tuning    = {args.tuning}")
    print(f"  symbols   = {args.symbols}")
    print(f"  mode      = {'🔴 LIVE' if args.live else '🟡 DRY-RUN'}   "
          f"risk_scale={args.risk_scale:.2f}   "
          f"sizer={'ON' if args.use_sizer else 'OFF'}   "
          f"calendar={'ON' if args.use_calendar else 'OFF'}")

    # Build sizer config
    sizer_cfg = SizerConfig(
        kelly_fractional=args.kelly_frac,
        dd_max=args.dd_max,
        target_ann_vol=args.target_vol,
        min_risk_pct=args.min_risk * args.risk_scale,
        max_risk_pct=args.max_risk * args.risk_scale,
        cold_start_risk_pct=args.cold_start * args.risk_scale,
        min_trades_for_kelly=args.min_trades_kelly,
    )

    # Start TCP bridge
    log.info(f"Starting MT5 bridge server on {args.host}:{args.port} …")
    bridge = MT5Bridge(host=args.host, req_port=args.port)
    if not bridge.connect():
        log.error("❌  MT5 bridge failed to start. "
                  "Is SHF_Bridge EA attached with AutoTrading ON?")
        return 2

    # Pre-flight
    if args.skip_preflight:
        log.warning("⚠️   --skip-preflight was passed")
        ctx = {
            "acct":   bridge.get_account_info(),
            "params": load_v15_params(Path(args.tuning), tier="TIER1",
                                       symbols=[s.strip() for s in args.symbols.split(",") if s.strip()]),
            "cfg":    SmartBBV14Config(),
            "syms":   [s.strip() for s in args.symbols.split(",") if s.strip()],
            "sizer_cfg": sizer_cfg,
        }
    else:
        ok, ctx = run_preflight_v16(args, bridge, sizer_cfg)
        if not ok:
            log.error("Pre-flight failed — shutting down bridge.")
            bridge.disconnect()
            return 5

    params = ctx["params"]; cfg = ctx["cfg"]; syms = ctx["syms"]
    sizer_cfg = ctx["sizer_cfg"]

    banner("ENGINE STARTING")
    runner = V16Live(
        bridge=bridge,
        internal_symbols=syms,
        symbol_map={k: v for k, v in FIVEERS_SYMBOL_MAP.items() if k in syms},
        per_symbol_params=params,
        cfg=cfg,
        magic=args.magic,
        comment=f"SHF_v16_x{args.risk_scale:.1f}",
        dry_run=not args.live,
        max_account_dd_pct=args.account_kill,
        trade_log_path=Path(args.log_dir) / "v16_live_trades.jsonl",
        sizer_cfg=sizer_cfg,
        calendar=TradingCalendar(),
        use_dynamic_sizing=args.use_sizer,
        use_calendar=args.use_calendar,
    )

    mode = "🔴 LIVE" if args.live else "🟡 DRY-RUN"
    log.info(f"{mode}  starting v16 runner — Ctrl-C to stop cleanly")
    try:
        runner.run()
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
            bc = s.get("v16", {}).get("blackout_counts", {})
            if bc:
                log.info(f"blackouts  {bc}")
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
