#!/usr/bin/env python3
"""Re-run the CURRENT backtest with the LIVE sizer config (cap_mult=5, γ=3)
to verify the $23,311 claim and prove whether the replay-based sweep matches
an actual engine run."""
from __future__ import annotations
import sys, json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from Scripts.preflight_checks import (
    SYMS, BALANCE,
    run_portfolio, apply_full_safety_rails,
    worst_single_day, ruin_probs, hold_duration_stats, concurrency_stats,
    MertonGZSizerConfig,
)
from Scripts.backtest_v22_lean_uk5 import stats
from Scripts.backtest_v23_locked import (
    load_news_events, apply_news_entry_block, apply_news_flatten,
    build_price_lookup,
)
from src.dd_breaker import apply_dd_breaker

NEWS_CSV = ROOT / "data" / "news" / "tier1_2026.csv"


def run(base_risk, cap_mult, gamma, add_news, label):
    cfg = MertonGZSizerConfig(
        base_risk_pct=base_risk, cap_mult=cap_mult, gamma=gamma,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )
    raw, tmin, tmax, _drop, streams = run_portfolio(SYMS, cfg)
    if add_news:
        events = load_news_events(NEWS_CSV)
        pl = build_price_lookup(streams)
        raw, _ = apply_news_entry_block(raw, events, buffer_min=15)
        raw, _ = apply_news_flatten(raw, events, pl, minutes_before=2)
    tr = apply_full_safety_rails(raw, slippage_ticks=1.0)
    s = stats(tr)
    pnls = np.array([t.net_pnl for t in tr], dtype=float)
    w_pnl, w_pct, w_dd, ndays = worst_single_day(tr)
    ruins = ruin_probs(pnls)
    dur = hold_duration_stats(tr)
    # 4% DD breaker sim
    _, br = apply_dd_breaker(tr, starting_balance=BALANCE, halt_pct=0.04)
    return dict(label=label, base_risk=base_risk, cap_mult=cap_mult, gamma=gamma,
                n=s["n"], net=s["net"], dd=s["dd_pct"], pf=s["pf"], sharpe=s["sharpe"],
                wr=s["wr"],
                worst_day_pct=w_pct, worst_daily_dd=w_dd,
                ruin5=ruins["ruin5"], ruin4=ruins["ruin4"],
                sub60s=dur["sub60s"],
                dd_breaker_halts=br.total_halts,
                max_dd_seen=br.max_dd_pct_seen * 100)


# -----------------------------------------------------------------
# A/B/C: audited vs live-config vs live-config-with-news
# -----------------------------------------------------------------
print("=" * 110)
print("  HEAD-TO-HEAD — audited config vs v23_live.py config (engine re-run, not replay)")
print("=" * 110)

rows = []
rows.append(run(0.00110, 3.0, 2.0, False, "BACKTEST (audited)  cap=3  γ=2  no-news"))
rows.append(run(0.00110, 3.0, 2.0, True,  "BACKTEST + news    cap=3  γ=2"))
rows.append(run(0.00110, 5.0, 3.0, False, "LIVE config        cap=5  γ=3  no-news"))
rows.append(run(0.00110, 5.0, 3.0, True,  "LIVE config + news cap=5  γ=3"))

hdr = (f"  {'Config':<44} {'N':>4} {'PnL':>10} {'DD%':>6} {'WrstDay%':>9} "
       f"{'DailyDD%':>8} {'PF':>5} {'Sharpe':>7} {'ruin5%':>7} {'sub60s':>6} {'maxDDseen%':>10}")
print(hdr)
print("  " + "-" * (len(hdr) - 2))
for r in rows:
    print(f"  {r['label']:<44} {r['n']:>4} ${r['net']:>+8,.0f} {r['dd']:>5.2f}% "
          f"{r['worst_day_pct']:>+7.2f}% {r['worst_daily_dd']:>7.2f}% "
          f"{r['pf']:>5.2f} {r['sharpe']:>+6.2f} {r['ruin5']:>6.1f}% "
          f"{r['sub60s']:>5d} {r['max_dd_seen']:>9.3f}%")

out = ROOT / "Results" / "audit_v23_cap5.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(rows, f, indent=2, default=str)
print(f"\nSaved: {out}")
