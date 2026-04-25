#!/usr/bin/env python3
"""
stress_test_v25_slippage_matrix.py — Cliff-detection for real-world slippage.

Luke's concern (2026-04-25):
    "We assume 1 tick of slippage in the backtest. In the real world it
     can get a lot worse — Frankfurt open, NFP, gold fixing.  We picked
     0.170 % as optimal, but if slippage gets worse 0.170 % could fall
     off the edge the same way 0.175 % did.  We need to know AT WHAT
     SLIPPAGE the recommended risk level breaks down — and which
     slippage-resilient risk to ship if real fills are 2-3 ticks instead
     of 1."

This script builds three evidence layers:

    LAYER 1 — Risk × Slippage matrix on real data (baseline scenario)
        Risks   : 0.150, 0.165, 0.170, 0.175, 0.180 %  (all dd_cap=4 %)
                  + 0.180 % / dd_cap=5 %  (Luke's Path-2 candidate)
        Slips   : 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0 ticks
        Maps    : the cliff edge for each risk level — at what slippage
                  does each one stop being net profitable?

    LAYER 2 — Recommended config (0.170 %) under adverse scenarios at
              elevated slippage (because the cliff in adverse markets
              is what kills accounts in real life)
        Scenarios : baseline, vol_explosion, chop_hell, liquidity_crisis,
                    catastrophe (kitchen-sink)
        Slips     : 1.0, 2.0, 3.0 ticks
        Maps      : do safety rules still hold (DD ≤ 4 %, WorstDay > -4 %)
                    when both scenario AND slippage are adversarial?

    LAYER 3 — Cliff verdict per risk, summarised over slippage
        For each risk level, finds the highest slippage at which it
        remains net positive AND DD-compliant.  Picks the most
        slippage-resilient risk that still beats the v25 control.

Output:
    Results/stress_test_v25_slippage_matrix.txt
    Results/stress_test_v25_slippage_matrix.json
"""
from __future__ import annotations

import json, sys, time
from pathlib import Path
from dataclasses import asdict
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

import Scripts.backtest_v22_lean_uk5 as btv22
from Scripts.preflight_checks import (
    SYMS, BALANCE,
    apply_full_safety_rails,
    worst_single_day, ruin_probs,
)
from Scripts.backtest_v22_lean_uk5 import stats
from Scripts.backtest_v23_locked import (
    load_news_events, apply_news_entry_block, apply_news_flatten,
    build_price_lookup,
)
from Scripts.backtest_v23_nochase import apply_no_chase
from src.dynamic_sizer_v21 import MertonGZSizerConfig
from src.stress import SCENARIOS, apply_scenario
from src.dd_breaker import apply_dd_breaker

NEWS_CSV           = ROOT / "data" / "news" / "tier1_2026.csv"
NOCHASE_COOLDOWN_S = 300.0
DAILY_HALT_PCT     = 0.04
DD_BREAKER_PCT     = 0.04

# ----------------------------------------------------------------------------
# Risk configurations to map cliff for
# ----------------------------------------------------------------------------
RISK_CONFIGS = {
    "R150_dd4": MertonGZSizerConfig(
        base_risk_pct=0.00150, cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    ),
    "R165_dd4": MertonGZSizerConfig(   # current v25 ship
        base_risk_pct=0.00165, cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    ),
    "R170_dd4": MertonGZSizerConfig(   # the "optimal" pick
        base_risk_pct=0.00170, cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    ),
    "R175_dd4": MertonGZSizerConfig(   # the prior "fell off the cliff"
        base_risk_pct=0.00175, cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    ),
    "R180_dd4": MertonGZSizerConfig(   # 0.180 with strangled sizer
        base_risk_pct=0.00180, cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    ),
    "R180_dd5": MertonGZSizerConfig(   # Path-2 candidate
        base_risk_pct=0.00180, cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.05,
        pool_symbols=True, no_edge_multiplier=1.0,
    ),
}

SLIPPAGE_GRID  = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0]

# Recommended config to validate under adverse scenarios
RECOMMENDED_KEY      = "R170_dd4"
ADVERSE_SCENARIO_KEYS = ["baseline", "vol_explosion", "chop_hell",
                         "liquidity_crisis", "catastrophe"]
ADVERSE_SLIPS        = [1.0, 2.0, 3.0]


# ============================================================================
def _ts_to_date(ts):
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    if isinstance(ts, datetime):
        return ts.date()
    return ts


def apply_daily_halt(trades, starting_balance, halt_pct):
    if not trades:
        return trades, dict(halts=0, halt_days=[], dropped=0)
    trades_sorted = sorted(trades, key=lambda t: t.exit_time)
    threshold     = -halt_pct * starting_balance
    by_day        = defaultdict(float)
    halted_days   = set()
    out = []; dropped = 0
    for t in trades_sorted:
        entry_day = _ts_to_date(t.entry_time)
        exit_day  = _ts_to_date(t.exit_time)
        if entry_day in halted_days:
            dropped += 1; continue
        if by_day[entry_day] <= threshold:
            halted_days.add(entry_day); dropped += 1; continue
        out.append(t)
        by_day[exit_day] += t.net_pnl
        if by_day[exit_day] <= threshold:
            halted_days.add(exit_day)
    return out, dict(halts=len(halted_days),
                     halt_days=sorted(map(str, halted_days)),
                     dropped=dropped)


# ----------------------------------------------------------------------------
# Monkey-patch loader so scenarios can warp the bars
# ----------------------------------------------------------------------------
_ORIG_LOAD_M1     = btv22.load_m1
_CURRENT_SCENARIO = "baseline"
_WARPED_STREAMS   : dict = {}


def _patched_load_m1(path, tmin, tmax):
    bars = _ORIG_LOAD_M1(path, tmin, tmax)
    sym  = Path(path).stem.replace("_M1", "")
    warped = apply_scenario(bars, _CURRENT_SCENARIO)
    _WARPED_STREAMS[sym] = warped
    return warped


btv22.load_m1 = _patched_load_m1


# ============================================================================
def gen_news_filtered_raw(cfg_key: str, sc_key: str):
    """Produce raw trades (after news filters, before rails/slippage)."""
    global _CURRENT_SCENARIO, _WARPED_STREAMS
    _CURRENT_SCENARIO = sc_key
    _WARPED_STREAMS   = {}

    sizer = RISK_CONFIGS[cfg_key]
    raw, wmin, wmax = btv22.run_portfolio(SYMS, sizer)
    events = load_news_events(NEWS_CSV)
    pl     = build_price_lookup(_WARPED_STREAMS)
    raw, _ = apply_news_entry_block(raw, events, buffer_min=15)
    raw, _ = apply_news_flatten(raw, events, pl, minutes_before=2)
    return raw


def apply_rails_with_slip(raw, slip_ticks: float) -> dict:
    """Run rails -> no_chase -> daily_halt -> dd_breaker -> compute stats."""
    trades = apply_full_safety_rails(list(raw), slippage_ticks=slip_ticks)
    trades, chase_report = apply_no_chase(trades, cooldown_s=NOCHASE_COOLDOWN_S)
    trades, halt_state   = apply_daily_halt(trades, BALANCE, DAILY_HALT_PCT)
    trades, breaker_st   = apply_dd_breaker(trades, starting_balance=BALANCE,
                                            halt_pct=DD_BREAKER_PCT)
    s = stats(trades)
    pnls = np.array([t.net_pnl for t in trades], dtype=float)
    wp, wd_pct, wd_dd, n_days = worst_single_day(trades)
    ruins = (ruin_probs(pnls) if len(pnls) > 0
             else {"ruin@3": 0, "ruin@4": 0, "ruin@5": 0})

    return dict(
        slip=slip_ticks, n=s["n"], net=s["net"], ret_pct=s["ret_pct"],
        dd_pct=s["dd_pct"], pf=s["pf"], sharpe=s["sharpe"], wr=s["wr"],
        n_days=n_days, worst_day_pnl=wp, worst_day_pct=wd_pct,
        worst_daily_dd_pct=wd_dd,
        chases_dropped=chase_report["chases_dropped"],
        daily_halts=halt_state["halts"],
        breaker_trips=breaker_st.total_halts,
        breaker_max_dd_seen_pct=round(breaker_st.max_dd_pct_seen * 100, 3),
        **{k: v for k, v in ruins.items()},
    )


# ============================================================================
def main():
    lines = []
    p = lambda m="": (print(m), lines.append(m))

    p("=" * 124)
    p("  V25 — REAL-WORLD SLIPPAGE STRESS MATRIX")
    p("  Question: at what slippage does each risk level fall off the cliff?")
    p("=" * 124)
    p("")
    p(f"  Risks tested (6) : {', '.join(RISK_CONFIGS.keys())}")
    p(f"  Slip grid (8)    : {SLIPPAGE_GRID}  (1 tick = backtest assumption)")
    p(f"  Daily halt       : {DAILY_HALT_PCT*100:.0f} %")
    p(f"  DD breaker       : {DD_BREAKER_PCT*100:.0f} %")
    p("")

    # ========================================================================
    # LAYER 1 — Risk × Slippage matrix on baseline (real data)
    # ========================================================================
    p("=" * 124)
    p("  LAYER 1 — RISK × SLIPPAGE on baseline (real 5ers data, 3-mo)")
    p("=" * 124)

    layer1 = {}    # cfg_key -> [result(slip), ...]
    for cfg_key in RISK_CONFIGS:
        print(f"\n  ┌─ {cfg_key}  (base="
              f"{RISK_CONFIGS[cfg_key].base_risk_pct*100:.3f}%, "
              f"dd_cap={RISK_CONFIGS[cfg_key].dd_cap_pct*100:.0f}%)")
        t0 = time.time()
        raw = gen_news_filtered_raw(cfg_key, "baseline")
        rows = []
        for slip in SLIPPAGE_GRID:
            r = apply_rails_with_slip(raw, slip)
            print(f"  │  slip={slip:>4.1f}  N={r['n']:>3}  "
                  f"PnL=${r['net']:>+9,.0f}  DD={r['dd_pct']:>5.2f}%  "
                  f"WD={r['worst_day_pct']:>+5.2f}%")
            rows.append(r)
        layer1[cfg_key] = rows
        print(f"  └─ done in {time.time()-t0:.1f}s")

    p("")
    p("  TABLE — Net PnL ($)  by  risk × slippage")
    header = (f"  {'risk_cfg':<12}  "
              + "  ".join(f"{f'slip={s:.1f}':>10}" for s in SLIPPAGE_GRID))
    p(header); p("  " + "-" * (len(header)-2))
    for cfg_key in RISK_CONFIGS:
        row = f"  {cfg_key:<12}  "
        row += "  ".join(f"${r['net']:>+9,.0f}" for r in layer1[cfg_key])
        p(row)

    p("")
    p("  TABLE — Max DD (%)  by  risk × slippage")
    p(header); p("  " + "-" * (len(header)-2))
    for cfg_key in RISK_CONFIGS:
        row = f"  {cfg_key:<12}  "
        row += "  ".join(f"{r['dd_pct']:>9.2f}%" for r in layer1[cfg_key])
        p(row)

    p("")
    p("  TABLE — Worst single-day (%)  by  risk × slippage")
    p(header); p("  " + "-" * (len(header)-2))
    for cfg_key in RISK_CONFIGS:
        row = f"  {cfg_key:<12}  "
        row += "  ".join(f"{r['worst_day_pct']:>+9.2f}%" for r in layer1[cfg_key])
        p(row)

    # ========================================================================
    # LAYER 2 — Recommended (R170_dd4) under adverse scenarios at high slip
    # ========================================================================
    p("")
    p("=" * 124)
    p(f"  LAYER 2 — STRESS VALIDATION of {RECOMMENDED_KEY} "
      f"(adverse scenarios × elevated slippage)")
    p("=" * 124)
    p("")
    p(f"  Validates that the chosen ship config holds up when BOTH the")
    p(f"  market is hostile (vol explosion, chop, illiquidity) AND fills")
    p(f"  are 2-3 ticks worse than backtest.")
    p("")

    layer2 = {}
    for sc_key in ADVERSE_SCENARIO_KEYS:
        sc = next((s for s in SCENARIOS if s.key == sc_key), None)
        if sc is None:
            continue
        print(f"\n  ┌─ scenario: {sc_key}  ({sc.label})")
        t0 = time.time()
        raw = gen_news_filtered_raw(RECOMMENDED_KEY, sc_key)
        rows = []
        for slip in ADVERSE_SLIPS:
            r = apply_rails_with_slip(raw, slip)
            print(f"  │  slip={slip:>4.1f}  N={r['n']:>3}  "
                  f"PnL=${r['net']:>+9,.0f}  DD={r['dd_pct']:>5.2f}%  "
                  f"WD={r['worst_day_pct']:>+5.2f}%  halts={r['daily_halts']}")
            rows.append(r)
        layer2[sc_key] = rows
        print(f"  └─ done in {time.time()-t0:.1f}s")

    p("")
    p(f"  TABLE — {RECOMMENDED_KEY} : Net PnL by  scenario × slippage")
    sub_header = (f"  {'scenario':<22}  "
                  + "  ".join(f"{f'slip={s:.1f}':>11}" for s in ADVERSE_SLIPS))
    p(sub_header); p("  " + "-" * (len(sub_header)-2))
    for sc_key in ADVERSE_SCENARIO_KEYS:
        if sc_key not in layer2: continue
        row = f"  {sc_key:<22}  "
        row += "  ".join(f"${r['net']:>+10,.0f}" for r in layer2[sc_key])
        p(row)

    p("")
    p(f"  TABLE — {RECOMMENDED_KEY} : Max DD (%) by  scenario × slippage")
    p(sub_header); p("  " + "-" * (len(sub_header)-2))
    for sc_key in ADVERSE_SCENARIO_KEYS:
        if sc_key not in layer2: continue
        row = f"  {sc_key:<22}  "
        row += "  ".join(f"{r['dd_pct']:>10.2f}%" for r in layer2[sc_key])
        p(row)

    p("")
    p(f"  TABLE — {RECOMMENDED_KEY} : Worst-day (%) by  scenario × slippage")
    p(sub_header); p("  " + "-" * (len(sub_header)-2))
    for sc_key in ADVERSE_SCENARIO_KEYS:
        if sc_key not in layer2: continue
        row = f"  {sc_key:<22}  "
        row += "  ".join(f"{r['worst_day_pct']:>+10.2f}%" for r in layer2[sc_key])
        p(row)

    # ========================================================================
    # LAYER 3 — Cliff edges and verdict
    # ========================================================================
    p("")
    p("=" * 124)
    p("  LAYER 3 — CLIFF EDGES AND ROBUSTNESS VERDICT")
    p("=" * 124)

    cliff_rows = []
    p("")
    p("  Cliff = highest slippage that keeps both rules satisfied:")
    p("    rule 1 : Net PnL ≥ 0   (still profitable)")
    p("    rule 2 : Max DD ≤ 4 %   (5ers daily/halt limit)")
    p("    rule 3 : Worst-day ≥ -4 % (Luke's halt holds)")
    p("")
    p(f"  {'risk_cfg':<12}  {'slip@PnL>=0':>12}  {'slip@DD<=4%':>12}  "
      f"{'slip@WD>=-4%':>13}  {'verdict':<28}  {'@slip=2.0 PnL':>14}  "
      f"{'@slip=3.0 PnL':>14}")
    p("  " + "-" * 122)
    for cfg_key in RISK_CONFIGS:
        rows = layer1[cfg_key]
        slip_pnl = max((r["slip"] for r in rows if r["net"] >= 0),  default=0.0)
        slip_dd  = max((r["slip"] for r in rows if r["dd_pct"] <= 4.0), default=0.0)
        slip_wd  = max((r["slip"] for r in rows if r["worst_day_pct"] >= -4.0),
                       default=0.0)
        # 'verdict' = lowest cliff
        cliff = min(slip_pnl, slip_dd, slip_wd)
        if   cliff >= 5.0: verdict = "robust to flash-move slip"
        elif cliff >= 3.0: verdict = "robust to NY-open whipsaw"
        elif cliff >= 2.0: verdict = "robust to common 2-tick fills"
        elif cliff >= 1.5: verdict = "borderline, retail-MT5 risk"
        elif cliff >= 1.0: verdict = "FRAGILE — backtest-only OK"
        else:              verdict = "BROKEN even at backtest slip"

        # Also pull PnL at slip=2.0 and slip=3.0 for context
        r2 = next((r for r in rows if r["slip"] == 2.0), {"net": 0})
        r3 = next((r for r in rows if r["slip"] == 3.0), {"net": 0})
        cliff_rows.append(dict(
            cfg=cfg_key, slip_at_pnl_pos=slip_pnl,
            slip_at_dd_compliant=slip_dd, slip_at_wd_compliant=slip_wd,
            cliff=cliff, verdict=verdict,
            pnl_at_slip2=r2["net"], pnl_at_slip3=r3["net"],
        ))
        p(f"  {cfg_key:<12}  {slip_pnl:>11.1f}t  {slip_dd:>11.1f}t  "
          f"{slip_wd:>12.1f}t  {verdict:<28}  ${r2['net']:>+12,.0f}  "
          f"${r3['net']:>+12,.0f}")

    # Also a "robustness ranking" — best risk if real slip is X
    p("")
    p("  ROBUSTNESS RANKING — best risk to ship by assumed real-world slippage")
    p("  " + "-" * 100)
    for assumed_slip in [1.0, 1.5, 2.0, 2.5, 3.0]:
        # For each risk, get its (PnL, DD) at the assumed slip
        candidates = []
        for cfg_key, rows in layer1.items():
            r = next((rr for rr in rows if rr["slip"] == assumed_slip), None)
            if r is None: continue
            # Only include if it satisfies all 3 safety rules at that slip
            safe = (r["net"] >= 0 and r["dd_pct"] <= 4.0
                    and r["worst_day_pct"] >= -4.0)
            candidates.append((cfg_key, r, safe))
        # Best safe candidate by net PnL
        safe_sorted = sorted([c for c in candidates if c[2]],
                              key=lambda c: c[1]["net"], reverse=True)
        if safe_sorted:
            best = safe_sorted[0]
            p(f"  if real slip = {assumed_slip:>3.1f} ticks  →  "
              f"best safe ship = {best[0]:<12}  "
              f"PnL=${best[1]['net']:>+9,.0f}  "
              f"DD={best[1]['dd_pct']:.2f}%  "
              f"WD={best[1]['worst_day_pct']:+.2f}%")
        else:
            p(f"  if real slip = {assumed_slip:>3.1f} ticks  →  "
              f"NO RISK CONFIG SATISFIES ALL 3 SAFETY RULES")

    # ========================================================================
    p("")
    p("=" * 124)
    p("  HEADLINE — does the recommended ship hold up?")
    p("=" * 124)
    rec = next(c for c in cliff_rows if c["cfg"] == RECOMMENDED_KEY)
    ctrl = next(c for c in cliff_rows if c["cfg"] == "R165_dd4")
    risk175 = next(c for c in cliff_rows if c["cfg"] == "R175_dd4")
    p("")
    p(f"  Recommended ship   ({RECOMMENDED_KEY}):  cliff @ slip = "
      f"{rec['cliff']:.1f} ticks  → {rec['verdict']}")
    p(f"  Current v25 ship   (R165_dd4):  cliff @ slip = "
      f"{ctrl['cliff']:.1f} ticks  → {ctrl['verdict']}")
    p(f"  The 'fall-off' case (R175_dd4): cliff @ slip = "
      f"{risk175['cliff']:.1f} ticks  → {risk175['verdict']}")
    p("")
    if rec["cliff"] >= 2.0 and ctrl["cliff"] >= rec["cliff"]:
        p("  ✅ Both 0.165 % and 0.170 % survive 2+ tick slippage. "
          "0.170 % is shippable.")
    elif rec["cliff"] < ctrl["cliff"]:
        p(f"  ⚠️  0.170 % has a tighter slippage cliff ({rec['cliff']:.1f}t) "
          f"than 0.165 % ({ctrl['cliff']:.1f}t).")
        p("     If you cannot rely on ≤ 1.5-tick fills, prefer 0.165 %.")
    else:
        p(f"  ⚠️  Cliffs vary — see ROBUSTNESS RANKING above.")

    # ========================================================================
    # Save
    # ========================================================================
    out_dir = ROOT / "Results"; out_dir.mkdir(exist_ok=True)
    txt_path = out_dir / "stress_test_v25_slippage_matrix.txt"
    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    out_json = {
        "configs":          {k: asdict(v) for k, v in RISK_CONFIGS.items()},
        "slippage_grid":    SLIPPAGE_GRID,
        "daily_halt_pct":   DAILY_HALT_PCT,
        "dd_breaker_pct":   DD_BREAKER_PCT,
        "recommended_key":  RECOMMENDED_KEY,
        "adverse_scenario_keys": ADVERSE_SCENARIO_KEYS,
        "adverse_slips":    ADVERSE_SLIPS,
        "layer1":           {k: v for k, v in layer1.items()},
        "layer2":           {k: v for k, v in layer2.items()},
        "cliff_rows":       cliff_rows,
    }
    json_path = out_dir / "stress_test_v25_slippage_matrix.json"
    with open(json_path, "w") as f:
        json.dump(out_json, f, indent=2, default=str)
    p(f"\n  Saved: {txt_path}")
    p(f"  Saved: {json_path}")


if __name__ == "__main__":
    main()
