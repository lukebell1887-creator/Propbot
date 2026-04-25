#!/usr/bin/env python3
"""
_verify_180_vs_165.py — apples-to-apples A/B.

Run the reference v23+no-chase pipeline (the one Luke quoted) at BOTH
0.165 % and 0.180 %, with NO stress warping, NO daily halt, NO DD breaker
— identical to the risk sweep that produced 273 trades / $29,540 at 0.180 %.

Also run the same risks through the 180bps stress-test pipeline (with
news rails + daily halt + DD breaker + baseline scenario) to prove
that the trade-count difference comes from the EXTRA FILTERS, not the
engine.
"""
from __future__ import annotations
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

# --- Reference pipeline (matches Scripts/backtest_v23_nochase_risk_sweep.py)
from Scripts.backtest_v23_nochase import build_base_trades, run_from_base

print("=" * 90)
print("  A/B VERIFICATION — reference pipeline (the one Luke quoted)")
print("=" * 90)

for r in (0.00150, 0.00165, 0.00180):
    tr_base = build_base_trades(r, news_rails=True)
    result  = run_from_base(tr_base, r, no_chase_cooldown_s=300.0)
    print(f"  risk={r*100:.3f}%   n={result['n']:>4}   "
          f"net=${result['net']:>+8,.0f}   DD={result['dd_pct']:>5.2f}%   "
          f"PF={result['pf']:.2f}   WR={result['wr']*100:.1f}%")

print()
print("=" * 90)
print("  Now the SAME risks through the 180bps stress-test harness (+breaker+halt)")
print("=" * 90)

# Patch the harness to accept risk as a parameter
import Scripts.stress_test_v25_180bps as s180
from src.dynamic_sizer_v21 import MertonGZSizerConfig

for risk in (0.00165, 0.00180):
    s180.V25_180_SIZER_CFG = MertonGZSizerConfig(
        base_risk_pct=risk, cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )
    r = s180.run_one_scenario("baseline")
    print(f"  risk={risk*100:.3f}%   n={r['n']:>4}   "
          f"net=${r['net']:>+8,.0f}   DD={r['dd_pct']:>5.2f}%   "
          f"PF={r['pf']:.2f}   WR={r['wr']*100:.1f}%   "
          f"breaker_trips={r['breaker_trips']}   "
          f"breaker_dropped={r['breaker_dropped']}   "
          f"chases_dropped={r['chases_dropped']}   "
          f"daily_halts={r['daily_halts']}")

print()
print("=" * 90)
print("  EXPECTED: reference rows should match Luke's risk-sweep table exactly")
print("  EXPECTED: 180bps-harness rows should show whether extra filters cut trades")
print("=" * 90)
