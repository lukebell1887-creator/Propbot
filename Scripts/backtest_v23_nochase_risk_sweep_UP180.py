#!/usr/bin/env python3
"""
backtest_v23_nochase_risk_sweep_UP180.py — EXTEND the risk sweep above 0.165%
using the SAME harness that produced the real $27,023 baseline. Adds 0.170%,
0.175%, 0.180% so we can answer Luke's question:

    "Can we stretch base_risk to 0.180% and still stay safe?"

We DO NOT apply the v25 4%-rolling-DD-breaker here. We only apply:
    - v23 news rails (entry block + flatten)
    - v23 safety rails (1 tick slippage, concurrency, etc.)
    - NO-CHASE-300s filter (v25 fix)
    - Merton-GZ sizer with its internal dd_cap=4% (same as live)

That is the CLEANEST apples-to-apples extension of the sweep that produced
$27,023 at 0.165%.
"""
from __future__ import annotations

import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from Scripts.backtest_v23_nochase import (
    build_base_trades, run_from_base, print_row,
)

COOLDOWN_S = 300.0
# Re-run the full curve so anyone can reproduce
RISKS = [0.00110, 0.00120, 0.00130, 0.00140, 0.00150,
         0.00165, 0.00170, 0.00175, 0.00180]


def main():
    print("=" * 140)
    print("  V23 + NO-CHASE(300s) — RISK SWEEP EXTENDED TO 0.180 %")
    print("  (same harness as backtest_v23_nochase_risk_sweep.py — just more risk steps)")
    print("=" * 140)
    print()

    out = []
    header = (f"{'tag':14s} | {'n':>4} | {'net':>9} | {'ret':>7} | {'DD':>6} | "
              f"{'PF':>4} | {'WR':>6} | {'Sharpe':>6} | {'worst_day':>9} | "
              f"{'daily_DD':>8} | chases")
    print(header)
    print("-" * len(header))

    for r in RISKS:
        tr_base = build_base_trades(r, news_rails=True)
        res = run_from_base(tr_base, r, no_chase_cooldown_s=COOLDOWN_S)
        tag = f"risk {r*100:.3f}%"
        print_row(tag, res)
        out.append({"risk": r, **res})

    # Find max risk that keeps DD <= 4%
    ok = [r for r in out if r["dd_pct"] <= 4.0]
    if ok:
        winner = max(ok, key=lambda r: r["net"])
        print()
        print(f"  BEST FEASIBLE (DD<=4%): risk={winner['risk']*100:.3f}%   "
              f"net=${winner['net']:,.0f}  DD={winner['dd_pct']:.2f}%  "
              f"worst_day={winner['worst_day_pct']:+.2f}%")

    # Also: best feasible under Luke's tighter self-halt (worst_day >= -4%)
    # i.e. "as long as no single day loses >4%, we're safe"
    ok_day4 = [r for r in out if r["worst_day_pct"] >= -4.0]
    if ok_day4:
        winner_day = max(ok_day4, key=lambda r: r["net"])
        print(f"  BEST under '4% daily halt' rule: risk={winner_day['risk']*100:.3f}%   "
              f"net=${winner_day['net']:,.0f}  DD={winner_day['dd_pct']:.2f}%  "
              f"worst_day={winner_day['worst_day_pct']:+.2f}%")

    # Also: best feasible under 5ers-strict (worst_day >= -5%, DD <= 8%)
    ok_5ers = [r for r in out if r["worst_day_pct"] >= -5.0 and r["dd_pct"] <= 8.0]
    if ok_5ers:
        winner_5 = max(ok_5ers, key=lambda r: r["net"])
        print(f"  BEST under '5ers rules' (day>=-5%, DD<=8%): risk={winner_5['risk']*100:.3f}%   "
              f"net=${winner_5['net']:,.0f}  DD={winner_5['dd_pct']:.2f}%  "
              f"worst_day={winner_5['worst_day_pct']:+.2f}%")

    out_path = ROOT / "Results" / "backtest_v23_nochase_risk_sweep_UP180.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\n  -> saved: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
