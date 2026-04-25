#!/usr/bin/env python3
"""
stress_test_v25_FULL_MATRIX.py — Luke's request, finally answered straight.

  14 stress scenarios × 7 risk levels (0.110 → 0.180 %) × {300 s cooldown,
  4 % daily halt, 4 % rolling DD breaker}.

  For EACH cell we show:
    - raw trade count (what the sizer produced)
    - how many were dropped by the no-chase 300-second filter
    - how many were dropped by the 4 % daily halt
    - how many were dropped by the 4 % rolling DD breaker
    - final trade count
    - net PnL, DD, worst day, halt days

  This is the FULL TRANSPARENT picture that answers:
    Q1  Is the trade-count cliff between 0.170 % and 0.175 % real or a bug?
    Q2  Does the 4 % daily halt actually fire at higher risks?
    Q3  What's the best risk level for each scenario?

  Output:
    Results/stress_test_v25_FULL_MATRIX.txt
    Results/stress_test_v25_FULL_MATRIX.json
"""
from __future__ import annotations

import json, sys, time
from pathlib import Path
from dataclasses import asdict
from collections import defaultdict

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

# --- reuse EVERYTHING from the approved pipeline -----------------------------
import Scripts.backtest_v22_lean_uk5 as btv22
from Scripts.preflight_checks import (
    SYMS, BALANCE,
    apply_full_safety_rails,
    worst_single_day,
)
from Scripts.backtest_v22_lean_uk5 import stats
from Scripts.backtest_v23_locked import (
    load_news_events, apply_news_entry_block, apply_news_flatten,
    build_price_lookup,
)
from Scripts.backtest_v23_nochase import apply_no_chase
from Scripts.stress_test_v25_180bps import apply_daily_halt
from src.dynamic_sizer_v21 import MertonGZSizerConfig
from src.stress import SCENARIOS, apply_scenario
from src.dd_breaker import apply_dd_breaker

NEWS_CSV = ROOT / "data" / "news" / "tier1_2026.csv"

# =============================================================================
#  Matrix parameters — Luke's request
# =============================================================================
RISKS = [0.00110, 0.00130, 0.00150, 0.00165, 0.00170, 0.00175, 0.00180]
NOCHASE_COOLDOWN_S = 300.0     # Luke's 5-min post-close block
DAILY_HALT_PCT     = 0.04      # Luke's PERSONAL 4 % DAILY P&L halt (NOT DD)
DD_BREAKER_PCT     = 0.04      # 4 % rolling-DD flatten-and-lock

def make_cfg(base_risk: float) -> MertonGZSizerConfig:
    return MertonGZSizerConfig(
        base_risk_pct=base_risk,
        cap_mult=5.0,
        gamma=3.0,
        ewma_alpha=0.20,
        warmup_trades=15,
        dd_cap_pct=0.04,
        pool_symbols=True,
        no_edge_multiplier=1.0,
    )

# =============================================================================
#  Monkey-patch data loader to apply scenario warp
# =============================================================================
_ORIG_LOAD_M1 = btv22.load_m1
_CURRENT_SCENARIO: str = "baseline"
_WARPED_STREAMS: dict = {}

def _patched_load_m1(path, tmin, tmax):
    bars = _ORIG_LOAD_M1(path, tmin, tmax)
    sym = Path(path).stem.replace("_M1", "")
    warped = apply_scenario(bars, _CURRENT_SCENARIO)
    _WARPED_STREAMS[sym] = warped
    return warped

btv22.load_m1 = _patched_load_m1

# =============================================================================
#  One cell of the matrix
# =============================================================================
def run_one_cell(sc_key: str, base_risk: float, symbols=None) -> dict:
    """
    Run one scenario at one risk level with the full live rail stack.

    Returns a dict with the DETAILED funnel: how many trades the sizer
    produced, and how many were dropped at each filter stage.
    """
    global _CURRENT_SCENARIO, _WARPED_STREAMS
    _CURRENT_SCENARIO = sc_key
    _WARPED_STREAMS = {}
    symbols = symbols or SYMS

    cfg = make_cfg(base_risk)
    t0 = time.time()
    raw_trades, wmin, wmax = btv22.run_portfolio(symbols, cfg)
    n_sizer = len(raw_trades)  # what the sizer produced (pre-filters)

    # Stage 1 — news rails (entry block + flatten)
    events = load_news_events(NEWS_CSV)
    pl = build_price_lookup(_WARPED_STREAMS)
    trades = raw_trades
    n0 = len(trades)
    trades, _ = apply_news_entry_block(trades, events, buffer_min=15)
    n_dropped_newsblock = n0 - len(trades)
    n0 = len(trades)
    trades, _ = apply_news_flatten(trades, events, pl, minutes_before=2)
    n_dropped_newsflatten = n0 - len(trades)

    # Stage 2 — safety rails + 1 tick slippage
    n0 = len(trades)
    trades = apply_full_safety_rails(trades, slippage_ticks=1.0)
    n_dropped_safety = n0 - len(trades)

    # Stage 3 — no-chase filter (300 s)
    n0 = len(trades)
    trades, chase_report = apply_no_chase(trades, cooldown_s=NOCHASE_COOLDOWN_S)
    n_dropped_nochase = n0 - len(trades)

    # Stage 4 — Luke's 4 % daily halt
    n0 = len(trades)
    trades, halt_state = apply_daily_halt(trades, BALANCE, DAILY_HALT_PCT)
    n_dropped_halt = n0 - len(trades)

    # Stage 5 — 4 % rolling DD breaker
    n0 = len(trades)
    trades, breaker_state = apply_dd_breaker(
        trades, starting_balance=BALANCE, halt_pct=DD_BREAKER_PCT)
    n_dropped_breaker = n0 - len(trades)

    # Final metrics
    s = stats(trades)
    pnls = np.array([t.net_pnl for t in trades], dtype=float)
    worst_pnl, worst_pct, worst_dd, n_days = worst_single_day(trades)

    return dict(
        scenario=sc_key,
        base_risk=base_risk,
        elapsed_s=round(time.time() - t0, 1),
        # funnel
        n_sizer=n_sizer,
        n_final=s["n"],
        n_dropped_newsblock=n_dropped_newsblock,
        n_dropped_newsflatten=n_dropped_newsflatten,
        n_dropped_safety=n_dropped_safety,
        n_dropped_nochase=n_dropped_nochase,
        n_dropped_halt=n_dropped_halt,
        n_dropped_breaker=n_dropped_breaker,
        # metrics
        net=s["net"],
        ret_pct=s["ret_pct"],
        dd_pct=s["dd_pct"],
        pf=s["pf"],
        sharpe=s["sharpe"],
        wr=s["wr"],
        worst_day_pnl=worst_pnl,
        worst_day_pct=worst_pct,
        worst_daily_dd_pct=worst_dd,
        # kill-switches
        daily_halts=halt_state["halts"],
        daily_halt_days=halt_state["halt_days"],
        breaker_trips=breaker_state.total_halts,
        breaker_max_dd_seen_pct=round(breaker_state.max_dd_pct_seen * 100, 3),
    )


# =============================================================================
#  Verdict logic — Luke's rules:  daily <= 4 %,  total DD <= 8 %,  PnL >= 0 or catastrophe
# =============================================================================
def verdict(r: dict, is_cat: bool) -> str:
    dd   = r["dd_pct"]
    wday = r["worst_day_pct"]
    ret  = r["ret_pct"]
    # Hard fails = would breach 5ers rules
    if wday < -5.0 or dd > 8.0:
        return "FAIL"
    # Safety-only pass? (loss but within rails, and severe scenario)
    if ret <= 0 and not is_cat:
        if wday >= -4.0 and dd <= 4.0:
            return "WARN"
        return "WARN"
    # Full pass
    if wday >= -4.0 and dd <= 4.0 and (ret > 0 or is_cat):
        return "PASS"
    if wday >= -5.0 and dd <= 5.0:
        return "WARN"
    return "WARN"


# =============================================================================
#  Main — run every (scenario, risk) cell and dump a master matrix
# =============================================================================
def main():
    lines = []
    p = lambda m="": (print(m, flush=True), lines.append(m))

    p("=" * 130)
    p("  V25 FULL-MATRIX STRESS — 14 scenarios × 7 risk levels × {300 s cooldown + 4 % daily halt + 4 % DD breaker}")
    p("  Luke's safety ladder:")
    p("      Today's P&L  <=  -4 %   →  DAILY HALT (stop trading today)")
    p("      Rolling DD    >=  4 %   →  DD BREAKER (flatten + lock week)")
    p("      5ers daily limit  = 5 %  (1 % below = your 4 %)")
    p("      5ers total limit  = 8 %  (4 % below = your DD breaker)")
    p("=" * 130)
    p(f"  Risk levels: {[f'{r*100:.3f}%' for r in RISKS]}")
    p(f"  Scenarios  : {len(SCENARIOS)}")
    p(f"  Symbols    : {SYMS}")
    p("")

    # cells[sc_key][base_risk] = run dict
    cells: dict = {sc.key: {} for sc in SCENARIOS}

    total_cells = len(SCENARIOS) * len(RISKS)
    done = 0
    t_start = time.time()

    for sc in SCENARIOS:
        for risk in RISKS:
            done += 1
            print(f"    [{done:>3}/{total_cells}] {sc.key:<16} risk={risk*100:.3f}% ...",
                  end=" ", flush=True)
            try:
                r = run_one_cell(sc.key, risk)
                print(f"N_sizer={r['n_sizer']:>3}  N_final={r['n_final']:>3}  "
                      f"PnL=${r['net']:>+8,.0f}  DD={r['dd_pct']:>5.2f}%  "
                      f"worst_day={r['worst_day_pct']:>+5.2f}%  "
                      f"halts={r['daily_halts']}  break={r['breaker_trips']}  "
                      f"({r['elapsed_s']}s)", flush=True)
            except Exception as e:
                print(f"FAILED: {type(e).__name__}: {e}", flush=True)
                r = {"scenario": sc.key, "base_risk": risk, "error": str(e),
                     "n_sizer": 0, "n_final": 0, "net": 0, "ret_pct": 0,
                     "dd_pct": 0, "worst_day_pct": 0, "daily_halts": 0,
                     "breaker_trips": 0}
            cells[sc.key][f"{risk*100:.3f}"] = r

    elapsed_total = time.time() - t_start
    p(f"\n  Done in {elapsed_total/60:.1f} min.\n")

    # =========================================================================
    #  HEADLINE MATRIX — one row per scenario, one column per risk
    # =========================================================================
    p("=" * 130)
    p("  HEADLINE — net PnL ($) by (scenario, risk)")
    p("=" * 130)
    hdr_risks = "  ".join(f"{r*100:>6.3f}%" for r in RISKS)
    p(f"  {'scenario':<30}  {hdr_risks}")
    p("  " + "-" * (30 + len(hdr_risks) + 4))
    for sc in SCENARIOS:
        row = []
        for risk in RISKS:
            r = cells[sc.key].get(f"{risk*100:.3f}", {})
            net = r.get("net", 0)
            row.append(f"${net:>+6,.0f}")
        p(f"  {sc.label[:30]:<30}  " + "  ".join(f"{v:>7}" for v in row))
    p("")

    # -------------------------------------------------------------------------
    #  DD matrix
    p("=" * 130)
    p("  DD % by (scenario, risk)")
    p("=" * 130)
    p(f"  {'scenario':<30}  {hdr_risks}")
    p("  " + "-" * (30 + len(hdr_risks) + 4))
    for sc in SCENARIOS:
        row = []
        for risk in RISKS:
            r = cells[sc.key].get(f"{risk*100:.3f}", {})
            row.append(f"{r.get('dd_pct', 0):>6.2f}%")
        p(f"  {sc.label[:30]:<30}  " + "  ".join(f"{v:>7}" for v in row))
    p("")

    # -------------------------------------------------------------------------
    #  Worst-day matrix (YOUR 4 % daily halt check)
    p("=" * 130)
    p("  Worst-single-day % by (scenario, risk)    — rule: must be > −4 % (Luke's halt) to pass")
    p("=" * 130)
    p(f"  {'scenario':<30}  {hdr_risks}")
    p("  " + "-" * (30 + len(hdr_risks) + 4))
    for sc in SCENARIOS:
        row = []
        for risk in RISKS:
            r = cells[sc.key].get(f"{risk*100:.3f}", {})
            v = r.get("worst_day_pct", 0)
            flag = " " if v >= -4.0 else "!"
            row.append(f"{v:>+5.2f}%{flag}")
        p(f"  {sc.label[:30]:<30}  " + "  ".join(f"{v:>7}" for v in row))
    p("")

    # -------------------------------------------------------------------------
    #  Trade count matrix — to show if the cliff is real
    p("=" * 130)
    p("  FINAL trade count by (scenario, risk)    — if this collapses, see the funnel below")
    p("=" * 130)
    p(f"  {'scenario':<30}  {hdr_risks}")
    p("  " + "-" * (30 + len(hdr_risks) + 4))
    for sc in SCENARIOS:
        row = []
        for risk in RISKS:
            r = cells[sc.key].get(f"{risk*100:.3f}", {})
            n = r.get("n_final", 0)
            row.append(f"{n:>7d}")
        p(f"  {sc.label[:30]:<30}  " + "  ".join(row))
    p("")

    # -------------------------------------------------------------------------
    #  Daily-halt days matrix — did your 4 % halt ever fire?
    p("=" * 130)
    p("  DAILY-HALT FIRINGS by (scenario, risk)   — how many days Luke's 4 % halt actually triggered")
    p("=" * 130)
    p(f"  {'scenario':<30}  {hdr_risks}")
    p("  " + "-" * (30 + len(hdr_risks) + 4))
    for sc in SCENARIOS:
        row = []
        for risk in RISKS:
            r = cells[sc.key].get(f"{risk*100:.3f}", {})
            row.append(f"{r.get('daily_halts', 0):>7d}")
        p(f"  {sc.label[:30]:<30}  " + "  ".join(row))
    p("")

    # -------------------------------------------------------------------------
    #  SIZER funnel: raw-trade-count by (scenario, risk) — this shows if the
    #  trade-count cliff is the SIZER refusing to trade or the filters
    p("=" * 130)
    p("  SIZER RAW TRADE COUNT (before any filter)   — if this drops, the sizer itself is refusing trades")
    p("=" * 130)
    p(f"  {'scenario':<30}  {hdr_risks}")
    p("  " + "-" * (30 + len(hdr_risks) + 4))
    for sc in SCENARIOS:
        row = []
        for risk in RISKS:
            r = cells[sc.key].get(f"{risk*100:.3f}", {})
            row.append(f"{r.get('n_sizer', 0):>7d}")
        p(f"  {sc.label[:30]:<30}  " + "  ".join(row))
    p("")

    # -------------------------------------------------------------------------
    #  FUNNEL for baseline @ each risk — where exactly do trades get dropped?
    p("=" * 130)
    p("  FUNNEL BREAKDOWN — baseline scenario only, per risk")
    p("=" * 130)
    p(f"  {'risk':<10}  {'sizer':>6}  {'-news_blk':>9}  {'-news_flat':>10}  "
      f"{'-safety':>8}  {'-nochase':>8}  {'-halt':>6}  {'-breaker':>8}  {'final':>6}")
    p("  " + "-" * 85)
    for risk in RISKS:
        r = cells["baseline"].get(f"{risk*100:.3f}", {})
        p(f"  {risk*100:>5.3f}%    {r.get('n_sizer', 0):>6d}  "
          f"{r.get('n_dropped_newsblock', 0):>9d}  "
          f"{r.get('n_dropped_newsflatten', 0):>10d}  "
          f"{r.get('n_dropped_safety', 0):>8d}  "
          f"{r.get('n_dropped_nochase', 0):>8d}  "
          f"{r.get('n_dropped_halt', 0):>6d}  "
          f"{r.get('n_dropped_breaker', 0):>8d}  "
          f"{r.get('n_final', 0):>6d}")
    p("")

    # -------------------------------------------------------------------------
    #  Best risk per scenario (Luke's 4 % DD and 4 % halt)
    p("=" * 130)
    p("  BEST RISK PER SCENARIO — max PnL where DD <= 4 % AND worst_day >= -4 %")
    p("=" * 130)
    p(f"  {'scenario':<35}  {'best_risk':>10}  {'best_net':>10}  {'best_DD':>7}  {'worst_day':>10}")
    p("  " + "-" * 85)
    for sc in SCENARIOS:
        safe_cells = [r for r in cells[sc.key].values()
                      if r.get("dd_pct", 99) <= 4.0
                      and r.get("worst_day_pct", -99) >= -4.0]
        if not safe_cells:
            p(f"  {sc.label:<35}  {'—':>10}  {'—':>10}  {'—':>7}  {'—':>10}")
            continue
        best = max(safe_cells, key=lambda r: r.get("net", -1e18))
        p(f"  {sc.label:<35}  {best['base_risk']*100:>9.3f}%  "
          f"${best['net']:>+9,.0f}  {best['dd_pct']:>6.2f}%  "
          f"{best['worst_day_pct']:>+9.2f}%")
    p("")

    # -------------------------------------------------------------------------
    #  Overall recommendation: which risk has the best worst-case across all 14
    p("=" * 130)
    p("  RISK-LEVEL SUMMARY — worst case across all 14 scenarios")
    p("=" * 130)
    p(f"  {'risk':<8}  {'worst_net':>10}  {'worst_dd':>8}  {'worst_day':>10}  "
      f"{'halt_fires':>10}  {'break_fires':>11}  {'baseline_net':>12}")
    p("  " + "-" * 80)
    for risk in RISKS:
        worst_net = min((cells[sc.key][f"{risk*100:.3f}"].get("net", 0) for sc in SCENARIOS
                         if f"{risk*100:.3f}" in cells[sc.key]), default=0)
        worst_dd = max((cells[sc.key][f"{risk*100:.3f}"].get("dd_pct", 0) for sc in SCENARIOS
                        if f"{risk*100:.3f}" in cells[sc.key]), default=0)
        worst_day = min((cells[sc.key][f"{risk*100:.3f}"].get("worst_day_pct", 0) for sc in SCENARIOS
                         if f"{risk*100:.3f}" in cells[sc.key]), default=0)
        total_halts = sum((cells[sc.key][f"{risk*100:.3f}"].get("daily_halts", 0) for sc in SCENARIOS
                           if f"{risk*100:.3f}" in cells[sc.key]))
        total_breaks = sum((cells[sc.key][f"{risk*100:.3f}"].get("breaker_trips", 0) for sc in SCENARIOS
                            if f"{risk*100:.3f}" in cells[sc.key]))
        base_net = cells["baseline"].get(f"{risk*100:.3f}", {}).get("net", 0)
        p(f"  {risk*100:>5.3f}%    ${worst_net:>+9,.0f}  {worst_dd:>7.2f}%  "
          f"{worst_day:>+9.2f}%  {total_halts:>10d}  {total_breaks:>11d}  "
          f"${base_net:>+10,.0f}")
    p("")

    # Save
    out_dir = ROOT / "Results"; out_dir.mkdir(exist_ok=True)
    with open(out_dir / "stress_test_v25_FULL_MATRIX.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    json_data = {
        "cooldown_s": NOCHASE_COOLDOWN_S,
        "daily_halt_pct": DAILY_HALT_PCT,
        "dd_breaker_pct": DD_BREAKER_PCT,
        "risks": RISKS,
        "scenarios": [sc.key for sc in SCENARIOS],
        "cells": cells,
    }
    with open(out_dir / "stress_test_v25_FULL_MATRIX.json", "w") as f:
        json.dump(json_data, f, indent=2, default=str)
    p(f"  Saved: Results/stress_test_v25_FULL_MATRIX.txt + .json")


if __name__ == "__main__":
    main()
