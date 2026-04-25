#!/usr/bin/env python3
"""
cooldown_shootout_170.py — Compare 4 cooldown policies at 0.170 % base risk
on the REAL 3-month 5ers data, with per-symbol breakdowns including losing
trades, biggest losses, gross profit/loss, etc.

Configs tested:
   A) RAW                  : no cooldown filter (raw v23 at 0.170 %)
   B) 300S CROSS           : current v25 — 300 s cross-symbol cooldown
   C) ONE-SHOT PER SESSION : Luke's proposal — only ONE trade per (symbol, UTC date)
   D) BOTH                 : 300 s cross-symbol + one-shot per session

Output:
   Results/cooldown_shootout_170.txt
   Results/cooldown_shootout_170.json
"""
from __future__ import annotations

import json, sys
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


# ============================================================================
#  ONE-SHOT PER (SYMBOL, UTC DATE) FILTER
# ============================================================================
def apply_one_shot_per_session(trades):
    """
    Drop any trade whose (symbol, UTC date of entry) was already used by an
    earlier-entered trade. In other words: take the FIRST breakout per symbol
    per session, ignore everything after.
    """
    by_entry: Dict[Tuple[str, float], List] = {}
    for tr in trades:
        by_entry.setdefault((tr.symbol, tr.entry_time), []).append(tr)

    entries = sorted(by_entry.items(), key=lambda kv: kv[0][1])  # sort by entry_time

    seen_keys: set = set()
    kept_parts: List = []
    dropped_count = 0
    for (sym, t_in), parts in entries:
        # Convert entry timestamp → UTC date string YYYY-MM-DD
        date_str = datetime.fromtimestamp(t_in, tz=timezone.utc).strftime("%Y-%m-%d")
        key = (sym, date_str)
        if key in seen_keys:
            dropped_count += 1
            continue
        seen_keys.add(key)
        kept_parts.extend(parts)
    return kept_parts, dropped_count


# ============================================================================
#  PER-SYMBOL DETAIL (group partials → single "trade events")
# ============================================================================
def per_symbol_detail(trades) -> Dict[str, dict]:
    """For each symbol, return win/loss counts, gross profit, gross loss, PF,
    biggest winner, biggest loser, total net PnL — grouped by ENTRY (so a
    multi-partial scaled exit counts as ONE trade)."""
    by_entry: Dict[Tuple[str, float], List] = {}
    for tr in trades:
        by_entry.setdefault((tr.symbol, tr.entry_time), []).append(tr)

    out: Dict[str, dict] = defaultdict(lambda: dict(
        n=0, wins=0, losses=0, scratches=0,
        gross_profit=0.0, gross_loss=0.0, net=0.0,
        biggest_win=0.0, biggest_loss=0.0,
        pnls=[],
    ))

    for (sym, t_in), parts in by_entry.items():
        agg = sum(p.net_pnl for p in parts)
        d = out[sym]
        d["n"] += 1
        d["pnls"].append(agg)
        d["net"] += agg
        if agg > 1e-6:
            d["wins"] += 1
            d["gross_profit"] += agg
            if agg > d["biggest_win"]: d["biggest_win"] = agg
        elif agg < -1e-6:
            d["losses"] += 1
            d["gross_loss"] += agg
            if agg < d["biggest_loss"]: d["biggest_loss"] = agg
        else:
            d["scratches"] += 1

    # finalize
    final: Dict[str, dict] = {}
    for sym, d in out.items():
        n = d["n"]; w = d["wins"]; l = d["losses"]
        gp = d["gross_profit"]; gl = abs(d["gross_loss"])
        pf = (gp / gl) if gl > 0 else float("inf") if gp > 0 else 0.0
        wr = (w / max(1, w + l)) * 100.0
        avg_w = (gp / w) if w else 0.0
        avg_l = (-gl / l) if l else 0.0
        final[sym] = dict(
            n=n, wins=w, losses=l, scratches=d["scratches"],
            gross_profit=gp, gross_loss=-gl,
            net=d["net"],
            biggest_win=d["biggest_win"], biggest_loss=d["biggest_loss"],
            avg_win=avg_w, avg_loss=avg_l,
            pf=pf, wr=wr,
        )
    return final


def headline(trades) -> dict:
    """Portfolio-level headline using existing stats() helper, but de-duplicating
    via partials count (matches what the report tables show)."""
    s = stats(trades)
    pnls = np.array([t.net_pnl for t in trades], dtype=float)
    worst_pnl, worst_pct, _wd_dd, _n_days = worst_single_day(trades)
    n_unique = len(set((t.symbol, t.entry_time) for t in trades))
    return dict(
        n_partials=s["n"], n_unique_entries=n_unique,
        net=s["net"], ret_pct=s["ret_pct"],
        dd_pct=s["dd_pct"], pf=s["pf"], sharpe=s["sharpe"], wr=s["wr"],
        worst_day_pct=worst_pct,
    )


# ============================================================================
def main():
    print("="*88)
    print("  COOLDOWN POLICY SHOOT-OUT @ base_risk = 0.170 %")
    print("  Real 3-month 5ers data | DE40, US30, XAUUSD, US500 | 1-tick slippage")
    print("="*88)

    # Build base trade list once (no filters)
    cfg = MertonGZSizerConfig(
        base_risk_pct=RISK, cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )
    raw, tmin, tmax, _dropped, streams = run_portfolio(SYMS, cfg)

    # News rails + safety rails
    events = load_news_events(NEWS_CSV)
    pl = build_price_lookup(streams)
    raw, _ = apply_news_entry_block(raw, events, buffer_min=15)
    raw, _ = apply_news_flatten(raw, events, pl, minutes_before=2)
    base = apply_full_safety_rails(raw, slippage_ticks=1.0)
    print(f"\n  Base trade-partial count after safety rails: {len(base)}")
    print(f"  Base unique entries: {len(set((t.symbol, t.entry_time) for t in base))}")

    # ---- Config A: RAW (no cooldown) ----
    A_trades = list(base)

    # ---- Config B: 300 s cross-symbol cooldown ----
    B_trades, _ = apply_no_chase(list(base), cooldown_s=300.0)

    # ---- Config C: one-shot per (symbol, UTC date) ----
    C_trades, c_dropped = apply_one_shot_per_session(list(base))

    # ---- Config D: BOTH ----
    D_step1, _ = apply_no_chase(list(base), cooldown_s=300.0)
    D_trades, d_dropped = apply_one_shot_per_session(D_step1)

    configs = [
        ("A_RAW",   A_trades, "no cooldown filter"),
        ("B_300S",  B_trades, "300 s cross-symbol cooldown (current v25)"),
        ("C_ONE",   C_trades, "one-shot per (symbol, UTC date)"),
        ("D_BOTH",  D_trades, "300 s cross-symbol + one-shot per session"),
    ]

    results = {}
    for name, trs, desc in configs:
        h = headline(trs)
        ps = per_symbol_detail(trs)
        results[name] = dict(desc=desc, headline=h, per_symbol=ps)

    # ===== PRINT =====
    # Headline comparison
    print("\n" + "="*88)
    print("  PORTFOLIO HEADLINES")
    print("="*88)
    print(f"  {'Config':<10}  {'unique':>7}  {'partials':>9}  {'Net PnL':>11}  "
          f"{'Ret%':>8}  {'DD%':>7}  {'PF':>5}  {'WR%':>6}  {'WorstDay':>9}  Description")
    print("  " + "-"*86)
    for name, trs, desc in configs:
        h = results[name]["headline"]
        print(f"  {name:<10}  {h['n_unique_entries']:>7}  {h['n_partials']:>9}  "
              f"${h['net']:>9,.0f}  "
              f"{h['ret_pct']:>+7.2f}%  {h['dd_pct']:>6.2f}%  "
              f"{h['pf']:>5.2f}  {h['wr']:>5.1f}%  {h['worst_day_pct']:>+8.2f}%  {desc}")

    # Per-symbol detail per config
    for name, trs, desc in configs:
        print("\n" + "="*88)
        print(f"  CONFIG {name}: {desc}")
        print("="*88)
        ps = results[name]["per_symbol"]
        print(f"  {'Symbol':<8}  {'N':>4}  {'W':>4}  {'L':>4}  "
              f"{'GrossPro':>10}  {'GrossLoss':>11}  {'Net':>10}  "
              f"{'BigWin':>9}  {'BigLoss':>9}  {'PF':>6}  {'WR%':>5}")
        print("  " + "-"*86)
        total = dict(n=0, w=0, l=0, gp=0.0, gl=0.0, net=0.0)
        for sym in SYMS:
            d = ps.get(sym)
            if d is None:
                print(f"  {sym:<8}  {'-':>4}")
                continue
            print(f"  {sym:<8}  {d['n']:>4}  {d['wins']:>4}  {d['losses']:>4}  "
                  f"${d['gross_profit']:>8,.0f}  ${d['gross_loss']:>9,.0f}  "
                  f"${d['net']:>8,.0f}  "
                  f"${d['biggest_win']:>7,.0f}  ${d['biggest_loss']:>7,.0f}  "
                  f"{d['pf']:>6.2f}  {d['wr']:>4.1f}%")
            total['n']  += d['n']; total['w'] += d['wins']; total['l'] += d['losses']
            total['gp'] += d['gross_profit']; total['gl'] += d['gross_loss']
            total['net'] += d['net']
        pf_t = total['gp'] / abs(total['gl']) if total['gl'] < 0 else float("inf")
        wr_t = total['w'] / max(1, total['w'] + total['l']) * 100.0
        print("  " + "-"*86)
        print(f"  {'TOTAL':<8}  {total['n']:>4}  {total['w']:>4}  {total['l']:>4}  "
              f"${total['gp']:>8,.0f}  ${total['gl']:>9,.0f}  ${total['net']:>8,.0f}  "
              f"{'':>9}  {'':>9}  {pf_t:>6.2f}  {wr_t:>4.1f}%")

    # ===== SAVE =====
    out_txt = ROOT / "Results" / "cooldown_shootout_170.txt"
    out_json = ROOT / "Results" / "cooldown_shootout_170.json"
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Saved JSON: {out_json}")
    print(f"  (TXT version is just stdout above)")

    # Save TXT mirror — useful for the report
    import io
    buf = io.StringIO()
    print("="*88, file=buf)
    print("  COOLDOWN SHOOT-OUT @ 0.170 % — Results/cooldown_shootout_170.txt", file=buf)
    print("="*88, file=buf)
    print(f"  {'Config':<10}  {'unique':>7}  {'Net PnL':>11}  {'DD%':>7}  {'PF':>5}  {'WR%':>6}  Description", file=buf)
    for name, trs, desc in configs:
        h = results[name]["headline"]
        print(f"  {name:<10}  {h['n_unique_entries']:>7}  ${h['net']:>9,.0f}  "
              f"{h['dd_pct']:>6.2f}%  {h['pf']:>5.2f}  {h['wr']:>5.1f}%  {desc}", file=buf)
    out_txt.write_text(buf.getvalue(), encoding="utf-8")


if __name__ == "__main__":
    main()
