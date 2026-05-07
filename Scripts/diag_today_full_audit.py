"""
diag_today_full_audit.py
========================
Reads Results/v30_live_events.log and prints, for EVERY entry today:
  - timestamp, symbol, side, entry, SL, TP1, TP2
  - every subsequent event for that ticket (TP1_PARTIAL, TP2_PARTIAL,
    TRAIL_SL, LAYER1_FIRED, CLOSE, BE_MOVE, RECONCILE_GRACE_SKIPPED, ...)
  - did TP1 get touched in the M1 bars (yes/no)
  - final classification:  TP1_HIT / SL_HIT / LAYER1_CLOSE / STILL_OPEN / NO_LADDER_EVENTS

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


def parse_target_date(argv: list[str]) -> str:
    if len(argv) >= 2:
        return argv[1]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def parse_events(target_day: str) -> tuple[list[dict], dict[int, list[dict]]]:
    """Return (entry_events, per_ticket_events)."""
    if not EVENTS.exists():
        print(f"[ERR] {EVENTS} not found - run on VPS, not locally.")
        sys.exit(2)

    entries: list[dict] = []
    per_ticket: dict[int, list[dict]] = defaultdict(list)

    with EVENTS.open(encoding="utf-8", errors="replace") as f:
        for raw in f:
            if "[event]" not in raw:
                continue
            try:
                head, _, body = raw.partition("[event]")
                ts_str = head.split()[0] + " " + head.split()[1]
                tag, _, payload = body.strip().partition(" ")
                obj = json.loads(payload) if payload.startswith("{") else {}
                if not obj:
                    continue
            except Exception:
                continue
            if not ts_str.startswith(target_day):
                continue
            tk = obj.get("ticket") or obj.get("position_id") or obj.get("open_ticket")
            obj["_ts"] = ts_str
            obj["_tag"] = tag
            if tag == "ENTRY":
                entries.append(obj)
            if tk:
                per_ticket[int(tk)].append(obj)
    return entries, per_ticket


def parse_trades(target_day: str) -> dict[int, dict]:
    out: dict[int, dict] = {}
    if not TRADES.exists():
        return out
    with TRADES.open(encoding="utf-8", errors="replace") as f:
        for raw in f:
            try:
                rec = json.loads(raw)
            except Exception:
                continue
            ts = rec.get("close_time") or rec.get("open_time") or ""
            if not str(ts).startswith(target_day):
                continue
            tk = rec.get("ticket") or rec.get("open_ticket")
            if tk:
                out[int(tk)] = rec
    return out


def classify(events: list[dict], trade: dict | None) -> str:
    tags = [e.get("_tag", "") for e in events]
    reasons = [str(e.get("reason", "")).lower() for e in events]
    if any("TP1_PARTIAL" in t for t in tags):
        if any("TP2_PARTIAL" in t for t in tags):
            return "TP1+TP2_HIT (full ladder)"
        if any("TRAIL_SL" in t or "trail" in r for t, r in zip(tags, reasons)):
            return "TP1_HIT_then_TRAIL"
        return "TP1_HIT (still open or BE)"
    if any("layer1" in r for r in reasons):
        # Layer 1 close - was it after SL or before TP1?
        return "LAYER1_CLOSE_AT_SL (real loss, not missed TP)"
    if any("CLOSE" in t for t in tags):
        if trade and trade.get("realised_R", 0) < 0:
            return "SL_HIT (loss)"
        return "CLOSED_OTHER"
    return "STILL_OPEN_OR_NO_EVENT"


def main() -> int:
    day = parse_target_date(sys.argv)
    entries, per_ticket = parse_events(day)
    trades = parse_trades(day)

    print()
    print("=" * 96)
    print(f"  TODAY'S TRADE AUDIT - {day}")
    print(f"  events: {EVENTS}   trades: {TRADES}")
    print("=" * 96)
    print(f"  total entries today: {len(entries)}")
    print(f"  unique tickets seen: {len(per_ticket)}")
    print()

    if not entries:
        print("  No ENTRY events found for that date.")
        return 0

    # Header
    print(f"  {'#':>2}  {'time':<19}  {'sym':<6}  {'side':<5}  "
          f"{'entry':>10}  {'SL':>10}  {'TP1':>10}  "
          f"{'tp1?':<5}  {'classification'}")
    print("  " + "-" * 94)

    for i, ent in enumerate(entries, 1):
        sym = ent.get("symbol", "?")
        side = ent.get("side") or ent.get("dir") or "?"
        entry_px = ent.get("entry") or ent.get("intended_px") or ent.get("fill")
        sl_px = ent.get("SL") or ent.get("sl")
        tp1_px = ent.get("TP1") or ent.get("tp1")
        ts = ent.get("_ts", "?")[:19]
        tk = ent.get("ticket") or ent.get("open_ticket") or 0
        post_events = per_ticket.get(int(tk), [])

        # Did any TP1_PARTIAL fire? did Layer1 fire? did broker close?
        tp1_fired = any(e.get("_tag", "").startswith("TP1") for e in post_events)
        klass = classify(post_events, trades.get(int(tk)))

        print(f"  {i:>2}  {ts:<19}  {str(sym):<6}  {str(side):<5}  "
              f"{entry_px!s:>10.10}  {sl_px!s:>10.10}  {tp1_px!s:>10.10}  "
              f"{'YES' if tp1_fired else 'no':<5}  {klass}")

        # Print every subsequent event for that ticket
        for ev in post_events:
            if ev.get("_tag") == "ENTRY":
                continue
            tag = ev.get("_tag", "?")
            ts2 = ev.get("_ts", "?")[:19]
            reason = ev.get("reason", "")
            extra = ""
            for k in ("realised_R", "pnl_approx", "close_px", "current_px",
                      "sl_trigger_px", "raw_slip_pts", "action"):
                if k in ev:
                    extra += f"  {k}={ev[k]}"
            print(f"          > {ts2}  {tag:<26}  {reason}{extra}")
        print()

    # Summary
    classes: dict[str, int] = defaultdict(int)
    for ent in entries:
        tk = int(ent.get("ticket") or ent.get("open_ticket") or 0)
        classes[classify(per_ticket.get(tk, []), trades.get(tk))] += 1

    print("=" * 96)
    print("  SUMMARY")
    print("=" * 96)
    for k, v in sorted(classes.items(), key=lambda kv: -kv[1]):
        print(f"    {k:<48}  {v}")
    print()

    # Sanity rails
    no_ladder = [k for k, v in per_ticket.items()
                 if not any(e.get("_tag", "").startswith(("TP1", "TP2", "TRAIL", "BE_MOVE",
                                                          "LAYER1", "CLOSE")) for e in v)]
    if no_ladder:
        print(f"  [WARN]  {len(no_ladder)} ticket(s) have ENTRY but NO subsequent ladder/close event:")
        print(f"          {no_ladder}")
        print(f"          -> these are the ones to investigate (potential stuck/missed TP)")
    else:
        print("  [OK]    Every ticket has at least one ladder/close event.")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
