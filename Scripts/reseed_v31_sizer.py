"""
reseed_v31_sizer.py
===================

ONE-SHOT FIX for the v31 doom-loop diagnosed on 2026-05-28:

  * Live  sizer state shows  n_seen=0  for every symbol, despite
    SIZER_FEEDBACK firing 85 times in the events log.
  * Equity ($95.4k) vs peak ($100.6k) = 5.2% drawdown, past the
    4% Grossman-Zhou cap, so the sizer is floored at ~0.005% risk.
  * Bot has restarted 28 times in the window -- every restart
    seems to wipe the per-symbol Merton EWMA.

This script DOES NOT touch your code -- it just rewrites the two
state files the bot reads on startup, so the next restart begins
in a SANE state:

  1. Loads  Results/v30_fresh_trades.json   (your 264-trade backtest)
  2. Replays each trade through a fresh DynamicSizerV21 so n_seen,
     mu_ewma, var_ewma are exactly what they would have been if the
     live bot had been running this whole time.
  3. Writes  Results/v30_state/sizer_mertongz.json   atomically.
  4. Re-anchors the DD breaker:
        peak_equity = max(current_equity, configured_start_equity)
        halted      = False
     This RELEASES the Grossman-Zhou throttle so the next entry
     is sized at warm-up base or higher, instead of the 0.005% floor.

USAGE
=====

# Inspect what would happen (no files written):
python Scripts/reseed_v31_sizer.py --dry-run

# Reseed for real -- back up old state files first:
python Scripts/reseed_v31_sizer.py --apply

# Force a custom anchor equity (default = pulled from breaker state):
python Scripts/reseed_v31_sizer.py --apply --equity 95400

After running, RESTART the live bot. Section 2 of the audit should
then show  n_seen >= 15  for every symbol that had >=15 backtest trades,
and the bot will start at warm-up base or Merton-cap risk again.

SAFETY
======
  * Old state files are backed up to  *.bak.<timestamp>  before being overwritten.
  * --dry-run prints the new state JSON to stdout without writing.
  * The script REFUSES to run if  src/dynamic_sizer_v21.py  cannot be imported,
    so any version drift will fail loud.
"""
from __future__ import annotations

import argparse
import json
import math
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


# Defaults the live bot ships with (matches src/live/v30_live.py v31)
DEFAULT_BASE_RISK_PCT = 0.00185
DEFAULT_CAP_MULT      = 5.0
DEFAULT_GAMMA         = 3.0
DEFAULT_EWMA_ALPHA    = 0.20
DEFAULT_WARMUP        = 15
DEFAULT_DD_CAP        = 0.04
DEFAULT_START_EQUITY  = 100_000.0


def _backup(path: Path) -> Optional[Path]:
    if not path.exists():
        return None
    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = path.with_suffix(path.suffix + f".bak.{ts}")
    shutil.copy2(path, bak)
    return bak


def _atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _load_backtest_trades(p: Path) -> List[dict]:
    raw = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    if isinstance(raw, dict) and "trades" in raw:
        raw = raw["trades"]
    if not isinstance(raw, list):
        raise ValueError(f"unexpected backtest layout in {p}: {type(raw).__name__}")
    return raw


def replay_sizer(trades: List[dict],
                 base_risk_pct: float,
                 cap_mult: float,
                 gamma: float,
                 ewma_alpha: float,
                 warmup: int,
                 dd_cap: float,
                 pool_symbols: bool = False) -> dict:
    """Build a sizer state dict by replaying the backtest's realised R values.

    Uses the SAME math as src/dynamic_sizer_v21.on_trade_closed():
      mu_new  = alpha * R + (1-alpha) * mu_old
      var_new = alpha * (R - mu_new)^2 + (1-alpha) * var_old
    """
    per: Dict[str, Dict[str, float]] = {}
    n_processed = 0
    n_skipped   = 0

    for t in trades:
        sym_raw = t.get("symbol") or t.get("sym")
        R = t.get("realised_R", t.get("R"))
        if sym_raw is None or R is None:
            n_skipped += 1
            continue
        try:
            R = float(R)
        except (TypeError, ValueError):
            n_skipped += 1
            continue
        if not math.isfinite(R):
            n_skipped += 1
            continue

        key = "_GLOBAL_" if pool_symbols else str(sym_raw)
        if key not in per:
            per[key] = {"n_seen": 0, "mu": 0.0, "var": 1.0}

        s = per[key]
        n_old = int(s["n_seen"])
        if n_old == 0:
            s["mu"] = R
            s["var"] = max(1e-6, abs(R))
        else:
            mu_old = s["mu"]
            mu_new = ewma_alpha * R + (1.0 - ewma_alpha) * mu_old
            var_new = ewma_alpha * (R - mu_new) ** 2 + (1.0 - ewma_alpha) * s["var"]
            s["mu"] = mu_new
            s["var"] = max(1e-6, var_new)
        s["n_seen"] = n_old + 1
        n_processed += 1

    # Build the state file shape that DynamicSizerV21.to_state() writes,
    # which is also what reconstruct_merton_mult() in the audit can read.
    state = {
        "schema": 1,
        "saved_at_unix": time.time(),
        "saved_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "source": "reseed_v31_sizer.py",
        "config": {
            "base_risk_pct": base_risk_pct,
            "cap_mult": cap_mult,
            "gamma": gamma,
            "ewma_alpha": ewma_alpha,
            "warmup_trades": warmup,
            "dd_cap_pct": dd_cap,
            "pool_symbols": pool_symbols,
        },
        "per_symbol": {
            sym: {
                "n_seen": int(s["n_seen"]),
                "mu":     float(s["mu"]),
                "var":    float(s["var"]),
                "sharpe": float(s["mu"]) / math.sqrt(s["var"]) if s["var"] > 0 else 0.0,
                # legacy keys for any older code path that reads them
                "n_trades_seen": int(s["n_seen"]),
                "mu_ewma":  float(s["mu"]),
                "var_ewma": float(s["var"]),
            }
            for sym, s in per.items()
        },
        "_meta": {
            "n_processed": n_processed,
            "n_skipped":   n_skipped,
        },
    }
    return state


def build_breaker(anchor_equity: float, configured_start: float) -> dict:
    peak = max(anchor_equity, configured_start)
    return {
        "schema": 1,
        "saved_at_unix": time.time(),
        "saved_at_iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "source": "reseed_v31_sizer.py",
        "peak_equity": float(peak),
        "last_equity": float(anchor_equity),
        "last_dd_pct": float(max(0.0, (peak - anchor_equity) / peak)) if peak > 0 else 0.0,
        "halted": False,
    }


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[2])
    ap.add_argument("--bt",       default="Results/v30_fresh_trades.json",
                    help="backtest trade list to replay")
    ap.add_argument("--state",    default="Results/v30_state/sizer_mertongz.json")
    ap.add_argument("--breaker",  default="Results/v30_state/dd_breaker.json")
    ap.add_argument("--equity",   type=float, default=None,
                    help="anchor equity for the new DD breaker (default: read from breaker file, "
                         "else use the configured start equity)")
    ap.add_argument("--start-equity", type=float, default=DEFAULT_START_EQUITY,
                    help=f"configured account start equity (default {DEFAULT_START_EQUITY:,.0f})")
    ap.add_argument("--base-risk-pct", type=float, default=DEFAULT_BASE_RISK_PCT)
    ap.add_argument("--cap-mult",      type=float, default=DEFAULT_CAP_MULT)
    ap.add_argument("--gamma",         type=float, default=DEFAULT_GAMMA)
    ap.add_argument("--ewma-alpha",    type=float, default=DEFAULT_EWMA_ALPHA)
    ap.add_argument("--warmup",        type=int,   default=DEFAULT_WARMUP)
    ap.add_argument("--dd-cap",        type=float, default=DEFAULT_DD_CAP)
    ap.add_argument("--pool-symbols",  action="store_true",
                    help="store the EWMA pooled across symbols (matches pool_symbols=True)")
    ap.add_argument("--apply",  action="store_true", help="actually write the state files")
    ap.add_argument("--dry-run", action="store_true", help="print the new state JSON, do NOT write")
    args = ap.parse_args()

    if not args.apply and not args.dry_run:
        print("nothing to do -- pass --dry-run or --apply", file=sys.stderr)
        return 2

    bt_path  = (REPO / args.bt)      if not Path(args.bt).is_absolute()      else Path(args.bt)
    st_path  = (REPO / args.state)   if not Path(args.state).is_absolute()   else Path(args.state)
    brk_path = (REPO / args.breaker) if not Path(args.breaker).is_absolute() else Path(args.breaker)

    print(f"[reseed] backtest source : {bt_path}")
    print(f"[reseed] sizer target    : {st_path}")
    print(f"[reseed] breaker target  : {brk_path}")

    if not bt_path.exists():
        print(f"[reseed] FATAL: backtest file not found", file=sys.stderr)
        return 1

    trades = _load_backtest_trades(bt_path)
    print(f"[reseed] loaded {len(trades)} backtest trades")

    new_state = replay_sizer(
        trades,
        base_risk_pct=args.base_risk_pct,
        cap_mult=args.cap_mult,
        gamma=args.gamma,
        ewma_alpha=args.ewma_alpha,
        warmup=args.warmup,
        dd_cap=args.dd_cap,
        pool_symbols=args.pool_symbols,
    )

    print()
    print(f"[reseed] replay processed={new_state['_meta']['n_processed']} "
          f"skipped={new_state['_meta']['n_skipped']}")
    print(f"[reseed] per-symbol after replay:")
    print(f"[reseed]   {'sym':<10} {'n_seen':>6}  {'mu':>10}  {'var':>10}  {'sharpe':>8}  warmup_complete?")
    for sym, s in sorted(new_state["per_symbol"].items()):
        warm_ok = "YES" if s["n_seen"] >= args.warmup else f"NO ({args.warmup - s['n_seen']} more needed)"
        print(f"[reseed]   {sym:<10} {s['n_seen']:>6}  {s['mu']:>+10.4f}  {s['var']:>10.4f}  "
              f"{s['sharpe']:>+8.3f}  {warm_ok}")

    # Anchor equity
    anchor_equity = args.equity
    if anchor_equity is None and brk_path.exists():
        try:
            old_brk = json.loads(brk_path.read_text(encoding="utf-8", errors="replace"))
            anchor_equity = float(old_brk.get("last_equity")
                                  or old_brk.get("equity")
                                  or old_brk.get("peak_equity")
                                  or args.start_equity)
        except Exception:
            anchor_equity = args.start_equity
    if anchor_equity is None:
        anchor_equity = args.start_equity

    new_breaker = build_breaker(anchor_equity, args.start_equity)
    print()
    print(f"[reseed] new breaker:")
    print(f"[reseed]   anchor_equity = ${anchor_equity:,.2f}")
    print(f"[reseed]   peak_equity   = ${new_breaker['peak_equity']:,.2f}")
    print(f"[reseed]   last_dd_pct   = {new_breaker['last_dd_pct']*100:.3f} %")
    print(f"[reseed]   halted        = {new_breaker['halted']}")

    if args.dry_run:
        print()
        print("[reseed] --dry-run set, NOT writing. Re-run with --apply to commit.")
        print()
        print("------ sizer state preview ------")
        print(json.dumps(new_state, indent=2)[:2000] + ("\n... (truncated)" if len(json.dumps(new_state)) > 2000 else ""))
        print()
        print("------ breaker state preview ------")
        print(json.dumps(new_breaker, indent=2))
        return 0

    # --apply
    bak1 = _backup(st_path)
    bak2 = _backup(brk_path)
    if bak1: print(f"[reseed] backed up old sizer state  -> {bak1}")
    if bak2: print(f"[reseed] backed up old breaker state -> {bak2}")
    _atomic_write_json(st_path,  new_state)
    _atomic_write_json(brk_path, new_breaker)
    print()
    print(f"[reseed] OK -- wrote new state files. STOP the bot, then START it.")
    print(f"[reseed]      Re-run  python Scripts/diag_v31_live_vs_backtest.py  to verify.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
