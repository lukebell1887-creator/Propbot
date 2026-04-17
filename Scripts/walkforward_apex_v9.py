#!/usr/bin/env python3
"""
Walk-forward validation for Apex v9.

Splits the 90-day window into train (60d) and test (30d).  Runs the
filter-discovery study on TRAIN ONLY, picks the top filters, then
backtests JUST THOSE filters on the TEST window.  This is a proper
out-of-sample check — no peeking.

Also reports what happens if we use DIFFERENT gap thresholds discovered
on train.  Exposes how much the strategy depends on the fixed 0.25 OR.
"""

from __future__ import annotations

import sys
import json
import copy
import time as _time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from Scripts.deep_dive_orb_stratified import (
    analyze_symbol, stratified_wr, percentile_rank
)
from Scripts.market_dna_v1 import load_bars, common_window, ORB_WINDOW_UTC


# =====================================================================
#  Walk-forward filter discovery
# =====================================================================

def discover_filters_on_train(sym, bars_train, or_start, or_end, trade_end):
    """
    Stratified WR scan on training data.  Return filters with WR >= 55%
    and n >= 10 — these are candidates for OOS test.
    """
    candidates = []
    # Sweep (gap_sign, gap_min_abs, tp_mult, sl_mult)
    for tp_mult, sl_mult in [(1.0, 1.0), (1.5, 1.0), (0.5, 1.0)]:
        records = analyze_symbol(sym, bars_train, or_start, or_end,
                                   trade_end, tp_mult, sl_mult)

        # Up-gap filters (various thresholds)
        for gap_min in [0.15, 0.25, 0.35, 0.50]:
            wr, n_res, _ = stratified_wr(records, [
                lambda r, gm=gap_min: r["gap_to_or"] > gm,
            ])
            if wr is None or n_res < 10:
                continue
            exp_R = wr * tp_mult - (1 - wr) * sl_mult
            candidates.append({
                "sym": sym, "side": "up", "gap_min": gap_min,
                "tp_mult": tp_mult, "sl_mult": sl_mult,
                "wr": wr, "n": n_res, "exp_R": exp_R,
            })
        # Down-gap filters
        for gap_min in [0.15, 0.25, 0.35, 0.50]:
            wr, n_res, _ = stratified_wr(records, [
                lambda r, gm=gap_min: r["gap_to_or"] < -gm,
            ])
            if wr is None or n_res < 10:
                continue
            exp_R = wr * tp_mult - (1 - wr) * sl_mult
            candidates.append({
                "sym": sym, "side": "dn", "gap_min": gap_min,
                "tp_mult": tp_mult, "sl_mult": sl_mult,
                "wr": wr, "n": n_res, "exp_R": exp_R,
            })
        # OR-pct filters (no gap requirement)
        for or_lo, or_hi in [(0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.01),
                              (0.5, 1.01), (0.0, 1.01)]:
            wr, n_res, _ = stratified_wr(records, [
                lambda r, lo=or_lo, hi=or_hi: lo <= r["or_pct"] < hi,
            ])
            if wr is None or n_res < 10:
                continue
            exp_R = wr * tp_mult - (1 - wr) * sl_mult
            candidates.append({
                "sym": sym, "side": "any", "gap_min": 0.0,
                "or_pct_lo": or_lo, "or_pct_hi": or_hi,
                "tp_mult": tp_mult, "sl_mult": sl_mult,
                "wr": wr, "n": n_res, "exp_R": exp_R,
            })

    # Sort by exp_R (edge per trade)
    candidates.sort(key=lambda x: -x["exp_R"])
    return candidates


def apply_filter_on_test(sym, bars_test, flt, or_start, or_end, trade_end):
    """Run the chosen filter on test bars, return (wr, n, pnl_R)."""
    records = analyze_symbol(sym, bars_test, or_start, or_end,
                               trade_end, flt["tp_mult"], flt["sl_mult"])
    fns = []
    if flt.get("side") == "up":
        fns.append(lambda r, gm=flt["gap_min"]: r["gap_to_or"] > gm)
    elif flt.get("side") == "dn":
        fns.append(lambda r, gm=flt["gap_min"]: r["gap_to_or"] < -gm)
    else:
        fns.append(
            lambda r, lo=flt.get("or_pct_lo", 0), hi=flt.get("or_pct_hi", 1.01):
                lo <= r["or_pct"] < hi
        )
    wr, n_res, n_all = stratified_wr(records, fns)
    if wr is None:
        return None, n_res, 0
    pnl_R = n_res * (wr * flt["tp_mult"] - (1 - wr) * flt["sl_mult"])
    return wr, n_res, pnl_R


# =====================================================================
#  Main
# =====================================================================

def main():
    data_dir = ROOT / "data" / "historical"
    files = {
        "US100":  data_dir / "US100_M1.csv",
        "XAUUSD": data_dir / "XAUUSD_M1.csv",
        "DE40":   data_dir / "DE40_M1.csv",
    }
    bt_start, bt_end = common_window(files, 3)

    # Split 2/3 train, 1/3 test
    total_days = (bt_end - bt_start).days
    train_end = bt_start + timedelta(days=int(total_days * 2 / 3))
    test_start = train_end
    test_end = bt_end
    print(f"\nTrain: {bt_start:%Y-%m-%d} -> {train_end:%Y-%m-%d} ({(train_end-bt_start).days}d)")
    print(f"Test:  {test_start:%Y-%m-%d} -> {test_end:%Y-%m-%d} ({(test_end-test_start).days}d)")

    for sym, p in files.items():
        print(f"\n========================================================")
        print(f"  {sym}")
        print(f"========================================================")
        bars = load_bars(p, bt_start, bt_end)
        bars_train = [b for b in bars if bt_start <= b.t < train_end]
        bars_test = [b for b in bars if test_start <= b.t < test_end]
        print(f"  Train bars: {len(bars_train):,}   Test bars: {len(bars_test):,}")

        or_start, or_end = ORB_WINDOW_UTC[sym]
        trade_end = or_end + 6 * 60

        # Discover on train
        candidates = discover_filters_on_train(
            sym, bars_train, or_start, or_end, trade_end)

        # Take top 5 by exp_R
        top5 = [c for c in candidates if c["exp_R"] > 0.1 and c["n"] >= 10][:5]

        print(f"\n  Top-5 filters discovered on TRAIN:")
        print(f"    {'side':<5} {'gap':>5} {'orpct':<12} {'tp':>4} {'sl':>4} "
              f"{'WR_tr':>6} {'n_tr':>4} {'expR_tr':>8}")
        for c in top5:
            or_band = f"{c.get('or_pct_lo',0):.2f}-{c.get('or_pct_hi',1.01):.2f}" if c["side"] == "any" else "-"
            print(f"    {c['side']:<5} {c['gap_min']:>5.2f} {or_band:<12} "
                  f"{c['tp_mult']:>4.1f} {c['sl_mult']:>4.1f} "
                  f"{c['wr']*100:>5.1f}% {c['n']:>4} {c['exp_R']:>+7.3f}")

        # Apply each top-5 to TEST
        print(f"\n  Performance on TEST (out-of-sample):")
        print(f"    {'side':<5} {'gap':>5} {'orpct':<12} {'tp':>4} {'sl':>4} "
              f"{'WR_te':>6} {'n_te':>4} {'pnl_R':>8}")
        total_n = 0; total_R = 0.0
        for c in top5:
            wr_te, n_te, pnl_R = apply_filter_on_test(
                sym, bars_test, c, or_start, or_end, trade_end)
            or_band = f"{c.get('or_pct_lo',0):.2f}-{c.get('or_pct_hi',1.01):.2f}" if c["side"] == "any" else "-"
            wr_str = f"{wr_te*100:.1f}%" if wr_te is not None else "n<10"
            print(f"    {c['side']:<5} {c['gap_min']:>5.2f} {or_band:<12} "
                  f"{c['tp_mult']:>4.1f} {c['sl_mult']:>4.1f} "
                  f"{wr_str:>6} {n_te:>4} {pnl_R:>+7.2f}R")
            total_n += n_te
            total_R += pnl_R

        print(f"\n  TOTAL on test: {total_n} trades, {total_R:+.2f}R")
        if total_n > 0:
            exp_R = total_R / total_n
            # 1% risk → 1% * total_R per 100k
            pnl_100k_1pct = 1000 * total_R   # each R at 1% of 100k = $1000
            print(f"  Avg exp_R/trade: {exp_R:+.3f}")
            print(f"  P&L on $100k @ 1% risk: ${pnl_100k_1pct:+.2f}  ({pnl_100k_1pct/1000:.2f}%)")


if __name__ == "__main__":
    main()
