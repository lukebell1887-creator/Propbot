"""
diag_layer1_slippage_today.py
=============================
Layer-1-focused audit. For every LAYER1_FIRED event today, prints the
decision-time data (sl_trigger_px, current_px, raw_slip_pts, action,
seconds_since_breach) plus the realised R from the matching
SIZER_FEEDBACK record. Also pulls entry slippage from
v30_live_slippage.jsonl.

The aim is to answer:
    1. Is Layer 1 firing only when price actually touches SL?
       (raw_slip_pts >= 0 at decision, current_px at/past sl_trigger_px)

    2. Is the realised R close to -1.0R (clean SL hit) or much worse
       (Layer 1's market-close slipped further than the broker stop
       would have)?

    3. Was sl_trigger_px equal to the original SL from entry, or had
       it been moved (BE / trail) -- this distinguishes "real SL"
       from a moved-stop close.

Run on VPS:
    python Scripts/diag_layer1_slippage_today.py
or with a date:
    python Scripts/diag_layer1_slippage_today.py 2026-05-07
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
SLIP = ROOT / "Results" / "v30_live_slippage.jsonl"


def parse_target_date(argv):
    if len(argv) >= 2:
        return argv[1]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _read_jsonl(path):
    if not path.exists():
        return []
    out = []
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


def _row_date(r):
    for k in ("ts_utc", "ts", "time", "timestamp"):
        v = r.get(k)
        if isinstance(v, str) and len(v) >= 10:
            return v[:10]
    return ""


def _row_time(r):
    for k in ("ts_utc", "ts", "time", "timestamp"):
        v = r.get(k)
        if isinstance(v, str):
            return v[:19]
    return "?"


def main():
    day = parse_target_date(sys.argv)
    events = [r for r in _read_jsonl(EVENTS) if _row_date(r) == day]
    entries = [r for r in _read_jsonl(TRADES)
               if r.get("event") == "ENTRY" and _row_date(r) == day]
    slip_rows = [r for r in _read_jsonl(SLIP) if _row_date(r) == day]

    print()
    print("=" * 96)
    print(f"  LAYER 1 SLIPPAGE AUDIT - {day}")
    print(f"  events:    {EVENTS}")
    print(f"  trades:    {TRADES}")
    print(f"  slippage:  {SLIP}")
    print("=" * 96)
    print(f"  total events today        : {len(events)}")
    print(f"  total entries today       : {len(entries)}")
    print(f"  total slippage rows today : {len(slip_rows)}")
    print()

    # Index entries and feedback by symbol (most recent first)
    entries_by_sym = defaultdict(list)
    for e in entries:
        entries_by_sym[str(e.get("symbol"))].append(e)

    layer1_events = [e for e in events if str(e.get("kind")) == "LAYER1_FIRED"]
    sizer_events = [e for e in events if str(e.get("kind")) == "SIZER_FEEDBACK"]
    close_events = [e for e in events if str(e.get("kind")) == "CLOSE"]
    block_events = [e for e in events if str(e.get("kind")) == "BLOCK_NOCHASE_COOLDOWN"]

    print(f"  LAYER1_FIRED        : {len(layer1_events)}")
    print(f"  SIZER_FEEDBACK      : {len(sizer_events)}")
    print(f"  CLOSE               : {len(close_events)}")
    print(f"  BLOCK_NOCHASE_COOLDOWN : {len(block_events)}")
    print()

    # ENTRY slippage table
    print("-" * 96)
    print("  ENTRY SLIPPAGE (v30_live_slippage.jsonl):")
    print("-" * 96)
    if not slip_rows:
        print("    (no rows today)")
    else:
        print(f"    {'time':<19}  {'sym':<7}  {'side':<5}  {'intend':>10}  {'fill':>10}  "
              f"{'slip_pts':>9}  {'lots':>6}")
        for r in slip_rows:
            ts = _row_time(r)
            sym = str(r.get("symbol", "?"))
            side = str(r.get("side", "?"))
            intent = r.get("intended_px") or r.get("requested_px") or "?"
            fill = r.get("fill_px") or r.get("actual_px") or "?"
            slip = r.get("slip_pts") or r.get("entry_slip_pts") or "?"
            lots = r.get("lots", "?")
            def f(v):
                try: return f"{float(v):>10.4f}"
                except: return f"{str(v):>10.10}"
            def fs(v):
                try: return f"{float(v):>9.3f}"
                except: return f"{str(v):>9.9}"
            print(f"    {ts:<19}  {sym:<7}  {side:<5}  {f(intent)}  {f(fill)}  "
                  f"{fs(slip)}  {str(lots):>6}")
    print()

    # Per-LAYER1_FIRED breakdown
    print("-" * 96)
    print("  LAYER 1 DECISIONS:")
    print("-" * 96)
    if not layer1_events:
        print("    (no LAYER1_FIRED today)")
    else:
        for i, ev in enumerate(layer1_events, 1):
            sym = str(ev.get("symbol"))
            ts = _row_time(ev)
            action = ev.get("action", "?")
            sl_trig = ev.get("sl_trigger_px")
            cur = ev.get("current_px")
            raw_slip = ev.get("raw_slip_pts")
            cap = ev.get("cap_pts")
            sec = ev.get("seconds_since_breach")
            reason = ev.get("reason", "")
            side = ev.get("side")

            # Find the matching ENTRY (most recent before this event for this sym)
            ent = None
            for e in entries_by_sym.get(sym, []):
                if _row_time(e) <= ts:
                    ent = e
            orig_sl = ent.get("SL") if ent else None
            orig_entry = ent.get("entry") if ent else None

            # Find matching SIZER_FEEDBACK after this layer1 fire (same symbol)
            fb = None
            for s in sizer_events:
                if str(s.get("symbol")) == sym and _row_time(s) >= ts:
                    fb = s
                    break

            print(f"    [{i}] {ts}  {sym:<7}  side={side}  action={action}")
            print(f"          sl_trigger_px        = {sl_trig}")
            print(f"          current_px (decision)= {cur}")
            print(f"          raw_slip_pts         = {raw_slip}    cap={cap}")
            print(f"          seconds_since_breach = {sec}")
            print(f"          original SL (entry)  = {orig_sl}")
            print(f"          original entry       = {orig_entry}")
            # Compare sl_trigger vs original SL: if different, BE/trail moved it
            if orig_sl is not None and sl_trig is not None:
                try:
                    diff = float(sl_trig) - float(orig_sl)
                    if abs(diff) < 1e-6:
                        sl_state = "SL UNCHANGED (original SL still in place)"
                    else:
                        sl_state = f"SL MOVED by {diff:+.4f} (BE/trail active)"
                    print(f"          SL drift             = {sl_state}")
                except Exception:
                    pass
            if fb is not None:
                rR = fb.get("realised_R")
                pnl = fb.get("pnl_approx")
                print(f"          realised_R           = {rR}    pnl_approx={pnl}")
                # Decision marker
                try:
                    rRf = float(rR)
                    if rRf <= -1.05:
                        verdict = "WORSE THAN -1R -- slippage exceeded SL distance"
                    elif rRf <= -0.95:
                        verdict = "CLEAN SL hit (~-1R)"
                    elif rRf < 0:
                        verdict = f"early close, partial loss {rRf:+.2f}R"
                    else:
                        verdict = f"closed at +{rRf:.2f}R"
                    print(f"          VERDICT              = {verdict}")
                except Exception:
                    pass
            else:
                print(f"          (no SIZER_FEEDBACK matched for this fire)")
            print(f"          reason: {reason}")
            print()

    # BLOCK_NOCHASE_COOLDOWN summary (cross-symbol cooldown that user suspects)
    print("-" * 96)
    print("  BLOCK_NOCHASE_COOLDOWN events (300s cross-symbol cooldown rejections):")
    print("-" * 96)
    if not block_events:
        print("    (none today)")
    else:
        for ev in block_events:
            ts = _row_time(ev)
            sym = ev.get("symbol", "?")
            blocked_by = ev.get("blocked_by", "?")
            seconds_left = ev.get("seconds_left") or ev.get("seconds_remaining") or "?"
            print(f"    {ts}  blocked={sym:<7} (waiting on {blocked_by}, {seconds_left}s left)")
    print()


if __name__ == "__main__":
    sys.exit(main() or 0)
