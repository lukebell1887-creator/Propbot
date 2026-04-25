#!/usr/bin/env python3
"""
backtest_v23_nochase_risk_sweep.py — with the no-chase 300s filter ON, how
much more risk can we take before hitting the 4% DD ceiling?

Runs: risk = 0.110% (baseline), 0.120%, 0.130%, 0.140%, 0.150%
      cooldown = 300s (5 minutes) — the sweet-spot from the main A/B.

Pass gate: DD <= 4.0%.
"""
from __future__ import annotations

import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from Scripts.backtest_v23_nochase import (
    build_base_trades, run_from_base, print_row, RISK,
)

COOLDOWN_S = 300.0
RISKS = [0.00110, 0.00120, 0.00130, 0.00140, 0.00150, 0.00165]

def main():
    print("=" * 140)
    print("  V23 + NO-CHASE(300s) — RISK SWEEP")
    print("=" * 140)
    print()

    out = []
    header = f"{'tag':16s} | {'n':>4} | {'net':>9} | {'ret':>7} | {'DD':>6} | {'PF':>4} | {'WR':>6} | {'Sharpe':>6} | {'worst_day':>9} | {'daily_DD':>8}"
    print(header)
    print("-" * len(header))

    for r in RISKS:
        # Each risk requires a fresh engine run (sizer scales differently)
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
        print(f"  BEST FEASIBLE: risk={winner['risk']*100:.3f}%   "
              f"net=${winner['net']:,.0f}  DD={winner['dd_pct']:.2f}%")

    out_path = ROOT / "Results" / "backtest_v23_nochase_risk_sweep.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"  -> saved: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
