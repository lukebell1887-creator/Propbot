#!/usr/bin/env python3
"""Detailed per-symbol + micro-behaviour audit of backtest_v23_final (news-rails track)."""
from __future__ import annotations
import sys, json, math, statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from Scripts.preflight_checks import (
    SYMS, BALANCE,
    run_portfolio, apply_full_safety_rails,
    worst_single_day, ruin_probs, hold_duration_stats, concurrency_stats,
    MertonGZSizerConfig,
)
from Scripts.backtest_v22_lean_uk5 import stats, BROKER_TICK_SIZE
from Scripts.backtest_v23_locked import (
    load_news_events, apply_news_entry_block, apply_news_flatten,
    build_price_lookup,
)
from src.dd_breaker import apply_dd_breaker

RISK = 0.00110
NEWS_CSV = ROOT / "data" / "news" / "tier1_2026.csv"

cfg = MertonGZSizerConfig(
    base_risk_pct=RISK, cap_mult=3.0, gamma=2.0,
    ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
    pool_symbols=True, no_edge_multiplier=1.0,
)
raw, tmin, tmax, _drop, streams = run_portfolio(SYMS, cfg)
events = load_news_events(NEWS_CSV)
pl = build_price_lookup(streams)
raw, _ = apply_news_entry_block(raw, events, buffer_min=15)
raw, _ = apply_news_flatten(raw, events, pl, minutes_before=2)
tr = apply_full_safety_rails(raw, slippage_ticks=1.0)

print(f"\nWindow : {tmin}  →  {tmax}")
print(f"Total partial-trade records after full rails: {len(tr)}")

# ----------------------------------------------------------------- GLOBAL
s = stats(tr)
pnls = np.array([t.net_pnl for t in tr], dtype=float)
worst_pnl, worst_pct, worst_dd, n_days = worst_single_day(tr)
dur = hold_duration_stats(tr)

# Distinct entries (partials share entry_time within symbol)
entries = {}
for t in tr:
    k = (t.symbol, t.entry_time)
    entries.setdefault(k, []).append(t)

# <60s holds per-partial
sub60 = sum(1 for t in tr if (t.exit_time - t.entry_time) < 60)

# Same-bar (entry and exit both inside the same M1 bar, 60-second span)
def _bar_key(ts_float):
    # floor to M1 boundary in UTC-seconds
    return int(ts_float // 60)

same_bar_entries = 0
for k, parts in entries.items():
    e0 = min(p.entry_time for p in parts)
    x_last = max(p.exit_time for p in parts)
    if _bar_key(e0) == _bar_key(x_last):
        same_bar_entries += 1

same_bar_partials = sum(1 for t in tr if _bar_key(t.entry_time) == _bar_key(t.exit_time))

# 4% DD breaker simulation
kept, br = apply_dd_breaker(tr, starting_balance=BALANCE, halt_pct=0.04)
dd_triggered = br.total_halts > 0

print("\n" + "=" * 80)
print("GLOBAL — 4-symbol portfolio")
print("=" * 80)
print(f"  Partial-trade records     : {s['n']}")
print(f"  Distinct entries          : {len(entries)}")
print(f"  Net PnL                   : ${s['net']:+,.2f}")
print(f"  Return %                  : {s['ret_pct']:+.2f}%")
print(f"  Max DD %                  : {s['dd_pct']:.2f}%")
print(f"  Worst single day          : ${worst_pnl:+,.2f}  ({worst_pct:+.2f}% of balance)")
print(f"  Worst daily DD            : {worst_dd:.2f}%")
print(f"  Hold duration median/min  : {dur['median_min']:.1f}  p10={dur['p10_min']:.1f}  p90={dur['p90_min']:.1f}")
print(f"  #partials held < 60 s     : {sub60}    (ANY < 60 s? {'YES' if sub60>0 else 'NO'})")
print(f"  #partials same-bar O&C    : {same_bar_partials} / {s['n']}  ({same_bar_partials/max(1,s['n'])*100:.2f}%)")
print(f"  #entries same-bar O&C     : {same_bar_entries} / {len(entries)}  ({same_bar_entries/max(1,len(entries))*100:.2f}%)")
print(f"  4 % DD breaker triggered? : {'YES  ('+str(br.total_halts)+' halts)' if dd_triggered else 'NO'}  "
      f"(max_dd_seen during walk = {br.max_dd_pct_seen*100:.3f}%)")

# ----------------------------------------------------------------- PER SYMBOL
print("\n" + "=" * 80)
print("PER SYMBOL")
print("=" * 80)
print(f"  {'SYM':<7} {'N':>4} {'Entries':>8} {'PnL':>12} {'Ret%':>7} "
      f"{'DD%':>6} {'WrstDay%':>9} {'sub60s':>7} {'sameBar%':>9}")
print("  " + "-" * 74)
per_sym_json = {}
for sym in SYMS:
    syms_tr = [t for t in tr if t.symbol == sym]
    if not syms_tr:
        continue
    # Standalone per-symbol DD (on pnl stream, starting from 100k)
    pnls_s = [t.net_pnl for t in sorted(syms_tr, key=lambda x: x.entry_time)]
    eq, peak, mdd = BALANCE, BALANCE, 0.0
    for p in pnls_s:
        eq += p
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        mdd = max(mdd, dd)
    wpnl, wpct, _, _ = worst_single_day(syms_tr)
    ent_s = {k: v for k, v in entries.items() if k[0] == sym}
    sub_s = sum(1 for t in syms_tr if (t.exit_time - t.entry_time) < 60)
    same_s = sum(1 for t in syms_tr if _bar_key(t.entry_time) == _bar_key(t.exit_time))
    print(f"  {sym:<7} {len(syms_tr):>4} {len(ent_s):>8} "
          f"${sum(pnls_s):>+10,.0f} {sum(pnls_s)/BALANCE*100:>+6.2f}% "
          f"{mdd*100:>5.2f}% {wpct:>+8.2f}% {sub_s:>7} "
          f"{same_s/max(1,len(syms_tr))*100:>8.2f}%")
    per_sym_json[sym] = dict(
        partials=len(syms_tr), entries=len(ent_s),
        net=float(sum(pnls_s)), ret_pct=float(sum(pnls_s)/BALANCE*100),
        max_dd_pct=float(mdd*100), worst_day_pnl=float(wpnl), worst_day_pct=float(wpct),
        sub60s_partials=int(sub_s), same_bar_partials=int(same_s),
    )

# Save consolidated JSON
out = {
    "window": [str(tmin), str(tmax)],
    "global": {
        "partials": int(s["n"]), "entries": len(entries),
        "net": float(s["net"]), "ret_pct": float(s["ret_pct"]),
        "max_dd_pct": float(s["dd_pct"]),
        "worst_day_pnl": float(worst_pnl), "worst_day_pct": float(worst_pct),
        "worst_daily_dd_pct": float(worst_dd),
        "sub60s_partials": int(sub60),
        "same_bar_partials": int(same_bar_partials),
        "same_bar_entries": int(same_bar_entries),
        "dd_breaker_4pct_triggered": bool(dd_triggered),
        "dd_breaker_max_dd_seen_pct": float(br.max_dd_pct_seen * 100),
    },
    "per_symbol": per_sym_json,
}
(ROOT / "Results").mkdir(exist_ok=True)
with open(ROOT / "Results" / "audit_v23_detail.json", "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2, default=str)
print(f"\nSaved: Results/audit_v23_detail.json")
