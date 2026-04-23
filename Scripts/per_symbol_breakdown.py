#!/usr/bin/env python3
"""Per-symbol breakdown of the v22 Phase-A/B champion (same code path)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from Scripts.backtest_v22_phase_b import (
    SYMBOLS, BALANCE, MertonGZSizerConfig,
    run_portfolio, apply_full_safety_rails,
)
from Scripts.backtest_v22_lean_uk5 import stats

sizer_cfg = MertonGZSizerConfig(
    base_risk_pct=0.0015, cap_mult=3.0, gamma=2.0,
    ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
    pool_symbols=True, no_edge_multiplier=1.0,
)

print("=" * 96)
print("  v22 CHAMPION — PER-SYMBOL BREAKDOWN (3-MONTH BACKTEST, ALL-5 PORTFOLIO)")
print("=" * 96)

raw, wmin, wmax, _, _ = run_portfolio(SYMBOLS, sizer_cfg)
tr = apply_full_safety_rails(raw, slippage_ticks=1.0)
total = stats(tr)
print(f"\n  Window           : {wmin}  →  {wmax}")
print(f"  Total duration   : ~{(__import__('datetime').datetime.fromisoformat(wmax.replace(' ','T')) - __import__('datetime').datetime.fromisoformat(wmin.replace(' ','T'))).days} days (~{(__import__('datetime').datetime.fromisoformat(wmax.replace(' ','T')) - __import__('datetime').datetime.fromisoformat(wmin.replace(' ','T'))).days/30:.1f} months)")
print(f"  Portfolio PnL    : ${total['net']:+,.2f}  ({total['ret_pct']:+.2f}% of $100k)")
print(f"  Portfolio DD     : {total['dd_pct']:.2f}%")
print(f"  Portfolio WR     : {total['wr']*100:.1f}%")
print(f"  Portfolio PF     : {total['pf']:.2f}")
print(f"  Portfolio trades : {total['n']}")
print("\n  -------- PER-SYMBOL -------- ")
print(f"  {'Symbol':<8} {'N':>4} {'PnL ($)':>14} {'% of total':>12} {'WR':>7} {'PF':>6} {'avg/trade':>12}")
print("  " + "-" * 78)
by_sym = {}
for t in tr:
    by_sym.setdefault(t.symbol, []).append(t)
for sym in sorted(by_sym.keys(), key=lambda s: -sum(t.net_pnl for t in by_sym[s])):
    lst = by_sym[sym]
    s = stats(lst)
    share = s['net'] / total['net'] * 100 if total['net'] else 0
    avg = s['net'] / s['n']
    print(f"  {sym:<8} {s['n']:>4} ${s['net']:>+12,.0f} {share:>11.1f}% "
          f"{s['wr']*100:>6.1f}% {s['pf']:>5.2f} ${avg:>+10,.0f}")

print("\n" + "=" * 96)
print(f"  ANSWER: ALL-5 portfolio made ${total['net']:+,.0f} over ~{((__import__('datetime').datetime.fromisoformat(wmax.replace(' ','T')) - __import__('datetime').datetime.fromisoformat(wmin.replace(' ','T'))).days)/30:.1f} months.")
print(f"  That is +{total['ret_pct']:.1f}% on a $100k account, with max DD of only {total['dd_pct']:.2f}% (well under the 4% cap).")
print("=" * 96)
