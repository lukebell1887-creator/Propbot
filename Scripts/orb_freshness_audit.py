#!/usr/bin/env python3
"""
orb_freshness_audit.py — Audit "stale OR break" trades.

Hypothesis (Luke, 2026-04-25):
    DE40 opens at 06:00 UTC; its trade window is 06:15–07:15.
    US30 / US500 / XAUUSD all open at 13:30 UTC. Their OR ends at
    13:45 UTC and the trade window is 13:45–14:45.

    With max 2 concurrent positions and 3 symbols competing during
    the US window, when two symbols fire fast, the third gets
    rejected. ~25 min later one closes; a slot frees up; the third
    symbol's NEW breakout (which could be 30+ min after OR end)
    fires and is admitted. That's a "stale" entry — the OR break is
    no longer fresh.

This script:
   1. Computes minutes-since-OR-end for every trade.
   2. Buckets trades by freshness (early, mid, late).
   3. Reports per-bucket PnL / WR / PF — does the late bucket lose?
   4. Tests a freshness filter that drops trades entering >N minutes
      after their symbol's OR end.

Output:
   Results/orb_freshness_audit.txt
   Results/orb_freshness_audit.json
"""
from __future__ import annotations

import json, sys, io
from collections import defaultdict
from datetime import datetime, timezone
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
    worst_single_day, MertonGZSizerConfig,
)
from Scripts.backtest_v22_lean_uk5 import stats
from Scripts.backtest_v23_locked import (
    load_news_events, apply_news_entry_block, apply_news_flatten,
    build_price_lookup,
)
from Scripts.backtest_v23_nochase import apply_no_chase

NEWS_CSV = ROOT / "data" / "news" / "tier1_2026.csv"
RISK = 0.00170

# OR-end times in BROKER LOCAL hours (broker = UTC+2 winter, UTC+3 summer)
# Source of truth: src/live/v23_live.py::V23_ORB_CONFIGS
#   DE40   or_start=08:00 broker, or_minutes=30  → OR ends 08:30 broker
#   US30   or_start=14:30 broker, or_minutes=30  → OR ends 15:00 broker
#   XAUUSD or_start=14:30 broker, or_minutes=30  → OR ends 15:00 broker
#   US500  or_start=14:30 broker, or_minutes=15  → OR ends 14:45 broker
# All trade windows: 120 minutes after OR-end
OR_END_BROKER = {
    "DE40":   (8, 30),
    "US30":   (15, 0),
    "XAUUSD": (15, 0),
    "US500":  (14, 45),
}
TRADE_WINDOW_MIN = 120  # all 4 symbols

# EU DST (broker follows EU rules):
#  Winter (broker = UTC+2): before last Sunday of March 01:00 UTC
#  Summer (broker = UTC+3): from last Sunday of March 01:00 UTC to last Sunday of Oct 01:00 UTC
# In 2026: DST starts 2026-03-29 01:00 UTC, ends 2026-10-25 01:00 UTC
DST_START_2026 = datetime(2026, 3, 29, 1, 0, tzinfo=timezone.utc).timestamp()
DST_END_2026   = datetime(2026, 10, 25, 1, 0, tzinfo=timezone.utc).timestamp()


def broker_offset_seconds(ts_utc: float) -> int:
    """Return the broker UTC offset in seconds at the given UTC timestamp."""
    if DST_START_2026 <= ts_utc < DST_END_2026:
        return 3 * 3600   # summer: UTC+3
    return 2 * 3600       # winter: UTC+2


def minutes_since_or_end(symbol: str, ts: float) -> float:
    """Return minutes between OR end and trade entry.
    Uses broker-local time so DST transitions don't break alignment with the bot."""
    offset = broker_offset_seconds(ts)
    # Broker-local datetime: shift UTC -> broker
    dt_broker = datetime.fromtimestamp(ts + offset, tz=timezone.utc)  # naive-broker via fixed shift
    h, m = OR_END_BROKER[symbol]
    or_end_broker = dt_broker.replace(hour=h, minute=m, second=0, microsecond=0)
    delta = (dt_broker - or_end_broker).total_seconds() / 60.0
    return delta



def freshness_bucket(mins: float) -> str:
    if mins < 0:        return "before_OR"  # pre-OR-end (shouldn't happen)
    if mins <= 5.0:     return "0-5min"
    if mins <= 15.0:    return "5-15min"
    if mins <= 30.0:    return "15-30min"
    if mins <= 45.0:    return "30-45min"
    if mins <= 60.0:    return "45-60min"
    return "60min+"


def aggregate_by_entry(trades) -> List[dict]:
    """Group partials → one record per (symbol, entry_time)."""
    by_entry: Dict[Tuple[str, float], list] = {}
    for t in trades:
        by_entry.setdefault((t.symbol, t.entry_time), []).append(t)
    out = []
    for (sym, t_in), parts in by_entry.items():
        agg = sum(p.net_pnl for p in parts)
        out.append(dict(
            sym=sym, t_in=t_in,
            mins=minutes_since_or_end(sym, t_in),
            pnl=agg,
        ))
    return sorted(out, key=lambda r: r["t_in"])


def bucket_stats(records: List[dict]) -> Dict[str, dict]:
    buckets: Dict[str, dict] = defaultdict(lambda: dict(
        n=0, wins=0, losses=0,
        gross_profit=0.0, gross_loss=0.0, net=0.0,
    ))
    for r in records:
        b = freshness_bucket(r["mins"])
        d = buckets[b]
        d["n"] += 1
        d["net"] += r["pnl"]
        if r["pnl"] > 1e-6:
            d["wins"] += 1
            d["gross_profit"] += r["pnl"]
        elif r["pnl"] < -1e-6:
            d["losses"] += 1
            d["gross_loss"] += r["pnl"]
    out = {}
    for b, d in buckets.items():
        gp = d["gross_profit"]; gl = abs(d["gross_loss"])
        pf = (gp / gl) if gl > 0 else float("inf") if gp > 0 else 0.0
        wr = (d["wins"] / max(1, d["wins"] + d["losses"])) * 100.0
        avg = d["net"] / d["n"] if d["n"] else 0.0
        out[b] = dict(n=d["n"], wins=d["wins"], losses=d["losses"],
                      net=d["net"], gross_profit=gp, gross_loss=-gl,
                      pf=pf, wr=wr, avg_per_trade=avg)
    return out


def apply_freshness_filter(trades, max_minutes: float):
    """Drop trades whose entry is more than `max_minutes` after symbol's OR end."""
    keep, dropped = [], 0
    for t in trades:
        mins = minutes_since_or_end(t.symbol, t.entry_time)
        if mins <= max_minutes:
            keep.append(t)
        else:
            dropped += 1
    return keep, dropped


# ============================================================================
def main():
    print("="*90)
    print("  ORB FRESHNESS AUDIT @ base_risk = 0.170 %")
    print("  Question: do trades entering 30+ min after OR-end actually lose money?")
    print("="*90)

    # Build base trade set (with current 300s cooldown active)
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
    base = apply_full_safety_rails(raw, slippage_ticks=1.0)
    base, _ = apply_no_chase(base, cooldown_s=300.0)   # current v25 baseline

    print(f"\n  Base trades (v25 with 300s cooldown): {len(base)} partials")
    records = aggregate_by_entry(base)
    print(f"  Unique entries: {len(records)}")

    # =====================================================================
    # 1) Distribution of mins-since-OR-end, per symbol
    # =====================================================================
    print("\n" + "="*90)
    print("  1) DISTRIBUTION: minutes since OR-end (entry timing)")
    print("="*90)

    by_sym: Dict[str, list] = defaultdict(list)
    for r in records:
        by_sym[r["sym"]].append(r["mins"])

    print(f"  {'Symbol':<8}  {'N':>4}  {'Min':>6}  {'P25':>6}  {'Med':>6}  {'P75':>6}  {'Max':>6}  {'Mean':>6}")
    print("  " + "-"*70)
    for sym in SYMS:
        arr = np.array(by_sym.get(sym, []), dtype=float)
        if not len(arr):
            continue
        print(f"  {sym:<8}  {len(arr):>4}  {arr.min():>6.1f}  "
              f"{np.percentile(arr,25):>6.1f}  {np.median(arr):>6.1f}  "
              f"{np.percentile(arr,75):>6.1f}  {arr.max():>6.1f}  {arr.mean():>6.1f}")

    # =====================================================================
    # 2) Per-bucket performance
    # =====================================================================
    print("\n" + "="*90)
    print("  2) PERFORMANCE BY FRESHNESS BUCKET (all symbols)")
    print("="*90)
    buckets = bucket_stats(records)
    bucket_order = ["0-5min", "5-15min", "15-30min", "30-45min", "45-60min", "60min+"]
    print(f"  {'Bucket':<12}  {'N':>4}  {'W':>4}  {'L':>4}  "
          f"{'Net PnL':>11}  {'Avg/trade':>10}  {'PF':>7}  {'WR%':>6}")
    print("  " + "-"*78)
    cum_n = 0; cum_net = 0.0; cum_w = 0; cum_l = 0; cum_gp = 0.0; cum_gl = 0.0
    for b in bucket_order:
        d = buckets.get(b)
        if not d:
            print(f"  {b:<12}  {'-':>4}")
            continue
        print(f"  {b:<12}  {d['n']:>4}  {d['wins']:>4}  {d['losses']:>4}  "
              f"${d['net']:>9,.0f}  ${d['avg_per_trade']:>8,.0f}  "
              f"{d['pf']:>7.2f}  {d['wr']:>5.1f}%")
        cum_n += d['n']; cum_net += d['net']
        cum_w += d['wins']; cum_l += d['losses']
        cum_gp += d['gross_profit']; cum_gl += d['gross_loss']
    pf_t = cum_gp / abs(cum_gl) if cum_gl < 0 else float('inf')
    wr_t = cum_w / max(1, cum_w + cum_l) * 100.0
    print("  " + "-"*78)
    print(f"  {'TOTAL':<12}  {cum_n:>4}  {cum_w:>4}  {cum_l:>4}  "
          f"${cum_net:>9,.0f}  ${cum_net/max(1,cum_n):>8,.0f}  {pf_t:>7.2f}  {wr_t:>5.1f}%")

    # =====================================================================
    # 3) Same breakdown PER SYMBOL — is this concentrated in US session?
    # =====================================================================
    print("\n" + "="*90)
    print("  3) FRESHNESS BUCKETS, PER SYMBOL")
    print("="*90)
    for sym in SYMS:
        sym_recs = [r for r in records if r["sym"] == sym]
        if not sym_recs:
            continue
        b = bucket_stats(sym_recs)
        print(f"\n  --- {sym} ({len(sym_recs)} trades) ---")
        print(f"    {'Bucket':<12}  {'N':>3}  {'W':>3}  {'L':>3}  "
              f"{'Net':>9}  {'Avg':>8}  {'PF':>6}  {'WR%':>5}")
        for buc in bucket_order:
            d = b.get(buc)
            if not d:
                continue
            print(f"    {buc:<12}  {d['n']:>3}  {d['wins']:>3}  {d['losses']:>3}  "
                  f"${d['net']:>7,.0f}  ${d['avg_per_trade']:>6,.0f}  "
                  f"{d['pf']:>6.2f}  {d['wr']:>4.1f}%")

    # =====================================================================
    # 4) Sweep the freshness filter — does cutting late entries help?
    # =====================================================================
    print("\n" + "="*90)
    print("  4) FRESHNESS FILTER SWEEP (drop entries beyond N minutes post-OR)")
    print("="*90)
    print(f"  {'Cutoff':<10}  {'N':>4}  {'Dropped':>8}  {'Net PnL':>11}  "
          f"{'DD%':>6}  {'PF':>5}  {'WR%':>5}  {'WorstDay':>9}  vs baseline")
    print("  " + "-"*86)
    sweep = []
    for cutoff_min in [15, 20, 30, 45, 60, 999]:
        filtered, dropped = apply_freshness_filter(list(base), cutoff_min)
        s = stats(filtered)
        worst_pnl, worst_pct, _wdd, _nd = worst_single_day(filtered)
        unique_n = len(set((t.symbol, t.entry_time) for t in filtered))
        delta = s["net"] - 27668.0  # vs current v25 baseline
        sweep.append(dict(cutoff=cutoff_min, n=unique_n, dropped=dropped,
                          net=s["net"], dd=s["dd_pct"], pf=s["pf"], wr=s["wr"],
                          worst_day=worst_pct, delta=delta))
        label = f"{cutoff_min} min" if cutoff_min < 999 else "all (60min)"
        print(f"  {label:<10}  {unique_n:>4}  {dropped:>8}  ${s['net']:>9,.0f}  "
              f"{s['dd_pct']:>5.2f}%  {s['pf']:>5.2f}  {s['wr']:>4.1f}%  "
              f"{worst_pct:>+8.2f}%  {delta:+,.0f}")

    # Save
    out_json = ROOT / "Results" / "orb_freshness_audit.json"
    out_txt  = ROOT / "Results" / "orb_freshness_audit.txt"
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(dict(buckets=buckets, sweep=sweep,
                       per_symbol_buckets={s: bucket_stats([r for r in records if r["sym"]==s])
                                           for s in SYMS}), f, indent=2, default=str)

    # quick TXT mirror
    print(f"\n  Saved: {out_json}")


if __name__ == "__main__":
    main()
