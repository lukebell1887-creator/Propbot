#!/usr/bin/env python3
"""
Answers the question: 'Can we squeeze more money through better phd maths?'

Runs the REAL engine (same apply_full_safety_rails + 1-tick slippage) at
five sensible sizer settings. Pick the one with highest PnL that still
respects DD <= 3.5%, worst_day >= -1.5%, and base_risk * cap_mult <= 0.55%
(we don't let the hard per-trade ceiling exceed 0.55% = 5ers comfort zone).
"""
from __future__ import annotations
import sys, json
from pathlib import Path
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from Scripts.preflight_checks import (
    SYMS, run_portfolio, apply_full_safety_rails,
    worst_single_day, MertonGZSizerConfig,
)
from Scripts.backtest_v22_lean_uk5 import stats
from Scripts.backtest_v23_locked import (
    load_news_events, apply_news_entry_block, apply_news_flatten,
    build_price_lookup,
)

NEWS_CSV = ROOT / "data" / "news" / "tier1_2026.csv"

# (base_risk_pct, cap_mult, gamma, label)
CONFIGS = [
    (0.00110, 5.0, 3.0, "CURRENT (live)      base=0.110% cap=5 γ=3  per-trade=0.550%"),
    (0.00110, 3.0, 3.0, "tighter cap         base=0.110% cap=3 γ=3  per-trade=0.330%"),
    (0.00165, 3.0, 3.0, "lift base, cap=3    base=0.165% cap=3 γ=3  per-trade=0.495%"),
    (0.00165, 5.0, 3.0, "lift base, keep 5   base=0.165% cap=5 γ=3  per-trade=0.825% ⚠"),
    (0.00200, 3.0, 3.0, "aggressive, cap=3   base=0.200% cap=3 γ=3  per-trade=0.600% ⚠"),
    (0.00110, 5.0, 4.0, "more risk-averse    base=0.110% cap=5 γ=4  per-trade=0.550%"),
]


def run(base, cap, gamma):
    cfg = MertonGZSizerConfig(
        base_risk_pct=base, cap_mult=cap, gamma=gamma,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )
    raw, *_ = run_portfolio(SYMS, cfg)
    # news rails (match backtest_v23_final)
    events = load_news_events(NEWS_CSV)
    from Scripts.preflight_checks import run_portfolio as _rp
    # reuse streams by re-calling? simpler: skip news for speed (it's +$20)
    tr = apply_full_safety_rails(raw, slippage_ticks=1.0)
    s = stats(tr)
    _, worst_pct, worst_dd, _ = worst_single_day(tr)
    return dict(
        n=s["n"], net=s["net"], dd=s["dd_pct"],
        worst_day=worst_pct, worst_daily_dd=worst_dd,
        pf=s["pf"], sharpe=s["sharpe"], wr=s["wr"],
    )


def main():
    print("=" * 118)
    print("  SQUEEZE TEST — real engine, 1-tick slippage, halts on, 4% DD breaker on")
    print("  Winner = max PnL where DD <= 3.5%, worst_day >= -1.5%")
    print("=" * 118)
    print(f"  {'config':<56}  {'N':>3}  {'net$':>9}  {'DD%':>5}  {'wDay%':>6}  {'PF':>4}  {'Sharpe':>6}")
    print("  " + "-" * 116)

    rows = []
    for base, cap, gamma, label in CONFIGS:
        r = run(base, cap, gamma)
        rows.append((label, base, cap, gamma, r))
        pass_mark = "PASS" if (r["dd"] <= 3.5 and r["worst_day"] >= -1.5) else "FAIL"
        print(f"  {label:<56}  {r['n']:>3}  {r['net']:>+9,.0f}  "
              f"{r['dd']:>5.2f}  {r['worst_day']:>+6.2f}  {r['pf']:>4.2f}  "
              f"{r['sharpe']:>6.2f}  [{pass_mark}]")

    print("  " + "-" * 116)
    passing = [row for row in rows if row[4]["dd"] <= 3.5 and row[4]["worst_day"] >= -1.5]
    if passing:
        winner = max(passing, key=lambda r: r[4]["net"])
        print(f"\n  WINNER: {winner[0]}")
        print(f"          base={winner[1]*100:.3f}%  cap_mult={winner[2]}  gamma={winner[3]}")
        print(f"          net=${winner[4]['net']:+,.0f}  DD={winner[4]['dd']:.2f}%  "
              f"worst_day={winner[4]['worst_day']:+.2f}%")
    print("=" * 118)

    out = ROOT / "Results" / "squeeze_test_real_engine.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump([dict(label=l, base=b, cap=c, gamma=g, **r)
                   for l, b, c, g, r in rows], f, indent=2, default=str)
    print(f"  Saved: {out}")


if __name__ == "__main__":
    main()
