"""
Forensic analysis of v18 trades to determine whether the "winning stop_loss"
exits are (a) legitimate break-even-trail outcomes, or (b) artifacts of the
BB-overshoot SL-direction bug.

For each trade we reconstruct:
  - SL distance at entry (R_dist) — if tiny and on wrong side, this is the bug
  - Exit fill - entry_price  — should have SAME SIGN as side for a win
  - bars_held  — 0 for intrabar exits
  - realised_R — size of win/loss in R units

Classification:
  BUG_SUSPECT  = exit_reason=="stop_loss" AND bars_held==0 AND realised_R > 0
                 AND (exit - entry) * side > 0   (SL was on wrong side)
  BE_TRAIL     = exit_reason=="stop_loss" AND realised_R > 0 AND bars_held >= 1
                 (classic trailed stop)
  REAL_SL_HIT  = exit_reason=="stop_loss" AND realised_R <= 0
  REAL_TP      = exit_reason=="take_profit"
"""
import json
from collections import Counter

TRADES_PATH = "Results/v18_100000_3m_trades.json"


def classify(t):
    side = t["side"]
    pnl_pts = (t["exit_price"] - t["entry_price"]) * side
    r = t["realised_R"]
    bh = t["bars_held"]
    reason = t["exit_reason"]

    if reason == "take_profit":
        return "REAL_TP"
    if reason == "stop_loss":
        if r <= 0:
            return "REAL_SL_HIT"
        # Winning "stop_loss" — now split by bars_held
        if bh == 0:
            return "BUG_SUSPECT_INTRABAR"
        return "BE_TRAIL"
    return f"OTHER_{reason}"


def main():
    with open(TRADES_PATH) as f:
        trades = json.load(f)

    buckets = Counter()
    sum_pnl = {}
    for t in trades:
        c = classify(t)
        buckets[c] += 1
        sum_pnl[c] = sum_pnl.get(c, 0.0) + t["net_pnl"]

    print(f"Total trades : {len(trades)}")
    print(f"Total net P&L: ${sum(t['net_pnl'] for t in trades):,.2f}\n")

    print(f"{'Bucket':<28} {'N':>5} {'%':>6} {'Net P&L':>14}")
    print("-" * 60)
    for k, n in buckets.most_common():
        pct = 100.0 * n / len(trades)
        print(f"{k:<28} {n:>5} {pct:>5.1f}%  ${sum_pnl[k]:>+12,.2f}")

    print("\n=== First 10 winning stop_loss trades - raw rows ===")
    print(f"{'Sym':<8}{'S':<3}{'Entry':<12}{'Exit':<12}{'dPts':<10}"
          f"{'R_dist':<10}{'R':<8}{'BH':<4}{'Net$':<10}")
    n = 0
    for t in trades:
        if t["exit_reason"] == "stop_loss" and t["realised_R"] > 0:
            dp = (t["exit_price"] - t["entry_price"]) * t["side"]
            print(f"{t['symbol']:<8}{t['side']:<3}{t['entry_price']:<12.2f}"
                  f"{t['exit_price']:<12.2f}{dp:<10.2f}"
                  f"{t['R_dist']:<10.2f}{t['realised_R']:<8.3f}"
                  f"{t['bars_held']:<4}{t['net_pnl']:<10.2f}")
            n += 1
            if n >= 10:
                break


if __name__ == "__main__":
    main()
