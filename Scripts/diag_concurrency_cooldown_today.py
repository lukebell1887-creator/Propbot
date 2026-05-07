r"""
diag_concurrency_cooldown_today.py
==================================
Answers the THREE questions you actually care about today:

  1. Did the 2-position concurrency cap block any entries?
  2. Did the 300 s cross-symbol no-chase cooldown block any entries?
  3. Did multiple symbols (3+) try to fire inside the same minute today,
     so that the cap + cooldown CHAIN (cap rejects #3, then cooldown blocks
     #1 and #2 once they close)?

It also reports, for every ENTRY today:
   - Was a TP1 set? (entry.tp1 > 0 AND ladder_armed in entry row)
   - Did the bot log a TP1_PARTIAL or TP2_PARTIAL event for this ticket?
   - Was the position closed by the bot (CLOSE / FLATTEN_ALL / LAYER1_FIRED)
     OR by the broker (POS_CLOSED_BY_BROKER) OR is it still open?
   - For STILL_OPEN tickets: run a quick MT5 M1-bar check -- has the bar
     high (LONG) / low (SHORT) crossed TP1 yet? If YES and TP1_PARTIAL was
     never logged, that's the same STUCK_LADDER_BUG signature as the
     dedicated detector.

Usage on VPS:
    cd C:\PropBot
    python Scripts\diag_concurrency_cooldown_today.py
    python Scripts\diag_concurrency_cooldown_today.py 2026-05-07
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
EVENTS = ROOT / "Results" / "v30_live_events.log"
TRADES = ROOT / "Results" / "v30_live_trades.jsonl"


# ---------------------------------------------------------------------------
def _read_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out: List[dict] = []
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
    for k in ("ts_utc", "ts", "time", "timestamp", "open_time", "close_time"):
        v = row.get(k)
        if isinstance(v, str) and len(v) >= 10:
            return v[:10]
    return ""


def _row_iso(row: dict) -> str:
    for k in ("ts_utc", "ts", "time", "timestamp", "open_time", "close_time"):
        v = row.get(k)
        if isinstance(v, str) and len(v) >= 19:
            return v[:19]
        if isinstance(v, str):
            return v
    return "?"


def _row_epoch(row: dict) -> Optional[int]:
    s = _row_iso(row)
    if not s or s == "?":
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        if "+" not in s2 and "T" in s2:
            s2 = s2 + "+00:00"
        return int(datetime.fromisoformat(s2).timestamp())
    except Exception:
        return None


def _row_ticket(row: dict) -> Optional[int]:
    for k in ("ticket", "open_ticket", "position_id"):
        v = row.get(k)
        if v is not None:
            try:
                return int(v)
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
def parse_target_date(argv: list[str]) -> str:
    if len(argv) >= 2:
        return argv[1]
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
def main() -> int:
    day = parse_target_date(sys.argv)
    cutoff_lo = day + "T00:00:00"
    cutoff_hi = day + "T23:59:59"

    all_events = _read_jsonl(EVENTS)
    all_trades = _read_jsonl(TRADES)

    today_events = [e for e in all_events if _row_date(e) == day]
    today_entries = [t for t in all_trades
                     if str(t.get("event", "")).upper() == "ENTRY"
                     and _row_date(t) == day]

    print()
    print("=" * 100)
    print(f"  CONCURRENCY + COOLDOWN AUDIT  --  {day} (UTC)")
    print(f"  events: {EVENTS}")
    print(f"  trades: {TRADES}")
    print("=" * 100)
    print(f"  events today  = {len(today_events)}")
    print(f"  entries today = {len(today_entries)}")
    print()

    # =====================================================================
    # 1. Cooldown blocks (BLOCK_NOCHASE_COOLDOWN events)
    # =====================================================================
    cooldown_blocks = [e for e in today_events
                       if str(e.get("kind", "")) == "BLOCK_NOCHASE_COOLDOWN"]
    print("-" * 100)
    print("  [1]  300 s NO-CHASE COOLDOWN BLOCKS today")
    print("-" * 100)
    if not cooldown_blocks:
        print("       (none)")
    else:
        print(f"  {'time (UTC)':<19}  {'blocked':<8}  {'because':<8}  "
              f"{'gap_s':>7}  {'cooldown_s':>10}")
        for e in cooldown_blocks:
            print(f"  {_row_iso(e):<19}  {str(e.get('symbol','?')):<8}  "
                  f"{str(e.get('blocked_by','?')):<8}  "
                  f"{e.get('gap_s', '?'):>7}  {e.get('cooldown_s', '?'):>10}")
        # Group by (blocker, blocked) pair to show the matrix
        pair_counts = Counter(
            (e.get("blocked_by", "?"), e.get("symbol", "?")) for e in cooldown_blocks
        )
        print()
        print("       Block matrix  (blocker -> blocked, count):")
        for (b, s), n in sorted(pair_counts.items(), key=lambda kv: -kv[1]):
            print(f"          {b:<8}  ->  {s:<8}  {n}")

    # =====================================================================
    # 2. Concurrency-cap reasoning (events.log doesn't tag this, so we
    #    *reconstruct* it: walk all ENTRY/CLOSE/POS_CLOSED_BY_BROKER/
    #    FLATTEN_ALL/LAYER1_FIRED events in time order, maintain an open-
    #    position-count, and flag any other ENTRY-attempt-blocking event
    #    -- in v30 the cap is silent, so the smoking-gun is "we had >=2
    #    open AND a BLOCK_NOCHASE_COOLDOWN event fired AT THE SAME MINUTE
    #    on a third symbol that wasn't already open").
    # =====================================================================
    print()
    print("-" * 100)
    print("  [2]  CONCURRENCY-CAP analysis  (max_concurrent_positions = 2)")
    print("-" * 100)

    open_now: Dict[str, dict] = {}    # symbol -> entry row
    timeline: List[dict] = []         # rolling snapshots
    # Build a unified, time-sorted timeline of entry/close events
    flow: List[Tuple[int, str, dict]] = []   # (epoch, kind_tag, row)
    for t in today_entries:
        ep = _row_epoch(t)
        if ep is not None:
            flow.append((ep, "ENTRY", t))
    for e in today_events:
        kind = str(e.get("kind", ""))
        if kind in ("CLOSE", "POS_CLOSED_BY_BROKER", "FLATTEN_ALL",
                    "LAYER1_FIRED", "TP2_PARTIAL"):
            ep = _row_epoch(e)
            if ep is not None:
                flow.append((ep, kind, e))
    flow.sort(key=lambda x: x[0])

    # Tracking
    cap_hits: List[str] = []
    sym_open_count = 0
    open_syms: set = set()
    print(f"  {'time (UTC)':<19}  {'event':<22}  {'symbol':<8}  "
          f"{'open_after':>10}  {'open_set':<25}")
    for ep, tag, row in flow:
        sym = str(row.get("symbol", "?"))
        ts = _row_iso(row)
        if tag == "ENTRY":
            open_syms.add(sym)
        elif tag in ("CLOSE", "POS_CLOSED_BY_BROKER", "FLATTEN_ALL"):
            open_syms.discard(sym)
        # LAYER1_FIRED is a soft event -- the bot still flattens via CLOSE
        # so we don't double-decrement here.
        sym_open_count = len(open_syms)
        print(f"  {ts:<19}  {tag:<22}  {sym:<8}  "
              f"{sym_open_count:>10}  {sorted(open_syms)}")

    # Find candidates that were blocked specifically because the cap was full.
    # Heuristic: BLOCK_NOCHASE_COOLDOWN fires AFTER the cap check, so
    # cap_blocks_inferred = cooldown blocks that hit while open_set already
    # had 2 distinct symbols at that time.
    print()
    cap_inferred = 0
    for cb in cooldown_blocks:
        ep = _row_epoch(cb)
        if ep is None:
            continue
        # Replay open set up to this epoch
        s = set()
        for ep2, tag, row in flow:
            if ep2 > ep:
                break
            sym = str(row.get("symbol", "?"))
            if tag == "ENTRY":
                s.add(sym)
            elif tag in ("CLOSE", "POS_CLOSED_BY_BROKER", "FLATTEN_ALL"):
                s.discard(sym)
        if len(s) >= 2:
            cap_inferred += 1
    print(f"  [inferred] cooldown-blocks that ALSO hit while >=2 positions open: {cap_inferred}")
    print(f"             (these would have been double-blocked: cap *and* cooldown)")

    # =====================================================================
    # 3. Time-cluster: how many ENTRIES landed within the same 1-minute window
    # =====================================================================
    print()
    print("-" * 100)
    print("  [3]  ENTRY time-clusters (3+ symbols within 60 s = the v30 chain risk)")
    print("-" * 100)
    minute_buckets: Dict[str, List[str]] = defaultdict(list)
    for t in today_entries:
        ts = _row_iso(t)
        if ts == "?":
            continue
        bucket = ts[:16]   # YYYY-MM-DDTHH:MM
        minute_buckets[bucket].append(str(t.get("symbol", "?")))
    multi = {k: v for k, v in minute_buckets.items() if len(v) >= 2}
    if not multi:
        print("       (no minute had 2+ entries; no chain risk today)")
    else:
        for bucket, syms in sorted(multi.items()):
            tag = "  *** 3+ SIMULTANEOUS ***" if len(syms) >= 3 else ""
            print(f"       {bucket}   {len(syms)} entries: {syms}{tag}")

    # Also show the broader 5-minute window so we can see "DE40 + US30 + XAUUSD
    # all fired between 13:30 and 13:35"
    print()
    print("       5-minute clusters:")
    minute_buckets5: Dict[str, List[str]] = defaultdict(list)
    for t in today_entries:
        ts = _row_iso(t)
        if ts == "?":
            continue
        # round down to nearest 5 min
        try:
            dt = datetime.fromisoformat(ts.replace("Z", ""))
        except Exception:
            continue
        bucket = dt.strftime("%Y-%m-%dT%H:") + f"{(dt.minute // 5) * 5:02d}"
        minute_buckets5[bucket].append(str(t.get("symbol", "?")))
    multi5 = {k: v for k, v in minute_buckets5.items() if len(v) >= 2}
    if not multi5:
        print("           (no 5-min window had 2+ entries)")
    else:
        for bucket, syms in sorted(multi5.items()):
            tag = "  *** 3+ in 5 min ***" if len(syms) >= 3 else ""
            print(f"           {bucket}   {len(syms)} entries: {syms}{tag}")

    # =====================================================================
    # 4. Per-entry TP/ladder status   (the "did my TP fire?" question)
    # =====================================================================
    print()
    print("-" * 100)
    print("  [4]  PER-ENTRY TP / LADDER STATUS")
    print("-" * 100)
    if not today_entries:
        print("       (no entries today)")
    else:
        events_by_ticket: Dict[int, List[dict]] = defaultdict(list)
        for e in today_events:
            tk = _row_ticket(e)
            if tk is not None:
                events_by_ticket[tk].append(e)

        print(f"  {'time':<19}  {'sym':<7}  {'side':<5}  {'ticket':>11}  "
              f"{'entry':>10}  {'TP1':>10}  {'TP2':>10}  "
              f"{'TP1?':<5} {'TP2?':<5} {'closed?':<8}  outcome")
        print("  " + "-" * 96)
        for t in today_entries:
            ts = _row_iso(t)
            sym = str(t.get("symbol", "?"))
            side = str(t.get("side") or t.get("dir") or "?")
            tk = _row_ticket(t)
            entry_px = t.get("entry") or t.get("intended_px") or t.get("fill") or t.get("fill_px")
            tp1 = t.get("tp1") or t.get("TP1") or 0.0
            tp2 = t.get("tp2") or t.get("TP2") or 0.0

            evs = events_by_ticket.get(tk, []) if tk is not None else []
            kinds = {str(e.get("kind", "")) for e in evs}
            tp1_fired = "TP1_PARTIAL" in kinds
            tp2_fired = "TP2_PARTIAL" in kinds
            closed = bool(kinds & {"CLOSE", "POS_CLOSED_BY_BROKER", "FLATTEN_ALL"})

            if tp2_fired:
                outcome = "TP2 (full ladder)"
            elif tp1_fired and "TRAIL_SL" in kinds:
                outcome = "TP1 + trail"
            elif tp1_fired:
                outcome = "TP1 only"
            elif "LAYER1_FIRED" in kinds:
                outcome = "Layer1 stop"
            elif closed:
                outcome = "closed (no TP)"
            else:
                outcome = "*** STILL OPEN ***"

            def _fmt(v):
                try:
                    return f"{float(v):>10.4f}"
                except Exception:
                    return f"{'?':>10}"

            print(f"  {ts:<19}  {sym:<7}  {side:<5}  {str(tk):>11}  "
                  f"{_fmt(entry_px)}  {_fmt(tp1)}  {_fmt(tp2)}  "
                  f"{('YES' if tp1_fired else 'no'):<5} "
                  f"{('YES' if tp2_fired else 'no'):<5} "
                  f"{('YES' if closed else 'no'):<8}  {outcome}")

    # =====================================================================
    # 5. Counters from the bot's own log (definitive numbers, no inference)
    # =====================================================================
    print()
    print("-" * 100)
    print("  [5]  COUNTER ROLLUP from today's events.log")
    print("-" * 100)
    counters = Counter(str(e.get("kind", "?")) for e in today_events)
    for k, n in counters.most_common():
        print(f"       {k:<32}  {n}")

    # =====================================================================
    # 6. STILL-OPEN tickets:  did TP1 already touch on broker M1 bars?
    # =====================================================================
    still_open: List[dict] = []
    if today_entries:
        events_by_ticket = defaultdict(list)
        for e in today_events:
            tk = _row_ticket(e)
            if tk is not None:
                events_by_ticket[tk].append(e)
        for t in today_entries:
            tk = _row_ticket(t)
            evs = events_by_ticket.get(tk, []) if tk is not None else []
            kinds = {str(e.get("kind", "")) for e in evs}
            if not (kinds & {"CLOSE", "POS_CLOSED_BY_BROKER", "FLATTEN_ALL", "TP2_PARTIAL"}):
                still_open.append(t)

    print()
    print("-" * 100)
    print(f"  [6]  STILL-OPEN tickets ({len(still_open)})  --  did TP1 already touch?")
    print("-" * 100)
    if not still_open:
        print("       (none)")
    else:
        try:
            import MetaTrader5 as mt5
        except ImportError:
            print("       MetaTrader5 module not installed -- cannot check broker M1 bars.")
            print("       Install on VPS or run on the trading box. Skipping the touch test.")
            mt5 = None  # type: ignore

        if mt5 is not None and mt5.initialize():
            for t in still_open:
                tk = _row_ticket(t)
                sym_bot = str(t.get("symbol", "?")).upper()
                side = str(t.get("side") or t.get("dir") or "?").upper()
                ts = _row_iso(t)
                ep_in = _row_epoch(t)
                tp1 = float(t.get("tp1") or t.get("TP1") or 0.0)
                tp2 = float(t.get("tp2") or t.get("TP2") or 0.0)

                # Resolve broker symbol
                broker_sym = None
                aliases = {
                    "DE40":   ["DE40", "DAX40", "GER40", "DE30", "DE40.cash", "GER40.cash", "DAX"],
                    "US500":  ["US500", "SP500", "SPX500", "US500.cash", "SP500.cash", "SPX"],
                    "US30":   ["US30", "DJ30", "DJI30", "US30.cash", "DJ30.cash", "DJIA"],
                    "XAUUSD": ["XAUUSD", "GOLD", "XAUUSD.raw", "XAU/USD"],
                    "NAS100": ["NAS100", "US100", "NAS100.cash", "USTEC", "NDX"],
                }
                for cand in aliases.get(sym_bot, [sym_bot]):
                    if mt5.symbol_info(cand) is not None:
                        broker_sym = cand
                        break

                bars: list = []
                if broker_sym:
                    rates = mt5.copy_rates_from_pos(broker_sym, mt5.TIMEFRAME_M1, 0, 14000)
                    now_ep = int(datetime.now(timezone.utc).timestamp())
                    if rates is not None:
                        for r in rates:
                            t_ep = int(r["time"])
                            if ep_in is not None and t_ep < ep_in - 60:
                                continue
                            if t_ep > now_ep + 60:
                                continue
                            bars.append((t_ep, float(r["high"]), float(r["low"])))

                if not bars:
                    print(f"  ticket={tk}  sym={sym_bot}  no M1 bars (broker_sym={broker_sym})")
                    continue

                if side == "LONG":
                    tp1_hit = any(h >= tp1 for _, h, _ in bars) and tp1 > 0
                    tp2_hit = any(h >= tp2 for _, h, _ in bars) and tp2 > 0
                    extreme = max(h for _, h, _ in bars)
                    extreme_label = "max_high"
                else:
                    tp1_hit = any(l <= tp1 for _, _, l in bars) and tp1 > 0
                    tp2_hit = any(l <= tp2 for _, _, l in bars) and tp2 > 0
                    extreme = min(l for _, _, l in bars)
                    extreme_label = "min_low"

                verdict = ""
                if tp1_hit and tp2_hit:
                    verdict = "*** BOTH TP1 AND TP2 ALREADY TOUCHED -- bot SHOULD have ladder-closed ***"
                elif tp1_hit:
                    verdict = "*** TP1 ALREADY TOUCHED -- bot SHOULD have logged TP1_PARTIAL ***"
                else:
                    verdict = "TP1 not yet touched (bot is correct to keep position open)"

                print(f"  ticket={tk}  sym={sym_bot}  side={side}  entry_ts={ts}")
                print(f"     TP1={tp1:.4f}  TP2={tp2:.4f}  "
                      f"{extreme_label}={extreme:.4f}   "
                      f"tp1_touched={'YES' if tp1_hit else 'no'}  "
                      f"tp2_touched={'YES' if tp2_hit else 'no'}")
                print(f"     {verdict}")
                print()
            mt5.shutdown()
        elif mt5 is not None:
            print(f"       mt5.initialize() failed: {mt5.last_error()}")

    print()
    print("=" * 100)
    print("  END OF AUDIT")
    print("=" * 100)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
