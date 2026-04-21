"""
SmartBB v15 — LIVE LAUNCHER
===========================

Single-command launcher for v15 live on 5%ers MTB.

Defaults are deliberately SAFE:
  - Tier 1 symbols only (the five symbols backtested profitable)
  - Per-symbol params from v15_ultimate_tuning.json
  - 8 % account-level kill-switch (2 % safety margin under 5%ers 10 %)
  - DRY-RUN mode ON by default (no orders placed, just logs decisions)

Usage
-----

    # Phase A — prove the bridge + signal generation, no real orders
    python Scripts\\run_v15_live.py

    # Phase B — go live at HALF normal risk (0.25 % per trade)
    python Scripts\\run_v15_live.py --live --risk-scale 0.5

    # Phase C — full-size live (after 30 good trades)
    python Scripts\\run_v15_live.py --live --risk-scale 1.0
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# Ensure project root on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.execution.mt5_bridge import MT5Bridge
from src.live.v15_live import V15Live, load_v15_params
from src.smartbb_engine_v14 import SmartBBV14Config

# ---------------------------------------------------------------------
# 5%ers broker symbol map (internal → broker)
# Verified 2026-04-21 from MT5 Market Watch on account #26059964
# ---------------------------------------------------------------------
FIVEERS_SYMBOL_MAP: dict[str, str] = {
    "US30":    "US30",
    "US100":   "NAS100",
    "US500":   "SP500",
    "DE40":    "DAX40",
    "XAUUSD":  "XAUUSD",
}

TIER1_SYMBOLS = list(FIVEERS_SYMBOL_MAP.keys())


def setup_logging(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / "v15_live.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        handlers=[
            logging.FileHandler(logfile, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main():
    p = argparse.ArgumentParser(description="SmartBB v15 live launcher")
    p.add_argument("--host", default="127.0.0.1",
                   help="MT5 bridge host (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=9090,
                   help="MT5 bridge port (default 9090)")
    p.add_argument("--tuning",
                   default="Results/v15_ultimate_tuning.json",
                   help="Path to v15 per-symbol tuning JSON")
    p.add_argument("--live", action="store_true",
                   help="Turn OFF dry-run and actually place orders")
    p.add_argument("--risk-scale", type=float, default=0.5,
                   help="Multiplies base_risk_pct (0.5 = half size for Phase B)")
    p.add_argument("--account-kill", type=float, default=0.08,
                   help="Hard kill-switch at this account-level DD (default 0.08)")
    p.add_argument("--magic", type=int, default=15000, help="Order magic #")
    p.add_argument("--symbols", default=",".join(TIER1_SYMBOLS),
                   help="Comma list of INTERNAL symbols to trade")
    p.add_argument("--log-dir", default="Results", help="Log + trade log directory")
    args = p.parse_args()

    setup_logging(Path(args.log_dir))
    log = logging.getLogger("smartbb.v15.launch")

    # ---- 1. Connect to MT5 bridge
    log.info(f"Connecting to MT5 bridge {args.host}:{args.port} …")
    bridge = MT5Bridge(host=args.host, port=args.port)
    if not bridge.connect():
        log.error("❌  Could not connect to MT5 bridge. Is SHF_Bridge EA running?")
        return 2

    # Sanity: print account
    acct = bridge.get_account_info()
    log.info(
        f"✅  Connected. login={acct.login} balance=${acct.balance:,.2f} "
        f"equity=${acct.equity:,.2f} server={acct.server}"
    )

    # ---- 2. Verify broker knows our symbols
    avail = set(bridge.get_available_symbols())
    missing = [b for b in FIVEERS_SYMBOL_MAP.values() if b not in avail]
    if missing:
        log.error(f"❌  Broker is missing these symbols: {missing}")
        log.error("    Open MT5 → Market Watch → right-click → Symbols → add them.")
        return 3
    log.info(f"✅  All {len(FIVEERS_SYMBOL_MAP)} broker symbols visible in MT5.")

    # ---- 3. Load per-symbol v15 params
    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    params = load_v15_params(Path(args.tuning), tier="TIER1", symbols=syms)
    if not params:
        log.error(f"❌  No TIER1 params found in {args.tuning}")
        return 4
    log.info(f"✅  Loaded per-symbol params: {list(params.keys())}")
    for sym, p in params.items():
        log.info(
            f"    {sym}: z_q={p.z_quantile:.2f} [{p.z_min_abs:.1f},{p.z_max_abs:.1f}] "
            f"stop={p.stop_atr_mult:.2f} tp={p.tp_frac:.2f} "
            f"hurst_q={p.hurst_quantile:.2f}<{p.hurst_max_abs:.2f} "
            f"ou_hl<{p.ou_max_halflife:.0f} risk_x={p.risk_multiplier:.2f}"
        )

    # ---- 4. Build engine cfg with risk scaling
    cfg = SmartBBV14Config()
    cfg.base_risk_pct = cfg.base_risk_pct * args.risk_scale
    cfg.min_risk_pct  = cfg.min_risk_pct  * args.risk_scale
    cfg.max_risk_pct  = cfg.max_risk_pct  * args.risk_scale
    log.info(
        f"Risk envelope (scaled by {args.risk_scale:.2f}): "
        f"base={cfg.base_risk_pct*100:.3f}% min={cfg.min_risk_pct*100:.3f}% "
        f"max={cfg.max_risk_pct*100:.3f}%"
    )

    # ---- 5. Build & run
    runner = V15Live(
        bridge=bridge,
        internal_symbols=syms,
        symbol_map={k: v for k, v in FIVEERS_SYMBOL_MAP.items() if k in syms},
        per_symbol_params=params,
        cfg=cfg,
        magic=args.magic,
        comment=f"SHF_v15_x{args.risk_scale:.1f}",
        dry_run=not args.live,
        max_account_dd_pct=args.account_kill,
        trade_log_path=Path(args.log_dir) / "v15_live_trades.jsonl",
    )

    mode = "🔴 LIVE" if args.live else "🟡 DRY-RUN"
    log.info(f"{mode}  starting v15 runner (Ctrl-C to stop)")
    try:
        runner.run()
    except KeyboardInterrupt:
        log.warning("KeyboardInterrupt — shutting down")
    finally:
        runner.stop()
        bridge.disconnect()
        # Final summary
        s = runner.engine.summary()
        log.info("=" * 78)
        log.info(f"FINAL  trades={s['trades']}  pnl=${s['net_pnl']:,.2f}  "
                 f"wr={s.get('win_rate',0)*100:.1f}%  "
                 f"pf={s.get('pf',0):.2f}  max_dd={s.get('max_dd_pct',0):.2f}%")
        log.info("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
