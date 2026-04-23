#!/usr/bin/env python3
"""analyze_phd_grid.py — extract key findings from phd_grid_search_orb_v20.json.

Shows:
  - Top 10 overall by J
  - How profitable configs distribute across each axis (marginal effects)
  - Per-symbol breakdown
  - Recommended config (simple, robust)
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "Results" / "phd_grid_search_orb_v20.json"

if not PATH.exists():
    print(f"ERROR: {PATH} not found — run Scripts/phd_grid_search_orb.py first")
    sys.exit(1)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

data = json.loads(PATH.read_text())
all_r = data["all"]
valid = [r for r in all_r if r.get("J") is not None]

print("=" * 100)
print(f"  ORB v20 PhD GRID SEARCH — {len(valid)}/{len(all_r)} valid  (N>=20, DD<8%)")
print("=" * 100)
print(f"  Window: {data['window']['start'][:10]} -> {data['window']['end'][:10]}")
print(f"  Balance: ${data['balance']:,.0f}  |  Elapsed: {data['elapsed_sec']:.0f}s")
print()

# ---- TOP 10 by J ----
print(f"{'rank':>4}  {'J':>8}  {'N':>4}  {'WR':>5}  {'PF':>5}  {'PnL':>10}  "
      f"{'DD':>5}  |  or  tp1/tp2  sl   amp   nr   hurst")
print("-" * 100)
for i, r in enumerate(valid[:10], 1):
    c = r["cfg"]
    hurst = "trend" if c["hurst_min"] >= 0.55 else "all"
    nr = "Y" if c["require_nr7"] else "N"
    pf = r["pf"] if r.get("pf") is not None else 99.0
    print(f"{i:>4}  {r['J']:>8.0f}  {r.get('entries',0):>4}  "
          f"{r['win_rate']*100:>4.1f}%  {pf:>5.2f}  "
          f"${r['net_pnl']:>+8,.0f}  {r['max_dd_pct']:>4.1f}%  |  "
          f"{c['or_minutes']:>2}  {c['tp1']:.1f}/{c['tp2']:.1f}  "
          f"{c['sl_buffer']:.1f}  {c['amp_hurdle']:.1f}  {nr:<3}  {hurst}")
print()

# ---- Marginal effects: mean PnL per axis value ----
print("MARGINAL EFFECTS (mean net PnL across all 144 configs for each axis value):")
print("-" * 80)
axes = [
    ("or_minutes", lambda c: c["or_minutes"]),
    ("tp_ladder",  lambda c: f"{c['tp1']:.1f}/{c['tp2']:.1f}"),
    ("sl_buffer",  lambda c: c["sl_buffer"]),
    ("amp_hurdle", lambda c: c["amp_hurdle"]),
    ("nr7",        lambda c: c["require_nr7"]),
    ("hurst_gate", lambda c: "trending-only" if c["hurst_min"] >= 0.55 else "all-regimes"),
]
for name, key in axes:
    buckets = defaultdict(list)
    for r in all_r:
        buckets[key(r["cfg"])].append(r["net_pnl"])
    print(f"  {name:<12}: ", end="")
    rows = []
    for v, pnls in sorted(buckets.items(), key=lambda kv: str(kv[0])):
        mean_pnl = sum(pnls) / len(pnls)
        rows.append(f"{v}=${mean_pnl:+,.0f} (n={len(pnls)})")
    print("  ".join(rows))
print()

# ---- Per-symbol best ----
if "best_per_symbol" in data and data["best_per_symbol"]:
    print("BEST CONFIG PER SYMBOL:")
    print("-" * 80)
    for sym, d in data["best_per_symbol"].items():
        c = d["cfg"]
        s = d["stats"]
        hurst = "trend" if c["hurst_min"] >= 0.55 else "all"
        nr = "Y" if c["require_nr7"] else "N"
        print(f"  {sym:<7}  N={s['n']:>3}  WR={s['wr']*100:>4.1f}%  net=${s['net']:>+7,.0f}  |  "
              f"or={c['or_minutes']}  tp={c['tp1']:.1f}/{c['tp2']:.1f}  "
              f"sl={c['sl_buffer']:.1f}  amp={c['amp_hurdle']:.1f}  nr7={nr}  h={hurst}")
    print()

# ---- Recommendation (robust choice) ----
# Robust rule: choose the config among the top-10 with the smallest DD and N>=30
print("=" * 100)
robust_candidates = [r for r in valid[:15]
                     if r.get("entries", 0) >= 30 and r["max_dd_pct"] <= 3.5]
if not robust_candidates:
    robust_candidates = valid[:1]
rec = robust_candidates[0] if robust_candidates else None
if rec:
    c = rec["cfg"]
    print("RECOMMENDED CONFIG (robust pick from top-15, N>=30, DD<=3.5%):")
    print(f"  J={rec['J']:.0f}  N={rec.get('entries',0)}  WR={rec['win_rate']*100:.1f}%  "
          f"PF={rec['pf']:.2f}  PnL=${rec['net_pnl']:+,.0f}  DD={rec['max_dd_pct']:.2f}%")
    print()
    print(f"  or_minutes     = {c['or_minutes']}")
    print(f"  tp1/tp2 (×OR)  = {c['tp1']:.2f} / {c['tp2']:.2f}")
    print(f"  sl_buffer      = {c['sl_buffer']:.2f} × OR_range (beyond OR-mirror)")
    print(f"  amp_hurdle     = {c['amp_hurdle']:.2f}")
    print(f"  require_nr7    = {c['require_nr7']}")
    hmin, hmax = c["hurst_min"], c["hurst_max"]
    print(f"  hurst gate     = [{hmin:.2f}, {hmax:.2f}]"
          + ("  (trending only)" if hmin >= 0.55 else "  (disabled)"))
print("=" * 100)
