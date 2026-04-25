#!/usr/bin/env python3
"""
backtest_v23_nochase.py — A/B test the "no queue-release chase entries" idea.

SCENARIO:
    CONTROL   = locked v23 behaviour exactly as live_v23 / backtest_v23_final does.
                (concurrency cap drops blocked trades; no post-filter)
    NO-CHASE  = identical to CONTROL, PLUS an extra post-filter that also drops
                any admitted trade whose entry_time is within N seconds of
                another (different-symbol) trade's exit_time.
                This simulates "don't take the trade just because a slot freed".

HYPOTHESIS:
    If chase entries are systematically worse (lower R, higher DD contribution),
    NO-CHASE should show a lower DD than CONTROL — even at the cost of a few
    fewer trades. That would let us risk more per trade.

HOW TO RUN:
    python Scripts/backtest_v23_nochase.py --cooldown 60
    python Scripts/backtest_v23_nochase.py --cooldown 300
    python Scripts/backtest_v23_nochase.py --cooldown 600

Output: Results/backtest_v23_nochase_<cooldown>s.json + console table.
"""
from __future__ import annotations

import argparse, json, sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Tuple

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

RISK = 0.00110
NEWS_CSV = ROOT / "data" / "news" / "tier1_2026.csv"


# -------------------------------------------------------------------------
#  NEW FILTER: drop trades entered within `cooldown_s` of another trade's exit
# -------------------------------------------------------------------------
def apply_no_chase(trades, cooldown_s: float):
    """
    Drop any trade whose entry_time t_in satisfies:

        exists another trade j with
            exit_time_j > 0
            exit_time_j <= t_in
            t_in - exit_time_j <= cooldown_s
            symbol_j != symbol_self  (don't treat same-symbol back-to-backs)

    In other words: if this entry happened within `cooldown_s` seconds AFTER
    another trade just closed, treat it as a "queue-release chase" and drop.

    NOTE: Our Trade objects are partials of a single entry, so we group by
    (symbol, entry_time) first — dropping chases consistently removes ALL
    partials of the same entry. Exit_time of a partial is its own exit (TP1/
    TP2/SL); we use the MIN across partials as the "first close that frees
    the slot".
    """
    # Group partials
    by_entry: Dict[Tuple[str, float], List] = {}
    for tr in trades:
        by_entry.setdefault((tr.symbol, tr.entry_time), []).append(tr)

    # Pre-compute per-entry exit time (first exit of the group)
    entries = [
        dict(
            sym=k[0], t_in=k[1],
            t_out=min(p.exit_time for p in parts),
            parts=parts,
        )
        for k, parts in by_entry.items()
    ]
    entries.sort(key=lambda e: e["t_in"])

    kept_parts: List = []
    dropped_chases: List[Dict] = []

    # For each entry, check if a DIFFERENT-symbol entry's exit_time falls in
    # the preceding `cooldown_s` window.
    for e in entries:
        is_chase = False
        chase_info = None
        for other in entries:
            if other is e: continue
            if other["sym"] == e["sym"]: continue
            if other["t_out"] > e["t_in"]: continue       # other hasn't closed yet at our entry
            gap = e["t_in"] - other["t_out"]
            if 0.0 <= gap <= cooldown_s:
                is_chase = True
                chase_info = dict(
                    sym=e["sym"], t_in=e["t_in"],
                    trigger_sym=other["sym"], trigger_exit=other["t_out"],
                    gap_s=gap,
                )
                break
        if is_chase:
            dropped_chases.append(chase_info)
        else:
            kept_parts.extend(e["parts"])

    return kept_parts, {"chases_dropped": len(dropped_chases),
                        "chase_details": dropped_chases}


# -------------------------------------------------------------------------
def build_base_trades(risk: float, news_rails: bool = True):
    cfg = MertonGZSizerConfig(
        base_risk_pct=risk, cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )
    raw, tmin, tmax, _dropped, streams = run_portfolio(SYMS, cfg)

    if news_rails:
        events = load_news_events(NEWS_CSV)
        pl = build_price_lookup(streams)
        raw, _ = apply_news_entry_block(raw, events, buffer_min=15)
        raw, _ = apply_news_flatten(raw, events, pl, minutes_before=2)

    tr = apply_full_safety_rails(raw, slippage_ticks=1.0)
    return tr


def run_from_base(tr_base, risk: float, no_chase_cooldown_s: float = 0.0):
    tr = list(tr_base)   # shallow copy
    chase_report = None
    if no_chase_cooldown_s > 0:
        tr, chase_report = apply_no_chase(tr, cooldown_s=no_chase_cooldown_s)

    s = stats(tr)
    pnls = np.array([t.net_pnl for t in tr], dtype=float)
    worst_pnl, worst_pct, worst_dd, n_days = worst_single_day(tr)
    ruins = ruin_probs(pnls)
    dur = hold_duration_stats(tr)
    conc = concurrency_stats(tr)

    return dict(
        risk=risk,
        cooldown_s=no_chase_cooldown_s,
        n=s["n"], net=s["net"], ret_pct=s["ret_pct"],
        dd_pct=s["dd_pct"], pf=s["pf"], sharpe=s["sharpe"], wr=s["wr"],
        n_days=n_days,
        worst_day_pnl=worst_pnl, worst_day_pct=worst_pct,
        worst_daily_dd_pct=worst_dd,
        ruins=ruins,
        dur=dur, conc=conc,
        chases_dropped=(chase_report["chases_dropped"] if chase_report else 0),
        chase_sample=(chase_report["chase_details"][:10] if chase_report else []),
    )


def print_row(tag: str, r: dict):
    print(f"{tag:14s} | n={r['n']:4d} | net=${r['net']:>8,.0f} | "
          f"ret={r['ret_pct']:6.2f}% | DD={r['dd_pct']:5.2f}% | "
          f"PF={r['pf']:4.2f} | WR={r['wr']*100:5.1f}% | "
          f"Sharpe={r['sharpe']:5.2f} | "
          f"worst_day={r['worst_day_pct']:+5.2f}% | "
          f"daily_DD={r['worst_daily_dd_pct']:5.2f}% | "
          f"chases_dropped={r['chases_dropped']:3d}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cooldown", type=float, default=60.0,
                    help="No-chase cooldown in seconds (default: 60)")
    ap.add_argument("--all", action="store_true",
                    help="Run 0 / 60 / 300 / 600 / 1800 cooldown sweep")
    args = ap.parse_args()

    print("=" * 140)
    print("  V23 NO-CHASE A/B — drop entries that fire within N sec of another trade's close")
    print("=" * 140)
    print(f"risk = {RISK*100:.3f}%   symbols = {SYMS}")
    print()

    cooldowns = [0, 60, 300, 600, 1800] if args.all else [0, args.cooldown]

    header = f"{'scenario':14s} | {'n':>4} | {'net':>9} | {'ret':>7} | {'DD':>6} | {'PF':>4} | {'WR':>6} | {'Sharpe':>6} | {'worst_day':>9} | {'daily_DD':>8} | chases"
    print(header)
    print("-" * len(header))

    print("  (running engine once; applying no-chase filter at each cooldown)")
    tr_base = build_base_trades(RISK, news_rails=True)
    print(f"  base trade count (after all v23 rails): {len(tr_base)}")
    print()

    out = []
    for cd in cooldowns:
        tag = "CONTROL" if cd == 0 else f"NO-CHASE-{int(cd)}s"
        r = run_from_base(tr_base, RISK, no_chase_cooldown_s=cd)
        print_row(tag, r)
        out.append({"tag": tag, **r})
        # Drop chase_sample from save file (keep summary only)
        if "chase_sample" in out[-1] and out[-1]["chase_sample"]:
            for c in out[-1]["chase_sample"]:
                c["t_in"] = datetime.fromtimestamp(c["t_in"]).isoformat()
                c["trigger_exit"] = datetime.fromtimestamp(c["trigger_exit"]).isoformat()

    print()
    # Diff rows against CONTROL
    base = out[0]
    print("Delta vs CONTROL:")
    for r in out[1:]:
        d_net = r["net"] - base["net"]
        d_dd  = r["dd_pct"] - base["dd_pct"]
        d_n   = r["n"] - base["n"]
        d_pf  = r["pf"] - base["pf"]
        d_wr  = (r["wr"] - base["wr"]) * 100
        print(f"  {r['tag']:14s}  Δnet=${d_net:+7.0f}   ΔDD={d_dd:+5.2f}pp   Δn={d_n:+3d}   ΔPF={d_pf:+.2f}   ΔWR={d_wr:+4.1f}pp")

    # Save
    out_path = ROOT / "Results" / f"backtest_v23_nochase.json"
    out_path.parent.mkdir(exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, default=str)
    print(f"\n  -> saved: {out_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
