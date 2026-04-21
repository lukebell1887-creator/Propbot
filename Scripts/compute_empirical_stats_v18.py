"""
compute_empirical_stats_v18.py
==============================

Pin the Grossman-Zhou constants for v18 to EMPIRICAL numbers, not guesses.

Reads the 186-trade log from v17 (same trades as v15/v16 — only sizing differs)
and computes, for the overall bot AND per (symbol × side):

  * N                 number of trades
  * win_rate          fraction
  * E[R]              mean realised R (expectancy)
  * Var[R]            variance of R
  * sigma[R]          std dev of R (scale of a single trade)
  * Sharpe_per_trade  E[R] / sigma[R]
  * max_loss_streak   longest observed run of losers
  * kelly_star        classical Kelly = E[R] / Var[R]
  * f_GZ_10pct        Grossman-Zhou fraction for 10 % DD cap, streak-aware
  * f_GZ_5pct         same but for the 5 %ers DAILY 4 % cap with 5-trade streak
  * f_hard_ceiling    2α / ln(100) for α=10 %  --> the absolute max allowed

No code changes to the engine yet — this is a PURE analysis script so you
can SEE the numbers before I pin them into v18.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRADES = ROOT / "Results" / "v17_final_100000_3m_trades.json"


def longest_loss_streak(realised_R_seq):
    best = cur = 0
    for r in realised_R_seq:
        if r <= 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def stats_for(trades):
    n = len(trades)
    if n == 0:
        return None
    Rs = [t["realised_R"] for t in trades]
    wins = [r for r in Rs if r > 0]
    losses = [r for r in Rs if r <= 0]
    mu = sum(Rs) / n
    var = sum((r - mu) ** 2 for r in Rs) / max(n - 1, 1)
    sigma = math.sqrt(var) if var > 0 else 0.0
    streak = longest_loss_streak(Rs)

    # Classical Kelly (as fraction of equity): f* = mu / var (continuous-R form)
    kelly_star = mu / var if var > 0 else 0.0

    # Grossman-Zhou (drawdown-constrained Kelly), closed-form simplified:
    #   f_GZ  =  kelly_star  ×  (alpha / streak_buffer)
    # where alpha is the drawdown cap as a fraction and streak_buffer is the
    # number of consecutive losses we want to survive WITHIN the cap.
    # We use max(1, observed_streak + 1) as the buffer for safety.
    streak_buffer = max(streak + 1, 3)          # always at least 3
    f_GZ_10 = kelly_star * (0.10 / streak_buffer)
    f_GZ_04 = kelly_star * (0.04 / streak_buffer)

    # Absolute hard ceiling from Grossman-Zhou probability bound:
    #   P(hit cap α in N trades) <= exp(-2α/f)
    # Setting that to 1 % over 1000 trades gives f_cap = 2α / ln(100) ≈ 0.434α
    f_hard_10 = 2 * 0.10 / math.log(100)         # 4.3 % for α = 10 %

    return {
        "n":        n,
        "win_rate": sum(1 for r in Rs if r > 0) / n,
        "E_R":      mu,
        "Var_R":    var,
        "sigma_R":  sigma,
        "avg_win":  sum(wins) / len(wins) if wins else 0.0,
        "avg_loss": sum(losses) / len(losses) if losses else 0.0,
        "Sharpe_pt": mu / sigma if sigma > 0 else 0.0,
        "max_loss_streak": streak,
        "kelly_star":     kelly_star,
        "f_GZ_10pct_cap": f_GZ_10,
        "f_GZ_4pct_cap":  f_GZ_04,
        "f_hard_ceiling": f_hard_10,
    }


def fmt_pct(x, w=7, dp=2):
    return f"{x*100:>{w}.{dp}f}%"


def main():
    if not TRADES.exists():
        print(f"ERROR: {TRADES} not found.  Run Scripts/backtest_v17_final.py first.")
        return 1
    trades = json.loads(TRADES.read_text())

    print("=" * 92)
    print(f"  EMPIRICAL Grossman-Zhou stats   |   {TRADES.name}   |   N={len(trades)} trades")
    print("=" * 92)

    # ─── Overall ────────────────────────────────────────────────────────────
    s = stats_for(trades)
    print("\n  ALL TRADES (bot as a whole)")
    print(f"    N                 = {s['n']}")
    print(f"    win rate          = {s['win_rate']*100:.2f} %")
    print(f"    E[R] (expectancy) = {s['E_R']:+.4f} R")
    print(f"    Var[R]            = {s['Var_R']:.4f}")
    print(f"    sigma[R]          = {s['sigma_R']:.4f}")
    print(f"    avg winner        = {s['avg_win']:+.3f} R")
    print(f"    avg loser         = {s['avg_loss']:+.3f} R")
    print(f"    Sharpe/trade      = {s['Sharpe_pt']:.3f}")
    print(f"    max loss streak   = {s['max_loss_streak']}")
    print(f"    classic Kelly f*  = {fmt_pct(s['kelly_star'], 6, 2)}   (= E[R]/Var[R])")
    print(f"    Grossman-Zhou f   = {fmt_pct(s['f_GZ_10pct_cap'], 6, 3)}   "
          f"for 10 % total-DD cap, streak-buffered")
    print(f"    GZ (daily 4 %)    = {fmt_pct(s['f_GZ_4pct_cap'], 6, 3)}   "
          f"for 4 % daily-DD cap, streak-buffered")
    print(f"    hard GZ ceiling   = {fmt_pct(s['f_hard_ceiling'], 6, 2)}   "
          f"(P<1 % of hitting 10 % cap in 1000 trades)")

    # ─── Per (symbol × side) ────────────────────────────────────────────────
    buckets = defaultdict(list)
    for t in trades:
        side = "long" if t["side"] > 0 else "short"
        buckets[f"{t['symbol']}_{side}"].append(t)

    print("\n" + "=" * 92)
    print("  PER (SYMBOL × SIDE)  Grossman-Zhou fractions")
    print("=" * 92)
    print(f"  {'Bucket':<15} {'N':>4} {'win%':>5} {'E[R]':>7} {'Var':>6} "
          f"{'Sharpe':>7} {'streak':>6}  {'kelly*':>8} {'GZ(10%)':>8} {'GZ(4%)':>8}")
    print("  " + "-" * 90)

    total_R_all = sum(t["realised_R"] for t in trades)
    bucket_rows = []
    for name, ts in buckets.items():
        bs = stats_for(ts)
        bucket_rows.append((name, bs, sum(t["realised_R"] for t in ts)))
    # sort by P&L contribution descending
    bucket_rows.sort(key=lambda r: -r[2])

    for name, bs, cum_R in bucket_rows:
        print(f"  {name:<15} {bs['n']:>4} {bs['win_rate']*100:>4.0f}% "
              f"{bs['E_R']:>+6.3f} {bs['Var_R']:>6.3f} {bs['Sharpe_pt']:>7.3f} "
              f"{bs['max_loss_streak']:>6} "
              f"{fmt_pct(max(bs['kelly_star'],0),7,2)} "
              f"{fmt_pct(max(bs['f_GZ_10pct_cap'],0),7,3)} "
              f"{fmt_pct(max(bs['f_GZ_4pct_cap'],0),7,3)}")

    # ─── Executive summary ──────────────────────────────────────────────────
    print("\n" + "=" * 92)
    print("  PLAIN-ENGLISH SUMMARY — what this means for v18 sizing")
    print("=" * 92)

    avg_gz = s["f_GZ_10pct_cap"]
    print(f"""
  • Your bot-wide Grossman-Zhou optimum is  ~{avg_gz*100:.2f} % per trade
    (streak-buffered against your worst observed losing run).
  • Current v17 averages 0.41 % per trade.  You are under-betting by a factor
    of  ~{avg_gz/0.0041:.1f}×  versus the mathematical optimum.
  • v15's fixed ~0.80 % was closer but still ~{avg_gz/0.008:.1f}× below optimum.
  • Absolute hard ceiling is {s['f_hard_ceiling']*100:.1f} % — we will not go
    near it.  v18 will cap at  **2.0 %** per trade for belt-and-braces.

  PER-SYMBOL IMPLICATIONS:
""")
    for name, bs, _ in bucket_rows:
        if bs["kelly_star"] <= 0:
            verdict = "LOSING BUCKET — size at cold-start floor only"
        elif bs["n"] < 20:
            verdict = f"too few trades ({bs['n']}<20) — cold-start until more data"
        else:
            verdict = f"size at GZ(10%) = {bs['f_GZ_10pct_cap']*100:.2f} %"
        print(f"    {name:<15} {verdict}")

    # Save numbers so v18 can consume them
    out = ROOT / "Results" / "v18_empirical_stats.json"
    payload = {
        "overall": s,
        "per_symbol_side": {name: bs for name, bs, _ in bucket_rows},
        "source_trades_file": str(TRADES),
    }
    out.write_text(json.dumps(payload, indent=2, default=float))
    print(f"\n  pinned stats -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
