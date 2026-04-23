#!/usr/bin/env python3
"""
accountability_ledger.py

ONE script. Same 5 symbols, same Jan 20 → Apr 21 2026 window, same M1 data.
Shows EXACTLY where the PnL went between:

    Stage 0 :  raw engine, no rails, no HMM, no position cap      <-- closest to the PhD-grid "11k" number
    Stage 1 :  + realistic slippage (+1 tick round-trip)
    Stage 2 :  + position cap (<=2 concurrent)
    Stage 3 :  + weekend flat
    Stage 4 :  + daily kill-switch (-1% per day)
    Stage 5 :  + HMM regime gate (p_trend >= 0.55)                 <-- v22 Phase B
    Stage 6 :  + news entry block + news flatten (-2 min)           <-- v23 news-aware
    Stage 7 :  + cap_mult tightening 3.0 -> 2.5                    <-- v23 LOCKED final

All stages use the SAME base_risk_pct = 0.110% (your chosen value).
This IS real historical replay. No stress-test numbers in here.
"""
from __future__ import annotations
import sys
from copy import deepcopy
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from Scripts.backtest_v22_lean_uk5 import (
    BALANCE, stats,
    apply_slippage, apply_weekend_flat, apply_daily_kill_switch,
    apply_position_cap,
)
from Scripts.backtest_v22_phase_b import (
    run_portfolio, fit_hmm_for_symbol, HMM_TREND_THRESHOLD,
)
from Scripts.backtest_v23_locked import (
    LOCKED_SYMBOLS, load_news_events,
    apply_news_entry_block, apply_news_flatten,
    build_price_lookup,
)
from src.dynamic_sizer_v21 import MertonGZSizerConfig


def short_row(label, trades, prev_net, balance):
    s = stats(trades)
    delta = s["net"] - prev_net
    sign = "+" if delta >= 0 else ""
    return (f"  {label:<55}  N={s['n']:>4}   "
            f"net=${s['net']:>+9,.0f}   "
            f"delta={sign}${delta:>+9,.0f}   "
            f"DD={s['dd_pct']:>5.2f}%   "
            f"PF={s['pf']:.2f}   "
            f"WR={s['wr']*100:>4.1f}%")


def main():
    print("=" * 140)
    print("  ACCOUNTABILITY LEDGER — same data, same risk (0.110%), single pipeline")
    print("=" * 140)

    # ---------- build raw engine output ONCE (cap_mult=3.0 baseline) ----------
    cfg_loose = MertonGZSizerConfig(base_risk_pct=0.00110, cap_mult=3.0, gamma=2.0)
    print("  Building raw engine (base_risk=0.110%, cap_mult=3.0, NO rails) ...")
    raw_trades, tmin_s, tmax_s, _dropped_early, streams = run_portfolio(
        LOCKED_SYMBOLS, cfg_loose,
        hmm_gates=None, hmm_threshold=HMM_TREND_THRESHOLD,
    )
    print(f"  window              : {tmin_s}  ->  {tmax_s}")
    print(f"  raw engine trades   : {len(raw_trades)}")
    print()

    # Load news events (for stage 6)
    events = load_news_events(ROOT / "data" / "news" / "tier1_2026.csv")
    pl = build_price_lookup(streams)

    # Fit HMM per symbol (for stage 5)
    hmm_gates = {}
    for sym, bars in streams.items():
        _m, p = fit_hmm_for_symbol(bars)
        hmm_gates[sym] = p if p else {}

    print("  STAGE                                                    N      net        delta       DD       PF     WR")
    print("  " + "-" * 132)

    # ----- Stage 0: raw, nothing applied -----
    t0 = list(raw_trades)
    s0 = stats(t0)
    print(short_row("STAGE 0 : raw engine, NO rails, NO HMM, cap_mult=3.0", t0, 0, BALANCE))

    # ----- Stage 1: + slippage -----
    t1 = apply_slippage(list(t0), slippage_ticks=1.0)
    print(short_row("STAGE 1 : + slippage (+1 tick round-trip)", t1, s0["net"], BALANCE))

    # ----- Stage 2: + position cap -----
    t2, _ = apply_position_cap(list(t1), max_concurrent=2)
    s2 = stats(t2)
    print(short_row("STAGE 2 : + position cap (<=2 concurrent)", t2, stats(t1)["net"], BALANCE))

    # ----- Stage 3: + weekend flat -----
    t3, _ = apply_weekend_flat(list(t2), cutoff_hour_utc=20)
    print(short_row("STAGE 3 : + weekend flat (Fri >=20 UTC)", t3, s2["net"], BALANCE))

    # ----- Stage 4: + daily kill -----
    t4, _ = apply_daily_kill_switch(list(t3), threshold_pct=1.0)
    s3 = stats(t3)
    print(short_row("STAGE 4 : + daily kill-switch (-1%/day)", t4, s3["net"], BALANCE))

    # ----- Stage 5: + HMM gate -----
    s4 = stats(t4)
    hmm_trades = []
    for tr in t4:
        d = datetime.fromtimestamp(tr.entry_time).date()
        if hmm_gates.get(tr.symbol, {}).get(d, 1.0) >= HMM_TREND_THRESHOLD:
            hmm_trades.append(tr)
    print(short_row("STAGE 5 : + HMM regime gate (p_trend>=0.55) = v22 Phase B", hmm_trades, s4["net"], BALANCE))

    # ----- Stage 6: + news entry-block + news flatten -----
    s5 = stats(hmm_trades)
    t6, _ = apply_news_entry_block(list(hmm_trades), events, buffer_min=15)
    t6, _ = apply_news_flatten(t6, events, pl, minutes_before=2)
    print(short_row("STAGE 6 : + news block (+/-15min) + flatten (-2min) = v23 news", t6, s5["net"], BALANCE))

    # ----- Stage 7: re-run with cap_mult=2.5 -----
    # This requires re-running the engine (cap affects sizing, not just filter)
    print("\n  Re-running engine with cap_mult=2.5 (tighter sizing cap) ...")
    cfg_tight = MertonGZSizerConfig(base_risk_pct=0.00110, cap_mult=2.5, gamma=2.0)
    raw_tight, _, _, _, streams2 = run_portfolio(
        LOCKED_SYMBOLS, cfg_tight,
        hmm_gates=None, hmm_threshold=HMM_TREND_THRESHOLD,
    )
    pl2 = build_price_lookup(streams2)
    # re-apply all rails
    t7 = apply_slippage(list(raw_tight), slippage_ticks=1.0)
    t7, _ = apply_position_cap(t7, max_concurrent=2)
    t7, _ = apply_weekend_flat(t7, cutoff_hour_utc=20)
    t7, _ = apply_daily_kill_switch(t7, threshold_pct=1.0)
    hmm_t7 = []
    for tr in t7:
        d = datetime.fromtimestamp(tr.entry_time).date()
        if hmm_gates.get(tr.symbol, {}).get(d, 1.0) >= HMM_TREND_THRESHOLD:
            hmm_t7.append(tr)
    t7, _ = apply_news_entry_block(hmm_t7, events, buffer_min=15)
    t7, _ = apply_news_flatten(t7, events, pl2, minutes_before=2)
    s6 = stats(t6)
    print(short_row("STAGE 7 : + cap_mult 3.0 -> 2.5 tightening = v23 LOCKED FINAL", t7, s6["net"], BALANCE))

    s7 = stats(t7)
    print("  " + "-" * 132)
    print(f"\n  BOTTOM LINE: raw engine ${s0['net']:+,.0f}  ->  v23 LOCKED ${s7['net']:+,.0f}   "
          f"(haircut = ${s0['net']-s7['net']:+,.0f})")
    print(f"  Max observed DD went from {s0['dd_pct']:.2f}%  ->  {s7['dd_pct']:.2f}%")
    print()
    print("  THIS IS ALL HISTORICAL PLAYBACK. No simulation. Every dollar delta is a REAL rail pruning real trades.")
    print("=" * 140)


if __name__ == "__main__":
    main()
