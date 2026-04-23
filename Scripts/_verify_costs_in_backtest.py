#!/usr/bin/env python3
"""Proves whether commission + spread are already deducted from the $16,977."""
from __future__ import annotations
import sys
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from Scripts.preflight_checks import (
    SYMS, run_portfolio, apply_full_safety_rails, MertonGZSizerConfig,
)

cfg = MertonGZSizerConfig(
    base_risk_pct=0.00110, cap_mult=5.0, gamma=3.0,
    ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
    pool_symbols=True, no_edge_multiplier=1.0,
)

print("Running v23 CURRENT config ...")
raw, *_ = run_portfolio(SYMS, cfg)
print(f"  raw trades N = {len(raw)}")

tr = apply_full_safety_rails(raw, slippage_ticks=1.0)
print(f"  after safety rails + 1-tick slippage: N = {len(tr)}")
print()


def summarize(label, trades):
    tot = defaultdict(float); tot_n = 0
    by_sym = defaultdict(lambda: defaultdict(float))
    by_sym_n = defaultdict(int)
    for t in trades:
        s = t.symbol
        by_sym_n[s] += 1
        by_sym[s]["lots"] += abs(t.lots)
        by_sym[s]["gross"] += t.gross_pnl
        by_sym[s]["spread"] += t.spread_cost
        by_sym[s]["commission"] += t.commission
        by_sym[s]["net"] += t.net_pnl
        tot_n += 1
        tot["lots"] += abs(t.lots)
        tot["gross"] += t.gross_pnl
        tot["spread"] += t.spread_cost
        tot["commission"] += t.commission
        tot["net"] += t.net_pnl

    print(f"--- {label} ---")
    print(f"  {'symbol':<8}  {'N':>3}  {'lots':>7}  {'gross':>10}  {'spread':>9}  {'comm':>8}  {'net':>10}")
    for s in sorted(by_sym):
        r = by_sym[s]
        print(f"  {s:<8}  {by_sym_n[s]:>3}  {r['lots']:>7.2f}  "
              f"{r['gross']:>+10,.0f}  {r['spread']:>9,.0f}  "
              f"{r['commission']:>8,.2f}  {r['net']:>+10,.0f}")
    print(f"  {'TOTAL':<8}  {tot_n:>3}  {tot['lots']:>7.2f}  "
          f"{tot['gross']:>+10,.0f}  {tot['spread']:>9,.0f}  "
          f"{tot['commission']:>8,.2f}  {tot['net']:>+10,.0f}")
    computed = tot['gross'] - tot['spread'] - tot['commission']
    print(f"  identity check: gross - spread - commission = ${computed:+,.0f}  "
          f"vs net ${tot['net']:+,.0f}  delta = ${tot['net'] - computed:+,.2f}")
    print()
    return tot


print("=" * 100)
print("  COST MODEL VERIFICATION")
print("=" * 100)

t_raw = summarize("RAW ENGINE OUTPUT (before 1-tick slippage pad)", raw)
t_pad = summarize("AFTER apply_full_safety_rails (1-tick slippage pad added)", tr)

print("=" * 100)
print("  HEADLINE")
print("=" * 100)
print(f"  Raw engine net (already deducts spread + commission from gross) : ${t_raw['net']:+,.0f}")
print(f"  After +1 tick slippage pad (live-realistic)                     : ${t_pad['net']:+,.0f}")
print(f"  Slippage pad cost                                               : ${t_raw['net'] - t_pad['net']:+,.0f}")
print(f"  Commission paid (already inside net)                            : ${t_raw['commission']:,.2f}")
print(f"  Spread cost paid (already inside net)                           : ${t_raw['spread']:,.0f}")
print("=" * 100)
