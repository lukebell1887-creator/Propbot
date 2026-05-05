r"""
Scripts/diag_ticket_dealprice.py
================================
Pulls every MT5 history deal tied to a position ticket and prints them in
a human-readable table along with the entry / exit prices and a verdict
that compares the close price against the bot's logged SL / TP1 / TP2.

Why this exists
---------------
v30_live_events.log only records the EVENT KIND ("POS_CLOSED_BY_BROKER",
"CLOSE", "TP1_PARTIAL", ...) -- it does NOT record the close PRICE. For a
position closed externally (manual close in MT5, broker SL hit, broker
liquidation, etc.) the bot just sees "the position is no longer there"
and writes POS_CLOSED_BY_BROKER. To know what actually happened we have
to ask MT5 for the historical deals on that position.

Usage on VPS:
    python Scripts\diag_ticket_dealprice.py 547550971
    python Scripts\diag_ticket_dealprice.py 547550971 547564113   (multiple)
    python Scripts\diag_ticket_dealprice.py --today               (every entry today)

Output:
    TICKET     SYM   SIDE   ENTRY_TS              ENTRY_PX     EXIT_TS               EXIT_PX
    SL=...    TP1=...   TP2=...   PNL=...
    VERDICT  : matches TP1 / matches SL / between TP1 and TP2 / outside ladder = manual close
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional


def _read_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except Exception:
                pass
    return out


def _load_logged(root: Path) -> Dict[int, dict]:
    """Read v30_live_trades.jsonl and return {ticket: ENTRY-row}."""
    rows = _read_jsonl(root / "v30_live_trades.jsonl")
    out: Dict[int, dict] = {}
    for r in rows:
        if str(r.get("event", "")).upper() != "ENTRY":
            continue
        try:
            out[int(r["ticket"])] = r
        except Exception:
            continue
    return out


def _classify_exit(side: str, exit_px: float,
                   sl: Optional[float], tp1: Optional[float],
                   tp2: Optional[float]) -> str:
    """Bucket the exit price against the bot's logged ladder levels.
    Tolerance = 0.05 * (TP1 - entry) when available, else 0.5 raw points."""
    side = (side or "").upper()
    near = 0.0
    if tp1 is not None and sl is not None:
        near = max(abs(tp1 - sl) * 0.05, 0.5)
    else:
        near = 0.5

    def close_to(a: float, b: float) -> bool:
        return abs(a - b) <= near

    if sl is not None and close_to(exit_px, sl):
        return f"SL HIT  (px={exit_px:.5g} ~ SL={sl:.5g}, tol={near:.3g})"
    if tp2 is not None and close_to(exit_px, tp2):
        return f"TP2 HIT (px={exit_px:.5g} ~ TP2={tp2:.5g})"
    if tp1 is not None and close_to(exit_px, tp1):
        return f"TP1 HIT (px={exit_px:.5g} ~ TP1={tp1:.5g})"
    # Position relative to ladder
    if side == "LONG":
        if tp1 is not None and exit_px > tp1:
            if tp2 is not None and exit_px > tp2:
                return f"BEYOND TP2 (px={exit_px:.5g} > TP2={tp2:.5g}) -- manual close above ladder"
            return f"BETWEEN TP1 and TP2 (px={exit_px:.5g}) -- likely MANUAL TP at favourable price"
        if sl is not None and exit_px < sl:
            return f"WORSE THAN SL (px={exit_px:.5g} < SL={sl:.5g}) -- gap-through or slippage"
        return f"INSIDE RANGE (px={exit_px:.5g}) -- likely MANUAL CLOSE before TP/SL"
    elif side == "SHORT":
        if tp1 is not None and exit_px < tp1:
            if tp2 is not None and exit_px < tp2:
                return f"BEYOND TP2 (px={exit_px:.5g} < TP2={tp2:.5g}) -- manual close below ladder"
            return f"BETWEEN TP1 and TP2 (px={exit_px:.5g}) -- likely MANUAL TP at favourable price"
        if sl is not None and exit_px > sl:
            return f"WORSE THAN SL (px={exit_px:.5g} > SL={sl:.5g}) -- gap-through or slippage"
        return f"INSIDE RANGE (px={exit_px:.5g}) -- likely MANUAL CLOSE before TP/SL"
    return f"px={exit_px:.5g} -- side unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("tickets", nargs="*", type=int,
                    help="ticket numbers to inspect (omit if using --today)")
    ap.add_argument("--today", action="store_true",
                    help="inspect every ENTRY in v30_live_trades.jsonl from today UTC")
    ap.add_argument("--all", action="store_true",
                    help="inspect EVERY ENTRY in v30_live_trades.jsonl (full history)")
    ap.add_argument("--summary", action="store_true",
                    help="with --all: print one-line-per-ticket summary table only "
                         "(no per-ticket deal table); great for spotting the stuck-ladder "
                         "pattern across many positions")
    ap.add_argument("--root", default="Results",
                    help="directory holding v30_live_*.jsonl (default: Results)")
    args = ap.parse_args()

    root = Path(args.root)
    logged = _load_logged(root)

    # Resolve target ticket list
    if args.all:
        target = sorted(logged.keys(),
                        key=lambda tk: (logged[tk].get("ts_utc") or ""))
        if not target:
            print(f"[info] no ENTRY rows in {root / 'v30_live_trades.jsonl'}")
            return 0
    elif args.today:
        today = datetime.now(timezone.utc).date()
        target = []
        for tk, row in logged.items():
            ts = row.get("ts_utc") or ""
            if ts[:10] == today.isoformat():
                target.append(tk)
        if not target:
            print(f"[info] no ENTRY rows in {root / 'v30_live_trades.jsonl'} for {today}")
            return 0
    elif args.tickets:
        target = args.tickets
    else:
        ap.print_help()
        return 2

    # Lazy MT5 import so the script still parses on dev machines
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception as e:
        print(f"[err] MetaTrader5 module not importable: {e}")
        print("      run this on the VPS where the bot is connected to MT5.")
        return 2

    if not mt5.initialize():
        print(f"[err] mt5.initialize() failed: {mt5.last_error()}")
        return 2

    print()
    print("=" * 92)
    print("  V30 -- TICKET DEAL-PRICE LOOKUP")
    print("=" * 92)
    print(f"  root              = {root.resolve()}")
    print(f"  tickets requested = {target}")
    print(f"  now (UTC)         = {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    rc = 0
    summary_rows: List[dict] = []   # one entry per ticket, for the final table

    for tk in target:
        if not args.summary:
            print("-" * 92)
            print(f"  TICKET {tk}")
            print("-" * 92)
        row = logged.get(tk)
        if row is not None and not args.summary:
            print(f"  bot logged    : symbol={row.get('symbol')}  side={row.get('side')}  "
                  f"lots={row.get('lots')}")
            print(f"                  entry_px={row.get('fill_px')}  intended={row.get('intended_px')}")
            print(f"                  SL={row.get('sl')}  TP1={row.get('tp1')}  TP2={row.get('tp2')}")
            print(f"                  ts_utc={row.get('ts_utc')}")
        elif row is None and not args.summary:
            print(f"  bot logged    : (no ENTRY row found in trades.jsonl for this ticket)")

        # Pull all deals on this position
        deals = mt5.history_deals_get(position=tk)
        if deals is None or len(deals) == 0:
            t_to = datetime.now(timezone.utc)
            t_from = t_to - timedelta(days=14)
            deals = mt5.history_deals_get(t_from, t_to, position=tk)
        if deals is None or len(deals) == 0:
            if not args.summary:
                print(f"  mt5 deals     : NONE  last_error={mt5.last_error()}")
            summary_rows.append({"ticket": tk, "sym": (row or {}).get("symbol", "?"),
                                 "side": (row or {}).get("side", "?"), "verdict": "NO_MT5_DEALS",
                                 "exit_px": None, "held_min": None, "pnl": None, "row": row})
            rc = max(rc, 1)
            continue

        ds = sorted(deals, key=lambda d: int(d.time))
        if not args.summary:
            print(f"  mt5 deals     : {len(deals)} found")
            print(f"  {'#':<2} {'TIME(UTC)':<19} {'TYPE':<10} {'ENTRY':<5} {'PRICE':>12} "
                  f"{'VOL':>8} {'PROFIT':>10} {'COMMENT':<30}")
            for i, d in enumerate(ds):
                ts = datetime.fromtimestamp(int(d.time), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                type_map = {0: "BUY", 1: "SELL", 2: "BAL", 3: "CRD", 4: "CHG", 5: "COR", 6: "BON"}
                tname = type_map.get(int(d.type), str(int(d.type)))
                entry_map = {0: "IN", 1: "OUT", 2: "INOUT", 3: "OUT_BY"}
                ename = entry_map.get(int(d.entry), str(int(d.entry)))
                print(f"  {i:<2} {ts:<19} {tname:<10} {ename:<5} "
                      f"{float(d.price):>12.5f} {float(d.volume):>8.3f} "
                      f"{float(d.profit):>10.2f} {str(d.comment)[:30]:<30}")

        in_deal = next((d for d in ds if int(d.entry) == 0), None)
        out_deals = [d for d in ds if int(d.entry) in (1, 3)]
        partial_deals = [d for d in ds if int(d.entry) == 2]
        if not out_deals and not partial_deals:
            if not args.summary:
                print(f"  [info] position still OPEN -- no closing deals yet")
            summary_rows.append({"ticket": tk, "sym": (row or {}).get("symbol", "?"),
                                 "side": (row or {}).get("side", "?"), "verdict": "STILL_OPEN",
                                 "exit_px": None,
                                 "held_min": (int(datetime.now(timezone.utc).timestamp()) -
                                              int(in_deal.time))/60.0 if in_deal else None,
                                 "pnl": None, "row": row})
            continue

        final_deal = (out_deals or partial_deals)[-1]
        exit_px = float(final_deal.price)
        side = (row or {}).get("side")
        sl = (row or {}).get("sl")
        tp1 = (row or {}).get("tp1")
        tp2 = (row or {}).get("tp2")
        verdict = _classify_exit(side, exit_px, sl, tp1, tp2)
        held_s = (int(final_deal.time) - int(in_deal.time)) if in_deal is not None else 0
        total_profit = sum(float(d.profit) for d in ds)
        total_swap = sum(float(getattr(d, "swap", 0.0)) for d in ds)
        total_commis = sum(float(getattr(d, "commission", 0.0)) for d in ds)

        if not args.summary:
            print()
            print(f"  ENTRY price   = {float(in_deal.price):.5f}" if in_deal else "  ENTRY price = ?")
            print(f"  EXIT  price   = {exit_px:.5f}   ({len(out_deals)+len(partial_deals)} closing deal[s])")
            print(f"  held          = {held_s} s ({held_s/60.0:.1f} min)")
            print(f"  net P/L (gross) = {total_profit:.2f}   swap={total_swap:.2f}   commis={total_commis:.2f}")
            print(f"  VERDICT       = {verdict}")
            v = verdict.lower()
            if "manual" in v or "between tp1" in v:
                print(f"  >>> CLOSED MANUALLY -- bot did NOT fire its own TP/SL.")
            elif "tp1" in v or "tp2" in v:
                print(f"  >>> closed AT THE LADDER LEVEL the bot intended.")
            elif "sl hit" in v:
                print(f"  >>> closed AT THE STOP LOSS the bot set.")

        # Bucket the verdict for summary
        v = verdict.lower()
        if "tp2 hit" in v:
            bucket = "TP2"
        elif "tp1 hit" in v:
            bucket = "TP1"
        elif "sl hit" in v:
            bucket = "SL"
        elif "manual" in v or "between tp1" in v or "inside range" in v or "beyond tp2" in v:
            bucket = "MANUAL/STUCK"
        elif "worse than sl" in v:
            bucket = "GAP_THRU_SL"
        else:
            bucket = "?"
        summary_rows.append({"ticket": tk, "sym": (row or {}).get("symbol", "?"),
                             "side": (row or {}).get("side", "?"),
                             "verdict": bucket, "verdict_full": verdict,
                             "exit_px": exit_px, "held_min": held_s/60.0,
                             "pnl": total_profit, "row": row,
                             "entry_ts": (row or {}).get("ts_utc", "?")})

    try:
        mt5.shutdown()
    except Exception:
        pass

    # ========== SUMMARY TABLE ==========
    print()
    print("=" * 110)
    print("  SUMMARY  (one row per ticket -- bucket = how the position actually exited)")
    print("=" * 110)
    print(f"  {'TICKET':<11} {'ENTRY_TS(UTC)':<20} {'SYM':<7} {'SIDE':<6} "
          f"{'BUCKET':<14} {'EXIT_PX':>12} {'HELD_MIN':>9} {'PNL':>10}")
    print("  " + "-" * 100)
    counts: Dict[str, int] = {}
    pnl_by_bucket: Dict[str, float] = {}
    for s in summary_rows:
        ts = (s.get("entry_ts") or "?")[:19]
        ex = f"{s['exit_px']:.5f}" if s.get('exit_px') is not None else "?"
        hm = f"{s['held_min']:.1f}" if s.get('held_min') is not None else "?"
        pn = f"{s['pnl']:.2f}" if s.get('pnl') is not None else "?"
        flag = "  <-- STUCK LADDER" if s["verdict"] == "MANUAL/STUCK" else ""
        print(f"  {s['ticket']:<11} {ts:<20} {s['sym']:<7} {s['side']:<6} "
              f"{s['verdict']:<14} {ex:>12} {hm:>9} {pn:>10}{flag}")
        counts[s["verdict"]] = counts.get(s["verdict"], 0) + 1
        if s.get("pnl") is not None:
            pnl_by_bucket[s["verdict"]] = pnl_by_bucket.get(s["verdict"], 0.0) + s["pnl"]
    print("  " + "-" * 100)
    print()
    print("  COUNTS BY BUCKET:")
    for b in sorted(counts.keys()):
        pnl = pnl_by_bucket.get(b, 0.0)
        print(f"    {b:<14} : n={counts[b]:<3}   sum_pnl={pnl:>10.2f}")
    stuck = counts.get("MANUAL/STUCK", 0)
    total = sum(counts.values())
    if stuck > 0:
        print()
        print(f"  *** {stuck} of {total} positions ({100*stuck/total:.0f}%) exited as MANUAL/STUCK")
        print(f"      -- these are positions where the bot did NOT fire its own TP1/TP2/SL")
        print(f"      -- the close came from somewhere else (you, broker, external EA).")
        print(f"      -- IF YOU DID NOT MANUALLY CLOSE THESE, this is the partial-ladder bug.")
    print()
    print("=" * 110)
    print("  DONE.")
    print("=" * 110)
    return rc


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
