"""
parity_live_vs_backtest_window.py
==================================

THE question:
  "If I run the BACKTEST on the same 31 days the bot has been LIVE,
   does it produce the same trades?  Is live ACTUALLY behaving like
   the simulation we tuned the strategy on?"

Inputs:
  Results/v30_live_trades.jsonl   -- the 89 live ENTRY rows
  Results/v30_fresh_trades.json   -- the 264-trade backtest (Jan -> May)

What this script does:
  1. Reads both files, keeps trades in the window
     [first_live_entry .. last_live_entry].
  2. For every backtest trade in window, looks for a live trade on the
     same symbol, same side, within +/- 5 minutes of entry.
  3. Prints, side by side:
        * backtest count + total PnL
        * live count
        * MATCHED count (same symbol, same side, within 5 min)
        * BACKTEST-ONLY (signals the live bot MISSED)
        * LIVE-ONLY    (entries the live bot took that backtest didn't)
        * per-day comparison
        * per-symbol comparison

No interpretation.  Numbers only.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIVE_FILE = REPO / "Results" / "v30_live_trades.jsonl"
BT_FILE   = REPO / "Results" / "v30_fresh_trades.json"
MATCH_WINDOW_MIN = 5     # +/- minutes for "same signal"


# --------------------------------------------------------------------------- #
def parse_dt(s) -> datetime | None:
    if not s:
        return None
    s = str(s).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s[:25])
    except Exception:
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def norm_side(s) -> str:
    """Backtest uses 1 / -1.  Live uses 'LONG' / 'SHORT'.  Return 'L' / 'S' / '?'."""
    if s is None:
        return "?"
    if isinstance(s, str):
        return "L" if s.upper().startswith("L") else "S" if s.upper().startswith("S") else "?"
    try:
        n = int(s)
    except (TypeError, ValueError):
        return "?"
    return "L" if n > 0 else "S" if n < 0 else "?"


def load_live(p: Path) -> list[dict]:
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
                t = json.loads(line)
            except Exception:
                continue
            if (t.get("event") or "").upper() != "ENTRY":
                continue
            ent = parse_dt(t.get("ts_utc") or t.get("ts"))
            if ent is None:
                continue
            out.append({
                "src":     "LIVE",
                "dt":      ent,
                "symbol":  t.get("symbol"),
                "side":    norm_side(t.get("side")),
                "px":      t.get("fill_px"),
                "sl":      t.get("sl"),
                "tp1":     t.get("tp1"),
                "tp2":     t.get("tp2"),
                "lots":    t.get("lots"),
                "risk":    t.get("risk_usd"),
                "dry_run": t.get("dry_run"),
                "pnl":     None,    # close PnL not in this file
                "ticket":  t.get("ticket"),
            })
    out.sort(key=lambda r: r["dt"])
    return out


def load_bt(p: Path) -> list[dict]:
    if not p.exists():
        print(f"!! NOT FOUND: {p}")
        return []
    raw = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    if isinstance(raw, dict) and "trades" in raw:
        raw = raw["trades"]
    out = []
    for t in raw:
        ent = parse_dt(t.get("entry_time") or t.get("ts_utc"))
        if ent is None:
            continue
        out.append({
            "src":     "BT",
            "dt":      ent,
            "symbol":  t.get("symbol"),
            "side":    norm_side(t.get("side")),
            "px":      t.get("entry_price"),
            "exit_px": t.get("exit_price"),
            "exit_dt": parse_dt(t.get("exit_time")),
            "pnl":     t.get("net_pnl"),
            "R":       t.get("realised_R"),
        })
    out.sort(key=lambda r: r["dt"])
    return out


# --------------------------------------------------------------------------- #
def main() -> int:
    print("=" * 90)
    print("  PARITY: LIVE vs BACKTEST on the SAME date window")
    print("=" * 90)

    live = load_live(LIVE_FILE)
    bt   = load_bt(BT_FILE)
    if not live:
        print("no live trades found"); return 1
    if not bt:
        print("no backtest trades found"); return 1

    win_start = live[0]["dt"].replace(hour=0, minute=0, second=0, microsecond=0)
    win_end   = live[-1]["dt"].replace(hour=23, minute=59, second=59, microsecond=0)
    print(f"\nlive window  : {win_start.date()}  .. {win_end.date()}  ({len(live)} entries)")
    print(f"backtest full: {bt[0]['dt'].date()}  .. {bt[-1]['dt'].date()}  ({len(bt)} trades)")

    bt_win = [t for t in bt if win_start <= t["dt"] <= win_end]
    print(f"backtest IN window : {len(bt_win)} trades")

    # ------------------------------------------------------------------
    #  Matching: for each backtest trade in window, find live entry
    #  with same symbol + same side, within +/-MATCH_WINDOW_MIN
    # ------------------------------------------------------------------
    live_used: set[int] = set()
    matches: list[tuple[dict, dict]] = []
    bt_only:   list[dict] = []
    for b in bt_win:
        cand_idx = None
        for i, lv in enumerate(live):
            if i in live_used:
                continue
            if lv["symbol"] != b["symbol"]:
                continue
            if lv["side"] != b["side"]:
                continue
            if abs((lv["dt"] - b["dt"]).total_seconds()) > MATCH_WINDOW_MIN * 60:
                continue
            cand_idx = i
            break
        if cand_idx is None:
            bt_only.append(b)
        else:
            live_used.add(cand_idx)
            matches.append((b, live[cand_idx]))

    live_only = [lv for i, lv in enumerate(live) if i not in live_used]

    # ------------------------------------------------------------------
    #  Headline numbers
    # ------------------------------------------------------------------
    bt_pnl = sum((t["pnl"] or 0.0) for t in bt_win)
    bt_pnl_matched = sum((b["pnl"] or 0.0) for b, _ in matches)
    bt_pnl_missed  = sum((b["pnl"] or 0.0) for b in bt_only)

    print("\n" + "=" * 90)
    print("  HEADLINE")
    print("=" * 90)
    print(f"  backtest trades in window    : {len(bt_win):>4}  net PnL = ${bt_pnl:>+10,.2f}")
    print(f"  live entries in window       : {len(live):>4}")
    print(f"")
    print(f"  MATCHED (same sym+side+t)    : {len(matches):>4}  bt PnL = ${bt_pnl_matched:>+10,.2f}")
    print(f"  BACKTEST-ONLY (live MISSED)  : {len(bt_only):>4}  bt PnL = ${bt_pnl_missed:>+10,.2f}")
    print(f"  LIVE-ONLY (entries not in BT): {len(live_only):>4}")
    print(f"")
    miss_pct = (len(bt_only) / len(bt_win) * 100) if bt_win else 0
    extra_pct = (len(live_only) / len(live) * 100) if live else 0
    print(f"  miss rate (BT signals not taken live) : {miss_pct:5.1f} %")
    print(f"  extra rate (live entries not in BT)   : {extra_pct:5.1f} %")

    # ------------------------------------------------------------------
    #  By symbol
    # ------------------------------------------------------------------
    print("\n" + "-" * 90)
    print("  BY SYMBOL")
    print("-" * 90)
    syms = sorted(set([t["symbol"] for t in bt_win] + [t["symbol"] for t in live]))
    print(f"  {'symbol':<8} {'bt_n':>5} {'live_n':>7} {'matched':>8} {'missed':>7} "
          f"{'extra':>6} {'bt_pnl':>11} {'missed_pnl':>12}")
    for s in syms:
        bt_s = [t for t in bt_win if t["symbol"] == s]
        lv_s = [t for t in live if t["symbol"] == s]
        m_s = [m for m in matches if m[0]["symbol"] == s]
        miss_s = [t for t in bt_only if t["symbol"] == s]
        extra_s = [t for t in live_only if t["symbol"] == s]
        bt_pnl_s = sum((t["pnl"] or 0.0) for t in bt_s)
        miss_pnl_s = sum((t["pnl"] or 0.0) for t in miss_s)
        print(f"  {s or '?':<8} {len(bt_s):>5} {len(lv_s):>7} {len(m_s):>8} "
              f"{len(miss_s):>7} {len(extra_s):>6} ${bt_pnl_s:>+9,.2f} ${miss_pnl_s:>+10,.2f}")

    # ------------------------------------------------------------------
    #  By day
    # ------------------------------------------------------------------
    print("\n" + "-" * 90)
    print("  BY DAY  (bt_n / live_n / matched / miss / extra | backtest PnL)")
    print("-" * 90)
    days = sorted({t["dt"].date().isoformat() for t in bt_win} | {t["dt"].date().isoformat() for t in live})
    print(f"  {'date':<11}  bt   live  match miss  extra  bt_pnl     missed_pnl")
    for d in days:
        bt_d = [t for t in bt_win if t["dt"].date().isoformat() == d]
        lv_d = [t for t in live if t["dt"].date().isoformat() == d]
        m_d = [m for m in matches if m[0]["dt"].date().isoformat() == d]
        miss_d = [t for t in bt_only if t["dt"].date().isoformat() == d]
        extra_d = [t for t in live_only if t["dt"].date().isoformat() == d]
        bt_pnl_d = sum((t["pnl"] or 0.0) for t in bt_d)
        miss_pnl_d = sum((t["pnl"] or 0.0) for t in miss_d)
        print(f"  {d:<11} {len(bt_d):>3} {len(lv_d):>5}  {len(m_d):>4} {len(miss_d):>4} "
              f"{len(extra_d):>5}  ${bt_pnl_d:>+8,.2f}  ${miss_pnl_d:>+8,.2f}")

    # ------------------------------------------------------------------
    #  Detailed: BACKTEST-ONLY trades the live bot MISSED  (top 20)
    # ------------------------------------------------------------------
    print("\n" + "-" * 90)
    print("  TOP 20 BACKTEST-ONLY TRADES (signals the live bot DID NOT take)")
    print("-" * 90)
    print(f"  {'entry_time':<19}  {'sym':<7} {'side':<4} {'bt_pnl':>10}")
    for t in sorted(bt_only, key=lambda x: (x["pnl"] or 0.0))[:20]:
        print(f"  {t['dt'].strftime('%Y-%m-%d %H:%M:%S')}  {t['symbol']:<7} "
              f"{t['side']:<4} ${(t['pnl'] or 0.0):>+8,.2f}")

    # ------------------------------------------------------------------
    #  Detailed: LIVE-ONLY entries (the bot took, backtest didn't signal)
    # ------------------------------------------------------------------
    print("\n" + "-" * 90)
    print("  TOP 20 LIVE-ONLY ENTRIES (live took, backtest did NOT signal)")
    print("-" * 90)
    print(f"  {'entry_time':<19}  {'sym':<7} {'side':<4} {'risk$':>7} {'dry':<5} ticket")
    for t in live_only[:20]:
        dry = "DRY" if t["dry_run"] is True else "LIVE" if t["dry_run"] is False else "?"
        risk_s = f"${float(t['risk']):>5.0f}" if t["risk"] is not None else "      ?"
        print(f"  {t['dt'].strftime('%Y-%m-%d %H:%M:%S')}  {t['symbol']:<7} "
              f"{t['side']:<4} {risk_s} {dry:<5} {t['ticket']}")

    print("\n" + "=" * 90)
    print("  PARITY VERDICT")
    print("=" * 90)
    if miss_pct < 5 and extra_pct < 5:
        verdict = "GOOD PARITY -- live mirrors backtest"
    elif miss_pct > 30 or extra_pct > 30:
        verdict = "BAD PARITY -- live is NOT executing the same strategy as the backtest"
    else:
        verdict = "PARTIAL PARITY -- meaningful drift"
    print(f"  {verdict}")
    print(f"  If MISSED bt PnL is heavily positive  ->  live bot is dropping its WINNERS.")
    print(f"  If MISSED bt PnL is heavily negative  ->  live bot is avoiding losers (good).")
    print(f"  If LIVE-ONLY count is large           ->  live is firing on signals BT doesn't have.")
    print("=" * 90)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
