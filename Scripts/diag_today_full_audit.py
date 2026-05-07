"""
diag_today_full_audit.py
========================
Per-ticket audit of every entry today. Reads:
  - Results/v30_live_trades.jsonl     (one JSON line per ENTRY)
  - Results/v30_live_events.log       (one JSON line per event:
                                       kind in {TP1_PARTIAL, TP2_PARTIAL,
                                       TRAIL_SL, LAYER1_FIRED, CLOSE,
                                       SIZER_FEEDBACK, POS_CLOSED_BY_BROKER,
                                       BLOCK_NOCHASE_COOLDOWN,
                                       RECONCILE_GRACE_SKIPPED,
                                       FLATTEN_ALL, ORDER_FAILED, ...})

For every entry today prints:
  - timestamp, symbol, side, entry, SL, TP1
  - every subsequent event for that ticket (with realised_R if present)
  - classification:
      TP1+TP2_HIT (full ladder)
      TP1_HIT_then_TRAIL
      TP1_HIT (still open or BE)
      LAYER1_CLOSE_AT_SL    (real loss, not a missed TP)
      SL_HIT (loss)
      CLOSED_OTHER
      STILL_OPEN_OR_NO_EVENT

Plus a sanity rail at the bottom listing tickets with ENTRY but ZERO
subsequent ladder/close events -- those are the genuine
stuck/missed-TP candidates.

Run on VPS:
    python Scripts/diag_today_full_audit.py
or with a specific date:
    python Scripts/diag_today_full_audit.py 2026-05-07
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "Results" / "v30_live_events.log"
TRADES = ROOT / "Results" / "v30_live_trades.jsonl"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def parse_target_date(argv: list[str]) -> str:
    if len(argv) >= 2:
        return argv[1]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_jsonl(path: Path) -> list[dict]:
    """Robust JSONL reader. Skips blank/garbled lines."""
    if not path.exists():
        return []
    out: list[dict] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw or not raw.startswith("{"):
                continue
            try:
                out.append(json.loads(raw))
            except Exception:
                continue
    return out


def _row_date(row: dict) -> str:
    """Best-effort YYYY-MM-DD extraction from a row's timestamp field."""
    for k in ("ts_utc", "ts", "time", "timestamp", "open_time", "close_time"):
        v = row.get(k)
        if isinstance(v, str) and len(v) >= 10:
            return v[:10]
    return ""


def _row_time(row: dict) -> str:
    for k in ("ts_utc", "ts", "time", "timestamp", "open_time", "close_time"):
        v = row.get(k)
        if isinstance(v, str) and len(v) >= 19:
            return v[:19]
        if isinstance(v, str) and len(v) >= 10:
            return v
    return "?"


def _row_ticket(row: dict):
    for k in ("ticket", "open_ticket", "position_id", "deal_id"):
        v = row.get(k)
        if v is not None:
            try:
                return int(v)
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# loaders
# ---------------------------------------------------------------------------
def load_entries(target_day: str) -> list[dict]:
    """ENTRY rows from trades.jsonl filtered to target_day."""
    rows = _read_jsonl(TRADES)
    out: list[dict] = []
    for r in rows:
        if r.get("event") != "ENTRY":
            continue
        if _row_date(r) != target_day:
            continue
        out.append(r)
    return out


def load_events_indexed(target_day: str):
    """All event rows from events.log indexed by ticket AND by symbol."""
    rows = _read_jsonl(EVENTS)
    today_rows = [r for r in rows if _row_date(r) == target_day]
    by_ticket: dict[int, list[dict]] = defaultdict(list)
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    for r in today_rows:
        tk = _row_ticket(r)
        sym = r.get("symbol")
        if tk is not None:
            by_ticket[tk].append(r)
        if sym:
            by_symbol[str(sym)].append(r)
    return by_ticket, by_symbol, today_rows


# ---------------------------------------------------------------------------
# classify
# ---------------------------------------------------------------------------
LADDER_KINDS = {
    "TP1_PARTIAL", "TP2_PARTIAL", "TRAIL_SL", "BE_MOVE",
    "LAYER1_FIRED", "CLOSE", "POS_CLOSED_BY_BROKER",
    "FLATTEN_ALL", "SIZER_FEEDBACK",
}


def classify(events: list[dict]) -> str:
    kinds = [str(e.get("kind", "")) for e in events]
    reasons = [str(e.get("reason", "")).lower() for e in events]
    realised = None
    for e in events:
        if "realised_R" in e:
            try:
                realised = float(e["realised_R"])
            except Exception:
                pass

    has_tp1 = "TP1_PARTIAL" in kinds
    has_tp2 = "TP2_PARTIAL" in kinds
    has_trail = "TRAIL_SL" in kinds or any("trail" in r for r in reasons)
    has_layer1 = "LAYER1_FIRED" in kinds or any("layer1" in r for r in reasons)
    has_close = "CLOSE" in kinds or "POS_CLOSED_BY_BROKER" in kinds or "FLATTEN_ALL" in kinds

    if has_tp1 and has_tp2:
        return "TP1+TP2_HIT (full ladder)"
    if has_tp1 and has_trail:
        return "TP1_HIT_then_TRAIL"
    if has_tp1:
        return "TP1_HIT (still open or BE)"
    if has_layer1:
        return "LAYER1_CLOSE_AT_SL (real loss, not missed TP)"
    if has_close:
        if realised is not None and realised < 0:
            return f"SL_HIT (loss, R={realised:+.2f})"
        if realised is not None and realised >= 0:
            return f"CLOSED_OTHER (R={realised:+.2f})"
        return "CLOSED_OTHER"
    return "STILL_OPEN_OR_NO_EVENT"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    day = parse_target_date(sys.argv)
    entries = load_entries(day)
    events_by_ticket, events_by_symbol, all_events = load_events_indexed(day)

    print()
    print("=" * 96)
    print(f"  TODAY'S TRADE AUDIT - {day}")
    print(f"  events:  {EVENTS}")
    print(f"  trades:  {TRADES}")
    print("=" * 96)
    print(f"  total entries today      : {len(entries)}")
    print(f"  total events today       : {len(all_events)}")
    print(f"  unique tickets in events : {len(events_by_ticket)}")
    print()

    if not entries and not all_events:
        print("  No ENTRY rows AND no events for that date.")
        print("  -> Check the date (UTC) and that the bot was actually running.")
        return 0

    # Distinct event kinds today (helps spot odd/missing tags)
    kind_counts: dict[str, int] = defaultdict(int)
    for r in all_events:
        kind_counts[str(r.get("kind", "?"))] += 1
    if kind_counts:
        print("  event kinds today (count):")
        for k, c in sorted(kind_counts.items(), key=lambda kv: -kv[1]):
            print(f"     {k:<30}  {c}")
        print()

    if not entries:
        print("  No ENTRY rows in trades.jsonl for that date.")
        print("  (Events list above is from events.log directly.)")
        return 0

    # Per-entry detail
    print(f"  {'#':>2}  {'time':<19}  {'sym':<7}  {'side':<5}  "
          f"{'entry':>10}  {'SL':>10}  {'TP1':>10}  "
          f"{'tp1?':<5}  classification")
    print("  " + "-" * 94)

    summary: dict[str, int] = defaultdict(int)
    for i, ent in enumerate(entries, 1):
        sym = ent.get("symbol", "?")
        side = ent.get("side") or ent.get("dir") or "?"
        entry_px = ent.get("entry") or ent.get("intended_px") or ent.get("fill") or "?"
        sl_px = ent.get("SL") or ent.get("sl") or "?"
        tp1_px = ent.get("TP1") or ent.get("tp1") or "?"
        ts = _row_time(ent)
        tk = _row_ticket(ent)
        # Merge ticket-keyed events with symbol-keyed events that occurred
        # AFTER this entry (LAYER1_FIRED / SIZER_FEEDBACK lack a ticket
        # field but include symbol).
        per_ticket = events_by_ticket.get(tk, []) if tk is not None else []
        per_symbol = events_by_symbol.get(str(sym), [])
        # entry timestamp lower-bound (broker tz, but events are in UTC -- be permissive
        # and include any event that's not assigned to a *different* ticket)
        ticket_ids = {_row_ticket(r) for r in events_by_ticket}
        post_events = list(per_ticket)
        seen = {id(e) for e in post_events}
        for ev in per_symbol:
            if id(ev) in seen:
                continue
            ev_tk = _row_ticket(ev)
            if ev_tk is not None and ev_tk != tk:
                continue   # belongs to a different trade on the same symbol
            post_events.append(ev)
            seen.add(id(ev))
        # sort by ts
        post_events.sort(key=lambda r: _row_time(r))

        tp1_fired = any(str(e.get("kind", "")) == "TP1_PARTIAL" for e in post_events)
        klass = classify(post_events)
        summary[klass.split(" ")[0]] += 1

        def _fmt(v):
            try:
                return f"{float(v):>10.4f}"
            except Exception:
                return f"{str(v):>10.10}"

        print(f"  {i:>2}  {ts:<19}  {str(sym):<7}  {str(side):<5}  "
              f"{_fmt(entry_px)}  {_fmt(sl_px)}  {_fmt(tp1_px)}  "
              f"{'YES' if tp1_fired else 'no':<5}  {klass}")

        # Per-ticket event timeline
        if not post_events:
            print(f"          (no subsequent events for ticket={tk})")
        else:
            for ev in post_events:
                kind = str(ev.get("kind", "?"))
                ts2 = _row_time(ev)
                reason = ev.get("reason", "")
                extra_parts = []
                for k in ("realised_R", "pnl_approx", "close_px", "current_px",
                         "sl_trigger_px", "raw_slip_pts", "action", "lots_closed",
                         "new_sl"):
                    if k in ev and ev[k] is not None:
                        extra_parts.append(f"{k}={ev[k]}")
                extra = ("  " + "  ".join(extra_parts)) if extra_parts else ""
                rstr = f"  reason={reason}" if reason else ""
                print(f"          > {ts2}  {kind:<26}{rstr}{extra}")
        print()

    # Summary
    print("=" * 96)
    print("  SUMMARY")
    print("=" * 96)
    for k, v in sorted(summary.items(), key=lambda kv: -kv[1]):
        print(f"    {k:<28}  {v}")
    print()

    # Sanity rail: tickets with ENTRY but no ladder/close event
    no_ladder = []
    for ent in entries:
        tk = _row_ticket(ent)
        evs = events_by_ticket.get(tk, []) if tk is not None else []
        if not any(str(e.get("kind", "")) in LADDER_KINDS for e in evs):
            no_ladder.append((tk, ent.get("symbol"), _row_time(ent)))

    if no_ladder:
        print(f"  [WARN]  {len(no_ladder)} ticket(s) have ENTRY but NO subsequent ladder/close event:")
        for tk, sym, ts in no_ladder:
            print(f"          ticket={tk}  symbol={sym}  entry_time={ts}")
        print(f"          -> these are the genuinely stuck/missed-TP candidates.")
    else:
        print("  [OK]    Every ticket has at least one ladder/close event.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
