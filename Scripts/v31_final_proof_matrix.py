"""
v31_final_proof_matrix.py — THE FINAL TEST.

Locks the production config:
    risk        = 0.185%
    defense     = Layer 1 (5pt cap + 1.5x time-fallback)
    daily halt  = 4.00%
    fivers rail = 5.00%

And tests it against EVERY slip scenario from "no slip" to "true catastrophe":

  A)  Uniform slip stress       1pt, 2pt, 5pt, 10pt, 15pt, 20pt, 25pt, 30pt
  B)  Bar-microstructure model  optimistic / realistic / pessimistic / catastrophic
  C)  Compared head-to-head     vs NO defense at same risk

Outputs a single decision-grade table the user can show to anyone.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Scripts.v31_proof_pipeline import (   # noqa: E402
    ROOT, ADVERSITY_SCENARIOS,
    load_trades, load_bars, precompute_trade_metadata,
    replay_trades,
)
from Scripts.v31_stress_uniform import replay_uniform   # noqa: E402

# LOCKED PRODUCTION CONFIG ---------------------------------------------------
RISK          = 0.00185      # 0.185%
DEFENSE       = "layer1"
COMPARE       = "none"       # show what we save vs no defense
UNIFORM_SLIPS = [1, 2, 5, 10, 15, 20, 25, 30]


def main() -> int:
    print("=" * 92)
    print("  v31 FINAL PROOF MATRIX")
    print("  ----------------------")
    print(f"  Production config:  risk={RISK*100:.3f}%   defense={DEFENSE}   "
          f"daily-halt=4.00%   5%ers-rail=5.00%")
    print("=" * 92)

    print("\nLoading data ...")
    trades = load_trades()
    bars   = {s: load_bars(s) for s in ("DE40", "US30", "US500", "XAUUSD")}
    enriched = precompute_trade_metadata(trades, bars)
    n_stops = sum(1 for t in enriched if t["_is_stopout"])
    print(f"  Loaded {len(enriched)} trades, {n_stops} stop-outs across "
          f"{len(bars)} symbols")

    # ---------------------------------------------------------------------
    # A) UNIFORM SLIP STRESS (every index stop forced to N pt)
    # ---------------------------------------------------------------------
    rows_uniform = []
    for slip in UNIFORM_SLIPS:
        none_r   = replay_uniform(enriched, float(slip), RISK, COMPARE)
        layer1_r = replay_uniform(enriched, float(slip), RISK, DEFENSE)
        rows_uniform.append({
            "scenario":      f"Uniform {slip}pt every index stop",
            "none_pnl":      none_r["total_pnl"],
            "none_dd":       none_r["max_dd_pct"],
            "none_halts":    none_r["halt_days"],
            "layer1_pnl":    layer1_r["total_pnl"],
            "layer1_dd":     layer1_r["max_dd_pct"],
            "layer1_halts":  layer1_r["halt_days"],
            "delta_pnl":     layer1_r["total_pnl"] - none_r["total_pnl"],
            "delta_dd":      layer1_r["max_dd_pct"] - none_r["max_dd_pct"],
        })

    # ---------------------------------------------------------------------
    # B) BAR-MICROSTRUCTURE SCENARIOS (realistic distribution of slip)
    # ---------------------------------------------------------------------
    rows_micro = []
    for label, adv in ADVERSITY_SCENARIOS.items():
        none_r   = replay_trades(enriched, adv, RISK, COMPARE)
        layer1_r = replay_trades(enriched, adv, RISK, DEFENSE)
        rows_micro.append({
            "scenario":      f"Bar-micro: {label}",
            "none_pnl":      none_r["total_pnl"],
            "none_dd":       none_r["max_dd_pct"],
            "none_halts":    none_r["halt_days"],
            "layer1_pnl":    layer1_r["total_pnl"],
            "layer1_dd":     layer1_r["max_dd_pct"],
            "layer1_halts":  layer1_r["halt_days"],
            "delta_pnl":     layer1_r["total_pnl"] - none_r["total_pnl"],
            "delta_dd":      layer1_r["max_dd_pct"] - none_r["max_dd_pct"],
        })

    # ---------------------------------------------------------------------
    # PRINT — head-to-head decision table
    # ---------------------------------------------------------------------
    def header():
        print()
        print(f"  {'Scenario':<38} | {'NO DEFENSE':>20} | {'LAYER 1':>20} | "
              f"{'Δ PnL':>8} {'Δ DD':>7}")
        print(f"  {'':<38} | {'PnL ($)   DD%':>20} | {'PnL ($)   DD%':>20} | "
              f"{'':>8} {'':>7}")
        print("  " + "-" * 110)

    def line(r):
        none_breach   = "!!!" if r["none_dd"]   >= 5.0 else "   "
        layer1_breach = "!!!" if r["layer1_dd"] >= 5.0 else "   "
        print(f"  {r['scenario']:<38} | "
              f"${r['none_pnl']:>9,.0f}  {r['none_dd']:>5.2f}%{none_breach}| "
              f"${r['layer1_pnl']:>9,.0f}  {r['layer1_dd']:>5.2f}%{layer1_breach}| "
              f"${r['delta_pnl']:>+7,.0f} {r['delta_dd']:>+6.2f}pp")

    print("\n" + "=" * 92)
    print("  SECTION A — UNIFORM SLIP STRESS  (every index stop = N pt)")
    print("=" * 92)
    header()
    for r in rows_uniform:
        line(r)

    print("\n" + "=" * 92)
    print("  SECTION B — BAR-MICROSTRUCTURE SCENARIOS  (realistic slip distribution)")
    print("=" * 92)
    header()
    for r in rows_micro:
        line(r)

    # ---------------------------------------------------------------------
    # SUMMARY
    # ---------------------------------------------------------------------
    breaches_none  = sum(1 for r in rows_uniform + rows_micro
                          if r["none_dd"] >= 5.0)
    breaches_l1    = sum(1 for r in rows_uniform + rows_micro
                          if r["layer1_dd"] >= 5.0)
    worst_dd_none  = max(r["none_dd"]   for r in rows_uniform + rows_micro)
    worst_dd_l1    = max(r["layer1_dd"] for r in rows_uniform + rows_micro)
    avg_pnl_delta  = sum(r["delta_pnl"] for r in rows_uniform + rows_micro) / \
                     len(rows_uniform + rows_micro)

    print("\n" + "=" * 92)
    print("  SUMMARY")
    print("=" * 92)
    print(f"  Total scenarios tested:                    {len(rows_uniform)+len(rows_micro)}")
    print(f"  Scenarios where NO DEFENSE breaches 5%:    {breaches_none}")
    print(f"  Scenarios where LAYER 1 breaches 5%:       {breaches_l1}")
    print(f"  Worst max-DD with NO DEFENSE:              {worst_dd_none:.2f}%")
    print(f"  Worst max-DD with LAYER 1:                 {worst_dd_l1:.2f}%   "
          f"(margin to 5% rail: {5.0 - worst_dd_l1:.2f}pp)")
    print(f"  Average PnL delta (Layer 1 vs no defense): ${avg_pnl_delta:+,.0f}")

    # ---------------------------------------------------------------------
    # SAVE
    # ---------------------------------------------------------------------
    out = ROOT / "Results" / "v31_final_proof_matrix.json"
    out.write_text(json.dumps({
        "config": {"risk_pct": RISK*100, "defense": DEFENSE,
                   "daily_halt": 4.0, "fivers_rail": 5.0},
        "uniform_stress":   rows_uniform,
        "microstructure":   rows_micro,
        "summary": {
            "scenarios_tested":           len(rows_uniform)+len(rows_micro),
            "breaches_no_defense":        breaches_none,
            "breaches_layer1":            breaches_l1,
            "worst_dd_no_defense":        worst_dd_none,
            "worst_dd_layer1":            worst_dd_l1,
            "margin_to_rail_layer1_pp":   5.0 - worst_dd_l1,
            "avg_pnl_delta_layer1":       avg_pnl_delta,
        },
    }, indent=2))
    print(f"\n  Saved -> {out.relative_to(ROOT)}")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
