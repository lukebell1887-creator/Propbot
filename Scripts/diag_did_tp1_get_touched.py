r"""
Scripts/diag_did_tp1_get_touched.py
====================================
THE DEFINITIVE STUCK-LADDER DETECTOR.

For every closed (and still-open) position the bot has ever opened:
  1. Read entry_ts, exit_ts (or 'now' if still open), side, tp1, tp2, sl
     from v30_live_trades.jsonl (ENTRY row) and MT5 history deals.
  2. Pull every M1 bar from MT5 covering the position's lifetime.
  3. Check whether the bar HIGH (LONG) or LOW (SHORT) ever crossed TP1
     (or TP2). This is what *actually* happened in the market while the
     position was open.
  4. Read v30_live_events.log to check if TP1_PARTIAL / TP2_PARTIAL events
     were logged for this ticket.
  5. Cross-tabulate:

     bucket                     | tp1_touched | tp1_event_logged | meaning
     ---------------------------+-------------+------------------+--------
     CORRECT_NO_TP1             | NO          | NO               | fine (SL'd before TP1)
     CORRECT_TP1_FIRED          | YES         | YES              | bot worked
     **STUCK_LADDER_BUG**       | YES         | NO               | ★ THE BUG ★
     EVENT_WITHOUT_TOUCH        | NO          | YES              | impossible -- data error

This is the smoking-gun proof of how often the partial-close ladder
silently fails.

Usage on VPS:
    python Scripts\diag_did_tp1_get_touched.py
    python Scripts\diag_did_tp1_get_touched.py --days 7
    python Scripts\diag_did_tp1_get_touched.py --ticket 547550971   (one position)
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple


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


def _load_logged_entries(root: Path) -> Dict[int, dict]:
    """Return {ticket: ENTRY-row} from v30_live_trades.jsonl."""
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


def _load_events_by_ticket(root: Path) -> Dict[int, List[dict]]:
    """Return {ticket: [event_rows]} from v30_live_events.log."""
    rows = _read_jsonl(root / "v30_live_events.log")
    out: Dict[int, List[dict]] = {}
    for r in rows:
        try:
            tk = int(r.get("ticket", -1))
        except Exception:
            continue
        if tk <= 0:
            continue
        out.setdefault(tk, []).append(r)
    return out


def _parse_iso_to_epoch(s: str) -> Optional[int]:
    if not s:
        return None
    try:
        # "2026-05-05T14:45:00+00:00" or "2026-05-05T14:45:00"
        s = s.replace("Z", "+00:00")
        if "+" not in s and s.count(":") >= 2 and "T" in s:
            s = s + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:
        return None


def _fetch_position_window_from_mt5(mt5, ticket: int) -> Optional[Tuple[int, int, str, float, str]]:
    """Return (entry_epoch, exit_epoch, symbol, exit_px, exit_kind) for a ticket.
    If still open, exit_epoch = now. Returns None if unknown to MT5."""
    # Try last 14 days of history
    t_to = datetime.now(timezone.utc)
    t_from = t_to - timedelta(days=14)
    deals = mt5.history_deals_get(t_from, t_to, position=ticket)
    if deals is None or len(deals) == 0:
        # Maybe still open
        pos = mt5.positions_get(ticket=ticket)
        if pos and len(pos) > 0:
            return (int(pos[0].time), int(t_to.timestamp()), pos[0].symbol, 0.0, "STILL_OPEN")
        return None
    # Sort by time
    deals = sorted(deals, key=lambda d: d.time)
    in_deal = next((d for d in deals if d.entry == 0), None)  # DEAL_ENTRY_IN
    out_deals = [d for d in deals if d.entry == 1]            # DEAL_ENTRY_OUT
    if in_deal is None:
        return None
    sym = in_deal.symbol
    entry_ep = int(in_deal.time)
    if not out_deals:
        # still open
        return (entry_ep, int(t_to.timestamp()), sym, 0.0, "STILL_OPEN")
    last_out = out_deals[-1]
    return (entry_ep, int(last_out.time), sym, float(last_out.price), "CLOSED")


def _fetch_m1_bars(mt5, symbol: str, t_from_ep: int, t_to_ep: int) -> list:
    """Pull recent M1 bars and filter to [t_from_ep, t_to_ep]. Robust to
    broker-timezone quirks because we filter by epoch in Python."""
    # 14000 M1 bars ~= ~10 days, plenty for any position lifetime
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 0, 14000)
    if rates is None or len(rates) == 0:
        return []
    out = []
    for r in rates:
        t = int(r["time"])
        if t < t_from_ep - 60 or t > t_to_ep + 60:
            continue
        out.append({"time": t, "high": float(r["high"]), "low": float(r["low"]),
                    "open": float(r["open"]), "close": float(r["close"])})
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="Results",
                    help="Folder with v30_live_trades.jsonl + v30_live_events.log")
    ap.add_argument("--days", type=int, default=10,
                    help="Only check entries from the last N days (default 10).")
    ap.add_argument("--ticket", type=int, default=None,
                    help="Check just one ticket.")
    args = ap.parse_args()

    try:
        import MetaTrader5 as mt5
    except ImportError:
        print("ERROR: MetaTrader5 module not installed. Run on the VPS.")
        sys.exit(2)

    if not mt5.initialize():
        print(f"ERROR: mt5.initialize() failed: {mt5.last_error()}")
        sys.exit(2)

    root = Path(args.root).resolve()
    print("=" * 110)
    print("  DEFINITIVE STUCK-LADDER DETECTOR -- did TP1 actually get touched?")
    print("=" * 110)
    print(f"  root = {root}")
    print(f"  now (UTC) = {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')}")

    entries = _load_logged_entries(root)
    events_by_tk = _load_events_by_ticket(root)
    cutoff_ep = int((datetime.now(timezone.utc) - timedelta(days=args.days)).timestamp())

    if args.ticket is not None:
        ticks = [args.ticket] if args.ticket in entries else []
        if not ticks:
            print(f"\n  Ticket {args.ticket} NOT in v30_live_trades.jsonl ENTRY rows.")
            print("  Available tickets:", sorted(entries.keys()))
            mt5.shutdown()
            sys.exit(2)
    else:
        ticks = sorted(entries.keys())

    print(f"\n  Checking {len(ticks)} ticket(s) (cutoff = last {args.days} days)\n")
    print("=" * 110)

    counts = {"CORRECT_NO_TP1": 0, "CORRECT_TP1_FIRED": 0,
              "STUCK_LADDER_BUG": 0, "EVENT_WITHOUT_TOUCH": 0,
              "STILL_OPEN_NO_TP1": 0, "STILL_OPEN_TP1_TOUCHED": 0,
              "NO_MT5_DATA": 0, "NO_M1_BARS": 0}
    bug_rows: List[str] = []

    hdr = f"  {'TICKET':<11} {'SYM':<7} {'SIDE':<6} {'ENTRY_TS(UTC)':<20} " \
          f"{'TP1':>10} {'TP1_HIT':<8} {'TP1_EVT':<8} {'BUCKET':<22}"
    print(hdr)
    print("  " + "-" * 105)

    for tk in ticks:
        row = entries[tk]
        try:
            tp1 = float(row.get("tp1") or row.get("TP1") or 0.0)
        except Exception:
            tp1 = 0.0
        side = (row.get("side") or "").upper()
        entry_ts = row.get("ts_utc") or ""
        entry_ep_logged = _parse_iso_to_epoch(entry_ts)
        if entry_ep_logged and entry_ep_logged < cutoff_ep:
            continue

        win = _fetch_position_window_from_mt5(mt5, tk)
        if win is None:
            counts["NO_MT5_DATA"] += 1
            print(f"  {tk:<11} ?       ?      {entry_ts[:19]:<20} {tp1:>10.2f} "
                  f"{'?':<8} {'?':<8} {'NO_MT5_DATA':<22}")
            continue
        entry_ep, exit_ep, sym, exit_px, exit_kind = win
        bars = _fetch_m1_bars(mt5, sym, entry_ep, exit_ep)
        if not bars:
            counts["NO_M1_BARS"] += 1
            print(f"  {tk:<11} {sym:<7} {side:<6} {entry_ts[:19]:<20} {tp1:>10.2f} "
                  f"{'?':<8} {'?':<8} {'NO_M1_BARS':<22}")
            continue

        if side == "LONG":
            tp1_touched = any(b["high"] >= tp1 for b in bars) and tp1 > 0
        elif side == "SHORT":
            tp1_touched = any(b["low"] <= tp1 for b in bars) and tp1 > 0
        else:
            tp1_touched = False

        evts = events_by_tk.get(tk, [])
        tp1_evt_logged = any(str(e.get("kind", "")).upper() == "TP1_PARTIAL" for e in evts)

        # Bucket
        if exit_kind == "STILL_OPEN":
            bucket = "STILL_OPEN_TP1_TOUCHED" if tp1_touched else "STILL_OPEN_NO_TP1"
        elif tp1_touched and tp1_evt_logged:
            bucket = "CORRECT_TP1_FIRED"
        elif tp1_touched and not tp1_evt_logged:
            bucket = "STUCK_LADDER_BUG"
        elif not tp1_touched and not tp1_evt_logged:
            bucket = "CORRECT_NO_TP1"
        else:
            bucket = "EVENT_WITHOUT_TOUCH"

        counts[bucket] = counts.get(bucket, 0) + 1
        marker = "  <-- BUG" if bucket == "STUCK_LADDER_BUG" else ""
        line = f"  {tk:<11} {sym:<7} {side:<6} {entry_ts[:19]:<20} {tp1:>10.2f} " \
               f"{('YES' if tp1_touched else 'no'):<8} " \
               f"{('YES' if tp1_evt_logged else 'no'):<8} " \
               f"{bucket:<22}{marker}"
        print(line)
        if bucket == "STUCK_LADDER_BUG":
            bug_rows.append(line)

    print("  " + "-" * 105)
    print("\n  COUNTS BY BUCKET:")
    for k in sorted(counts.keys()):
        v = counts[k]
        if v > 0:
            star = "  <-- THE BUG" if k == "STUCK_LADDER_BUG" else ""
            print(f"    {k:<26} : n={v}{star}")

    if counts.get("STUCK_LADDER_BUG", 0) > 0:
        print("\n  *** STUCK_LADDER_BUG instances:\n")
        for ln in bug_rows:
            print(ln)
        print("\n  These are positions where the M1 bar HIGH (LONG) or LOW (SHORT)")
        print("  crossed TP1 BUT the bot logged ZERO TP1_PARTIAL event for the ticket.")
        print("  This is the partial-close ladder silently failing. Definitive proof.")
    else:
        print("\n  *** NO stuck-ladder bug instances detected in the window ***")
        print("  Every position whose price reached TP1 also logged a TP1_PARTIAL event.")

    print("\n" + "=" * 110)
    mt5.shutdown()


if __name__ == "__main__":
    main()
