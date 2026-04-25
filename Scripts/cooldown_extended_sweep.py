#!/usr/bin/env python3
"""
cooldown_extended_sweep.py — Test Luke's "wild west" concern directly.

Luke's concern (2026-04-25):
    "DAX trades alone in the morning. Then US30/US500/XAUUSD all open
    at the same time. With max 2 concurrent, the 3rd symbol gets
    rejected. ~25-45 min later one closes; a slot frees; the 3rd
    symbol enters on a stale signal. The 300s cooldown only catches
    the first 5 min — after that the bot trades regardless of how
    long ago the OR break was. Wild west."

This script tests the concern directly by sweeping the cross-symbol
cooldown from 300 s (current v25) up to 7200 s (2 hours = entire
trade window).

If long cooldowns drop:
    - GOOD trades → late entries are real edge, current 300s is fine
    - BAD trades  → Luke is right, current 300s is too short

Two filter variants tested:
    A) `apply_no_chase`         — current v25 (drop trades within N s
                                   of ANOTHER symbol's recent close)
    B) `apply_session_lock`     — once a symbol gets concurrency-rejected
                                   in a session, lock the WHOLE PORTFOLIO
                                   from new entries until next session
                                   (Luke's strict interpretation)

Output: Results/cooldown_extended_sweep.{txt,json}
"""
from __future__ import annotations

import json, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

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


def session_date_utc(ts: float) -> str:
    """Return the trading session date based on UTC date."""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def aggregate_to_entries(trades):
    """Group partials → unique entries (one record per (sym, entry_time))."""
    entries = {}
    for t in trades:
        k = (t.symbol, t.entry_time)
        entries.setdefault(k, []).append(t)
    out = []
    for (sym, t_in), parts in entries.items():
        agg_pnl = sum(p.net_pnl for p in parts)
        # Use minimum exit time across partials
        min_exit = min(p.exit_time for p in parts)
        out.append(dict(symbol=sym, entry_time=t_in, exit_time=min_exit,
                        pnl=agg_pnl, parts=parts))
    return sorted(out, key=lambda r: r["entry_time"])


def apply_extended_cooldown(trades, cooldown_s: float):
    """
    Cross-symbol cooldown: drop entries that occur within `cooldown_s`
    seconds of ANOTHER symbol's recent close.

    This is the same logic as apply_no_chase, just exposed at any duration.
    """
    return apply_no_chase(trades, cooldown_s=cooldown_s)


def apply_session_lock(trades):
    """
    Luke's strict interpretation: once any 2 trades fill the portfolio
    in a UTC session-day, lock the WHOLE portfolio from any further
    entries until the next UTC date. (i.e. simulate "no re-entry
    after slot freed up, regardless of cooldown duration").

    Implementation: per UTC session-date, keep entries in chronological
    order. Once we've taken N entries that overlap (concurrency = 2),
    drop every subsequent entry within the same UTC date.
    """
    if not trades:
        return trades, 0
    # Aggregate to unique entries
    entries = aggregate_to_entries(trades)
    keep_keys = set()
    dropped_keys = set()
    by_session = defaultdict(list)
    for e in entries:
        by_session[session_date_utc(e["entry_time"])].append(e)
    for sess_date, sess_entries in by_session.items():
        # Sort by entry_time ascending
        sess_entries.sort(key=lambda r: r["entry_time"])
        # Track active intervals (entry, exit). Concurrency = number active at moment t.
        # First N=2 trades are admitted; once both slots are filled and one closes,
        # any subsequent entry in this session is dropped.
        admitted = []   # list of (entry_time, exit_time)
        concurrency_ever_filled = False
        for e in sess_entries:
            t_in = e["entry_time"]
            # Compute current concurrency at t_in based on `admitted`
            cur = sum(1 for (a_in, a_out) in admitted if a_in <= t_in < a_out)
            if cur >= 2:
                # Bot would normally reject this trade for concurrency anyway;
                # we count it as dropped (though it may not have made it through
                # apply_full_safety_rails either)
                dropped_keys.add((e["symbol"], e["entry_time"]))
                continue
            if concurrency_ever_filled:
                # Lock active: this is exactly the "stale" entry Luke wants to drop
                dropped_keys.add((e["symbol"], e["entry_time"]))
                continue
            # Admit
            admitted.append((t_in, e["exit_time"]))
            keep_keys.add((e["symbol"], e["entry_time"]))
            # Check if portfolio just hit max concurrency (= 2)
            cur_after = sum(1 for (a_in, a_out) in admitted if a_in <= t_in < a_out)
            if cur_after >= 2:
                concurrency_ever_filled = True
    # Filter the original trade list (preserving partials)
    keep_trades = [t for t in trades if (t.symbol, t.entry_time) in keep_keys]
    n_dropped = len(dropped_keys)
    return keep_trades, n_dropped


# ============================================================================
def main():
    print("="*92)
    print("  COOLDOWN EXTENDED SWEEP @ base_risk = 0.170 %")
    print("  Question: would a longer cross-symbol cooldown catch Luke's 'stale' trades?")
    print("="*92)

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
    base_no_filter = apply_full_safety_rails(raw, slippage_ticks=1.0)

    n_base_unique = len(set((t.symbol, t.entry_time) for t in base_no_filter))
    print(f"\n  Base trades (post-safety-rails, NO cooldown): "
          f"{len(base_no_filter)} partials, {n_base_unique} unique entries")

    # =====================================================================
    # 1) Sweep cooldown durations
    # =====================================================================
    print("\n" + "="*92)
    print("  1) CROSS-SYMBOL COOLDOWN SWEEP")
    print("  (drop trade if ANOTHER symbol's trade closed within `cooldown` seconds)")
    print("="*92)
    print(f"  {'Cooldown':<12}  {'N':>5}  {'Drop':>5}  {'Net PnL':>11}  "
          f"{'DD%':>6}  {'PF':>5}  {'WR%':>5}  {'WorstDay':>9}  vs current v25")
    print("  " + "-"*92)
    sweep = []
    cooldowns_s = [0, 60, 300, 600, 900, 1800, 3600, 5400, 7200]
    for c in cooldowns_s:
        if c == 0:
            filtered = list(base_no_filter)
            dropped_n = 0
        else:
            filtered, dropped_n = apply_extended_cooldown(list(base_no_filter), c)
        s = stats(filtered)
        worst_pnl, worst_pct, _wdd, _nd = worst_single_day(filtered)
        unique_n = len(set((t.symbol, t.entry_time) for t in filtered))
        delta = s["net"] - 27668.0  # vs current v25 baseline
        sweep.append(dict(cooldown_s=c, n=unique_n, dropped=n_base_unique - unique_n,
                          net=s["net"], dd=s["dd_pct"], pf=s["pf"], wr=s["wr"],
                          worst_day=worst_pct, delta=delta))
        if c == 0:
            label = "0 s (none)"
        elif c == 300:
            label = "300 s ★"   # current v25
        elif c >= 3600:
            label = f"{c//60} min"
        else:
            label = f"{c} s"
        print(f"  {label:<12}  {unique_n:>5}  {n_base_unique-unique_n:>5}  "
              f"${s['net']:>9,.0f}  {s['dd_pct']:>5.2f}%  "
              f"{s['pf']:>5.2f}  {s['wr']:>4.1f}%  "
              f"{worst_pct:>+8.2f}%  {delta:+,.0f}")

    # =====================================================================
    # 2) Strict session lock
    # =====================================================================
    print("\n" + "="*92)
    print("  2) LUKE'S STRICT SESSION LOCK")
    print("  Once 2 trades fill the portfolio in a UTC date, no more entries that day.")
    print("="*92)
    filtered, dropped_n = apply_session_lock(list(base_no_filter))
    s = stats(filtered)
    worst_pnl, worst_pct, _wdd, _nd = worst_single_day(filtered)
    unique_n = len(set((t.symbol, t.entry_time) for t in filtered))
    delta = s["net"] - 27668.0
    print(f"\n  Session-locked trades: {unique_n} unique entries (dropped {dropped_n})")
    print(f"  Net PnL: ${s['net']:,.0f}  vs current v25: {delta:+,.0f}")
    print(f"  DD: {s['dd_pct']:.2f}%  PF: {s['pf']:.2f}  WR: {s['wr']:.1f}%")
    print(f"  Worst Day: {worst_pct:+.2f}%")

    # =====================================================================
    # 3) "Late-entry only" subset analysis
    # =====================================================================
    print("\n" + "="*92)
    print("  3) WHAT GETS DROPPED AT EACH COOLDOWN LEVEL?")
    print("  (cumulative — going from 300 s [current] to 7200 s [whole window])")
    print("="*92)

    # Get entry sets at each cooldown level
    sets_by_cooldown = {}
    for c in [300, 600, 1800, 3600, 7200]:
        f, _ = apply_extended_cooldown(list(base_no_filter), c)
        sets_by_cooldown[c] = set((t.symbol, t.entry_time) for t in f)
    # What the current 300s drops vs longer
    base_set = sets_by_cooldown[300]
    print(f"\n  At 300 s (current v25):  {len(base_set)} entries kept")
    for c in [600, 1800, 3600, 7200]:
        cur_set = sets_by_cooldown[c]
        newly_dropped_keys = base_set - cur_set
        # Compute PnL of newly-dropped entries (across ALL their partials)
        newly_dropped_pnl = sum(
            t.net_pnl for t in base_no_filter
            if (t.symbol, t.entry_time) in newly_dropped_keys
        )
        n_dropped_unique = len(newly_dropped_keys)
        # Per-symbol breakdown
        per_sym_drops = defaultdict(lambda: dict(n=0, pnl=0.0))
        for t in base_no_filter:
            if (t.symbol, t.entry_time) in newly_dropped_keys:
                per_sym_drops[t.symbol]["n"] += 0  # don't double-count partials
        seen = set()
        for t in base_no_filter:
            if (t.symbol, t.entry_time) in newly_dropped_keys:
                if (t.symbol, t.entry_time) not in seen:
                    per_sym_drops[t.symbol]["n"] += 1
                    seen.add((t.symbol, t.entry_time))
                per_sym_drops[t.symbol]["pnl"] += t.net_pnl
        sym_str = " ".join(
            f"{sym}={d['n']}({'+' if d['pnl']>=0 else ''}{d['pnl']:.0f})"
            for sym, d in per_sym_drops.items()
        )
        label = f"{c//60} min" if c >= 3600 else f"{c} s"
        print(f"  At {label:<8}:  {len(cur_set)} entries kept ({n_dropped_unique} more dropped vs 300 s)")
        print(f"                Newly-dropped PnL: ${newly_dropped_pnl:+,.0f}  →  {sym_str}")

    # =====================================================================
    # Summary verdict
    # =====================================================================
    print("\n" + "="*92)
    print("  VERDICT")
    print("="*92)
    best = max(sweep, key=lambda x: x["net"])
    print(f"  Best cooldown by Net PnL: {best['cooldown_s']} s → ${best['net']:,.0f} "
          f"(delta {best['delta']:+,.0f} vs current v25)")
    print(f"  Best cooldown by DD     : "
          + str(min(sweep, key=lambda x: x["dd"])))

    # Save
    out_json = ROOT / "Results" / "cooldown_extended_sweep.json"
    with out_json.open("w", encoding="utf-8") as f:
        json.dump(dict(sweep=sweep, session_lock=dict(
            n=unique_n, dropped=dropped_n, net=s["net"],
            dd=s["dd_pct"], pf=s["pf"], wr=s["wr"], worst_day=worst_pct,
        )), f, indent=2, default=str)
    print(f"\n  Saved: {out_json}")


if __name__ == "__main__":
    main()
