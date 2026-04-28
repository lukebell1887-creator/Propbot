#!/usr/bin/env python3
"""
backtest_v30_fresh.py — v30 SHIP-CONFIG backtest on freshly-downloaded 5%ers data.

Identical strategy logic to backtest_v23_final.py + news rails, but at the
v30 LIVE ship configuration:
    base_risk_pct = 0.00170    (★ v30 vs 0.00110 in v23-locked)
    cap_mult      = 5.0
    gamma         = 3.0
    pool_symbols  = True
    warmup_trades = 15
    news_rails    = ON (-15 min entry block, -2 min flatten)
    safety_rails  = full prop-firm + slippage
    symbols       = DE40, US30, XAUUSD, US500   (the 4-pair winner)

Outputs:
    Results/v30_fresh_backtest.json    — full headline metrics
    Results/v30_fresh_trades.json      — every closed trade (for sizer seeding)
    Results/v30_fresh_perSymbol.json   — per-symbol PnL/DD/wr breakdown
    console table

Then a SANITY-GATE block prints PASS/FAIL on:
    1. Net PnL > 0
    2. MaxDD < 4%   (sizer GZ barrier)
    3. WorstDailyDD < 5%   (5%ers daily rule)
    4. Trade count in [80, 400]
    5. No symbol contributes ≥ 80% of profit
    6. Sub-60s trades < 5% of all trades   (HFT compliance for prop firm)
"""
from __future__ import annotations
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

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

# ============================================================================
#  v30 SHIP CONFIG
# ============================================================================
RISK     = 0.00170     # ★ v30 base risk (vs 0.00110 in v23-locked)
CAP_MULT = 5.0
GAMMA    = 3.0
NEWS_CSV = ROOT / "data" / "news" / "tier1_2026.csv"

OUT_FULL    = ROOT / "Results" / "v30_fresh_backtest.json"
OUT_TRADES  = ROOT / "Results" / "v30_fresh_trades.json"
OUT_BYSYM   = ROOT / "Results" / "v30_fresh_perSymbol.json"


# ----------------------------------------------------------------------------
def per_symbol_breakdown(trades):
    by = defaultdict(lambda: {"n": 0, "wins": 0, "losses": 0, "net": 0.0,
                              "gross_win": 0.0, "gross_loss": 0.0,
                              "max_dd": 0.0, "peak": 0.0, "running": 0.0})
    for t in sorted(trades, key=lambda tr: tr.exit_time):
        s = by[t.symbol]
        s["n"] += 1
        s["net"] += float(t.net_pnl)
        if t.net_pnl > 0:
            s["wins"] += 1
            s["gross_win"] += float(t.net_pnl)
        else:
            s["losses"] += 1
            s["gross_loss"] += abs(float(t.net_pnl))
        s["running"] += float(t.net_pnl)
        s["peak"] = max(s["peak"], s["running"])
        dd = (s["peak"] - s["running"])
        s["max_dd"] = max(s["max_dd"], dd)
    out = {}
    for sym, d in by.items():
        out[sym] = {
            "n_trades": d["n"],
            "net_pnl": round(d["net"], 2),
            "win_rate_pct": round(100.0 * d["wins"] / max(1, d["n"]), 2),
            "profit_factor": round(d["gross_win"] / max(1e-9, d["gross_loss"]), 3),
            "max_dd_dollars": round(d["max_dd"], 2),
            "max_dd_pct_of_balance": round(100.0 * d["max_dd"] / BALANCE, 3),
        }
    return out


def trade_to_jsonable(t):
    """Serialise a Trade dataclass for sizer seeding later."""
    def _ts(x):
        if hasattr(x, "isoformat"):
            return x.isoformat()
        if isinstance(x, (int, float)):
            return datetime.fromtimestamp(float(x), tz=timezone.utc).isoformat()
        return str(x)
    risk_dollars = getattr(t, "risk_dollars", None)
    if risk_dollars is None or risk_dollars == 0:
        # Fall back: estimate from initial stop distance × contract spec.
        # If we can't, treat realised_R as net_pnl / 1000 (rough placeholder).
        risk_dollars = max(1.0, abs(float(t.net_pnl)) / max(0.1, getattr(t, "realised_R", 1.0)))
    realised_R = getattr(t, "realised_R", None)
    if realised_R is None:
        realised_R = float(t.net_pnl) / max(1.0, float(risk_dollars))
    realised_R = float(max(-5.0, min(5.0, realised_R)))   # match live clipping
    return {
        "symbol": t.symbol,
        "side": int(getattr(t, "side", 0)),
        "entry_time": _ts(t.entry_time),
        "exit_time": _ts(t.exit_time),
        "entry_price": float(getattr(t, "entry_price", 0.0)),
        "exit_price": float(getattr(t, "exit_price", 0.0)),
        "net_pnl": float(t.net_pnl),
        "realised_R": realised_R,
    }


# ----------------------------------------------------------------------------
def run_backtest(news: bool = True):
    cfg = MertonGZSizerConfig(
        base_risk_pct=RISK,
        cap_mult=CAP_MULT,
        gamma=GAMMA,
        ewma_alpha=0.20,
        warmup_trades=15,
        dd_cap_pct=0.04,
        pool_symbols=True,
        no_edge_multiplier=1.0,
    )

    raw, tmin, tmax, _dropped, streams = run_portfolio(SYMS, cfg)

    if news:
        events = load_news_events(NEWS_CSV)
        pl = build_price_lookup(streams)
        raw, _ = apply_news_entry_block(raw, events, buffer_min=15)
        raw, _ = apply_news_flatten(raw, events, pl, minutes_before=2)

    trades = apply_full_safety_rails(raw, slippage_ticks=1.0)

    s = stats(trades)
    pnls = np.array([t.net_pnl for t in trades], dtype=float)
    worst_pnl, worst_pct, worst_dd, n_days = worst_single_day(trades)
    ruins = ruin_probs(pnls)
    dur = hold_duration_stats(trades)
    conc = concurrency_stats(trades)
    by_sym = per_symbol_breakdown(trades)

    result = {
        "config": {
            "risk_pct": RISK,
            "cap_mult": CAP_MULT,
            "gamma": GAMMA,
            "symbols": SYMS,
            "balance": BALANCE,
            "news_rails": news,
            "data_window": {
                "first_trade": str(min((t.entry_time for t in trades), default=None)),
                "last_trade": str(max((t.exit_time for t in trades), default=None)),
                "n_calendar_days": n_days,
            },
        },
        "headline": {
            "n_trades": s["n"],
            "net_pnl": round(s["net"], 2),
            "ret_pct": round(s["ret_pct"], 3),
            "max_dd_pct": round(s["dd_pct"], 3),
            "profit_factor": round(s["pf"], 3),
            "sharpe": round(s["sharpe"], 3),
            "win_rate_pct": round(s["wr"] * 100, 2),
            "worst_day_pnl": round(worst_pnl, 2),
            "worst_day_pct": round(worst_pct, 3),
            "worst_daily_dd_pct": round(worst_dd, 3),
            "ruin_3pct": ruins["ruin3"],
            "ruin_4pct": ruins["ruin4"],
            "ruin_5pct": ruins["ruin5"],
            "median_hold_min": dur["median_min"],
            "p10_hold_min": dur["p10_min"],
            "p90_hold_min": dur["p90_min"],
            "sub60s_trades": dur["sub60s"],
        },
        "concurrency_pct": conc,
        "per_symbol": by_sym,
    }
    return result, trades


# ----------------------------------------------------------------------------
def sanity_gates(r):
    """Return (overall_pass, gates_dict)."""
    h = r["headline"]
    n = h["n_trades"]
    sub60s_pct = (100.0 * h["sub60s_trades"] / max(1, n))

    # Concentration: max single-symbol PnL share (positive symbols only)
    pos_pnls = [v["net_pnl"] for v in r["per_symbol"].values() if v["net_pnl"] > 0]
    total_pos = sum(pos_pnls) or 1.0
    max_share = max((p / total_pos for p in pos_pnls), default=0.0) * 100.0

    gates = {
        "G1_net_positive": (h["net_pnl"] > 0,
                            f"net=${h['net_pnl']:+,.0f}"),
        "G2_max_dd_under_4pct": (h["max_dd_pct"] < 4.0,
                                 f"MaxDD={h['max_dd_pct']:.2f}% (<4%)"),
        "G3_worst_daily_under_5pct": (h["worst_daily_dd_pct"] < 5.0,
                                      f"WorstDailyDD={h['worst_daily_dd_pct']:.2f}% (<5%)"),
        "G4_trade_count_in_range": (80 <= n <= 400,
                                    f"n_trades={n} (80-400)"),
        "G5_no_symbol_dominates": (max_share < 80.0,
                                   f"top-symbol share={max_share:.1f}% (<80%)"),
        "G6_sub60s_under_5pct": (sub60s_pct < 5.0,
                                 f"sub60s={sub60s_pct:.1f}% (<5%)"),
    }
    overall = all(passed for passed, _ in gates.values())
    return overall, gates


# ----------------------------------------------------------------------------
def main():
    print("=" * 100)
    print("  v30 FRESH backtest  —  ship config (risk=0.00170, cap_mult=5.0, gamma=3.0)")
    print(f"  symbols  : {SYMS}")
    print(f"  data     : freshly pulled from 5%ers MT5 ({datetime.now():%Y-%m-%d %H:%M})")
    print(f"  news     : ON (Tier-1 ±15min entry block, -2min flatten)")
    print(f"  rails    : full prop-firm safety + 1-tick slippage")
    print("=" * 100)

    r, trades = run_backtest(news=True)
    h = r["headline"]
    cfg = r["config"]

    # write outputs
    OUT_FULL.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_FULL, "w", encoding="utf-8") as f:
        json.dump(r, f, indent=2, default=str)
    with open(OUT_TRADES, "w", encoding="utf-8") as f:
        json.dump([trade_to_jsonable(t) for t in trades], f, indent=2)
    with open(OUT_BYSYM, "w", encoding="utf-8") as f:
        json.dump(r["per_symbol"], f, indent=2)

    print(f"\n  data window  : {cfg['data_window']['first_trade']}  -> {cfg['data_window']['last_trade']}")
    print(f"  trading days : {cfg['data_window']['n_calendar_days']}")
    print()
    print("-" * 100)
    print(f"  RESULTS")
    print("-" * 100)
    print(f"    n_trades              : {h['n_trades']}")
    print(f"    net PnL               : ${h['net_pnl']:+,.2f}  ({h['ret_pct']:+.2f}% of $100K)")
    print(f"    max DD (cumulative)   : {h['max_dd_pct']:.2f}%")
    print(f"    worst single day      : ${h['worst_day_pnl']:+,.2f}  ({h['worst_day_pct']:+.2f}%)")
    print(f"    worst daily DD        : {h['worst_daily_dd_pct']:.2f}%")
    print(f"    profit factor         : {h['profit_factor']:.2f}")
    print(f"    Sharpe                : {h['sharpe']:.2f}")
    print(f"    win rate              : {h['win_rate_pct']:.1f}%")
    print(f"    median hold           : {h['median_hold_min']:.1f} min  "
          f"(p10={h['p10_hold_min']:.1f}, p90={h['p90_hold_min']:.1f})")
    print(f"    sub-60s trades        : {h['sub60s_trades']}  "
          f"({100*h['sub60s_trades']/max(1,h['n_trades']):.1f}% of total)")
    print(f"    bootstrap ruin probs  : 3%={h['ruin_3pct']:.1f}%  "
          f"4%={h['ruin_4pct']:.1f}%  5%={h['ruin_5pct']:.1f}%")

    print()
    print("-" * 100)
    print(f"  PER-SYMBOL")
    print("-" * 100)
    print(f"    {'symbol':<8s} {'n':>4s}  {'net':>12s}  {'wr%':>6s}  {'PF':>6s}  {'maxDD$':>10s}  {'maxDD%bal':>10s}")
    for sym, d in r["per_symbol"].items():
        print(f"    {sym:<8s} {d['n_trades']:>4d}  "
              f"${d['net_pnl']:>+10,.0f}  "
              f"{d['win_rate_pct']:>5.1f}%  "
              f"{d['profit_factor']:>5.2f}  "
              f"${d['max_dd_dollars']:>9,.0f}  "
              f"{d['max_dd_pct_of_balance']:>9.2f}%")

    print()
    print("-" * 100)
    print(f"  CONCURRENCY (% of time)")
    print("-" * 100)
    print(f"    {r['concurrency_pct']}")

    print()
    print("=" * 100)
    print("  SANITY GATES")
    print("=" * 100)
    ok, gates = sanity_gates(r)
    for key, (passed, msg) in gates.items():
        flag = "[PASS]" if passed else "[FAIL]"
        print(f"    {flag}  {key:<28s}  {msg}")
    print()
    if ok:
        print("  ====>  ALL GATES PASS  —  strategy is healthy on freshest data.")
        print("         Safe to proceed to Stage 1 (persistence + seeding).")
    else:
        print("  ====>  ONE OR MORE GATES FAILED  —  STOP and investigate before live.")

    print()
    print(f"  outputs:")
    print(f"    {OUT_FULL.relative_to(ROOT)}")
    print(f"    {OUT_TRADES.relative_to(ROOT)}   ({len(trades)} trades — to be used as sizer seed)")
    print(f"    {OUT_BYSYM.relative_to(ROOT)}")
    return 0 if ok else 3


if __name__ == "__main__":
    raise SystemExit(main())
