"""
Cost-stress the v15 OOS backtest.

Re-runs the 3-month OOS backtest with an aggressive hidden-slippage overlay
(`extra_cost_per_lot`) that simulates broker requotes, partial fills, and
weekend-gap slippage.

Usage:
    python Scripts\\stress_test_v15_costs.py --extra 5
    python Scripts\\stress_test_v15_costs.py --extra 10
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import src.smartbb_engine_v14 as e14   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extra", type=float, default=5.0,
                     help="extra_cost_per_lot round-trip, $")
    ap.add_argument("--balance", type=float, default=100_000.0)
    ap.add_argument("--months", type=int, default=3)
    a = ap.parse_args()

    # Monkey-patch the SmartBBV14Config default to add hidden slippage
    OrigCfg = e14.SmartBBV14Config
    @dataclass
    class StressedCfg(OrigCfg):   # type: ignore
        extra_cost_per_lot: float = a.extra
    e14.SmartBBV14Config = StressedCfg

    # now import & run the backtest — it will use the stressed config
    from Scripts.backtest_v15_oos_3month import run    # noqa: E402

    print()
    print("#" * 82)
    print(f"#  STRESS TEST  --  extra_cost_per_lot = ${a.extra:.2f}/lot ROUND-TRIP")
    print(f"#  This simulates the live-only slippage NOT in the default model:")
    print(f"#    * latency (slow VPS->broker fills)")
    print(f"#    * requotes on fast-moving news bars")
    print(f"#    * partial fills on large lot sizes")
    print("#" * 82)
    print()

    s = run(a.balance, a.months,
             ROOT / "Results" / "v15_ultimate_tuning.json",
             ROOT / "Results_stressed", 10_000)

    # Compare to baseline
    import json
    base = json.load(open(ROOT / "Results" / "v15_oos_100000_3m.json"))
    print("\n" + "#" * 82)
    print("#  STRESSED  vs  BASELINE  (same trades, stressed costs)")
    print("#" * 82)
    print(f"  Metric              Baseline           Stressed          Delta")
    print(f"  Net P&L          ${base['net_pnl']:>10,.2f}"
          f"       ${s['net_pnl']:>10,.2f}"
          f"   {s['net_pnl']-base['net_pnl']:>+10,.2f}")
    print(f"  Return            {base['pct_return']:>10.2f} %"
          f"       {s['pct_return']:>10.2f} %"
          f"   {s['pct_return']-base['pct_return']:>+10.2f} pp")
    print(f"  PF                {base['pf']:>10.2f}"
          f"       {s['pf']:>10.2f}"
          f"   {s['pf']-base['pf']:>+10.2f}")
    print(f"  Win rate          {base['win_rate']*100:>10.1f} %"
          f"       {s['win_rate']*100:>10.1f} %"
          f"   {(s['win_rate']-base['win_rate'])*100:>+10.1f} pp")
    print(f"  Max DD            {base['max_dd_pct']:>10.2f} %"
          f"       {s['max_dd_pct']:>10.2f} %"
          f"   {s['max_dd_pct']-base['max_dd_pct']:>+10.2f} pp")
    print("#" * 82)


if __name__ == "__main__":
    main()
