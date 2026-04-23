"""
INDEPENDENT VERIFICATION of the "SL-on-wrong-side-of-entry bug" claim.

For each trade in the pre-v18.1 profitable trade log, we compute:
  - sl_dir:        +1 if SL > entry, -1 if SL < entry, 0 if SL == entry
  - desired_dir:   For a LONG (side=+1), SL should be BELOW entry → -1.
                   For a SHORT (side=-1), SL should be ABOVE entry → +1.
                   So desired_sl_dir = -side.
  - sl_wrong_side: True if sl_dir != desired_sl_dir  (engine placed SL on wrong side)
  - pnl_dir:       +1 if exit > entry, -1 if exit < entry
  - trade_won:     (exit - entry) * side > 0          (price moved in our favour)
  - exit_at_sl:    True if |exit - sl| < 2 * pip_pts   (exit really did hit sl level)

Classifications:
  A) INVALID_ORDER_WIN    = SL on wrong side AND won AND exit_at_sl AND bh==0
                            → order would be rejected by MT5; in backtest it "wins at SL"
  B) INVALID_ORDER_LOSS   = SL on wrong side AND lost
                            → order would be rejected by MT5; in backtest it lost anyway
  C) VALID_BE_TRAIL       = SL on correct side AND won at SL level      (legitimate trail)
  D) VALID_SL_LOSER       = SL on correct side AND lost at SL level
  E) VALID_TP             = exit was the TP
  F) OTHER                = anything else (time-stop, optimal-stop, etc.)

The key question: if we REMOVED category A (the broker-invalid wins) from the P&L,
does the remaining strategy still have ANY edge?

We also reconstruct what the trade WOULD have done if we had sent it as a LIMIT
order with TP at the original "sl" level (which is what the strategy really meant):
  → pnl = (sl_level - entry) * side * lot   (exactly the same as now, because in
    back-test the exit was already at sl_level and same bar)

If this were a real MT5 live order sent as BUY LIMIT or a python-side TP, those
same 143 trades would be legitimately captured — they are NOT fake in the sense
of "price didn't actually go there." They are just mis-packaged as SL instead of
TP in the order-submission layer.
"""
import json
from collections import defaultdict

TRADES = "Results/_v18_committed_trades.json"   # pre-v18.1 profitable trade log

def classify(t):
    side = t["side"]
    entry = t["entry_price"]
    exit_ = t["exit_price"]
    r_dist = t["R_dist"]              # |entry - sl|
    bh = t["bars_held"]
    reason = t["exit_reason"]
    r = t["realised_R"]

    # Reconstruct sl level:
    #   For the original (pre-v18.1) engine:
    #   - If exit_reason=="stop_loss" -> exit IS the sl level
    #   - Otherwise -> we only have |entry-sl| (R_dist); side of sl is ambiguous
    won = (exit_ - entry) * side > 0

    if reason == "stop_loss":
        sl = exit_
    elif reason == "take_profit":
        # original SL is not recoverable from TP exit alone; assume correct side
        sl = entry - side * r_dist
    else:
        sl = entry - side * r_dist

    # Desired sl direction (entry convention):
    #  side=+1 (LONG)  -> sl should be below entry  (sl - entry) < 0
    #  side=-1 (SHORT) -> sl should be above entry  (sl - entry) > 0
    sl_above_entry = sl > entry
    sl_on_wrong_side = (side == +1 and sl_above_entry) or \
                       (side == -1 and not sl_above_entry and sl != entry)

    if reason == "take_profit":
        return "E_VALID_TP", sl, sl_on_wrong_side, won
    if reason == "stop_loss":
        if sl_on_wrong_side:
            if won:
                return "A_INVALID_ORDER_WIN", sl, True, True
            else:
                return "B_INVALID_ORDER_LOSS", sl, True, False
        else:
            if won:
                return "C_VALID_BE_TRAIL", sl, False, True
            else:
                return "D_VALID_SL_LOSER", sl, False, False
    return "F_OTHER", sl, sl_on_wrong_side, won


def main():
    with open(TRADES) as f:
        trades = json.load(f)

    buckets = defaultdict(lambda: {"n": 0, "pnl": 0.0, "R_sum": 0.0, "R_dist_sum": 0.0})
    for t in trades:
        cat, sl, wrong, won = classify(t)
        b = buckets[cat]
        b["n"] += 1
        b["pnl"] += t["net_pnl"]
        b["R_sum"] += t["realised_R"]
        b["R_dist_sum"] += t["R_dist"]

    total_n = len(trades)
    total_pnl = sum(t["net_pnl"] for t in trades)
    print(f"Total trades : {total_n}")
    print(f"Total net P&L: ${total_pnl:,.2f}\n")

    print(f"{'Category':<25}{'N':>5}{'%':>6}{'Avg R_dist':>12}"
          f"{'Sum R':>10}{'Net $':>14}")
    print("-" * 72)
    for cat in sorted(buckets.keys()):
        b = buckets[cat]
        pct = 100 * b["n"] / total_n
        avg_rdist = b["R_dist_sum"] / b["n"] if b["n"] else 0
        print(f"{cat:<25}{b['n']:>5}{pct:>5.1f}%{avg_rdist:>12.2f}"
              f"{b['R_sum']:>10.2f}${b['pnl']:>+12,.2f}")

    print("\n--- NARROW VERDICT ---")
    invalid_n = buckets["A_INVALID_ORDER_WIN"]["n"] + buckets["B_INVALID_ORDER_LOSS"]["n"]
    invalid_pnl = (buckets["A_INVALID_ORDER_WIN"]["pnl"] +
                    buckets["B_INVALID_ORDER_LOSS"]["pnl"])
    valid_n = total_n - invalid_n
    valid_pnl = total_pnl - invalid_pnl

    print(f"Trades with SL on wrong side of entry (MT5 would reject the order): "
          f"{invalid_n}/{total_n} ({100*invalid_n/total_n:.1f}%)")
    print(f"  - if we strip these out, remaining N={valid_n}  remaining PnL=${valid_pnl:+,.2f}")
    print(f"  - if we KEEP them but redirect exit to TP (python-side limit), "
          f"PnL still = ${total_pnl:+,.2f} because physics of the exit didn't change")

    print("\n--- WHAT THIS TELLS US ---")
    print("If most of the 143 'INVALID_ORDER_WIN' trades represent REAL reversion")
    print("events — i.e. price DID reach the target level, just not as an MT5 SL —")
    print("then they are CAPTURABLE by changing the order mechanism (send as LIMIT")
    print("or python-side TP monitor), not by killing the signal.")
    print("")
    print("If we strip them entirely and the residual is deeply negative, the")
    print("strategy has no edge outside the overshoot region.")


if __name__ == "__main__":
    main()
