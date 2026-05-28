"""
daily_pnl_breakdown.py
======================

ONE job: read the LIVE trades file and print:
  1. Day-by-day net PnL  (date | n_trades | wins | losses | gross PnL | running equity)
  2. The 10 biggest losing trades (date | symbol | side | risk | PnL | reason)
  3. The 10 biggest winning trades
  4. The trades on the WORST single day

No story.  No interpretation.  Just the numbers from
Results/v30_live_trades.jsonl (and the events log for close-PnL fallback).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TRADES_FILE = REPO / "Results" / "v30_live_trades.jsonl"
EVENTS_FILE = REPO / "Results" / "v30_live_events.log"
START_EQUITY = 100_000.0  # 5%ers account size


# --------------------------------------------------------------------------- #
def parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    s = str(s).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s[:19])
    except Exception:
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def read_jsonl(p: Path) -> list[dict]:
    if not p.exists():
        print(f"!! NOT FOUND: {p}")
        return []
    out = []
    with p.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def parse_events(p: Path) -> dict[str, dict]:
    """Return {ticket -> dict(pnl, close_reason, close_time)} from the events log."""
    if not p.exists():
        print(f"!! NOT FOUND: {p}")
        return {}
    out: dict[str, dict] = {}
    with p.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if "[event]" not in line:
                continue
            # find the JSON payload at the end of the log line
            try:
                jstart = line.index("{", line.index("[event]"))
                payload = json.loads(line[jstart:])
            except Exception:
                continue
            ev = payload.get("event") or payload.get("ev")
            if ev not in ("CLOSE", "TP2", "SL_HIT", "POS_CLOSED_BY_BROKER",
                          "TRAIL_HIT", "TP1", "MANUAL_CLOSE"):
                continue
            tkt = str(payload.get("ticket") or payload.get("position_id") or "")
            if not tkt:
                continue
            pnl = payload.get("pnl") or payload.get("profit") or payload.get("net_pnl")
            if pnl is None:
                continue
            # take the LAST close event we see for each ticket (final close)
            out[tkt] = {
                "pnl": float(pnl),
                "reason": ev,
                "close_time": payload.get("ts") or payload.get("time") or "",
            }
    return out


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 90)
    print(f"  DAILY PnL BREAKDOWN  —  source: {TRADES_FILE.relative_to(REPO)}")
    print("=" * 90)

    trades = read_jsonl(TRADES_FILE)
    events = parse_events(EVENTS_FILE)

    if not trades:
        print("\nNo trades in file. Run on the VPS (or copy Results/ from VPS first).")
        return 1

    # Enrich every ENTRY row with its close PnL from the events log
    rows: list[dict] = []
    for t in trades:
        ev = (t.get("event") or "").upper()
        if ev != "ENTRY":
            continue
        ticket = str(t.get("ticket") or t.get("position_id") or "")
        ent_dt = parse_dt(
            t.get("ts_utc")     # v30 live writes ts_utc
            or t.get("ts")
            or t.get("time")
            or t.get("open_time")
        )
        if ent_dt is None:
            continue

        # Try to find this ticket's close PnL
        close = events.get(ticket, {})
        pnl = close.get("pnl")

        # If no event-log close, fall back to net_pnl/pnl field on the entry row itself
        if pnl is None:
            pnl = t.get("net_pnl") or t.get("pnl")
            try:
                pnl = float(pnl) if pnl is not None else None
            except Exception:
                pnl = None

        rows.append(
            {
                "ticket": ticket,
                "entry_dt": ent_dt,
                "date": ent_dt.date().isoformat(),
                "symbol": t.get("symbol") or "?",
                "side": t.get("side") or t.get("direction") or "?",
                "lots": t.get("lots") or t.get("volume") or 0.0,
                "risk_usd": t.get("risk_usd") or t.get("risk_dollars") or None,
                "risk_pct": t.get("risk_pct") or None,
                "entry_px": (
                    t.get("fill_px")    # v30 live writes fill_px
                    or t.get("entry")
                    or t.get("entry_price")
                ),
                "sl_px": t.get("sl") or t.get("stop_loss") or None,
                "tp1_px": t.get("tp1") or None,
                "tp2_px": t.get("tp2") or None,
                "equity_at_entry": t.get("equity") or None,
                "dry_run": t.get("dry_run"),
                "pnl": pnl,
                "close_reason": close.get("reason", "OPEN"),
            }
        )

    rows.sort(key=lambda r: r["entry_dt"])
    n_with_pnl = sum(1 for r in rows if r["pnl"] is not None)
    n_no_pnl = sum(1 for r in rows if r["pnl"] is None)

    n_dry = sum(1 for r in rows if r["dry_run"] is True)
    n_live = sum(1 for r in rows if r["dry_run"] is False)
    n_unknown_mode = len(rows) - n_dry - n_live

    print(f"\nentries in file        : {len(rows)}")
    print(f"entries with close-PnL : {n_with_pnl}")
    print(f"entries still OPEN/?   : {n_no_pnl}")
    print(f"  dry_run=True         : {n_dry}    <-- SIMULATED, no real broker order")
    print(f"  dry_run=False        : {n_live}    <-- REAL broker orders")
    print(f"  dry_run not recorded : {n_unknown_mode}")
    if rows:
        print(f"first entry            : {rows[0]['entry_dt'].isoformat()}")
        print(f"last entry             : {rows[-1]['entry_dt'].isoformat()}")

    # Find the FIRST time the bot flipped from dry_run -> live (if it did)
    flip_idx = None
    for i, r in enumerate(rows):
        if r["dry_run"] is False:
            flip_idx = i
            break
    if flip_idx is None and n_live == 0:
        print("\n*** ALL 89 ENTRIES ARE dry_run=True (paper trades). ***")
        print("*** The $4.5k loss on your 5%ers account did NOT come from these. ***")
        print("*** Check MT5 broker statement -- it came from somewhere else. ***")
    elif flip_idx is not None:
        flip = rows[flip_idx]
        print(f"\nFirst live (dry_run=False) entry: "
              f"{flip['entry_dt'].isoformat()}  #{flip_idx+1} of {len(rows)}")

    # ------------------------------------------------------------------
    #  EQUITY WALK  -- the entry rows record `equity` at trade time,
    #  so we can plot the equity curve WITHOUT needing the close events
    # ------------------------------------------------------------------
    print("\n" + "-" * 90)
    print("  EQUITY WALK FROM ENTRY ROWS (the `equity` field at the moment of each entry)")
    print("-" * 90)
    eq_rows = [r for r in rows if r["equity_at_entry"] is not None]
    if not eq_rows:
        print("  no equity field on entry rows")
    else:
        first_eq = float(eq_rows[0]["equity_at_entry"])
        last_eq = float(eq_rows[-1]["equity_at_entry"])
        min_eq = min(float(r["equity_at_entry"]) for r in eq_rows)
        max_eq = max(float(r["equity_at_entry"]) for r in eq_rows)
        print(f"  first entry equity : ${first_eq:>11,.2f}  ({eq_rows[0]['entry_dt'].date()})")
        print(f"  last  entry equity : ${last_eq:>11,.2f}  ({eq_rows[-1]['entry_dt'].date()})")
        print(f"  peak  entry equity : ${max_eq:>11,.2f}")
        print(f"  trough entry equity: ${min_eq:>11,.2f}")
        print(f"  net change         : ${last_eq - first_eq:>+11,.2f}")
        print(f"  max drawdown seen  : ${min_eq - max_eq:>+11,.2f}  "
              f"({(min_eq/max_eq - 1)*100:+.2f} %)")

    # Show equity at the start of each new DATE -- this is the daily walk
    print("\n  Daily snapshot (equity at the FIRST entry of each date):")
    print(f"  {'date':<11} {'first entry time':<19} {'equity':>12} "
          f"{'risk$':>9} {'risk%':>8} {'mode':<8}")
    seen_dates: set[str] = set()
    for r in rows:
        if r["date"] in seen_dates:
            continue
        seen_dates.add(r["date"])
        eq_s = (f"${float(r['equity_at_entry']):>10,.2f}"
                if r["equity_at_entry"] is not None else "         ?")
        risk_s = (f"${float(r['risk_usd']):>7.2f}"
                  if r["risk_usd"] is not None else "       ?")
        risk_pct_s = (f"{float(r['risk_pct'])*100:>6.3f}%"
                      if r["risk_pct"] is not None else "      ?")
        mode = ("DRY" if r["dry_run"] is True
                else "LIVE" if r["dry_run"] is False
                else "?")
        tm = r["entry_dt"].strftime("%Y-%m-%d %H:%M:%S")
        print(f"  {r['date']:<11} {tm:<19} {eq_s:>12} {risk_s:>9} "
              f"{risk_pct_s:>8} {mode:<8}")


    # 1. Day-by-day net PnL ---------------------------------------------------
    print("\n" + "-" * 90)
    print("  1. DAY-BY-DAY NET PnL  (running equity assumes start = $100,000)")
    print("-" * 90)
    print(f"  {'date':<11} {'n':>3} {'wins':>5} {'loss':>5} {'gross PnL':>12} "
          f"{'avg risk':>10} {'equity end':>12}")
    by_day: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_day[r["date"]].append(r)
    eq = START_EQUITY
    daily_summary: list[dict] = []
    for date in sorted(by_day):
        day_rows = by_day[date]
        closed = [r for r in day_rows if r["pnl"] is not None]
        pnl_sum = sum(r["pnl"] for r in closed)
        wins = sum(1 for r in closed if r["pnl"] > 0)
        losses = sum(1 for r in closed if r["pnl"] < 0)
        risks = [r["risk_usd"] for r in day_rows if r["risk_usd"] is not None]
        avg_risk = sum(risks) / len(risks) if risks else None
        eq += pnl_sum
        daily_summary.append({"date": date, "pnl": pnl_sum, "n": len(day_rows),
                              "wins": wins, "losses": losses, "eq_end": eq,
                              "avg_risk": avg_risk})
        risk_s = f"${avg_risk:>8.2f}" if avg_risk is not None else "       ?"
        print(f"  {date:<11} {len(day_rows):>3} {wins:>5} {losses:>5} "
              f"${pnl_sum:>+11,.2f} {risk_s} ${eq:>11,.2f}")

    # 2. Biggest losing days --------------------------------------------------
    print("\n" + "-" * 90)
    print("  2. WORST 5 DAYS BY NET PnL")
    print("-" * 90)
    losing_days = sorted(daily_summary, key=lambda d: d["pnl"])[:5]
    print(f"  {'date':<11} {'n':>3} {'wins':>5} {'loss':>5} {'gross PnL':>12} {'avg risk':>10}")
    for d in losing_days:
        risk_s = f"${d['avg_risk']:>8.2f}" if d['avg_risk'] is not None else "       ?"
        print(f"  {d['date']:<11} {d['n']:>3} {d['wins']:>5} {d['losses']:>5} "
              f"${d['pnl']:>+11,.2f} {risk_s}")

    # 3. Drill into the single worst day --------------------------------------
    if losing_days:
        worst = losing_days[0]
        print("\n" + "-" * 90)
        print(f"  3. EVERY TRADE ON THE WORST DAY ({worst['date']})  net=${worst['pnl']:+,.2f}")
        print("-" * 90)
        print(f"  {'time(UTC)':<19} {'sym':<7} {'side':<5} {'lots':>7} "
              f"{'risk$':>8} {'risk%':>7} {'pnl$':>10} {'reason':<10}")
        for r in by_day[worst["date"]]:
            tm = r["entry_dt"].strftime("%Y-%m-%d %H:%M:%S")
            pnl_s = f"${r['pnl']:>+9,.2f}" if r['pnl'] is not None else "      open"
            risk_s = f"${r['risk_usd']:>6.0f}" if r['risk_usd'] is not None else "      ?"
            risk_pct_s = (
                f"{r['risk_pct']*100:>6.3f}%"
                if r['risk_pct'] is not None
                else "     ?"
            )
            lots_s = f"{float(r['lots']):>7.3f}" if r['lots'] else "      ?"
            print(f"  {tm:<19} {r['symbol']:<7} {str(r['side']):<5} {lots_s} "
                  f"{risk_s} {risk_pct_s} {pnl_s} {r['close_reason']:<10}")

    # 4. Biggest single-trade winners / losers --------------------------------
    print("\n" + "-" * 90)
    print("  4. WORST 10 INDIVIDUAL TRADES")
    print("-" * 90)
    with_pnl = [r for r in rows if r["pnl"] is not None]
    worst_trades = sorted(with_pnl, key=lambda r: r["pnl"])[:10]
    print(f"  {'date':<11} {'time':<9} {'sym':<7} {'side':<5} "
          f"{'risk$':>8} {'pnl$':>10}  reason")
    for r in worst_trades:
        tm = r["entry_dt"].strftime("%H:%M:%S")
        pnl_s = f"${r['pnl']:>+9,.2f}"
        risk_s = f"${r['risk_usd']:>6.0f}" if r['risk_usd'] is not None else "      ?"
        print(f"  {r['date']:<11} {tm:<9} {r['symbol']:<7} {str(r['side']):<5} "
              f"{risk_s} {pnl_s}  {r['close_reason']}")

    print("\n" + "-" * 90)
    print("  5. BEST 10 INDIVIDUAL TRADES")
    print("-" * 90)
    best_trades = sorted(with_pnl, key=lambda r: r["pnl"], reverse=True)[:10]
    print(f"  {'date':<11} {'time':<9} {'sym':<7} {'side':<5} "
          f"{'risk$':>8} {'pnl$':>10}  reason")
    for r in best_trades:
        tm = r["entry_dt"].strftime("%H:%M:%S")
        pnl_s = f"${r['pnl']:>+9,.2f}"
        risk_s = f"${r['risk_usd']:>6.0f}" if r['risk_usd'] is not None else "      ?"
        print(f"  {r['date']:<11} {tm:<9} {r['symbol']:<7} {str(r['side']):<5} "
              f"{risk_s} {pnl_s}  {r['close_reason']}")

    # 6. Totals ---------------------------------------------------------------
    print("\n" + "=" * 90)
    closed_pnls = [r["pnl"] for r in rows if r["pnl"] is not None]
    print(f"  TOTAL closed trades : {len(closed_pnls)}")
    print(f"  TOTAL net PnL       : ${sum(closed_pnls):+,.2f}")
    print(f"  Wins                : {sum(1 for p in closed_pnls if p > 0)}")
    print(f"  Losses              : {sum(1 for p in closed_pnls if p < 0)}")
    if closed_pnls:
        avg_w = [p for p in closed_pnls if p > 0]
        avg_l = [p for p in closed_pnls if p < 0]
        print(f"  Avg win             : ${(sum(avg_w)/len(avg_w)) if avg_w else 0:+,.2f}")
        print(f"  Avg loss            : ${(sum(avg_l)/len(avg_l)) if avg_l else 0:+,.2f}")
    print("=" * 90)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
