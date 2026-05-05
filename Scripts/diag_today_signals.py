r"""
Scripts/diag_today_signals.py
=============================
VPS diagnostic -- answer "did the bot TP when it should have?"

WHY THIS EXISTS
---------------
2026-05-05: 3 ORB signals fired ~the same time, only 2 entered (concurrency
cap = 2), and at least one open position never auto-closed at TP -- the
operator had to manually take profit.

Possible suspects (in order of likelihood):
  1. The 3rd signal was blocked by either the 2-concurrent cap OR the 300s
     cross-symbol no-chase cooldown -- that's BY DESIGN, not a bug, but we
     need to confirm WHICH gate fired and on which symbol.
  2. The TP1/TP2 partial-close ladder lives in `LiveSymbolState.partial_state`
     which is **NOT persisted to disk**. If the bot crashed / restarted while
     a position was open, the broker still holds the position (with the
     emergency Layer-1 SL) but TP=0.0 at the broker -- the in-bar manager is
     gone, TPs never fire, position only closes on broker-SL hit (or manual).
  3. The PartialCloseManager only fires on **closed** M1 bars (`_poll_new_bars`
     skips bars where `bar.time <= last_bar_time`). On a slow/spikey M1 bar
     that touches TP1 mid-bar but closes back inside the range, the cross
     IS detected as soon as the bar closes (bar_high >= tp1), but if the
     5-second poller missed that bar (broker `get_history` failure / network
     blip), the TP firing is delayed until the next successful poll.
  4. `bridge.close_position(ticket, lots=...)` returned False (broker reject)
     and `_safe_close` set tp1_hit=False so the engine retries every M1 bar.
     If that's silently failing every retry the position is "stuck open".

WHAT THIS SCRIPT DOES
---------------------
Reads three JSON-lines files written by the live runner:

    Results/v30_live_trades.jsonl     - every ENTRY (intended SL/TP1/TP2/ticket)
    Results/v30_live_events.log       - TP1_PARTIAL, TP2_PARTIAL, TRAIL_SL,
                                        CLOSE, POS_CLOSED_BY_BROKER, FLATTEN_ALL,
                                        BLOCK_NOCHASE_COOLDOWN, LAYER1_FIRED,
                                        ORDER_FAILED, START, STOP, ...
    Results/v30_live_slippage.jsonl   - per-entry signed slip (cosmetic)

Plus one snapshot:

    Results/heartbeat_v30.json        - latest open positions + counters

Filters everything to today (UTC by default; pass --date YYYY-MM-DD to override
or --tail N to look at last N minutes of wallclock instead) and produces:

    1. ENTRIES TIMELINE       - one row per entry with:
                                  ticket, symbol, side, entry, SL, TP1, TP2, lots
                                  events that followed (TP1@HH:MM:SS, TP2@..., etc.)
                                  classification (NORMAL, STUCK, MANUAL_CLOSE, ...)
                                  lag from entry to first TP event

    2. CONCURRENCY GATE       - did the cap=2 actually fire today? counts both
                                blocks and the entries that survived

    3. NO-CHASE COOLDOWN      - which symbol blocked which, with the gap, the
                                cooldown setting, and the unblock time

    4. CURRENT OPEN POSITIONS - from heartbeat_v30.json: ticket, side, lots,
                                current SL (after BE move / trail), held seconds

    5. SUSPECT POSITIONS      - any entry today with NO TP/CLOSE event AND not
                                listed in heartbeat as open  --> "ghost" trade
                                (bot lost state but broker still holds it)
                                AND any entry that's still open with held > 90 min
                                and no TP1_PARTIAL event  --> stuck ladder

    6. RAIL COUNTERS DIFF     - START vs latest counters: how many
                                block_nochase_cooldown, block_concurrent_cap,
                                block_news_entry, block_halt_4pct happened today

Usage on VPS (PowerShell, from C:\PropBot or wherever the bot lives):
    python Scripts\diag_today_signals.py
    python Scripts\diag_today_signals.py --date 2026-05-05
    python Scripts\diag_today_signals.py --tail 90        # only last 90 min
    python Scripts\diag_today_signals.py --root D:\bot\Results

If MetaTrader5 is importable AND --check-prices is passed the script will also
pull the M1 high/low for each open / suspect position since its entry time and
flag positions where (bar_high >= TP1 for LONG / bar_low <= TP1 for SHORT) yet
no TP1_PARTIAL event was logged -- that's the smoking gun for "TP didn't fire".

Exit code is always 0 unless invocation was malformed (so cron can grep stdout).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Pretty-printing helpers (no third-party deps; works in stock cmd / PowerShell)
# ---------------------------------------------------------------------------
def _hr(ch: str = "=", n: int = 78) -> str:
    return ch * n


def _hdr(title: str) -> None:
    print()
    print(_hr("="))
    print(f"  {title}")
    print(_hr("="))


def _sub(title: str) -> None:
    print()
    print(_hr("-"))
    print(f"  {title}")
    print(_hr("-"))


def _fmt_dt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_hms(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%H:%M:%S")


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    s = s.strip()
    try:
        # Accept the trailing 'Z' that some loggers emit
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Robust JSONL reader: skips blank / malformed lines without crashing
# ---------------------------------------------------------------------------
def _read_jsonl(path: Path) -> List[dict]:
    out: List[dict] = []
    if not path.exists():
        return out
    n_bad = 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                out.append(json.loads(ln))
            except json.JSONDecodeError:
                n_bad += 1
                continue
    if n_bad:
        print(f"  [warn] {path.name}: skipped {n_bad} malformed line(s)")
    return out


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [warn] {path.name}: failed to parse ({e})")
        return None


# ---------------------------------------------------------------------------
# Day filter
# ---------------------------------------------------------------------------
def _row_dt(row: dict) -> Optional[datetime]:
    """Extract the row's timestamp from any of the known field names."""
    for key in ("ts_utc", "time", "ts", "timestamp_utc"):
        if key in row and row[key]:
            dt = _parse_iso(str(row[key]))
            if dt is not None:
                return dt
    return None


def _filter_window(rows: Iterable[dict],
                   start: datetime,
                   end: datetime) -> List[dict]:
    out: List[dict] = []
    for r in rows:
        dt = _row_dt(r)
        if dt is None:
            continue
        if start <= dt < end:
            out.append(r)
    out.sort(key=lambda r: _row_dt(r) or datetime.min.replace(tzinfo=timezone.utc))
    return out


# ---------------------------------------------------------------------------
# Per-ticket reducer: walk events in time order and build a structured record
# ---------------------------------------------------------------------------
def _reduce_by_ticket(entries: List[dict],
                      events: List[dict]) -> Dict[int, dict]:
    """
    Build {ticket: {entry, side, sl, tp1, tp2, lots, symbol, events:[...], close_*, classification}}

    Entries come from v30_live_trades.jsonl (event=ENTRY).
    Events come from v30_live_events.log (TP1_PARTIAL, TP2_PARTIAL, TRAIL_SL,
        CLOSE, POS_CLOSED_BY_BROKER, LAYER1_FIRED, FLATTEN_ALL, ...).
    """
    by_ticket: Dict[int, dict] = {}

    for e in entries:
        if str(e.get("event", "")).upper() != "ENTRY":
            continue
        tk = e.get("ticket")
        if tk is None:
            continue
        try:
            tk = int(tk)
        except (TypeError, ValueError):
            continue
        by_ticket[tk] = {
            "ticket":   tk,
            "symbol":   e.get("symbol"),
            "side":     e.get("side"),
            "entry_ts": _row_dt(e),
            "fill_px":  e.get("fill_px"),
            "intended": e.get("intended_px"),
            "sl":       e.get("sl"),
            "tp1":      e.get("tp1"),
            "tp2":      e.get("tp2"),
            "lots":     e.get("lots"),
            "risk_usd": e.get("risk_usd"),
            "or_range": e.get("or_range"),
            "events":   [],            # filled below
            "close_ts": None,
            "close_reason": None,
            "tp1_ts":   None,
            "tp2_ts":   None,
            "trail_moves": 0,
            "layer1":   None,
            "classification": None,    # set in _classify
        }

    # FLATTEN_ALL / DAY_HALTED_4PCT / TOTAL_DD_BREAKER_8PCT do not carry a
    # ticket -- they affect ALL open positions. We collect them separately and
    # apply them as an "umbrella close" if a ticket has no other close event.
    umbrella_closes: List[Tuple[datetime, str]] = []

    for ev in events:
        kind = str(ev.get("kind", "")).upper()
        ts = _row_dt(ev)
        if ts is None:
            continue
        # Per-ticket events carry a 'ticket' field (set explicitly in _log_event
        # calls inside v30_live.py -- TP1_PARTIAL, TP2_PARTIAL, TRAIL_SL, CLOSE,
        # POS_CLOSED_BY_BROKER). LAYER1_FIRED uses 'ticket' too.
        tk = ev.get("ticket")
        if tk is not None:
            try:
                tk_i = int(tk)
            except (TypeError, ValueError):
                tk_i = None
            if tk_i is not None and tk_i in by_ticket:
                rec = by_ticket[tk_i]
                rec["events"].append((ts, kind, ev))
                if kind == "TP1_PARTIAL" and rec["tp1_ts"] is None:
                    rec["tp1_ts"] = ts
                elif kind == "TP2_PARTIAL" and rec["tp2_ts"] is None:
                    rec["tp2_ts"] = ts
                elif kind == "TRAIL_SL":
                    rec["trail_moves"] += 1
                elif kind == "LAYER1_FIRED":
                    rec["layer1"] = ev
                elif kind in ("CLOSE", "POS_CLOSED_BY_BROKER"):
                    if rec["close_ts"] is None:
                        rec["close_ts"] = ts
                        rec["close_reason"] = ev.get("reason") or kind
                continue
        # Symbol-keyed but ticketless events (e.g. SIZER_FEEDBACK after close,
        # BLOCK_NOCHASE_COOLDOWN -- handled separately).
        if kind in ("FLATTEN_ALL", "DAY_HALTED_4PCT", "DAY_HALTED",
                    "TOTAL_DD_BREAKER_8PCT"):
            umbrella_closes.append((ts, f"{kind}:{ev.get('reason', '')}"))

    # Apply umbrella closes to any ticket that opened before the umbrella event
    # and has no per-ticket close yet.
    for ts, reason in umbrella_closes:
        for rec in by_ticket.values():
            if rec["close_ts"] is None and rec["entry_ts"] and rec["entry_ts"] <= ts:
                rec["close_ts"] = ts
                rec["close_reason"] = reason

    return by_ticket


def _classify(rec: dict, now_utc: datetime,
              still_open_tickets: set) -> str:
    if rec["ticket"] in still_open_tickets:
        if rec["tp1_ts"] is None:
            held_min = (now_utc - rec["entry_ts"]).total_seconds() / 60 \
                if rec["entry_ts"] else 0.0
            return f"OPEN_NO_TP1 (held {held_min:.0f}m)"
        return "OPEN_AFTER_TP1"
    if rec["close_ts"] is None:
        return "GHOST (entry logged, no close event AND not in heartbeat)"
    cr = (rec["close_reason"] or "").lower()
    if rec["tp2_ts"] and rec["tp1_ts"]:
        return "CLOSED_FULL_LADDER (TP1+TP2+trail)"
    if rec["tp1_ts"] and not rec["tp2_ts"]:
        if "broker" in cr or "snap_sl" in cr or "self_close" in cr:
            return "CLOSED_TP1_THEN_BE (broker SL at break-even after TP1)"
        return "CLOSED_AFTER_TP1 (no TP2)"
    if "news" in cr:
        return "FLATTENED_NEWS"
    if "window" in cr:
        return "TIME_STOP (window expiry)"
    if "halt" in cr or "breaker" in cr:
        return "KILLED_BY_DD_RAIL"
    if "layer1" in cr:
        return "LAYER1_INTERCEPT (slip cap)"
    if "broker" in cr:
        return "CLOSED_AT_SL (broker SL hit)"
    return f"CLOSED ({cr or 'unknown'})"


# ---------------------------------------------------------------------------
# Optional MT5 spot-check: did price actually trade through TP1 / TP2?
# ---------------------------------------------------------------------------
def _check_prices_via_mt5(records: List[dict],
                          symbol_to_broker: Dict[str, str]) -> None:
    """If MetaTrader5 is importable, pull M1 bars from broker side for each
    record's symbol from entry_ts -> now and flag any that have:
        - high >= tp1 (LONG) / low <= tp1 (SHORT) but no TP1_PARTIAL event
        - high >= tp2 (LONG) / low <= tp2 (SHORT) but no TP2_PARTIAL event
    This is the smoking gun for the "TP didn't fire" complaint."""
    try:
        import MetaTrader5 as mt5  # noqa: F401
    except Exception as e:
        print(f"  MetaTrader5 not importable on this host ({e}); skipping price check")
        return
    if not mt5.initialize():
        print(f"  mt5.initialize() failed: {mt5.last_error()}; skipping price check")
        return
    print("  Pulling broker M1 bars to verify TP touches ...")
    print(f"  {'TICKET':>10} {'SYM':<6} {'SIDE':<5} {'TP1':>10} "
          f"{'HIT_TP1?':<9} {'TP2':>10} {'HIT_TP2?':<9} {'BAR_HIT_TS':<19}")
    for rec in records:
        if rec["entry_ts"] is None or rec["tp1"] is None:
            continue
        broker_sym = symbol_to_broker.get(rec["symbol"], rec["symbol"])
        # Pull from entry_ts -> now+1m, M1.
        from_dt = rec["entry_ts"] - timedelta(minutes=1)
        to_dt = datetime.now(timezone.utc) + timedelta(minutes=1)
        try:
            bars = mt5.copy_rates_range(broker_sym, mt5.TIMEFRAME_M1,
                                        from_dt, to_dt)
        except Exception as e:
            print(f"  [{rec['ticket']}] copy_rates_range failed: {e}")
            continue
        if bars is None or len(bars) == 0:
            print(f"  [{rec['ticket']}] {broker_sym}: 0 M1 bars returned")
            continue
        side = (rec["side"] or "").upper()
        tp1 = float(rec["tp1"])
        tp2 = float(rec["tp2"]) if rec["tp2"] is not None else None
        hit_tp1_ts = None
        hit_tp2_ts = None
        for b in bars:
            bt = datetime.fromtimestamp(int(b["time"]), tz=timezone.utc)
            if side == "LONG":
                if hit_tp1_ts is None and float(b["high"]) >= tp1:
                    hit_tp1_ts = bt
                if tp2 is not None and hit_tp2_ts is None and float(b["high"]) >= tp2:
                    hit_tp2_ts = bt
            elif side == "SHORT":
                if hit_tp1_ts is None and float(b["low"]) <= tp1:
                    hit_tp1_ts = bt
                if tp2 is not None and hit_tp2_ts is None and float(b["low"]) <= tp2:
                    hit_tp2_ts = bt
        flag1 = ("YES" if hit_tp1_ts else "no")
        flag2 = ("YES" if hit_tp2_ts else "no")
        # Highlight discrepancies
        if hit_tp1_ts and rec["tp1_ts"] is None:
            flag1 += "*"   # price touched TP1 but bot logged no TP1_PARTIAL
        if hit_tp2_ts and rec["tp2_ts"] is None:
            flag2 += "*"
        ts_str = _fmt_dt(hit_tp1_ts) if hit_tp1_ts else "-"
        print(f"  {rec['ticket']:>10d} {str(rec['symbol']):<6} {side:<5} "
              f"{tp1:>10.2f} {flag1:<9} "
              f"{(tp2 if tp2 is not None else 0):>10.2f} {flag2:<9} {ts_str}")
    print("  (* = price touched the level but bot did NOT log the corresponding "
          "TP_PARTIAL event -- strong evidence of a stuck ladder)")
    try:
        mt5.shutdown()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default="Results",
                    help="directory holding v30_live_*.{log,jsonl,json} (default: Results)")
    ap.add_argument("--date", default=None,
                    help="YYYY-MM-DD (UTC) to inspect; default = today UTC")
    ap.add_argument("--tail", type=int, default=0,
                    help="if >0, ignore --date and inspect last N minutes wallclock")
    ap.add_argument("--check-prices", action="store_true",
                    help="ALSO pull M1 bars from MT5 to verify TP touches "
                         "(needs MetaTrader5 python pkg + running terminal)")
    ap.add_argument("--all-tickets", action="store_true",
                    help="show classification table for every ticket, not just "
                         "suspects (default: suspect-only)")
    args = ap.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"[err] --root path not found: {root.resolve()}")
        return 2

    # ------------------------------------------------------------------ window
    now_utc = datetime.now(timezone.utc)
    if args.tail > 0:
        win_start = now_utc - timedelta(minutes=args.tail)
        win_end = now_utc + timedelta(seconds=1)
        win_label = f"last {args.tail} min  ({_fmt_dt(win_start)} -> now)"
    else:
        if args.date:
            try:
                d = datetime.strptime(args.date, "%Y-%m-%d").date()
            except ValueError:
                print(f"[err] --date must be YYYY-MM-DD, got {args.date!r}")
                return 2
        else:
            d = now_utc.date()
        win_start = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        win_end = win_start + timedelta(days=1)
        win_label = f"UTC day {d.isoformat()}"

    # ------------------------------------------------------------------ load
    trades_path    = root / "v30_live_trades.jsonl"
    events_path    = root / "v30_live_events.log"
    slippage_path  = root / "v30_live_slippage.jsonl"
    heartbeat_path = root / "heartbeat_v30.json"
    telemetry_path = root / "v30_live_telemetry.json"

    _hdr(f"V30 LIVE BOT -- TODAY DIAGNOSTIC  ({win_label})")
    print(f"  root      = {root.resolve()}")
    print(f"  now (UTC) = {_fmt_dt(now_utc)}")
    print(f"  trades    = {trades_path.name}   "
          f"{'(present)' if trades_path.exists() else '(MISSING)'}")
    print(f"  events    = {events_path.name}   "
          f"{'(present)' if events_path.exists() else '(MISSING)'}")
    print(f"  slippage  = {slippage_path.name}   "
          f"{'(present)' if slippage_path.exists() else '(MISSING)'}")
    print(f"  heartbeat = {heartbeat_path.name}   "
          f"{'(present)' if heartbeat_path.exists() else '(MISSING)'}")

    trades_all   = _read_jsonl(trades_path)
    events_all   = _read_jsonl(events_path)
    slippage_all = _read_jsonl(slippage_path)
    heartbeat    = _read_json(heartbeat_path) or {}
    telemetry    = _read_json(telemetry_path) or {}

    trades   = _filter_window(trades_all,   win_start, win_end)
    events   = _filter_window(events_all,   win_start, win_end)
    slippage = _filter_window(slippage_all, win_start, win_end)

    print()
    print(f"  rows in window: trades={len(trades)}  events={len(events)}  "
          f"slippage={len(slippage)}")

    if heartbeat:
        # The heartbeat is a snapshot -- show its age so you know it's live
        hb_dt = _parse_iso(str(heartbeat.get("ts_utc", "")))
        hb_age = (now_utc - hb_dt).total_seconds() if hb_dt else None
        if hb_age is not None:
            staleness = (
                "FRESH" if hb_age < 120 else
                f"STALE ({hb_age:.0f}s old -- bot may be down)"
            )
            print(f"  heartbeat ts={_fmt_dt(hb_dt)}  ({staleness})")

    # ---------------------------------------------------------------- reduce
    by_ticket = _reduce_by_ticket(trades, events)

    # Tickets currently open per heartbeat snapshot
    still_open: Dict[int, dict] = {}
    syms_block = (heartbeat or {}).get("symbols") or {}
    if isinstance(syms_block, dict):
        for sym, sd in syms_block.items():
            tk = (sd or {}).get("open_ticket")
            if tk:
                try:
                    still_open[int(tk)] = {"symbol": sym, **(sd or {})}
                except (TypeError, ValueError):
                    pass

    for rec in by_ticket.values():
        rec["classification"] = _classify(rec, now_utc, set(still_open.keys()))

    # ====================================================================
    # 1) ENTRIES TIMELINE
    # ====================================================================
    _sub("1) ENTRIES TIMELINE  (one row per ticket; sorted by entry time)")
    if not by_ticket:
        print("  no ENTRY rows in this window -- bot did not enter any trades")
    else:
        # Header
        print(f"  {'TIME(UTC)':<8} {'TICKET':>10} {'SYM':<6} {'SIDE':<5} "
              f"{'ENTRY':>10} {'SL':>10} {'TP1':>10} {'TP2':>10} "
              f"{'LOTS':>6} {'RISK$':>8}")
        ordered = sorted(by_ticket.values(),
                         key=lambda r: r["entry_ts"] or now_utc)
        for rec in ordered:
            ts_str = _fmt_hms(rec["entry_ts"]) if rec["entry_ts"] else "????????"
            print(f"  {ts_str:<8} {rec['ticket']:>10d} "
                  f"{str(rec['symbol'])[:6]:<6} {str(rec['side'])[:5]:<5} "
                  f"{(rec['fill_px'] or 0):>10.2f} {(rec['sl'] or 0):>10.2f} "
                  f"{(rec['tp1'] or 0):>10.2f} {(rec['tp2'] or 0):>10.2f} "
                  f"{(rec['lots'] or 0):>6.3f} {(rec['risk_usd'] or 0):>8.2f}")

            # Per-ticket event tail
            for ts, kind, ev in rec["events"]:
                extras = []
                if kind == "TP1_PARTIAL":
                    extras.append(f"lots_closed={ev.get('lots_closed')}")
                elif kind == "TP2_PARTIAL":
                    extras.append(f"lots_closed={ev.get('lots_closed')}")
                elif kind == "TRAIL_SL":
                    extras.append(f"new_sl={ev.get('new_sl')}")
                elif kind == "LAYER1_FIRED":
                    extras.append(f"action={ev.get('action')}  "
                                  f"slip_t={ev.get('slip_ticks')}")
                elif kind in ("CLOSE", "POS_CLOSED_BY_BROKER"):
                    extras.append(f"reason={ev.get('reason') or kind}")
                lag = (ts - rec["entry_ts"]).total_seconds() / 60.0 \
                    if rec["entry_ts"] else 0.0
                print(f"             -- {_fmt_hms(ts)}  {kind:<22} "
                      f"(+{lag:5.1f}m)  {' '.join(extras)}")
            print(f"             => classification: {rec['classification']}")

    # ====================================================================
    # 2) CONCURRENCY GATE
    # ====================================================================
    _sub("2) CONCURRENCY CAP  (max 2 concurrent positions)")
    counters_now = (telemetry.get("counters") or
                    (heartbeat.get("counters") or {}))
    cap_blocks   = int(counters_now.get("block_concurrent_cap", 0))
    n_entries    = int(counters_now.get("entries", 0))
    print(f"  counter[block_concurrent_cap] (since bot start) = {cap_blocks}")
    print(f"  counter[entries]              (since bot start) = {n_entries}")
    print(f"  entries in this window                          = "
          f"{len(by_ticket)}")
    print(f"  positions open right now (per heartbeat)        = "
          f"{len(still_open)}  (cap=2)")

    # Find pairs of entries that opened "near-simultaneously" today (within
    # 60 s) to highlight the "3 fired at once -> only 2 entered" scenario.
    ordered = sorted([r for r in by_ticket.values() if r["entry_ts"]],
                     key=lambda r: r["entry_ts"])
    clusters: List[List[dict]] = []
    for rec in ordered:
        if clusters and (rec["entry_ts"] -
                         clusters[-1][-1]["entry_ts"]).total_seconds() <= 60:
            clusters[-1].append(rec)
        else:
            clusters.append([rec])
    multi = [c for c in clusters if len(c) >= 2]
    if multi:
        print()
        print("  near-simultaneous entry clusters (<=60s gap between entries):")
        for cl in multi:
            ts0 = _fmt_hms(cl[0]["entry_ts"])
            tsN = _fmt_hms(cl[-1]["entry_ts"])
            syms = ",".join(f"{r['symbol']}({r['side']})" for r in cl)
            print(f"    {ts0}-{tsN}  n={len(cl)}  {syms}")
    else:
        print()
        print("  no near-simultaneous entry clusters in this window.")

    # ====================================================================
    # 3) NO-CHASE COOLDOWN
    # ====================================================================
    _sub("3) NO-CHASE COOLDOWN (cross-symbol; default 300 s)")
    nc_events = [e for e in events
                 if str(e.get("kind", "")).upper() == "BLOCK_NOCHASE_COOLDOWN"]
    nc_total = int(counters_now.get("block_nochase_cooldown", 0))
    print(f"  counter[block_nochase_cooldown] (since bot start) = {nc_total}")
    print(f"  blocks logged in window                           = "
          f"{len(nc_events)}")
    if nc_events:
        print(f"  {'TIME':<8} {'BLOCKED':<6} {'BY':<6} {'GAP_S':>6} {'CD_S':>6}")
        for e in nc_events:
            print(f"  {_fmt_hms(_row_dt(e)):<8} "
                  f"{str(e.get('symbol'))[:6]:<6} "
                  f"{str(e.get('blocked_by'))[:6]:<6} "
                  f"{(e.get('gap_s') or 0):>6.1f} "
                  f"{(e.get('cooldown_s') or 0):>6.0f}")
    last_close = (heartbeat or {}).get("last_close_ts_by_symbol") or \
                 (telemetry or {}).get("last_close_ts_by_symbol") or {}
    if last_close:
        print()
        print("  last close timestamps (used to compute the cooldown right now):")
        for sym, ts in last_close.items():
            try:
                ts = float(ts)
            except (TypeError, ValueError):
                continue
            if ts <= 0:
                print(f"    {sym:<6}  never closed since restart")
                continue
            age_s = max(0.0, now_utc.timestamp() - ts)
            unblocks_in = max(0.0, 300.0 - age_s)
            print(f"    {sym:<6}  closed {age_s:6.0f}s ago  "
                  f"(unblocks others in {unblocks_in:5.0f}s)")

    # ====================================================================
    # 4) CURRENT OPEN POSITIONS
    # ====================================================================
    _sub("4) CURRENT OPEN POSITIONS (from heartbeat snapshot)")
    if not still_open:
        print("  none -- heartbeat reports zero open positions")
    else:
        print(f"  {'SYM':<6} {'TICKET':>10} {'SIDE':<5} "
              f"{'LOTS':>6} {'CUR_SL':>10} {'STATE_LADDER':<32}")
        for tk, sd in still_open.items():
            sym = sd.get("symbol")
            side = sd.get("open_side") or "?"
            lots = sd.get("open_lots") or sd.get("lots") or 0
            sl = sd.get("open_sl") or sd.get("sl") or 0
            # If we have a matching ticket in by_ticket, show ladder progress
            rec = by_ticket.get(tk)
            ladder = "-"
            if rec:
                tp1_done = "TP1+" if rec["tp1_ts"] else "TP1."
                tp2_done = "TP2+" if rec["tp2_ts"] else "TP2."
                tr = f"trailx{rec['trail_moves']}"
                ladder = f"{tp1_done} {tp2_done} {tr}"
            print(f"  {str(sym)[:6]:<6} {int(tk):>10d} {str(side)[:5]:<5} "
                  f"{float(lots or 0):>6.3f} {float(sl or 0):>10.2f} "
                  f"{ladder:<32}")

    # ====================================================================
    # 5) SUSPECT POSITIONS (the main diagnostic question)
    # ====================================================================
    _sub("5) SUSPECT POSITIONS  (the answer to 'why didn't TP fire?')")
    suspects: List[dict] = []
    for rec in by_ticket.values():
        cls = rec["classification"] or ""
        if cls.startswith("GHOST"):
            suspects.append(rec)
            continue
        if cls.startswith("OPEN_NO_TP1"):
            held = (now_utc - rec["entry_ts"]).total_seconds() / 60 \
                if rec["entry_ts"] else 0
            if held > 90:   # heuristic -- most ORB trades close inside 90 min
                suspects.append(rec)
            continue
        # Manual close = bot has classification "CLOSED" but operator may have
        # closed it themselves; the broker_close path with snap_sl source
        # already handles SL; if it shows snap_tp1 inferred it WAS close to TP1
    if not suspects:
        print("  no ghost / stuck-ladder positions detected in this window.")
        print("  (every entry has a matching close event OR is open <90min")
        print("   AND the heartbeat agrees with the trade log on what's open.)")
    else:
        print("  >>> THESE TICKETS NEED ATTENTION <<<")
        for rec in suspects:
            print()
            print(f"    ticket={rec['ticket']}  symbol={rec['symbol']}  "
                  f"side={rec['side']}")
            print(f"      entry  : {_fmt_dt(rec['entry_ts'])}  "
                  f"@ {rec['fill_px']}  lots={rec['lots']}")
            print(f"      SL/TP1/TP2: {rec['sl']} / {rec['tp1']} / {rec['tp2']}")
            print(f"      class  : {rec['classification']}")
            if rec["events"]:
                print( "      events :")
                for ts, kind, _ in rec["events"]:
                    print(f"        {_fmt_hms(ts)}  {kind}")
            else:
                print( "      events : NONE -- bot wrote ENTRY but no follow-up event!")
            print( "      likely : "
                  + (
                      "bot lost partial_state across a restart "
                      "(state is in-memory only)" if "GHOST" in rec["classification"]
                      else
                      "in-bar PartialCloseManager not firing -- "
                      "broker close_position() may be rejecting"
                  ))

    # ====================================================================
    # 6) RAIL COUNTERS (since bot start, NOT just window)
    # ====================================================================
    _sub("6) RAIL COUNTERS  (since current bot start -- full uptime)")
    if not counters_now:
        print("  no counters available (heartbeat / telemetry both empty)")
    else:
        # Display the highest-signal counters first
        priority = [
            "entries", "block_concurrent_cap", "block_nochase_cooldown",
            "block_news_entry", "block_halt_4pct", "block_size_below_min",
            "block_cal_weekend", "block_cal_rollover", "block_cal_holiday",
            "block_cal_news",
            "exit_window", "exit_broker", "flatten_news",
            "kill_total_dd_8pct", "kill_account", "halt_day",
            "exit_layer1_close_now", "exit_layer1_fallback_close",
        ]
        seen = set()
        for k in priority:
            if k in counters_now:
                print(f"    {k:<32} = {counters_now[k]}")
                seen.add(k)
        rest = [(k, v) for k, v in counters_now.items() if k not in seen]
        if rest:
            print("    --- other ---")
            for k, v in rest:
                print(f"    {k:<32} = {v}")

    # ====================================================================
    # OPTIONAL: --check-prices
    # ====================================================================
    if args.check_prices:
        _sub("7) MT5 PRICE TOUCH CHECK  (smoking-gun for stuck TP)")
        # Build symbol -> broker map from heartbeat / specs (fall back to identity)
        sym_to_broker: Dict[str, str] = {}
        for sym, sd in (syms_block or {}).items():
            if isinstance(sd, dict):
                sym_to_broker[sym] = sd.get("broker") or sym
        # Default broker names (matches V30_BROKER_NAMES in v30_live.py)
        for k, v in {"DE40": "DE40.cash", "US30": "US30.cash",
                     "US500": "US500.cash", "XAUUSD": "XAUUSD"}.items():
            sym_to_broker.setdefault(k, v)
        targets = list(by_ticket.values())
        if not args.all_tickets:
            targets = [r for r in targets
                       if r["classification"] and
                       ("OPEN" in r["classification"] or
                        "GHOST" in r["classification"])]
        if not targets:
            print("  no candidate tickets to price-check (use --all-tickets to "
                  "force-check every entry).")
        else:
            _check_prices_via_mt5(targets, sym_to_broker)

    # ====================================================================
    print()
    print(_hr("="))
    print("  DONE.  If section 5 listed any tickets, that's where the bug is.")
    print("  If section 5 is empty but you still had to manually TP, run again")
    print("  with --check-prices to compare actual broker M1 highs/lows against")
    print("  the bot's TP levels -- any '*'-marked TP touch is a stuck ladder.")
    print(_hr("="))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[interrupted]")
        sys.exit(130)
