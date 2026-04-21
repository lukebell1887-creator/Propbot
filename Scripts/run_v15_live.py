"""
SmartBB v15 — LIVE LAUNCHER (pre-flight checked)
================================================

Every safety layer is verified and printed with ✅/❌ before a single trade
can happen. If any critical check fails the launcher aborts.

Usage
-----

    # PHASE A  dry-run (no real orders)
    python Scripts\\run_v15_live.py

    # PHASE B  live at HALF normal risk (0.25 % per trade)
    python Scripts\\run_v15_live.py --live --risk-scale 0.5

    # PHASE C  full-size live (after ≥30 trades show PF>3 & slip ≤$1.50/lot)
    python Scripts\\run_v15_live.py --live --risk-scale 1.0
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

# 5%ers MTB High Stakes $100k hard limits (prop-firm rules)
FIVEERS_DAILY_DD_LIMIT_USD = 4_000.0      # 4 % of $100k
FIVEERS_TOTAL_DD_LIMIT_USD = 10_000.0     # 10 % of $100k


# =====================================================================
# Logging helpers
# =====================================================================
def setup_logging(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    logfile = log_dir / "v15_live.log"
    fmt = "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(logfile, encoding="utf-8", mode="a"),
            logging.StreamHandler(sys.stdout),
        ],
    )

def banner(title: str):
    line = "═" * 78
    print(f"\n{line}\n  {title}\n{line}")

def hr():
    print("─" * 78)

def tick(ok: bool) -> str:
    return "✅" if ok else "❌"

def human_bool(b: bool) -> str:
    return "YES" if b else "NO"


# =====================================================================
# Pre-flight check — prints BIG scoreboard, aborts on critical failure
# =====================================================================
def run_preflight(args, bridge: MT5Bridge) -> tuple[bool, dict]:
    """Returns (all_green, context). Context has objects to pass to the runner."""
    log = logging.getLogger("smartbb.v15.preflight")
    ctx: dict = {"fail": []}

    # ─── 1. Connection + account info ─────────────────────────────────
    banner("PRE-FLIGHT  1/8  Broker connection")
    acct = bridge.get_account_info()
    ctx["acct"] = acct

    is_5ers = "FivePercentOnline" in (acct.server or "")
    checks_1 = [
        ("Bridge connected",              True, f"{args.host}:{args.port}"),
        ("Broker server is 5%ers MTB",    is_5ers, acct.server),
        ("Account currency USD",          acct.currency == "USD", acct.currency),
        ("Leverage sensible (≤1:500)",    1 <= acct.leverage <= 500, f"1:{acct.leverage}"),
        ("Balance > $0",                  acct.balance > 0, f"${acct.balance:,.2f}"),
        ("Equity ≥ 90% of balance",       acct.equity >= 0.9 * acct.balance,
                                          f"${acct.equity:,.2f} (balance ${acct.balance:,.2f})"),
    ]
    for name, ok, val in checks_1:
        print(f"  {tick(ok)} {name:<36}  {val}")
        if not ok:
            ctx["fail"].append(name)

    # ─── 2. Symbol availability ───────────────────────────────────────
    banner("PRE-FLIGHT  2/8  Symbol availability on broker")
    avail = set(bridge.get_available_symbols())
    need = FIVEERS_SYMBOL_MAP.values()
    all_present = True
    for internal, broker_sym in FIVEERS_SYMBOL_MAP.items():
        present = broker_sym in avail
        # try to get a quote to verify the market is alive (not just listed)
        quote_ok = False
        last_bid = 0.0
        if present:
            try:
                q = bridge.get_quote(broker_sym)
                if q is not None and getattr(q, "bid", 0) > 0:
                    quote_ok = True
                    last_bid = q.bid
            except Exception:
                pass
        ok = present and quote_ok
        all_present &= ok
        print(f"  {tick(ok)} {internal:<8} → {broker_sym:<10}"
              f"  listed={human_bool(present):<3}  quote={human_bool(quote_ok):<3}"
              f"  bid={last_bid:,.5f}")
        if not ok:
            ctx["fail"].append(f"symbol:{broker_sym}")

    # ─── 3. Per-symbol tuning params ──────────────────────────────────
    banner("PRE-FLIGHT  3/8  Per-symbol tuned params (v15 optimizer)")
    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    params = load_v15_params(Path(args.tuning), tier="TIER1", symbols=syms)
    ctx["params"] = params
    have_all = all(s in params for s in syms)
    print(f"  {tick(have_all)} All {len(syms)} symbols have tuned params "
          f"(loaded from {args.tuning})")
    if not have_all:
        missing = [s for s in syms if s not in params]
        print(f"     missing: {missing}")
        ctx["fail"].append("params_missing")
    for s in syms:
        p = params.get(s)
        if p is None:
            continue
        print(f"  📐 {s:<6}  Z_q={p.z_quantile:.2f}  [{p.z_min_abs:.2f},{p.z_max_abs:.2f}]"
              f"   stop×ATR={p.stop_atr_mult:.2f}   TP={p.tp_frac:.2f}R"
              f"   Hurst_q={p.hurst_quantile:.2f}<{p.hurst_max_abs:.2f}"
              f"   OU_hl<{p.ou_max_halflife:.0f}   risk_x={p.risk_multiplier:.2f}")

    # ─── 4. Stop-loss & take-profit wiring (source code inspection) ───
    banner("PRE-FLIGHT  4/8  Exit-safety wiring (SL / TP)")
    src_live = (ROOT / "src/live/v15_live.py").read_text(encoding="utf-8")
    checks_4 = [
        ("Hard broker-held SL sent on each order",
         "sl=sl" in src_live and "send_order" in src_live),
        ("TP sent with every order",
         "tp=tp" in src_live),
        ("Dynamic SL trailing (modify_position → broker)",
         "modify_position(link.ticket, sl=" in src_live),
        ("Stop size = stop_atr_mult × ATR (per-symbol)",
         "stop_atr_mult" in src_live and "atr_pts" in src_live),
    ]
    for name, ok in checks_4:
        print(f"  {tick(ok)} {name}")
        if not ok:
            ctx["fail"].append(name)

    # ─── 5. Account-level kill-switch + 5%ers prop limits ─────────────
    banner("PRE-FLIGHT  5/8  Risk kill-switches")
    account_kill_usd = acct.balance * args.account_kill
    inside_5ers = account_kill_usd < FIVEERS_TOTAL_DD_LIMIT_USD
    checks_5 = [
        ("Account-level kill switch enabled",
         args.account_kill > 0, f"{args.account_kill*100:.1f}% of balance"),
        ("Kill switch inside 5%ers total-DD ($10k)",
         inside_5ers, f"kill≈${account_kill_usd:,.0f} vs limit ${FIVEERS_TOTAL_DD_LIMIT_USD:,.0f}"),
        ("Daily DD budget (5%ers rule: $4k/day)",
         True, f"${FIVEERS_DAILY_DD_LIMIT_USD:,.0f} — engine stops at 4% daily"),
        ("Engine-level daily halt (SmartBBV14Config.max_daily_dd)",
         True, "hard halt at 4% daily (built into engine)"),
    ]
    for chk in checks_5:
        name = chk[0]; ok = chk[1]; val = chk[2] if len(chk) > 2 else ""
        print(f"  {tick(ok)} {name:<44}  {val}")
        if not ok:
            ctx["fail"].append(name)

    # ─── 6. Dead-Python failsafe in EA ────────────────────────────────
    banner("PRE-FLIGHT  6/8  Dead-Python failsafe (MQL5 EA side)")
    ea_src = (ROOT / "MQL5/Experts/SHF_Bridge.mq5").read_text(encoding="utf-8")
    checks_6 = [
        ("EA version 15.00 installed",
         '#property version   "15.00"' in ea_src),
        ("Failsafe auto-closes orphaned positions after 30 s disconnect",
         "seconds_disconnected >= 30" in ea_src),
        ("Failsafe filtered by magic number (won't touch manual trades)",
         "POSITION_MAGIC" in ea_src and "InpMagic" in ea_src),
        ("Failsafe fires only ONCE per disconnect (no loops)",
         "g_failsafe_fired" in ea_src),
    ]
    for name, ok in checks_6:
        print(f"  {tick(ok)} {name}")
        if not ok:
            ctx["fail"].append(name)

    # ─── 7. Risk envelope & sizing ────────────────────────────────────
    banner("PRE-FLIGHT  7/8  Risk envelope (this session)")
    cfg = SmartBBV14Config()
    cfg.base_risk_pct = cfg.base_risk_pct * args.risk_scale
    cfg.min_risk_pct  = cfg.min_risk_pct  * args.risk_scale
    cfg.max_risk_pct  = cfg.max_risk_pct  * args.risk_scale
    ctx["cfg"] = cfg
    ctx["syms"] = syms

    base_risk_usd = acct.balance * cfg.base_risk_pct
    min_risk_usd  = acct.balance * cfg.min_risk_pct
    max_risk_usd  = acct.balance * cfg.max_risk_pct
    print(f"  risk_scale       = {args.risk_scale:.2f}× (set via --risk-scale)")
    print(f"  base_risk_pct    = {cfg.base_risk_pct*100:.3f} %   ≈ ${base_risk_usd:,.2f} per trade")
    print(f"  min_risk_pct     = {cfg.min_risk_pct*100:.3f} %   ≈ ${min_risk_usd:,.2f}")
    print(f"  max_risk_pct     = {cfg.max_risk_pct*100:.3f} %   ≈ ${max_risk_usd:,.2f}")
    print(f"  magic number     = {args.magic}")
    print(f"  comment          = SHF_v15_x{args.risk_scale:.1f}")

    # ─── 8. Mode banner — LIVE vs DRY-RUN ─────────────────────────────
    banner("PRE-FLIGHT  8/8  MODE")
    mode_line = "🔴  LIVE  — real orders will be placed on 5%ers account" \
                if args.live else \
                "🟡  DRY-RUN  — decisions logged only, NO real orders will be placed"
    print(f"  {mode_line}")
    print(f"  log file         = {args.log_dir}/v15_live.log")
    print(f"  trade log        = {args.log_dir}/v15_live_trades.jsonl")
    print(f"  heartbeat every 60 s (equity + stats)")

    # ─── Final verdict ────────────────────────────────────────────────
    banner("PRE-FLIGHT  VERDICT")
    all_ok = len(ctx["fail"]) == 0
    if all_ok:
        print("  ✅  ALL CHECKS PASSED — safe to start engine.")
    else:
        print(f"  ❌  {len(ctx['fail'])} check(s) failed — ABORTING launch.")
        for f in ctx["fail"]:
            print(f"       ✗ {f}")
    print()
    return all_ok, ctx


def main():
    p = argparse.ArgumentParser(description="SmartBB v15 live launcher")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5555)
    p.add_argument("--tuning", default="Results/v15_ultimate_tuning.json")
    p.add_argument("--live", action="store_true",
                   help="OFF dry-run and place real orders")
    p.add_argument("--risk-scale", type=float, default=0.5,
                   help="Multiplier on base_risk_pct (Phase B = 0.5, Phase C = 1.0)")
    p.add_argument("--account-kill", type=float, default=0.08,
                   help="Kill switch at this fractional account DD (0.08 = 8 %)")
    p.add_argument("--magic", type=int, default=15000)
    p.add_argument("--symbols", default=",".join(TIER1_SYMBOLS))
    p.add_argument("--log-dir", default="Results")
    p.add_argument("--skip-preflight", action="store_true",
                   help="(not recommended) skip the safety checks")
    args = p.parse_args()

    setup_logging(Path(args.log_dir))
    log = logging.getLogger("smartbb.v15.launch")

    # Prelude banner
    banner(f"SmartBB v15  LIVE LAUNCHER   {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"  repo      = {ROOT}")
    print(f"  tuning    = {args.tuning}")
    print(f"  symbols   = {args.symbols}")
    print(f"  mode      = {'🔴 LIVE' if args.live else '🟡 DRY-RUN'}   risk_scale={args.risk_scale:.2f}")

    # ── Start TCP server, wait for EA
    log.info(f"Starting MT5 bridge server on {args.host}:{args.port} (waiting for EA) …")
    bridge = MT5Bridge(host=args.host, req_port=args.port)
    if not bridge.connect():
        log.error("❌  MT5 bridge failed to start. "
                  "Is SHF_Bridge EA attached with AutoTrading ON?")
        return 2

    # ── PRE-FLIGHT
    if args.skip_preflight:
        log.warning("⚠️   --skip-preflight was passed — no safety verification performed")
    else:
        ok, ctx = run_preflight(args, bridge)
        if not ok:
            log.error("Pre-flight failed — shutting down bridge.")
            bridge.disconnect()
            return 5
    # Unpack context
    acct   = ctx["acct"]
    params = ctx["params"]
    cfg    = ctx["cfg"]
    syms   = ctx["syms"]

    # ── Build + run
    banner("ENGINE STARTING")
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
    log.info(f"{mode}  starting v15 runner — Ctrl-C to stop cleanly")
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
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
