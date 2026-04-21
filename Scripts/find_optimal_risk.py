"""
find_optimal_risk.py
--------------------
Sweeps risk per trade in 0.05 % increments and finds the single optimal
level that (a) MAXIMIZES expected monthly return while (b) keeping p99
bootstrap drawdown under the 5%ers $4000 daily cap, with a configurable
safety buffer.

Usage:  python Scripts/find_optimal_risk.py
"""
from __future__ import annotations
import json, random, statistics as st
from pathlib import Path

TRADES_FILE = Path(__file__).resolve().parent.parent / "Results" / "v16_SC_100000_3m_trades.json"
ACCOUNT     = 100_000
DAILY_CAP   = 0.04 * ACCOUNT        # 5%ers: 4 %  = $4,000
TOTAL_CAP   = 0.10 * ACCOUNT        # 5%ers: 10 % = $10,000
SAFETY_MULT = 1.5                   # require 1.5x margin below daily cap

def sim_dd(R, risk_pct, reps=3000, seed=42):
    random.seed(seed)
    usd_at_risk = ACCOUNT * risk_pct
    dds = []
    for _ in range(reps):
        order = random.sample(R, len(R))
        equity = ACCOUNT; peak = equity; max_dd = 0.0
        for r in order:
            equity += r * usd_at_risk
            peak = max(peak, equity)
            max_dd = min(max_dd, equity - peak)
        dds.append(-max_dd)
    dds.sort()
    return dds[int(0.50*reps)], dds[int(0.95*reps)], dds[int(0.99*reps)], dds[-1]

def main():
    trades = json.load(open(TRADES_FILE))
    R = [t.get("realised_R", t.get("R", 0.0)) for t in trades]
    n = len(R); mean_R = st.mean(R); trades_per_month = n / 3.0

    print("="*98)
    print("  OPTIMAL RISK SWEEP  -  finding THE single best risk/trade for 5%ers $100k")
    print("="*98)
    print(f"  {'risk%':>7}  {'$/trade':>9}  {'E[monthly]':>11}  {'p50 DD':>8}  "
          f"{'p95 DD':>8}  {'p99 DD':>8}  {'worst-case':>11}  safe?")
    print("  " + "-"*94)
    best = None
    for rp_bp in range(10, 201, 5):           # 0.10% -> 2.00% in 0.05% steps
        rp = rp_bp / 10000.0
        usd = ACCOUNT * rp
        gpt = mean_R * rp
        month = (1+gpt)**trades_per_month - 1
        p50, p95, p99, worst = sim_dd(R, rp)
        # 5%ers safety: p99 DD must be < daily_cap / SAFETY_MULT ($2,667)
        # AND worst-case must be < daily_cap ($4,000)
        safe = (p99 < DAILY_CAP / SAFETY_MULT) and (worst < DAILY_CAP)
        tag = "SAFE" if safe else ("fail p99" if p99 >= DAILY_CAP/SAFETY_MULT else "fail worst")
        if safe and (best is None or month > best[1]):
            best = (rp, month, usd, p50, p95, p99, worst)
        print(f"  {rp*100:>6.2f}%  ${usd:>8,.0f}  {month*100:>+10.1f}%  "
              f"${p50:>6,.0f}  ${p95:>6,.0f}  ${p99:>6,.0f}  ${worst:>9,.0f}  {tag}")
    print("  " + "-"*94)
    if best:
        rp, m, usd, p50, p95, p99, w = best
        print()
        print("  ============================================================================")
        print(f"  OPTIMAL = {rp*100:.2f}% per trade  =  ${usd:.0f} per trade on $100k")
        print(f"    expected monthly return (compounded): {m*100:+.1f}%")
        print(f"    p99 stress-test DD: ${p99:,.0f}  (5%ers daily cap = $4,000)")
        print(f"    safety buffer below daily cap: {DAILY_CAP/p99:.2f}x")
        print("  ============================================================================")
        print()
        print("  COMPARISON:")
        print(f"    current LIVE v15 = 0.50% / $500  -> monthly ~+14%  (too safe, under-betting)")
        print(f"    optimal for 5%ers = {rp*100:.2f}% / ${usd:.0f} -> monthly ~{m*100:+.0f}%  (the sweet spot)")
        print(f"    reckless 2.0%      -> monthly ~+70%  BUT p99 DD > $4k daily cap, will get kicked out")

if __name__ == "__main__":
    main()
