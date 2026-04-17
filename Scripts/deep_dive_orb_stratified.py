#!/usr/bin/env python3
"""
Deep-dive: stratify ORB breakout performance by OR-width quartile,
gap direction, day-of-week, and Hurst regime.

Goal: find the COMBINATION of filters where breakout WR exceeds 55%
at a tradeable R:R.  Then v9 can be built on that.
"""

from __future__ import annotations

import math
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from Scripts.market_dna_v1 import load_bars, common_window, ORB_WINDOW_UTC


def percentile_rank(value, sorted_list):
    if not sorted_list:
        return 0.5
    n = len(sorted_list)
    below = sum(1 for v in sorted_list if v < value)
    return below / n


def simulate_orb_trade(day_bars, or_hi, or_lo, or_end_min,
                        trade_end_min, tp_mult, sl_mult):
    """
    Simulate OCO breakout:
      - Long stop at OR high; SL = OR_hi - sl_mult * OR_range; TP = OR_hi + tp_mult * OR_range
      - Short stop at OR low; SL = OR_lo + sl_mult * OR_range; TP = OR_lo - tp_mult * OR_range
    Return (outcome, direction) where outcome ∈ {'win','loss','none'}.
    """
    or_range = or_hi - or_lo
    if or_range <= 0:
        return ("none", 0)

    in_trade = None   # None | +1 | -1
    entry_px = None
    tp_px = sl_px = None

    for b in day_bars:
        mod = b.minute_of_day
        if mod < or_end_min:
            continue
        if mod >= trade_end_min:
            break
        if in_trade is None:
            # Breakout detection
            if b.h > or_hi:
                in_trade = +1
                entry_px = or_hi
                tp_px = or_hi + tp_mult * or_range
                sl_px = or_hi - sl_mult * or_range
            elif b.l < or_lo:
                in_trade = -1
                entry_px = or_lo
                tp_px = or_lo - tp_mult * or_range
                sl_px = or_lo + sl_mult * or_range
            else:
                continue
            # After break, check if same bar takes us out
            # Worst-case ordering: if both hit, assume SL first (conservative)
            if in_trade == +1:
                if b.l <= sl_px:
                    return ("loss", in_trade)
                if b.h >= tp_px:
                    return ("win", in_trade)
            else:
                if b.h >= sl_px:
                    return ("loss", in_trade)
                if b.l <= tp_px:
                    return ("win", in_trade)
        else:
            # Manage open position
            if in_trade == +1:
                if b.l <= sl_px:
                    return ("loss", in_trade)
                if b.h >= tp_px:
                    return ("win", in_trade)
            else:
                if b.h >= sl_px:
                    return ("loss", in_trade)
                if b.l <= tp_px:
                    return ("win", in_trade)
    return ("none", in_trade or 0)


def analyze_symbol(sym, bars, or_start, or_end, trade_end, tp_mult, sl_mult):
    """Build day-level dataset and analyze stratified WR."""
    # Group by day
    days = defaultdict(list)
    for b in bars:
        days[b.t.strftime("%Y-%m-%d")].append(b)

    # Per-day records
    records = []
    prev_close = None
    for day in sorted(days):
        dbs = days[day]
        or_hi = or_lo = None
        day_open = dbs[0].o
        day_close = dbs[-1].c
        for b in dbs:
            mod = b.minute_of_day
            if or_start <= mod < or_end:
                or_hi = b.h if or_hi is None else max(or_hi, b.h)
                or_lo = b.l if or_lo is None else min(or_lo, b.l)
        if or_hi is None:
            prev_close = day_close
            continue
        or_range = or_hi - or_lo
        gap = (day_open - prev_close) if prev_close else 0
        gap_to_or = gap / or_range if or_range > 0 else 0
        outcome, direction = simulate_orb_trade(
            dbs, or_hi, or_lo, or_end, trade_end, tp_mult, sl_mult)
        records.append({
            "day": day, "weekday": dbs[0].t.weekday(),
            "or_range": or_range, "gap": gap, "gap_to_or": gap_to_or,
            "outcome": outcome, "direction": direction,
        })
        prev_close = day_close

    # Compute rolling OR-width percentile (past 20 days, excluding today)
    for i, r in enumerate(records):
        past = [records[j]["or_range"] for j in range(max(0, i - 20), i)]
        past_sorted = sorted(past)
        r["or_pct"] = percentile_rank(r["or_range"], past_sorted) if past_sorted else 0.5
    return records


def stratified_wr(records, filters):
    def ok(rec):
        return all(f(rec) for f in filters)
    filtered = [r for r in records if ok(r)]
    resolved = [r for r in filtered if r["outcome"] in ("win", "loss")]
    if len(resolved) < 10:
        return None, len(resolved), len(filtered)
    wins = sum(1 for r in resolved if r["outcome"] == "win")
    return wins / len(resolved), len(resolved), len(filtered)


def print_table(title, records, tp_mult, sl_mult):
    print(f"\n--- {title} (TP={tp_mult}R, SL={sl_mult}R) ---")
    print(f"  Base ORB WR: ", end="")
    wr, n_res, n_all = stratified_wr(records, [])
    print(f"{wr*100:.1f}% on {n_res}/{n_all} setups" if wr else "insufficient data")

    print(f"\n  By OR-width percentile:")
    for lo, hi in [(0.0, 0.25), (0.25, 0.5), (0.5, 0.75), (0.75, 1.01)]:
        wr, n_res, n_all = stratified_wr(records, [
            lambda r, lo=lo, hi=hi: lo <= r["or_pct"] < hi,
        ])
        lab = f"OR-pct {int(lo*100)}-{int(hi*100)}"
        print(f"    {lab:<20} WR={wr*100:.1f}%  n={n_res}" if wr else f"    {lab:<20} n<10")

    print(f"\n  By gap direction:")
    for name, fn in [
        ("up gap > 0.3 OR", lambda r: r["gap_to_or"] > 0.3),
        ("up gap 0-0.3 OR", lambda r: 0 < r["gap_to_or"] <= 0.3),
        ("dn gap 0-0.3 OR", lambda r: -0.3 <= r["gap_to_or"] < 0),
        ("dn gap > 0.3 OR", lambda r: r["gap_to_or"] < -0.3),
    ]:
        wr, n_res, n_all = stratified_wr(records, [fn])
        print(f"    {name:<20} WR={wr*100:.1f}%  n={n_res}" if wr else f"    {name:<20} n<10")

    print(f"\n  Combined: OR-pct > 0.5 AND |gap| < 0.5 OR:")
    wr, n_res, n_all = stratified_wr(records, [
        lambda r: r["or_pct"] > 0.5,
        lambda r: abs(r["gap_to_or"]) < 0.5,
    ])
    print(f"    WR={wr*100:.1f}%  n={n_res}" if wr else f"    n<10")

    print(f"\n  Combined: OR-pct > 0.75 AND |gap| < 0.3 OR:")
    wr, n_res, n_all = stratified_wr(records, [
        lambda r: r["or_pct"] > 0.75,
        lambda r: abs(r["gap_to_or"]) < 0.3,
    ])
    print(f"    WR={wr*100:.1f}%  n={n_res}" if wr else f"    n<10")

    print(f"\n  By weekday (OR-pct > 0.5):")
    days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    for wd, name in enumerate(days):
        wr, n_res, n_all = stratified_wr(records, [
            lambda r, wd=wd: r["weekday"] == wd,
            lambda r: r["or_pct"] > 0.5,
        ])
        print(f"    {name:<4} WR={wr*100:.1f}%  n={n_res}" if wr else f"    {name:<4} n<10")


def main():
    data_dir = ROOT / "data" / "historical"
    files = {
        "US100":  data_dir / "US100_M1.csv",
        "XAUUSD": data_dir / "XAUUSD_M1.csv",
        "DE40":   data_dir / "DE40_M1.csv",
    }
    bt_start, bt_end = common_window(files, 3)
    print(f"\nWindow: {bt_start} -> {bt_end}\n")

    for sym, p in files.items():
        print(f"\n========================================================")
        print(f"  SYMBOL: {sym}")
        print(f"========================================================")
        bars = load_bars(p, bt_start, bt_end)
        print(f"  Loaded {len(bars):,} bars")
        or_start, or_end = ORB_WINDOW_UTC[sym]
        trade_end = or_end + 6 * 60   # 6-hour trade window (session)

        for tp_mult, sl_mult in [(2.0, 1.0), (1.5, 1.0), (1.0, 1.0), (1.0, 0.5)]:
            records = analyze_symbol(sym, bars, or_start, or_end, trade_end, tp_mult, sl_mult)
            print_table(f"{sym}  TP={tp_mult}  SL={sl_mult}", records, tp_mult, sl_mult)


if __name__ == "__main__":
    main()
