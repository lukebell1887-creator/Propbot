#!/usr/bin/env python3
"""
stress_test_v25_nochase.py — Stress test the AGGRESSIVE v25 candidate:
    base_risk = 0.165 % (vs v24's 0.110 %)  +  no-chase 300 s filter
against all 14 market-regime scenarios.

This is the config that produced $27,023 / 3.09 % DD / PF 1.88 on baseline.
The question: does it hold up under flash-crash / vol-explosion / bear-trend /
GARCH-cluster / trend-inversion regimes?

Output:
  Results/stress_test_v25_nochase.txt
  Results/stress_test_v25_nochase.json
"""
from __future__ import annotations

import json, sys, time
from pathlib import Path
from dataclasses import asdict

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

# =============================================================================
#  V25 AGGRESSIVE CONFIG
# =============================================================================
V25_SIZER_CFG = MertonGZSizerConfig(
    base_risk_pct=0.00165,   # <<< +50 % vs v24's 0.110 %
    cap_mult=5.0,            # per-trade cap still 0.825 % (5× base)
    gamma=3.0,
    ewma_alpha=0.20,
    warmup_trades=15,
    dd_cap_pct=0.04,
    pool_symbols=True,
    no_edge_multiplier=1.0,
)
NOCHASE_COOLDOWN_S = 300.0     # <<< +5 min post-close block


# =============================================================================
#  Monkey-patch the data loader so scenarios warp the bar stream
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
#  Per-scenario run
# =============================================================================
def run_one_scenario(sc_key: str, *, symbols=None) -> dict:
    global _CURRENT_SCENARIO, _WARPED_STREAMS
    _CURRENT_SCENARIO = sc_key
    _WARPED_STREAMS = {}

    symbols = symbols or SYMS

    t0 = time.time()
    raw_trades, wmin, wmax = btv22.run_portfolio(symbols, V25_SIZER_CFG)

    # News rails
    events = load_news_events(NEWS_CSV)
    pl = build_price_lookup(_WARPED_STREAMS)
    raw_trades, _ = apply_news_entry_block(raw_trades, events, buffer_min=15)
    raw_trades, _ = apply_news_flatten(raw_trades, events, pl, minutes_before=2)

    # Safety rails + slippage
    trades = apply_full_safety_rails(raw_trades, slippage_ticks=1.0)

    # === NEW: NO-CHASE FILTER ===============================================
    n_before_nochase = len(trades)
    trades, chase_report = apply_no_chase(trades, cooldown_s=NOCHASE_COOLDOWN_S)
    n_dropped_nochase = n_before_nochase - len(trades)

    # DD circuit breaker
    n_before_breaker = len(trades)
    trades, breaker_state = apply_dd_breaker(trades, starting_balance=BALANCE,
                                             halt_pct=0.04)
    n_dropped_breaker = n_before_breaker - len(trades)

    # Metrics
    s = stats(trades)
    pnls = np.array([t.net_pnl for t in trades], dtype=float)
    worst_pnl, worst_pct, worst_dd, n_days = worst_single_day(trades)
    ruins = ruin_probs(pnls) if len(pnls) > 0 else {"ruin@3": 0, "ruin@4": 0, "ruin@5": 0}

    per_sym: dict = {}
    for sym in symbols:
        sym_trades = [t for t in trades if t.symbol == sym]
        ss = stats(sym_trades)
        per_sym[sym] = {
            "n": ss["n"], "net": ss["net"], "ret_pct": ss["ret_pct"],
            "dd_pct": ss["dd_pct"], "pf": ss["pf"], "wr": ss["wr"],
            "sharpe": ss["sharpe"],
        }

    return dict(
        scenario=sc_key,
        window=[str(wmin), str(wmax)],
        elapsed_s=round(time.time() - t0, 1),
        n=s["n"], net=s["net"], ret_pct=s["ret_pct"],
        dd_pct=s["dd_pct"], pf=s["pf"], sharpe=s["sharpe"], wr=s["wr"],
        n_days=n_days,
        worst_day_pnl=worst_pnl, worst_day_pct=worst_pct,
        worst_daily_dd_pct=worst_dd,
        chases_dropped=chase_report["chases_dropped"],
        breaker_trips=breaker_state.total_halts,
        breaker_dropped=n_dropped_breaker,
        breaker_max_dd_seen_pct=round(breaker_state.max_dd_pct_seen * 100, 3),
        **{k: v for k, v in ruins.items()},
        per_symbol=per_sym,
    )


# =============================================================================
#  Pretty-printer
# =============================================================================
def sev_icon(sev: str) -> str:
    return {"V+": "🟢", "+": "🟢", "N": "⚪", "-": "🟠",
            "V-": "🔴", "X":  "☠️ "}.get(sev, "  ")


def fmt_scen_line(r: dict, sc) -> str:
    dd = r["dd_pct"]; ret = r["ret_pct"]
    wd = r["worst_day_pct"]
    ok = (dd <= 4.0 and ret > 0 and wd > -4.0)
    verdict = "PASS" if ok else ("WARN" if dd <= 8.0 else "FAIL")
    vcolor = {"PASS": "✅", "WARN": "⚠️ ", "FAIL": "❌"}[verdict]
    return (f"  {sev_icon(sc.severity)} {sc.label:<36} "
            f"N={r['n']:>4} "
            f"PnL=${r['net']:>+9,.0f} "
            f"Ret={ret:>+6.2f}% "
            f"DD={dd:>5.2f}% "
            f"WorstDay={wd:>+6.2f}% "
            f"PF={r['pf']:>4.2f} "
            f"WR={r['wr']*100:>4.1f}% "
            f"nochase={r['chases_dropped']:>2} "
            f"{vcolor} {verdict}")


def fmt_per_symbol(r: dict) -> str:
    lines = []
    for sym, ss in r["per_symbol"].items():
        lines.append(f"      {sym:<7} N={ss['n']:>4}  "
                     f"PnL=${ss['net']:>+9,.0f}  "
                     f"DD={ss['dd_pct']:>5.2f}%  "
                     f"PF={ss['pf']:>4.2f}  "
                     f"WR={ss['wr']*100:>4.1f}%")
    return "\n".join(lines)


# =============================================================================
#  Main
# =============================================================================
def main():
    lines = []
    p = lambda m="": (print(m), lines.append(m))

    p("=" * 120)
    p("  V25 STRESS TEST — AGGRESSIVE no-chase config")
    p("  Bot: v23 ORB + news rails + Merton-GZ (base=0.165%, cap=5x, γ=3.0, DD_cap=4%)")
    p("       + no-chase filter (300s) + v25 DD-breaker")
    p("  Data: real 3-month 5ers M1 (DE40, US30, XAUUSD, US500) with 14 scenario warps")
    p("  Rails: all live safety rails + 1.0-tick slippage haircut")
    p("=" * 120)
    p("")
    p(f"  Running {len(SCENARIOS)} scenarios × {len(SYMS)} symbols ...")
    p("")

    results = []
    for sc in SCENARIOS:
        print(f"    [{sc.severity:>2}] {sc.key:<16} — {sc.label}", flush=True)
        try:
            r = run_one_scenario(sc.key)
        except Exception as e:
            print(f"      !! FAILED: {type(e).__name__}: {e}", flush=True)
            r = {"scenario": sc.key, "error": str(e), "n": 0,
                 "net": 0, "ret_pct": 0, "dd_pct": 0, "pf": 0, "sharpe": 0,
                 "wr": 0, "n_days": 0, "worst_day_pnl": 0, "worst_day_pct": 0,
                 "worst_daily_dd_pct": 0, "chases_dropped": 0, "per_symbol": {}}
        results.append((sc, r))

    # Headline
    p("")
    p("=" * 120)
    p("  HEADLINE TABLE")
    p("=" * 120)
    p(f"  {'Legend':<14}  🟢 good   ⚪ baseline   🟠 stress   🔴 severe   ☠️  catastrophe")
    p("")
    for sc, r in results:
        if "error" in r:
            p(f"  {sev_icon(sc.severity)} {sc.label:<36} ❌ ERROR: {r['error']}")
        else:
            p(fmt_scen_line(r, sc))
    p("")

    # Per-symbol
    p("=" * 120)
    p("  PER-SYMBOL BREAKDOWN")
    p("=" * 120)
    for sc, r in results:
        if "error" in r: continue
        p(f"\n  [{sc.severity}] {sc.label}")
        p(f"      ({sc.description})")
        p(fmt_per_symbol(r))

    # Exit-gate
    p("")
    p("=" * 120)
    p("  EXIT-GATE ANALYSIS — survives the 5ers challenge?")
    p("  Rule: DD ≤ 4 %  AND  WorstDay ≥ -4 %  AND  (ret > 0  OR  catastrophic)")
    p("=" * 120)

    survives, warnings, fails = [], [], []
    for sc, r in results:
        if "error" in r:
            fails.append((sc, r, "ERROR")); continue
        dd = r["dd_pct"]; wd = r["worst_day_pct"]; ret = r["ret_pct"]
        is_cat = sc.severity in ("V-", "X")
        pnl_ok = ret > 0 or is_cat
        if dd <= 4.0 and wd >= -4.0 and pnl_ok:
            survives.append((sc, r))
        elif dd <= 8.0 and wd >= -8.0:
            warnings.append((sc, r))
        else:
            fails.append((sc, r, "FAIL"))

    p(f"\n  ✅ SURVIVED ({len(survives)}/{len(SCENARIOS)}):")
    for sc, r in survives:
        p(f"      {sc.key:<16}  DD={r['dd_pct']:.2f}%  WorstDay={r['worst_day_pct']:+.2f}%  "
          f"Ret={r['ret_pct']:+.2f}%  nochase={r['chases_dropped']}")

    if warnings:
        p(f"\n  ⚠️  WARNINGS ({len(warnings)}):")
        for sc, r in warnings:
            p(f"      {sc.key:<16}  DD={r['dd_pct']:.2f}%  WorstDay={r['worst_day_pct']:+.2f}%  "
              f"Ret={r['ret_pct']:+.2f}%")

    if fails:
        p(f"\n  ❌ FAILED ({len(fails)}):")
        for item in fails:
            if len(item) == 3:
                sc, r, tag = item
                p(f"      {sc.key:<16}  {tag}")
            else:
                sc, r = item
                p(f"      {sc.key:<16}  DD={r['dd_pct']:.2f}%  WorstDay={r['worst_day_pct']:+.2f}%")

    # Summary
    p("")
    p("=" * 120)
    p("  SUMMARY")
    p("=" * 120)
    p(f"    Passed    : {len(survives):>2} / {len(SCENARIOS)}")
    p(f"    Warnings  : {len(warnings):>2} / {len(SCENARIOS)}")
    p(f"    Failed    : {len(fails):>2} / {len(SCENARIOS)}")
    pct = 100.0 * len(survives) / max(1, len(SCENARIOS))
    p(f"    Survival rate = {pct:.1f} %")
    bl = next((r for sc, r in results if sc.key == "baseline" and "error" not in r), None)
    if bl:
        p(f"\n    BASELINE : PnL=${bl['net']:+,.0f}  DD={bl['dd_pct']:.2f}%  "
          f"WR={bl['wr']*100:.1f}%  PF={bl['pf']:.2f}")
        p(f"    (v24 baseline was $+23,311 / 2.06 % DD — we should be HIGHER PnL, "
          f"similar/lower DD)")
    p("=" * 120)

    # Save
    out_dir = ROOT / "Results"; out_dir.mkdir(exist_ok=True)
    with open(out_dir / "stress_test_v25_nochase.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    json_data = {
        "bot": "v25 = v23 signal + no-chase(300s) + Merton-GZ(0.165%, cap=5x, γ=3.0, DD_cap=4%)",
        "sizer_cfg": asdict(V25_SIZER_CFG),
        "nochase_cooldown_s": NOCHASE_COOLDOWN_S,
        "symbols": SYMS,
        "scenarios": [
            {"key": sc.key, "label": sc.label, "severity": sc.severity,
             "description": sc.description, **r}
            for sc, r in results
        ],
        "summary": {
            "total": len(SCENARIOS),
            "passed": len(survives),
            "warnings": len(warnings),
            "failed": len(fails),
            "survival_rate_pct": round(pct, 1),
        },
    }
    with open(out_dir / "stress_test_v25_nochase.json", "w") as f:
        json.dump(json_data, f, indent=2, default=str)
    p(f"\n  Saved: Results/stress_test_v25_nochase.txt + .json")


if __name__ == "__main__":
    main()
