#!/usr/bin/env python3
"""Engine-truth Pareto sweep: squeeze max PnL without breaching user's 4% DD cap."""
from __future__ import annotations
import sys, json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from Scripts.preflight_checks import (
    SYMS, BALANCE,
    run_portfolio, apply_full_safety_rails,
    worst_single_day, ruin_probs,
    MertonGZSizerConfig,
)
from Scripts.backtest_v22_lean_uk5 import stats
from Scripts.backtest_v23_locked import (
    load_news_events, apply_news_entry_block, apply_news_flatten,
    build_price_lookup,
)
from src.dd_breaker import apply_dd_breaker

NEWS_CSV = ROOT / "data" / "news" / "tier1_2026.csv"
events = load_news_events(NEWS_CSV)


def run_cfg(base_risk, cap_mult, gamma):
    cfg = MertonGZSizerConfig(
        base_risk_pct=base_risk, cap_mult=cap_mult, gamma=gamma,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )
    raw, tmin, tmax, _drop, streams = run_portfolio(SYMS, cfg)
    pl = build_price_lookup(streams)
    raw, _ = apply_news_entry_block(raw, events, buffer_min=15)
    raw, _ = apply_news_flatten(raw, events, pl, minutes_before=2)
    tr = apply_full_safety_rails(raw, slippage_ticks=1.0)
    s = stats(tr)
    _, wpct, wdd, _ = worst_single_day(tr)
    pnls = np.array([t.net_pnl for t in tr], dtype=float)
    r = ruin_probs(pnls, caps=(4.0, 5.0, 10.0))
    _, br4 = apply_dd_breaker(tr, starting_balance=BALANCE, halt_pct=0.04)
    return dict(base=base_risk * 100, cap=cap_mult, g=gamma,
                n=s["n"], pnl=s["net"], dd=s["dd_pct"],
                wday=wpct, wdailydd=wdd, pf=s["pf"], sh=s["sharpe"],
                r4=r.get("ruin4", 0.0),
                r5=r.get("ruin5", 0.0),
                r10=r.get("ruin10", 0.0),
                maxseen=br4.max_dd_pct_seen * 100,
                breakerHits=br4.total_halts)


# Grid: base_risk × cap_mult × γ  (keep γ=2 moderate, γ=3 conservative)
base_risks = [0.0011, 0.0015, 0.0020, 0.0025, 0.0030]
caps       = [3.0, 4.0, 5.0]
gammas     = [2.0, 3.0]

print("=" * 130)
print("  ENGINE-TRUTH PARETO SWEEP — under user's 4% self-cap "
      "(5ers firm lines: 5% daily / 10% static)")
print("=" * 130)
print(f"  {'base%':>6} {'cap':>4} {'γ':>3} | {'N':>4} {'PnL':>10} "
      f"{'DD%':>5} {'Wday%':>7} {'DlyDD%':>7} {'PF':>5} {'Sh':>5} "
      f"{'r4%':>5} {'r5%':>5} {'r10%':>5} {'brkHits':>7}   verdict")
print("  " + "-" * 126)

rows = []
for b in base_risks:
    for c in caps:
        for g in gammas:
            r = run_cfg(b, c, g)
            # Pass: max DD < 4%, worst day < 4.5%, ruin@5% < 2%, breaker never fires,
            # ruin@10% effectively zero (< 0.1%)
            ok = (r["dd"] < 4.0 and r["wday"] > -4.5 and
                  r["r5"] < 2.0 and r["r10"] < 0.1 and
                  r["breakerHits"] == 0)
            verdict = "GO " if ok else "FAIL"
            rows.append({**r, "ok": ok})
            print(f"  {r['base']:>5.3f}% {r['cap']:>4.1f} {r['g']:>3.1f} | "
                  f"{r['n']:>4} ${r['pnl']:>+8,.0f} {r['dd']:>4.2f}% "
                  f"{r['wday']:>+6.2f}% {r['wdailydd']:>6.2f}% "
                  f"{r['pf']:>4.2f} {r['sh']:>+4.2f} "
                  f"{r['r4']:>4.1f}% {r['r5']:>4.1f}% {r['r10']:>4.1f}% "
                  f"{r['breakerHits']:>7d}   {verdict}")

viable = [x for x in rows if x["ok"]]
if viable:
    best = max(viable, key=lambda x: x["pnl"])
    print("\n" + "=" * 130)
    print(f"  CHAMPION (max PnL with DD<4%, WorstDay<4.5%, ruin@5%<2%, ruin@10%<0.1%, "
          f"breaker never fires):")
    print(f"     base_risk_pct = {best['base']:.3f}%    cap_mult = {best['cap']}    "
          f"γ = {best['g']}")
    print(f"     PnL = ${best['pnl']:+,.0f}   DD = {best['dd']:.2f}%   "
          f"Worst day = {best['wday']:+.2f}%   "
          f"ruin@5% = {best['r5']:.1f}%   ruin@10% = {best['r10']:.2f}%   "
          f"PF = {best['pf']:.2f}")
else:
    print("\n  No viable config in grid.")

out = ROOT / "Results" / "audit_v23_squeeze.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(rows, f, indent=2, default=str)
print(f"\nSaved: {out}")
