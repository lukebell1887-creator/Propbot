"""
v31_stress_uniform.py — answers the question:
    "What if EVERY stop has 15pt of slip, every day, forever?"

This is the LITERAL worst-case stress test the user demanded. Unlike the
bar-microstructure model in v31_proof_pipeline.py (where slip is bounded
by the actual bar's range), this script FORCES a fixed slip onto every
index stop, ignoring whether the bar even had that much movement.

It's physically impossible in real life — the broker can't fill outside
the bar's range — but it gives an absolute upper bound on how bad slip
can get if calibration is wrong.

Sweeps: uniform_slip ∈ {5, 10, 15, 20, 25} pt
        risk_pct     ∈ {0.170%, 0.200%}
        defense      ∈ {none, layer1}
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

# Reuse precompute + replay from the proof pipeline
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from Scripts.v31_proof_pipeline import (   # noqa: E402
    LAYER1_CAPS, LAYER1_FALLBACK_MULT, START_BALANCE,
    DAILY_HALT, FIVERS_LIMIT, ROOT,
    load_trades, load_bars, precompute_trade_metadata,
)

# How much slip to force per index stop (XAUUSD treated separately because
# its bar_excess is naturally tiny — applying 15pt to gold makes no sense).
UNIFORM_SLIP_PTS = [5.0, 10.0, 15.0, 20.0, 25.0]
RISK_LEVELS      = [0.00170, 0.00200]
DEFENSE_VARIANTS = ["none", "layer1"]


def replay_uniform(
    enriched: list[dict],
    uniform_slip: float,
    risk_pct: float,
    defense: str,
    base_risk: float = 0.00170,
) -> dict:
    """Like replay_trades() but applies a UNIFORM slip to every index stop
    instead of bar_excess × adversity. XAUUSD slip is fixed at 0.5pt
    (a generous cap reflecting metals microstructure)."""
    risk_scaler = risk_pct / base_risk

    equity     = START_BALANCE
    peak       = equity
    max_dd_pct = 0.0
    breach     = False

    cur_day      = None
    day_start_eq = equity
    halted_today = False
    halt_days    = 0

    for t in enriched:
        day = t["_day"]
        if cur_day != day:
            cur_day = day
            day_start_eq = equity
            halted_today = False
        if halted_today:
            continue

        sym = t["symbol"]

        # FORCE uniform slip on every stop (the whole point of this script)
        if t["_is_stopout"]:
            slip = uniform_slip if sym != "XAUUSD" else 0.5
            # Clip to bar_excess if smaller (broker can't fill outside the bar)
            slip = min(slip, t["_bar_excess"]) if t["_bar_excess"] > 0 else slip
        else:
            slip = 0.0

        # Layer 1 cap
        if defense == "layer1" and slip > 0:
            cap = LAYER1_CAPS.get(sym, 5.0)
            if slip > cap:
                slip = cap * LAYER1_FALLBACK_MULT

        if t["_is_stopout"]:
            extra_R_loss = slip / t["_sl_distance"]
            new_R   = t["realised_R"] - extra_R_loss
            new_pnl = t["net_pnl"] * (new_R / t["realised_R"])
        else:
            new_pnl = t["net_pnl"]

        new_pnl *= risk_scaler

        equity += new_pnl
        if equity > peak:
            peak = equity
        dd_from_peak = (peak - equity) / peak
        if dd_from_peak > max_dd_pct:
            max_dd_pct = dd_from_peak

        day_dd_pct = (day_start_eq - equity) / day_start_eq
        if day_dd_pct >= DAILY_HALT:
            halted_today = True
            halt_days   += 1
        if day_dd_pct >= FIVERS_LIMIT:
            breach = True

    return {
        "uniform_slip": uniform_slip,
        "risk_pct":     risk_pct * 100,
        "defense":      defense,
        "total_pnl":    equity - START_BALANCE,
        "max_dd_pct":   max_dd_pct * 100.0,
        "breach_5pct":  breach,
        "halt_days":    halt_days,
    }


def main() -> int:
    print("=" * 82)
    print("  v31 UNIFORM SLIP STRESS TEST")
    print("  --------------------------------")
    print("  Forces EVERY index stop (DE40/US30/US500) to slip a fixed N pts")
    print("  (clipped only by actual bar range). XAUUSD slip fixed at 0.5pt.")
    print("=" * 82)

    print("\nLoading data ...")
    trades = load_trades()
    bars   = {s: load_bars(s) for s in ("DE40", "US30", "US500", "XAUUSD")}
    enriched = precompute_trade_metadata(trades, bars)
    n_stops = sum(1 for t in enriched if t["_is_stopout"])
    print(f"  {len(enriched)} trades, {n_stops} stop-outs (none-XAU = "
          f"{sum(1 for t in enriched if t['_is_stopout'] and t['symbol']!='XAUUSD')})")

    rows = []
    for slip in UNIFORM_SLIP_PTS:
        for risk in RISK_LEVELS:
            for defense in DEFENSE_VARIANTS:
                rows.append(replay_uniform(enriched, slip, risk, defense))

    # ----  print  --------------------------------------------------------
    print()
    print("=" * 82)
    print(f"  {'Slip':>5} {'Risk%':>6} {'Defense':<8} {'PnL ($)':>11} "
          f"{'MaxDD%':>7} {'Breach':>7} {'Halts':>6}")
    print("  " + "-" * 70)
    for r in rows:
        print(f"  {r['uniform_slip']:>4.0f}pt {r['risk_pct']:>5.3f}% "

              f"{r['defense']:<8} ${r['total_pnl']:>10,.0f} "
              f"{r['max_dd_pct']:>6.2f}% "
              f"{('YES' if r['breach_5pct'] else 'no'):>7} "
              f"{r['halt_days']:>6}")

    out_path = ROOT / "Results" / "v31_stress_uniform.json"
    out_path.write_text(json.dumps(rows, indent=2))
    print(f"\n  Saved -> {out_path.relative_to(ROOT)}")

    print("\n" + "=" * 82)
    print("  KEY INTERPRETATIONS")
    print("=" * 82)
    # 15pt rows
    rows15 = [r for r in rows if r["uniform_slip"] == 15.0]
    for r in rows15:
        print(f"   slip=15pt  risk={r['risk_pct']:.3f}% defense={r['defense']:<6} "
              f"-> PnL ${r['total_pnl']:>8,.0f}  DD {r['max_dd_pct']:.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
