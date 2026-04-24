#!/usr/bin/env python3
"""
Extract the entry-time distribution from the v23 3-month backtest.

Answers the user's question: "What times did the bot actually trade?"

Outputs:
  1. Per-symbol hour-of-day (UTC) histogram of entries
  2. Same in BST (UK summer = UTC+1) for eyeballing
  3. Per-symbol median / 25-75 percentile of entry-minute *within* the OR window
  4. Trade count, win%, expectancy by symbol
"""
from __future__ import annotations
import json, sys
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

ROOT = Path(__file__).resolve().parents[1]

# v23 locked has the canonical 3-month trade log
SRC = ROOT / "Results" / "v23_locked.json"
if not SRC.exists():
    SRC = ROOT / "Results" / "v23_final.json"

print(f"Loading {SRC}")
blob = json.loads(SRC.read_text(encoding="utf-8"))

# Find the trades list — it's nested per symbol inside portfolio/per_symbol or similar
trades = []
def _walk(o):
    if isinstance(o, dict):
        if "entry_time" in o and "symbol" in o:
            trades.append(o)
        else:
            for v in o.values(): _walk(v)
    elif isinstance(o, list):
        for v in o: _walk(v)
_walk(blob)

if not trades:
    # Try alternative trade log files
    for p in ["v23_final.json", "backtest_v22_phase_b.json"]:
        alt = ROOT / "Results" / p
        if alt.exists():
            print(f"... no trades in v23_locked, falling back to {p}")
            blob = json.loads(alt.read_text(encoding="utf-8"))
            _walk(blob)
            if trades: break

print(f"Extracted {len(trades)} trades")
if not trades:
    print("NO TRADES FOUND — aborting")
    sys.exit(1)

# -------- Normalise entry_time to tz-aware UTC --------
def _parse(ts):
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    s = str(ts)
    # Try ISO
    try:
        dt = datetime.fromisoformat(s.replace("Z","+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

for t in trades:
    t["_dt_utc"] = _parse(t.get("entry_time"))
trades = [t for t in trades if t["_dt_utc"] is not None]
print(f"  ({len(trades)} with parseable entry_time)")

# -------- Group by symbol --------
by_sym = defaultdict(list)
for t in trades:
    by_sym[t.get("symbol","?")].append(t)

print()
print("="*78)
print(" v23 THREE-MONTH BACKTEST — WHEN DID THE BOT ACTUALLY TRADE?")
print("="*78)
print()

# Config for reference
CONFIG = {
    "DE40":   {"or": "08:00-08:30 UTC = 09:00-09:30 BST",
               "trade": "08:30-10:30 UTC = 09:30-11:30 BST"},
    "US30":   {"or": "14:30-15:00 UTC = 15:30-16:00 BST",
               "trade": "15:00-17:00 UTC = 16:00-18:00 BST"},
    "XAUUSD": {"or": "14:30-15:00 UTC = 15:30-16:00 BST",
               "trade": "15:00-17:00 UTC = 16:00-18:00 BST"},
    "US500":  {"or": "14:30-14:45 UTC = 15:30-15:45 BST",
               "trade": "14:45-16:45 UTC = 15:45-17:45 BST"},
}

total_pnl = 0.0
total_wins = 0
total_trades = 0

for sym in sorted(by_sym.keys()):
    lst = by_sym[sym]
    print(f"─── {sym} ─── ({len(lst)} trades)")
    cfg = CONFIG.get(sym, {})
    if cfg:
        print(f"  Configured OR window     : {cfg['or']}")
        print(f"  Configured entry window  : {cfg['trade']}")

    # hour-of-day histogram (UTC)
    hours_utc = Counter(t["_dt_utc"].hour for t in lst)
    hours_bst = Counter((t["_dt_utc"] + timedelta(hours=1)).hour for t in lst)
    print(f"  Entry hour (UTC) distribution:")
    for h in sorted(hours_utc):
        bar = "█" * min(40, hours_utc[h])
        print(f"    {h:02d}:00 UTC ({(h+1)%24:02d}:00 BST) | {hours_utc[h]:3d} | {bar}")

    # Minute-in-hour of first breakout (how long after OR close?)
    mins = [t["_dt_utc"].minute for t in lst]
    if mins:
        mins_sorted = sorted(mins)
        q25 = mins_sorted[len(mins_sorted)//4]
        q50 = mins_sorted[len(mins_sorted)//2]
        q75 = mins_sorted[3*len(mins_sorted)//4]
        print(f"  Entry minute-of-hour  : median={q50}  IQR=[{q25} .. {q75}]")

    # P&L stats
    pnls = [float(t.get("pnl", t.get("pnl_usd", 0))) for t in lst]
    wins = sum(1 for p in pnls if p > 0)
    total_p = sum(pnls)
    print(f"  Trades: {len(lst)}  Wins: {wins} ({100*wins/max(1,len(lst)):.1f}%)  "
          f"Total PnL: ${total_p:,.0f}  Avg/trade: ${total_p/max(1,len(lst)):,.1f}")
    total_pnl += total_p
    total_wins += wins
    total_trades += len(lst)
    print()

print("─── PORTFOLIO TOTAL ───")
print(f"  Trades: {total_trades}  Wins: {total_wins} ({100*total_wins/max(1,total_trades):.1f}%)  "
      f"Total PnL: ${total_pnl:,.0f}")
print()

# -------- Day-of-week --------
DOW = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
dow = Counter(DOW[t["_dt_utc"].weekday()] for t in trades)
print("Day-of-week distribution (whole portfolio):")
for d in DOW:
    bar = "█" * min(40, dow.get(d,0))
    print(f"  {d} | {dow.get(d,0):3d} | {bar}")

# Save JSON summary
out = {
    "source": str(SRC.relative_to(ROOT)),
    "n_trades": total_trades,
    "n_wins": total_wins,
    "win_rate": total_wins/max(1,total_trades),
    "total_pnl": total_pnl,
    "per_symbol": {
        sym: {
            "n": len(lst),
            "hours_utc": dict(Counter(t["_dt_utc"].hour for t in lst)),
            "pnl": sum(float(t.get("pnl", t.get("pnl_usd", 0))) for t in lst),
        }
        for sym, lst in by_sym.items()
    },
    "day_of_week": dict(dow),
}
out_path = ROOT / "Results" / "_entry_time_histogram.json"
out_path.write_text(json.dumps(out, indent=2))
print(f"\n--> {out_path.relative_to(ROOT)}")
