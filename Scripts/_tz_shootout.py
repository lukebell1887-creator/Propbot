#!/usr/bin/env python3
"""
TZ SHOOTOUT — does the v23 edge live at broker 14:30 (current, = NY pre-market)
or at broker 16:30 (= real NYSE cash open 9:30 ET)?

Runs the exact v23_final pipeline (sizer v24d, risk 0.11%, cap 5x, 4% DD breaker,
news rails) THREE times, each with a different OR-start anchor:

  A) CURRENT   : DE40=08:00 US=14:30  (= 2h BEFORE real market open)
                   This is what the backtest claimed 10,853 / 2.16% DD on.

  B) +2h shift : DE40=10:00 US=16:30  (= REAL market open in broker clock)
                   DE40 hits Frankfurt Xetra cash open.
                   US hits NYSE cash open 9:30 ET.

  C) +3h shift : DE40=11:00 US=17:30  (= 1h after real market open)
                   Post-opening-drive window; is the edge residual?

Per run we capture: total trades, net PnL $, return %, max DD %, worst day,
per-symbol breakdown, DD-breaker trigger, sub-60s holds, same-bar entries/exits.

Output: Results/_tz_shootout.json + console table.
"""
from __future__ import annotations
import sys, json, copy
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

import numpy as np

from src.momentum.orb import ORBConfig
from Scripts import backtest_v22_lean_uk5 as uk5mod
from Scripts.preflight_checks import (
    SYMS, BALANCE, MertonGZSizerConfig,
    run_portfolio, apply_full_safety_rails,
    worst_single_day, hold_duration_stats,
)
from Scripts.backtest_v22_lean_uk5 import stats
from Scripts.backtest_v23_locked import (
    load_news_events, apply_news_entry_block, apply_news_flatten,
    build_price_lookup,
)

RISK = 0.00110
NEWS_CSV = ROOT / "data" / "news" / "tier1_2026.csv"

ORIGINAL_CONFIGS = copy.deepcopy(uk5mod.ORB_CONFIGS)


def _set_hours(shift_hours: int):
    """Shift every ORB_CONFIGS entry's or_start_hour by +shift_hours."""
    new = {}
    for sym, c in ORIGINAL_CONFIGS.items():
        new[sym] = ORBConfig(
            or_start_hour=(c.or_start_hour + shift_hours) % 24,
            or_start_minute=c.or_start_minute,
            or_minutes=c.or_minutes,
            trade_window_minutes=c.trade_window_minutes,
            tp1_range_mult=c.tp1_range_mult,
            tp2_range_mult=c.tp2_range_mult,
            sl_buffer_range_mult=c.sl_buffer_range_mult,
        )
    uk5mod.ORB_CONFIGS.clear()
    uk5mod.ORB_CONFIGS.update(new)


def _run_one(shift_h: int):
    _set_hours(shift_h)
    cfg = MertonGZSizerConfig(
        base_risk_pct=RISK, cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )
    raw, tmin, tmax, _dropped, streams = run_portfolio(SYMS, cfg)

    events = load_news_events(NEWS_CSV)
    pl = build_price_lookup(streams)
    raw, _ = apply_news_entry_block(raw, events, buffer_min=15)
    raw, _ = apply_news_flatten(raw, events, pl, minutes_before=2)

    tr = apply_full_safety_rails(raw, slippage_ticks=1.0)
    s = stats(tr)
    wpnl, wpct, wdd, ndays = worst_single_day(tr)
    dur = hold_duration_stats(tr)

    # per-symbol
    per_sym = defaultdict(lambda: {"n": 0, "pnl": 0.0, "sub60s": 0, "same_bar": 0})
    for t in tr:
        ps = per_sym[t.symbol]
        ps["n"] += 1
        ps["pnl"] += float(t.net_pnl)
        hold_s = float(t.exit_time) - float(t.entry_time)
        if hold_s < 60: ps["sub60s"] += 1
        # "same M1 bar" = entry and exit within the same minute boundary
        if int(float(t.entry_time)) // 60 == int(float(t.exit_time)) // 60:
            ps["same_bar"] += 1

    any_sub60 = any(ps["sub60s"] > 0 for ps in per_sym.values())
    total_same_bar = sum(ps["same_bar"] for ps in per_sym.values())
    same_bar_pct = 100.0 * total_same_bar / max(s["n"], 1)
    dd_breaker_tripped = bool(getattr(s, "_dd_halt_ever", False))  # rails emit equity halts via cap

    return dict(
        shift=shift_h,
        n=s["n"], net=s["net"], ret_pct=s["ret_pct"],
        dd_pct=s["dd_pct"], pf=s["pf"], sharpe=s["sharpe"], wr=s["wr"],
        worst_day_pnl=wpnl, worst_day_pct=wpct, worst_daily_dd_pct=wdd, ndays=ndays,
        sub60s_total=int(dur.get("sub60s", 0)),
        any_sub60s=bool(any_sub60),
        same_bar_pct=round(same_bar_pct, 2),
        dd_breaker_tripped=dd_breaker_tripped,
        per_symbol={k: dict(v) for k, v in per_sym.items()},
    )


def main():
    results = {}
    labels = {0: "A_CURRENT (broker 14:30 / 08:00 = pre-market)",
              2: "B_REALMARKET (broker 16:30 / 10:00 = NYSE & Xetra open)",
              3: "C_POSTOPEN (broker 17:30 / 11:00 = 1h post-open)"}

    for shift in (0, 2, 3):
        print(f"\n{'='*72}\n  Running: {labels[shift]}\n{'='*72}")
        results[f"shift_{shift}h"] = _run_one(shift)

    # restore original
    _set_hours(0)

    # ---------- report ----------
    print("\n" + "=" * 100)
    print("  TZ SHOOTOUT RESULTS — where does the edge actually live?")
    print("=" * 100)
    hdr = f"  {'scenario':<44} {'N':>4}  {'net$':>10}  {'ret%':>6}  {'DD%':>5}  {'PF':>4}  {'WR%':>5}  {'worstDay%':>9}"
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for shift in (0, 2, 3):
        r = results[f"shift_{shift}h"]
        print(f"  {labels[shift]:<44} "
              f"{r['n']:>4}  "
              f"${r['net']:>+9,.0f}  "
              f"{r['ret_pct']:>+5.2f}%  "
              f"{r['dd_pct']:>4.2f}%  "
              f"{r['pf']:>4.2f}  "
              f"{r['wr']*100:>4.1f}%  "
              f"{r['worst_day_pct']:>+8.2f}%")

    print("\n  Per-symbol PnL by scenario:")
    syms = sorted({s for r in results.values() for s in r["per_symbol"]})
    print(f"  {'symbol':<8}   " + "  ".join(f"{labels[s][0:12]:>12}" for s in (0, 2, 3)))
    for sym in syms:
        row = [sym.ljust(8)]
        for shift in (0, 2, 3):
            ps = results[f"shift_{shift}h"]["per_symbol"].get(sym, {"n": 0, "pnl": 0})
            row.append(f"N={ps['n']:>2} ${ps['pnl']:>+7,.0f}")
        print("  " + "   ".join(row))

    print("\n  Other flags (all scenarios):")
    for shift in (0, 2, 3):
        r = results[f"shift_{shift}h"]
        print(f"    {labels[shift][0:12]}:  sub60s={r['sub60s_total']}  "
              f"same-bar%={r['same_bar_pct']:.1f}  DD-breaker={'YES' if r['dd_breaker_tripped'] else 'no'}")

    out = ROOT / "Results" / "_tz_shootout.json"
    out.parent.mkdir(exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Saved: {out}")
    print("=" * 100)

    print("""
  HOW TO READ:

  - If CURRENT dominates (A > B and A > C): edge is real at the current anchor,
    even though the label says "NY open" it's actually NY pre-market that matters.
    Keep the bot as-is. Fix the misleading docstring only.

  - If REALMARKET (B) dominates: author's original intent was correct but data
    was timezone-shifted; fixing the anchors +2h gives the TRUE edge. Update
    config, re-run parity tests, restart dry-run.

  - If BOTH A and B look similar (within ±15% on net and DD): edge is broad
    across the session — low risk either way, but "real NY open" is better
    hygiene.

  - If NEITHER is decisively positive, or only A is positive: the backtest
    result may be overfit to a thin-liquidity window and the edge is fragile.
    Don't go live until a proper session-aligned backtest confirms.
""")


if __name__ == "__main__":
    main()
