#!/usr/bin/env python3
"""Slippage-sensitivity sweep for v23.

Re-runs the exact v23 pipeline at slippage_ticks = {0.0, 0.5, 1.0, 2.0, 3.0, 5.0}
so we can see what actually happens to PnL / DD / Sharpe if live fills are
worse than the backtest's 1-tick assumption.

Usage:  python Scripts/_slippage_sensitivity.py
"""
from __future__ import annotations
import sys, json
from pathlib import Path
from collections import defaultdict

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from Scripts.preflight_checks import (
    SYMS, BALANCE, run_portfolio, MertonGZSizerConfig,
    worst_single_day, ruin_probs,
)
from Scripts.backtest_v22_phase_b import (
    apply_slippage, apply_weekend_flat, apply_daily_kill_switch,
    apply_position_cap,
)
from Scripts.backtest_v22_lean_uk5 import stats


def rails(trades, slip_ticks):
    trades, _ = apply_position_cap(trades, max_concurrent=2)
    trades, _ = apply_daily_kill_switch(trades, threshold_pct=1.0)
    trades, _ = apply_weekend_flat(trades)
    trades = apply_slippage(trades, slippage_ticks=slip_ticks)
    return trades


def main():
    cfg = MertonGZSizerConfig(
        base_risk_pct=0.00110, cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )
    print("  running v23 portfolio once ...")
    raw, *_ = run_portfolio(SYMS, cfg)
    print(f"  raw trades N = {len(raw)}")

    print()
    print("=" * 112)
    print(f"  {'slip_ticks':>10} | {'N':>3} | {'Net PnL':>10} | {'Ret %':>6} | "
          f"{'DD %':>5} | {'Sharpe':>6} | {'WR %':>5} | {'WorstDay %':>10} | "
          f"{'Ruin 5%':>7} | {'verdict':<40}")
    print("=" * 112)

    rows = []
    for ticks in (0.0, 0.5, 1.0, 2.0, 3.0, 5.0):
        tr = rails(list(raw), ticks)
        s = stats(tr)
        pnls = np.array([t.net_pnl for t in tr], dtype=float)
        _, wd_pct, wd_dd, _ = worst_single_day(tr)
        ruins = ruin_probs(pnls)

        step1_ok = s['net'] >= 8000.0
        dd_ok    = s['dd_pct'] < 4.0
        daily_ok = wd_dd < 4.0
        verdict  = ("PASS Step1" if step1_ok else "MISS Step1") + \
                   (" / DD OK" if dd_ok else " / DD FAIL") + \
                   (" / daily OK" if daily_ok else " / daily FAIL")

        print(f"  {ticks:>10.1f} | {s['n']:>3} | ${s['net']:>+8,.0f} | "
              f"{s['ret_pct']:>5.2f} | {s['dd_pct']:>4.2f} | "
              f"{s['sharpe']:>5.2f} | {s['wr']*100:>4.1f} | "
              f"{wd_pct:>+9.2f} | {ruins['ruin5']:>6.1f} | {verdict:<40}")

        rows.append(dict(ticks=ticks, n=s['n'], net=s['net'],
                         ret_pct=s['ret_pct'], dd_pct=s['dd_pct'],
                         sharpe=s['sharpe'], wr=s['wr'],
                         worst_day_pct=wd_pct, worst_daily_dd=wd_dd,
                         **ruins, verdict=verdict))

    print("=" * 112)
    print()
    print("  INTERPRETATION (read the 'verdict' column):")
    print("  * slip=1.0 is the backtest's shipped assumption  -> $16,957 / 3.35% DD")
    print("  * slip=2.0 is 'live fills 2 ticks worse than backtest'  (common retail MT5)")
    print("  * slip=3.0 is 'Frankfurt/NY open whipsaw'  (2-3x normal for 30s)")
    print("  * slip=5.0 is 'flash-move stop-out'  (very fat tail)")
    print()
    print("  The question: does Step-1 target ($8k) survive at slip=3.0?")
    print()

    out = ROOT / "Results" / "slippage_sensitivity.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"syms": SYMS, "risk": 0.00110,
                   "sizer": {"cap_mult": 5.0, "gamma": 3.0, "base": 0.00110},
                   "rows": rows}, f, indent=2, default=str)
    print(f"  Saved: {out}")


if __name__ == "__main__":
    main()
