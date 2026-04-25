#!/usr/bin/env python3
"""
stress_test_v25_180bps_loosened.py — Test Luke's real argument.

Luke's proposition (2026-04-25):
    "I have a 4% daily halt — that's already 1% INSIDE the 5ers' real
     5% daily limit. So I have a 1% safety buffer ON TOP OF whatever the
     bot does. The internal sizer's dd_cap is also at 4%, which makes
     it self-throttle BEFORE my own halt has anything to do. Why not
     loosen the sizer's dd_cap to match the 5ers reality (5%) and
     unlock the 0.180% base risk? The 4% daily halt is still my final
     stop, so it's still safe."

The previous V25_ULTRA_180BPS test ran 0.180% with dd_cap=4% and the
sizer self-strangled (only +$6.5k vs +$27k at 0.165%). This script
tests the variant Luke is actually asking for: keep the 4% daily halt
as the safety net, but loosen the internal Merton-GZ dd_cap so it
doesn't shrink size prematurely.

Configurations tested (same 14 scenarios for each):

    A. CONTROL : base=0.165 %, dd_cap=4 %, daily_halt=4 %  (current v25)
    B. PRIOR   : base=0.180 %, dd_cap=4 %, daily_halt=4 %  (prior fail)
    C. LUKE    : base=0.180 %, dd_cap=5 %, daily_halt=4 %  (HIS PROPOSAL)
    D. ULTRA   : base=0.180 %, dd_cap=6 %, daily_halt=4 %  (extra runway)

The question this answers:
    Does loosening dd_cap from 4 % → 5 % at 0.180 % base risk recover
    the profit lost in the prior test, while still respecting the
    4 % daily halt and the 5ers 5 % / 8 % rules?

Output:
    Results/stress_test_v25_180bps_loosened.txt
    Results/stress_test_v25_180bps_loosened.json
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

NEWS_CSV = ROOT / "data" / "news" / "tier1_2026.csv"
NOCHASE_COOLDOWN_S = 300.0
DAILY_HALT_PCT     = 0.04   # Luke's personal 4 % daily kill (1 % below 5ers 5 %)
DD_BREAKER_PCT     = 0.04   # 4 % rolling DD breaker (independent of sizer dd_cap)

# ============================================================================
#   FOUR CONFIGURATIONS — same daily-halt and breaker, varying sizer
# ============================================================================
CONFIGS = {
    "A_CONTROL_165_dd4": MertonGZSizerConfig(   # current v25 ship
        base_risk_pct=0.00165, cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    ),
    "B_PRIOR_180_dd4": MertonGZSizerConfig(    # the prior failed test
        base_risk_pct=0.00180, cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    ),
    "C_LUKE_180_dd5": MertonGZSizerConfig(     # LUKE'S ACTUAL PROPOSAL
        base_risk_pct=0.00180, cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.05,
        pool_symbols=True, no_edge_multiplier=1.0,
    ),
    "D_ULTRA_180_dd6": MertonGZSizerConfig(    # extra-loose comparator
        base_risk_pct=0.00180, cap_mult=5.0, gamma=3.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.06,
        pool_symbols=True, no_edge_multiplier=1.0,
    ),
}

# ============================================================================
#   Daily-halt filter (same as 180bps script)
# ============================================================================
def _ts_to_date(ts):
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    if isinstance(ts, datetime):
        return ts.date()
    return ts


def apply_daily_halt(trades, starting_balance: float, halt_pct: float):
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


# ============================================================================
#   Monkey-patch loader so scenarios warp the bar stream
# ============================================================================
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


# ============================================================================
#   Run one (config, scenario) pair
# ============================================================================
def run_one(cfg_key: str, sc_key: str) -> dict:
    global _CURRENT_SCENARIO, _WARPED_STREAMS
    _CURRENT_SCENARIO = sc_key
    _WARPED_STREAMS = {}

    sizer_cfg = CONFIGS[cfg_key]
    raw_trades, wmin, wmax = btv22.run_portfolio(SYMS, sizer_cfg)

    events = load_news_events(NEWS_CSV)
    pl = build_price_lookup(_WARPED_STREAMS)
    raw_trades, _ = apply_news_entry_block(raw_trades, events, buffer_min=15)
    raw_trades, _ = apply_news_flatten(raw_trades, events, pl, minutes_before=2)
    trades = apply_full_safety_rails(raw_trades, slippage_ticks=1.0)

    n_before = len(trades)
    trades, chase_report = apply_no_chase(trades, cooldown_s=NOCHASE_COOLDOWN_S)
    n_dropped_nochase = n_before - len(trades)

    n_before = len(trades)
    trades, halt_state = apply_daily_halt(trades, BALANCE, DAILY_HALT_PCT)
    n_dropped_halt = n_before - len(trades)

    n_before = len(trades)
    trades, breaker_state = apply_dd_breaker(trades, starting_balance=BALANCE,
                                             halt_pct=DD_BREAKER_PCT)
    n_dropped_breaker = n_before - len(trades)

    s = stats(trades)
    pnls = np.array([t.net_pnl for t in trades], dtype=float)
    worst_pnl, worst_pct, worst_dd, n_days = worst_single_day(trades)
    ruins = ruin_probs(pnls) if len(pnls) > 0 else {"ruin@3": 0, "ruin@4": 0, "ruin@5": 0}

    return dict(
        cfg=cfg_key, scenario=sc_key,
        n=s["n"], net=s["net"], ret_pct=s["ret_pct"],
        dd_pct=s["dd_pct"], pf=s["pf"], sharpe=s["sharpe"], wr=s["wr"],
        n_days=n_days,
        worst_day_pnl=worst_pnl, worst_day_pct=worst_pct,
        worst_daily_dd_pct=worst_dd,
        chases_dropped=chase_report["chases_dropped"],
        daily_halts=halt_state["halts"],
        daily_halt_days=halt_state["halt_days"],
        breaker_trips=breaker_state.total_halts,
        breaker_max_dd_seen_pct=round(breaker_state.max_dd_pct_seen * 100, 3),
        **{k: v for k, v in ruins.items()},
    )


# ============================================================================
#   Pretty helpers
# ============================================================================
def sev_icon(sev: str) -> str:
    return {"V+": "🟢", "+": "🟢", "N": "⚪", "-": "🟠",
            "V-": "🔴", "X":  "☠️ "}.get(sev, "  ")


def verdict_for(r):
    """Apply 5ers exit-gate logic."""
    dd = r["dd_pct"]; wd = r["worst_day_pct"]; ret = r["ret_pct"]
    is_cat = False  # set externally
    if dd <= 4.0 and wd >= -4.0 and ret > -10:
        return "PASS"
    if dd <= 8.0 and wd >= -5.0:
        return "WARN"
    return "FAIL"


# ============================================================================
def main():
    lines = []
    p = lambda m="": (print(m), lines.append(m))

    p("=" * 120)
    p("  V25 — 0.180 % BASE RISK + LOOSENED dd_cap STRESS TEST")
    p("  Luke's argument: my 4 % daily halt is already 1 % inside 5ers' 5 % rule,")
    p("                   so the sizer's dd_cap can match 5ers reality (5 %) safely.")
    p("=" * 120)
    p("")
    p("  Configurations:")
    for k, c in CONFIGS.items():
        p(f"    {k:<22} base_risk={c.base_risk_pct*100:.3f}%  "
          f"dd_cap={c.dd_cap_pct*100:.1f}%  "
          f"daily_halt={DAILY_HALT_PCT*100:.0f}%  "
          f"breaker={DD_BREAKER_PCT*100:.0f}%")
    p("")
    p(f"  Running {len(CONFIGS)} configs × {len(SCENARIOS)} scenarios = "
      f"{len(CONFIGS)*len(SCENARIOS)} runs ...")
    p("")

    all_results = {}   # cfg_key -> [(scenario, result), ...]
    for cfg_key in CONFIGS:
        print(f"\n  ┌─ Config: {cfg_key} " + "─" * (90 - len(cfg_key)))
        results = []
        t_start = time.time()
        for sc in SCENARIOS:
            print(f"  │  [{sc.severity:>2}] {sc.key:<18}", end="", flush=True)
            try:
                r = run_one(cfg_key, sc.key)
                print(f"  N={r['n']:>3}  PnL={r['net']:>+9,.0f}  "
                      f"DD={r['dd_pct']:>5.2f}%  WD={r['worst_day_pct']:>+5.2f}%  "
                      f"halts={r['daily_halts']}", flush=True)
            except Exception as e:
                print(f"  !! FAILED: {type(e).__name__}: {e}", flush=True)
                r = {"cfg": cfg_key, "scenario": sc.key, "error": str(e),
                     "n": 0, "net": 0, "ret_pct": 0, "dd_pct": 0, "pf": 0,
                     "sharpe": 0, "wr": 0, "n_days": 0, "worst_day_pnl": 0,
                     "worst_day_pct": 0, "worst_daily_dd_pct": 0,
                     "chases_dropped": 0, "daily_halts": 0,
                     "daily_halt_days": [], "breaker_trips": 0,
                     "breaker_max_dd_seen_pct": 0}
            results.append((sc, r))
        all_results[cfg_key] = results
        print(f"  └─ done in {time.time()-t_start:.0f}s")

    # ========================================================================
    # COMPARISON TABLE — focus on baseline + worst-case across all 14 scenarios
    # ========================================================================
    p("=" * 120)
    p("  COMPARISON HEADLINE — same scenarios, 4 sizing configs")
    p("=" * 120)
    p(f"  {'Scenario':<28}  "
      + "  ".join(f"{k.split('_',1)[1]:>16}" for k in CONFIGS))
    p(f"  {'(severity)':<28}  "
      + "  ".join(f"{'PnL':>9} {'DD':>6}" for _ in CONFIGS))
    p("  " + "-" * 116)

    for sc in SCENARIOS:
        row = f"  {sev_icon(sc.severity)} {sc.label[:24]:<26}"
        for cfg_key in CONFIGS:
            r = next(rr for s, rr in all_results[cfg_key] if s.key == sc.key)
            if "error" in r:
                row += f"  {'ERR':>9} {'-':>6}"
            else:
                row += f"  ${r['net']:>+8,.0f} {r['dd_pct']:>5.2f}%"
        p(row)

    # ========================================================================
    # PER-CONFIG SUMMARY
    # ========================================================================
    p("")
    p("=" * 120)
    p("  PER-CONFIG SUMMARY")
    p("=" * 120)
    summaries = {}
    for cfg_key, results in all_results.items():
        c = CONFIGS[cfg_key]
        baseline = next((r for s, r in results if s.key == "baseline"), None)
        worst_dd = max((r["dd_pct"] for s, r in results if "error" not in r),
                       default=0)
        worst_wd = min((r["worst_day_pct"] for s, r in results if "error" not in r),
                       default=0)
        n_pass = sum(1 for s, r in results
                     if "error" not in r and r["dd_pct"] <= 4.0
                     and r["worst_day_pct"] >= -4.0
                     and (r["ret_pct"] > 0 or s.severity in ("V-", "X")))
        n_warn = sum(1 for s, r in results
                     if "error" not in r and r["dd_pct"] > 4.0 and r["dd_pct"] <= 8.0)
        n_fail = sum(1 for s, r in results
                     if "error" not in r and (r["dd_pct"] > 8.0
                                              or r["worst_day_pct"] < -5.0))
        total_halts = sum(r["daily_halts"] for s, r in results
                          if "error" not in r)
        total_breakers = sum(r["breaker_trips"] for s, r in results
                             if "error" not in r)

        summaries[cfg_key] = dict(
            base_risk=c.base_risk_pct, dd_cap=c.dd_cap_pct,
            baseline_pnl=baseline["net"] if baseline else 0,
            baseline_dd=baseline["dd_pct"] if baseline else 0,
            baseline_worst_day=baseline["worst_day_pct"] if baseline else 0,
            worst_scenario_dd=worst_dd, worst_scenario_worst_day=worst_wd,
            n_pass=n_pass, n_warn=n_warn, n_fail=n_fail,
            total_halts_3mo=total_halts, total_breakers_3mo=total_breakers,
        )

        p(f"\n  {cfg_key}  (base={c.base_risk_pct*100:.3f}%, dd_cap={c.dd_cap_pct*100:.1f}%)")
        if baseline:
            p(f"    Baseline (real data) :  PnL=${baseline['net']:+,.0f}  "
              f"DD={baseline['dd_pct']:.2f}%  WorstDay={baseline['worst_day_pct']:+.2f}%  "
              f"halts={baseline['daily_halts']}  breakers={baseline['breaker_trips']}")
        p(f"    Across all 14 scenarios:")
        p(f"      Worst DD seen      : {worst_dd:.2f}%")
        p(f"      Worst single day   : {worst_wd:+.2f}%")
        p(f"      PASS/WARN/FAIL     : {n_pass} / {n_warn} / {n_fail}")
        p(f"      Daily halts (sum)  : {total_halts}")
        p(f"      Breaker trips (sum): {total_breakers}")

    # ========================================================================
    # THE VERDICT
    # ========================================================================
    p("")
    p("=" * 120)
    p("  VERDICT — does loosening dd_cap unlock 0.180 % profit?")
    p("=" * 120)

    a = summaries["A_CONTROL_165_dd4"]
    b = summaries["B_PRIOR_180_dd4"]
    c = summaries["C_LUKE_180_dd5"]
    d = summaries["D_ULTRA_180_dd6"]

    p(f"\n  A. CURRENT v25 (0.165 %, dd_cap=4 %)")
    p(f"        baseline PnL=${a['baseline_pnl']:+,.0f}  worst-DD={a['worst_scenario_dd']:.2f}%  "
      f"PASS={a['n_pass']}/14")
    p(f"\n  B. PRIOR FAIL (0.180 %, dd_cap=4 %)")
    p(f"        baseline PnL=${b['baseline_pnl']:+,.0f}  worst-DD={b['worst_scenario_dd']:.2f}%  "
      f"PASS={b['n_pass']}/14")
    p(f"        delta vs A: {b['baseline_pnl']-a['baseline_pnl']:+,.0f} (the sizer self-throttle)")
    p(f"\n  C. LUKE'S PROPOSAL (0.180 %, dd_cap=5 %)")
    p(f"        baseline PnL=${c['baseline_pnl']:+,.0f}  worst-DD={c['worst_scenario_dd']:.2f}%  "
      f"PASS={c['n_pass']}/14")
    p(f"        delta vs A: {c['baseline_pnl']-a['baseline_pnl']:+,.0f}")
    p(f"        delta vs B: {c['baseline_pnl']-b['baseline_pnl']:+,.0f}  ←  did loosening help?")
    p(f"\n  D. EXTRA LOOSE (0.180 %, dd_cap=6 %)")
    p(f"        baseline PnL=${d['baseline_pnl']:+,.0f}  worst-DD={d['worst_scenario_dd']:.2f}%  "
      f"PASS={d['n_pass']}/14")
    p(f"        delta vs C: {d['baseline_pnl']-c['baseline_pnl']:+,.0f}")

    p("")
    p("  KEY SAFETY CHECK (Luke's argument:")
    p("     'my 4 % daily halt is the ultimate safety net'):")
    for k in CONFIGS:
        s = summaries[k]
        wd = s["worst_scenario_worst_day"]
        ok_daily = wd > -4.0
        ok_5ers  = wd > -5.0
        sym_d = "✅" if ok_daily else "❌"
        sym_5 = "✅" if ok_5ers  else "❌"
        p(f"    {k:<22}  WorstDay={wd:+.2f}%  "
          f"vs Luke's 4% halt: {sym_d}   "
          f"vs 5ers 5%: {sym_5}   "
          f"halts/3mo={s['total_halts_3mo']}")

    p("")
    p("=" * 120)
    p("  RECOMMENDATION (auto-derived from numbers above)")
    p("=" * 120)
    best = max(summaries.values(), key=lambda x: x["baseline_pnl"])
    best_key = next(k for k, v in summaries.items() if v is best)
    safe_keys = [k for k, v in summaries.items()
                 if v["worst_scenario_worst_day"] > -4.0
                 and v["worst_scenario_dd"] <= 6.0
                 and v["n_fail"] == 0]
    if safe_keys:
        best_safe_key = max(safe_keys, key=lambda k: summaries[k]["baseline_pnl"])
        bs = summaries[best_safe_key]
        p(f"\n  Best CONFIG that satisfies all safety rules: {best_safe_key}")
        p(f"    base_risk={bs['base_risk']*100:.3f}%  dd_cap={bs['dd_cap']*100:.1f}%")
        p(f"    baseline PnL=${bs['baseline_pnl']:+,.0f}  "
          f"worst-day={bs['worst_scenario_worst_day']:+.2f}%  "
          f"max-DD={bs['worst_scenario_dd']:.2f}%")
        p(f"    PASS={bs['n_pass']}/14  WARN={bs['n_warn']}  FAIL={bs['n_fail']}")
    else:
        p(f"\n  ❌ No config satisfies all safety rules.")

    # Save
    out_dir = ROOT / "Results"; out_dir.mkdir(exist_ok=True)
    with open(out_dir / "stress_test_v25_180bps_loosened.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    out_json = {
        "configs": {k: asdict(v) for k, v in CONFIGS.items()},
        "daily_halt_pct": DAILY_HALT_PCT,
        "dd_breaker_pct": DD_BREAKER_PCT,
        "nochase_cooldown_s": NOCHASE_COOLDOWN_S,
        "summaries": summaries,
        "all_results": {k: [(s.key, r) for s, r in v]
                        for k, v in all_results.items()},
    }
    with open(out_dir / "stress_test_v25_180bps_loosened.json", "w") as f:
        json.dump(out_json, f, indent=2, default=str)
    p(f"\n  Saved: Results/stress_test_v25_180bps_loosened.{{txt,json}}")


if __name__ == "__main__":
    main()
