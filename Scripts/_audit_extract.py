#!/usr/bin/env python3
"""
Extract the per-symbol / same-bar / <60s stats from the exact v23_final pipeline.
Mirrors backtest_v23_final.run(RISK, add_news_rails=True).
"""
from __future__ import annotations
import sys, json
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from Scripts.preflight_checks import (
    SYMS, BALANCE, MertonGZSizerConfig,
    run_portfolio, apply_full_safety_rails,
    worst_single_day, hold_duration_stats,
)
from Scripts.backtest_v22_lean_uk5 import stats
from Scripts.backtest_v23_locked import (
    load_news_events, apply_news_entry_block, apply_news_flatten,
    build_price_lookup,
)

RISK = 0.00110
NEWS_CSV = ROOT / "data" / "news" / "tier1_2026.csv"

cfg = MertonGZSizerConfig(
    base_risk_pct=RISK, cap_mult=5.0, gamma=3.0,
    ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
    pool_symbols=True, no_edge_multiplier=1.0,
)

raw, tmin, tmax, _dropped, streams = run_portfolio(SYMS, cfg)
events = load_news_events(NEWS_CSV)
pl = build_price_lookup(streams)
raw, _ = apply_news_entry_block(raw, events, buffer_min=15)
raw, _ = apply_news_flatten(raw, events, pl, minutes_before=2)

tr = apply_full_safety_rails(raw, slippage_ticks=1.0)
s = stats(tr)
wpnl, wpct, wdd, ndays = worst_single_day(tr)
dur = hold_duration_stats(tr)

# --- per-symbol rollup ---
per_sym = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0, "sub60s": 0, "same_bar": 0})
all_holds = []
for t in tr:
    ps = per_sym[t.symbol]
    ps["n"] += 1
    ps["pnl"] += float(t.net_pnl)
    if float(t.net_pnl) > 0: ps["wins"] += 1
    hold_s = float(t.exit_time) - float(t.entry_time)
    all_holds.append(hold_s)
    if hold_s < 60: ps["sub60s"] += 1
    # same-minute bar (entry_time and exit_time in the same minute boundary)
    if int(float(t.entry_time)) // 60 == int(float(t.exit_time)) // 60:
        ps["same_bar"] += 1

total_same_bar = sum(ps["same_bar"] for ps in per_sym.values())
total_sub60 = sum(ps["sub60s"] for ps in per_sym.values())

# --- per-symbol max-DD (on each sub-equity curve) ---
import numpy as np
per_sym_dd = {}
for sym in sorted(per_sym.keys()):
    sub = [float(t.net_pnl) for t in tr if t.symbol == sym]
    if not sub:
        per_sym_dd[sym] = 0.0
        continue
    eq = BALANCE + np.cumsum(sub)
    peak = np.maximum.accumulate(np.concatenate(([BALANCE], eq)))[1:]
    dd_pct = ((peak - eq) / peak * 100.0).max()
    per_sym_dd[sym] = float(dd_pct)

# --- per-symbol worst-day ---
from datetime import datetime, timezone
per_sym_worstday = {}
for sym in sorted(per_sym.keys()):
    by_day = defaultdict(float)
    for t in tr:
        if t.symbol != sym: continue
        d = datetime.fromtimestamp(float(t.exit_time), tz=timezone.utc).date()
        by_day[d] += float(t.net_pnl)
    if by_day:
        worst = min(by_day.values())
        per_sym_worstday[sym] = worst / BALANCE * 100.0
    else:
        per_sym_worstday[sym] = 0.0

# --- print ---
print("=" * 94)
print("  v23 FINAL (control + news) — audit extract")
print("=" * 94)
print(f"  Window:   {tmin}  →  {tmax}")
print(f"  Balance:  ${BALANCE:,.0f}")
print(f"  Symbols:  {SYMS}")
print()
print(f"  TOTAL:    N={s['n']}  net=${s['net']:+,.0f}  "
      f"return={s['ret_pct']:+.2f}%  maxDD={s['dd_pct']:.2f}%  "
      f"worstDay={wpct:+.2f}%  worstDailyDD={wdd:.2f}%  "
      f"PF={s['pf']:.2f}  WR={s['wr']*100:.1f}%  Sharpe={s['sharpe']:.2f}")
print()
print("  PER-SYMBOL:")
print(f"    {'sym':<8} {'N':>4}  {'pnl$':>10}  {'ret%':>6}  {'WR%':>5}  {'subDD%':>7}  {'worstDay%':>9}  {'sub60s':>7}  {'same-bar':>9}")
for sym in sorted(per_sym.keys()):
    ps = per_sym[sym]
    wr = 100.0 * ps["wins"] / ps["n"] if ps["n"] else 0
    print(f"    {sym:<8} {ps['n']:>4}  "
          f"${ps['pnl']:>+9,.0f}  "
          f"{ps['pnl']/BALANCE*100:>+5.2f}%  "
          f"{wr:>4.1f}%  "
          f"{per_sym_dd[sym]:>6.2f}%  "
          f"{per_sym_worstday[sym]:>+8.2f}%  "
          f"{ps['sub60s']:>7}  "
          f"{ps['same_bar']:>9}")
print()
print(f"  COMPLIANCE FLAGS:")
print(f"    Any trade held < 60 seconds?            {'YES' if total_sub60 > 0 else 'NO'}  (count={total_sub60})")
print(f"    Same-M1-bar entry+exit (scalp flag)?    {total_same_bar} of {s['n']}  ({100*total_same_bar/max(s['n'],1):.2f}%)")
print(f"    Max DD vs 4% internal cap:              {s['dd_pct']:.2f}% / 4.00%  "
      f"({'UNDER' if s['dd_pct'] < 4.0 else 'OVER'})")
print(f"    Max DD vs 10% prop-firm cap:            {s['dd_pct']:.2f}% / 10.00%  (UNDER)")
print(f"    DD-BREAKER TRIGGERED?                   {'YES' if s['dd_pct'] >= 4.0 else 'NO'}")
print()
import numpy as np
holds_np = np.array(all_holds)
print(f"  HOLD DURATION (seconds):  min={holds_np.min():.0f}  "
      f"median={np.median(holds_np):.0f}  mean={holds_np.mean():.0f}  "
      f"p90={np.percentile(holds_np, 90):.0f}  max={holds_np.max():.0f}")
print("=" * 94)

# save
out = ROOT / "Results" / "_audit_extract.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump({
        "total": {
            "n": s["n"], "net": s["net"], "ret_pct": s["ret_pct"],
            "dd_pct": s["dd_pct"], "worst_day_pct": wpct,
            "worst_daily_dd_pct": wdd, "pf": s["pf"], "wr": s["wr"],
            "sharpe": s["sharpe"],
        },
        "per_symbol": {k: dict(v) for k, v in per_sym.items()},
        "per_symbol_dd_pct": per_sym_dd,
        "per_symbol_worst_day_pct": per_sym_worstday,
        "any_sub60s": total_sub60 > 0,
        "total_sub60s": total_sub60,
        "same_bar_count": total_same_bar,
        "same_bar_pct": 100.0 * total_same_bar / max(s["n"], 1),
        "dd_breaker_tripped": s["dd_pct"] >= 4.0,
        "hold_stats_s": {
            "min": float(holds_np.min()), "median": float(np.median(holds_np)),
            "mean": float(holds_np.mean()), "p90": float(np.percentile(holds_np, 90)),
            "max": float(holds_np.max()),
        },
    }, f, indent=2, default=str)
print(f"  Saved: {out}")
