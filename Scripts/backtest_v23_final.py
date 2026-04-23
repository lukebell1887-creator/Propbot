#!/usr/bin/env python3
"""
backtest_v23_final.py — MINIMAL.

Replicates preflight_checks.run(0.00110) EXACTLY (4 pairs, cap_mult=3.0, etc.)
and adds ONLY the two news rails (+/-15min entry block, -2min flatten) on top.

Nothing else changes. No symbol additions, no cap_mult changes, no HMM threshold
changes. Two A/B runs side-by-side:

    A. "CONTROL"  — exact replica of preflight_checks.run(0.00110)
                     (should reproduce the $10,841 / 2.16% DD number)
    B. "v23-news" — same as A, with news entry-block + flatten applied BEFORE
                     apply_full_safety_rails

Output: Results/v23_final.json + console diff table.
"""
from __future__ import annotations
import sys, json
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

# --- reuse EVERYTHING from the approved pipeline. Nothing re-implemented. ---
from Scripts.preflight_checks import (
    SYMS, BALANCE,
    run_portfolio, apply_full_safety_rails,
    worst_single_day, ruin_probs, hold_duration_stats, concurrency_stats,
    MertonGZSizerConfig,
)
from Scripts.backtest_v22_lean_uk5 import stats

# --- the only NEW code: two news rails, imported from the v23 locked file ---
from Scripts.backtest_v23_locked import (
    load_news_events, apply_news_entry_block, apply_news_flatten,
    build_price_lookup,
)

RISK = 0.00110        # THE locked risk
NEWS_CSV = ROOT / "data" / "news" / "tier1_2026.csv"


def run(risk, add_news_rails: bool):
    # Identical to preflight_checks.run(risk) — do NOT deviate from these params
    cfg = MertonGZSizerConfig(
        base_risk_pct=risk, cap_mult=3.0, gamma=2.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )
    raw, tmin, tmax, _dropped, streams = run_portfolio(SYMS, cfg)

    if add_news_rails:
        events = load_news_events(NEWS_CSV)
        pl = build_price_lookup(streams)
        raw, _ = apply_news_entry_block(raw, events, buffer_min=15)
        raw, _ = apply_news_flatten(raw, events, pl, minutes_before=2)

    tr = apply_full_safety_rails(raw, slippage_ticks=1.0)
    s = stats(tr)
    pnls = np.array([t.net_pnl for t in tr], dtype=float)
    worst_pnl, worst_pct, worst_dd, n_days = worst_single_day(tr)
    ruins = ruin_probs(pnls)
    dur = hold_duration_stats(tr)
    conc = concurrency_stats(tr)

    return dict(
        risk=risk, n=s["n"], net=s["net"], ret_pct=s["ret_pct"],
        dd_pct=s["dd_pct"], pf=s["pf"], sharpe=s["sharpe"], wr=s["wr"],
        n_days=n_days,
        worst_day_pnl=worst_pnl, worst_day_pct=worst_pct,
        worst_daily_dd_pct=worst_dd,
        **ruins, dur=dur, conc=conc,
    )


def main():
    print("=" * 120)
    print(f"  v23 FINAL — 4 pairs {SYMS}, risk={RISK*100:.3f}%, cap_mult=3.0")
    print(f"  A/B: CONTROL vs CONTROL + news rails only")
    print("=" * 120)

    print("\n  [A] CONTROL (no news rails) — should reproduce $10,841 / 2.16% DD ...")
    a = run(RISK, add_news_rails=False)
    print("\n  [B] v23-news (identical + news block + news flatten) ...")
    b = run(RISK, add_news_rails=True)

    def line(label, r):
        return (f"    {label:<16}  N={r['n']:>3}  "
                f"net=${r['net']:+9,.0f}  "
                f"DD={r['dd_pct']:>5.2f}%  "
                f"PF={r['pf']:.2f}  "
                f"WR={r['wr']*100:>4.1f}%  "
                f"Sharpe={r['sharpe']:.2f}  "
                f"WorstDay={r['worst_day_pct']:+5.2f}%  "
                f"DailyDD={r['worst_daily_dd_pct']:.2f}%  "
                f"ruin5={r['ruin5']:.1f}%  "
                f"sub60s={r['dur']['sub60s']}")

    print("\n" + "=" * 120)
    print("  RESULTS (A/B SIDE BY SIDE)")
    print("=" * 120)
    print(line("[A] CONTROL", a))
    print(line("[B] v23 + news", b))

    d_net = b["net"] - a["net"]
    d_dd  = b["dd_pct"] - a["dd_pct"]
    print()
    print(f"    delta net PnL  :  ${d_net:+,.0f}")
    print(f"    delta Max DD   :  {d_dd:+.2f} pp")
    print()
    print("  Expected: CONTROL matches the $10,841 / 2.16% number exactly.")
    print("  Expected: news rails subtract a tiny number (rarely fire — session timing mismatch).")
    print()

    out = ROOT / "Results" / "v23_final.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"syms": SYMS, "risk": RISK,
                   "control": a, "with_news": b,
                   "delta_net": d_net, "delta_dd_pp": d_dd}, f, indent=2, default=str)
    print(f"  Saved: {out}")
    print("=" * 120)


if __name__ == "__main__":
    main()
