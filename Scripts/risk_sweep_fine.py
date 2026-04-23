#!/usr/bin/env python3
"""Fine-grained risk sweep — finds the optimal risk between 0.075% and 0.150%.

For each risk level, runs the 4-pair portfolio backtest with full safety
rails and the 5000-path stationary-block bootstrap, then prints every
metric the prop-firm rules care about.
"""
from __future__ import annotations
import sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from Scripts.preflight_checks import run, SYMS, BALANCE   # reuse all the machinery

RISKS = [
    0.00080, 0.00090, 0.00100, 0.00105, 0.00110,
    0.00115, 0.00120, 0.00125, 0.00130,
]


def main():
    print("=" * 120)
    print("  FINE-GRAINED RISK SWEEP   (4-pair portfolio, 5000-path bootstrap, full safety rails)")
    print("=" * 120)

    rows = []
    for r in RISKS:
        print(f"  [running]  risk = {r*100:.3f} %  ...", flush=True)
        rows.append(run(r))

    # Annualised-compound projection
    def compound_12m(three_month_ret_pct):
        m = (1 + three_month_ret_pct / 100.0) ** (1/3) - 1
        return (1 + m) ** 12 - 1   # fraction

    hdr = ("RISK     N   PnL(3m)     DD%   WrstDay%   DailyDD%    "
           "Ruin3%  Ruin4%  Ruin5%  |  MonthlyGrow  Year1-Bal    Year1-Ret   Payout(80%)/mo")
    print("\n  " + hdr)
    print("  " + "-" * len(hdr))
    for r in rows:
        m_ret   = (1 + r["ret_pct"]/100) ** (1/3) - 1
        y1_ret  = (1 + m_ret) ** 12 - 1
        y1_bal  = BALANCE * (1 + y1_ret)
        # Steady-state monthly payout (80% split) from Month 6+ on funded account
        # assume flat balance around Year-1 midpoint ≈ BALANCE × (1+y1_ret)^0.5
        mid_bal = BALANCE * (1 + y1_ret) ** 0.5
        monthly_profit = mid_bal * m_ret
        payout = 0.80 * monthly_profit

        print(f"  {r['risk']*100:>5.3f}%  {r['n']:>3}  "
              f"${r['net']:>+8,.0f}  {r['dd_pct']:>4.2f}   "
              f"{r['worst_day_pct']:>+6.2f}     "
              f"{r['worst_daily_dd_pct']:>5.2f}      "
              f"{r['ruin3']:>5.1f}   {r['ruin4']:>5.1f}   {r['ruin5']:>5.1f}  |  "
              f"{m_ret*100:>5.2f}%       "
              f"${y1_bal:>9,.0f}  "
              f"{y1_ret*100:>+6.1f}%     "
              f"${payout:>6,.0f}")

    # --- which risks pass hard rules? ---
    print("\n" + "=" * 120)
    print("  RULE-COMPLIANCE VERDICT (5ers real rules: 5% static DD + 5% daily DD)")
    print("=" * 120)
    best = None
    for r in rows:
        hard_pass = (
            r["dd_pct"]             < 5.0 and
            r["worst_daily_dd_pct"] < 5.0 and
            r["ruin5"]              < 5.0 and
            r["dur"]["sub60s"]      == 0
        )
        tight_pass = r["ruin4"] < 5.0     # extra buffer vs 5% cap
        label = "[OK]" if hard_pass else "[FAIL]"
        strict = "[TIGHT-OK]" if tight_pass else "[TIGHT-FAIL]"
        y1_ret = (1 + r["ret_pct"]/100) ** 4 - 1
        print(f"    risk={r['risk']*100:5.3f}%  PnL=${r['net']:+,.0f}  "
              f"ruin5={r['ruin5']:.1f}%  ruin4={r['ruin4']:.1f}%  "
              f"Y1-ret={y1_ret*100:+.1f}%   {label}  {strict}")
        if hard_pass and tight_pass:
            if best is None or r["net"] > best["net"]:
                best = r

    if best:
        print(f"\n  >>> BEST risk that passes BOTH hard-rules AND tight (ruin@4% <5%) filter:")
        print(f"      risk = {best['risk']*100:.3f} %")
        print(f"      3m PnL = ${best['net']:+,.0f}  DD = {best['dd_pct']:.2f}%  "
              f"ruin@5% = {best['ruin5']:.1f}%  ruin@4% = {best['ruin4']:.1f}%")

    # save
    out_json = ROOT / "Results" / "risk_sweep_fine.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"syms": SYMS, "balance": BALANCE, "rows": rows}, f, indent=2, default=str)
    print(f"\n  Saved: {out_json}")
    print("=" * 120)


if __name__ == "__main__":
    main()
