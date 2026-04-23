#!/usr/bin/env python3
"""Quick sweep of portfolio combos at the v21 winning sizer config."""
from __future__ import annotations
import sys
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from Scripts.per_symbol_autopsy_v21 import run_portfolio, v21_winning_sizer

WIN = (datetime.fromisoformat("2026-01-19 01:05:00"),
       datetime.fromisoformat("2026-04-07 08:37:00"))

COMBOS = [
    ("Baseline 5     DE40 US30 XAU US100 US500",  ["DE40","US30","XAUUSD","US100","US500"]),
    ("Lean 4         drop US100",                  ["DE40","US30","XAUUSD","US500"]),
    ("Lean+UK  5     drop US100 + add UK100",      ["DE40","US30","XAUUSD","US500","UK100"]),
    ("6 pairs        5 + UK100",                   ["DE40","US30","XAUUSD","US100","US500","UK100"]),
    ("Carriers 4     DE40 US30 XAU US500",         ["DE40","US30","XAUUSD","US500"]),
    ("Minimal 3      DE40 US30 XAU",               ["DE40","US30","XAUUSD"]),
    ("Minimal 2      DE40 US30 (biggest carriers)",["DE40","US30"]),
    ("DE40 only      single-symbol monster",       ["DE40"]),
]

print(f"{'Config':<48} {'N':>4} {'PnL':>10} {'Ret%':>7} {'DD%':>6} {'PF':>5} {'Sharpe':>6}  Verdict")
print("-" * 102)
BASELINE_PNL = 14622
for label, syms in COMBOS:
    r = run_portfolio(syms, v21_winning_sizer(), lock_window=WIN)
    beats = r["net_pnl"] > BASELINE_PNL and r["max_dd_pct"] <= 4.0
    lower_dd = r["max_dd_pct"] < 3.36
    verdict = ("⭐ BEATS BASELINE" if beats
               else "✅ LOWER DD"    if lower_dd
               else "—")
    print(f"{label:<48} {r['n']:>4} ${r['net_pnl']:>+9,.0f} {r['return_pct']:>+6.2f}% "
          f"{r['max_dd_pct']:>5.2f}% {r['pf']:>5.2f} {r['sharpe']:>+6.2f}  {verdict}")
