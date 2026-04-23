#!/usr/bin/env python3
"""
stress_test_v24_scenarios.py — PhD-grade multi-regime stress test.

Runs the EXACT live bot pipeline (v23 signal + news rails + Merton-GZ @ v24d
sweet-spot: base=0.110%, cap=5x, γ=3.0, dd_cap=4%) against 14 different market
regimes on the real 3-month 5ers data. Each scenario warps the price path
while preserving OHLC integrity; the bot sees the warped bars just like it
would see live bars.

Output:
  Results/stress_test_v24.txt   — big console-style summary table
  Results/stress_test_v24.json  — full per-scenario / per-symbol metrics
"""
from __future__ import annotations

import json, sys, time
from pathlib import Path
from copy import deepcopy
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
from src.dynamic_sizer_v21 import MertonGZSizerConfig

# --- the new stress-test library ---------------------------------------------
from src.stress import SCENARIOS, apply_scenario

# --- V25 hard 4 % DD circuit breaker -----------------------------------------
from src.dd_breaker import apply_dd_breaker

NEWS_CSV = ROOT / "data" / "news" / "tier1_2026.csv"


# =============================================================================
#  V24 LOCKED SIZER CONFIG  (from Docs/V24_FINAL_LOCKED.md)
# =============================================================================
V24_SIZER_CFG = MertonGZSizerConfig(
    base_risk_pct=0.00110,   # 0.110 % Merton unit
    cap_mult=5.0,            # hard per-trade cap = 0.550 %
    gamma=3.0,               # v24 shootout winner
    ewma_alpha=0.20,
    warmup_trades=15,
    dd_cap_pct=0.04,         # Grossman-Zhou absorbing barrier at 4 %
    pool_symbols=True,
    no_edge_multiplier=1.0,
)


# =============================================================================
#  Monkey-patch the data loader: intercept every symbol's bar stream and
#  apply the chosen scenario before the engine sees it.
# =============================================================================
_ORIG_LOAD_M1 = btv22.load_m1
_CURRENT_SCENARIO: str = "baseline"
_WARPED_STREAMS: dict = {}   # sym → warped bar list (for price lookup later)


def _patched_load_m1(path, tmin, tmax):
    """Drop-in replacement for btv22.load_m1 that warps every stream by
    the currently-active scenario. Symbol inferred from file stem."""
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
    """Run the full pipeline under scenario `sc_key` and return metrics."""
    global _CURRENT_SCENARIO, _WARPED_STREAMS
    _CURRENT_SCENARIO = sc_key
    _WARPED_STREAMS = {}

    symbols = symbols or SYMS    # 4-pair portfolio by default

    t0 = time.time()
    raw_trades, wmin, wmax = btv22.run_portfolio(symbols, V24_SIZER_CFG)

    # ----- News rails (v23 add-ons) --------------------------------------
    events = load_news_events(NEWS_CSV)
    # Build a price lookup from the warped streams we captured above
    # (build_price_lookup expects dict[sym] -> list[(t, o, h, l, c)] which
    # is exactly what _WARPED_STREAMS contains)
    pl = build_price_lookup(_WARPED_STREAMS)
    raw_trades, _ = apply_news_entry_block(raw_trades, events, buffer_min=15)
    raw_trades, _ = apply_news_flatten(raw_trades, events, pl, minutes_before=2)

    # ----- Full safety rails (broker realism) ----------------------------
    trades = apply_full_safety_rails(raw_trades, slippage_ticks=1.0)

    # ----- HARD 4 % TOTAL-DD CIRCUIT BREAKER (v25) -----------------------
    # Close-all-at-4%-DD: in LIVE, the bot flattens all positions the instant
    # account DD reaches 4 %. Here we simulate the "block new trades" side of
    # that; already-running trades still close at their original SL/TP (live
    # will be STRICTER because it flattens them immediately).
    n_before_breaker = len(trades)
    trades, breaker_state = apply_dd_breaker(trades, starting_balance=BALANCE,
                                             halt_pct=0.04)
    n_dropped_by_breaker = n_before_breaker - len(trades)

    # ----- Portfolio metrics ---------------------------------------------
    s = stats(trades)
    pnls = np.array([t.net_pnl for t in trades], dtype=float)
    worst_pnl, worst_pct, worst_dd, n_days = worst_single_day(trades)
    ruins = ruin_probs(pnls) if len(pnls) > 0 else {"ruin@3": 0, "ruin@4": 0, "ruin@5": 0}

    # ----- Per-symbol breakdown ------------------------------------------
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
        # v25 DD-breaker telemetry
        breaker_trips=breaker_state.total_halts,
        breaker_dropped=n_dropped_by_breaker,
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
    # flags
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

    p("=" * 110)
    p("  V24 STRESS TEST — Multi-Regime PhD Suite")
    p("  Bot: v23 ORB + news rails + Merton-GZ (base=0.110%, cap=5x, γ=3.0, DD_cap=4%)")
    p("  Data: real 3-month 5ers M1 (DE40, US30, XAUUSD, US500) with price-path warps")
    p("  Rails: all live safety rails + 1.0-tick slippage haircut")
    p("=" * 110)
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
                 "worst_daily_dd_pct": 0, "per_symbol": {}}
        results.append((sc, r))

    # ------------------------------------------------------------------
    p("")
    p("=" * 110)
    p("  HEADLINE TABLE — sorted by severity (positive → catastrophic)")
    p("=" * 110)
    p(f"  {'Legend':<14}  🟢 good regime   ⚪ baseline   🟠 stress   🔴 severe stress   ☠️  catastrophe")
    p("")
    header = (f"  {'':<2}{'Scenario':<38} {'N':>5} "
              f"{'PnL':>11} {'Ret%':>7} {'DD%':>6} {'WorstDay%':>10} "
              f"{'PF':>5} {'WR':>6}   Verdict")
    p(header)
    p("  " + "-" * 106)
    for sc, r in results:
        if "error" in r:
            p(f"  {sev_icon(sc.severity)} {sc.label:<36} ❌ ERROR: {r['error']}")
        else:
            p(fmt_scen_line(r, sc))
    p("")

    # ------------------------------------------------------------------
    p("=" * 110)
    p("  PER-SYMBOL BREAKDOWN (all scenarios)")
    p("=" * 110)
    for sc, r in results:
        if "error" in r:
            continue
        p(f"\n  [{sc.severity}] {sc.label}")
        p(f"      ({sc.description})")
        p(fmt_per_symbol(r))

    # ------------------------------------------------------------------
    #  Exit-gate analysis
    # ------------------------------------------------------------------
    p("")
    p("=" * 110)
    p("  EXIT-GATE ANALYSIS — '5ers challenge survives this regime?'")
    p("  Rule: DD ≤ 4 %  AND  WorstDay ≥ -4 %  AND  (ret > 0  OR  scenario is catastrophic)")
    p("=" * 110)

    survives, warnings, fails = [], [], []
    for sc, r in results:
        if "error" in r:
            fails.append((sc, r, "ERROR"))
            continue
        dd = r["dd_pct"]; wd = r["worst_day_pct"]; ret = r["ret_pct"]
        is_catastrophic = sc.severity in ("V-", "X")
        pnl_ok = ret > 0 or is_catastrophic
        if dd <= 4.0 and wd >= -4.0 and pnl_ok:
            survives.append((sc, r))
        elif dd <= 8.0 and wd >= -8.0:
            warnings.append((sc, r))
        else:
            fails.append((sc, r, "FAIL"))

    p(f"\n  ✅ SURVIVED ({len(survives)}/{len(SCENARIOS)}):")
    for sc, r in survives:
        p(f"      {sc.key:<16}  DD={r['dd_pct']:.2f}%  WorstDay={r['worst_day_pct']:+.2f}%  "
          f"Ret={r['ret_pct']:+.2f}%")

    if warnings:
        p(f"\n  ⚠️  WARNINGS ({len(warnings)}):  (DD 4-8 % or WorstDay -4 to -8 %)")
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

    # ------------------------------------------------------------------
    p("")
    p("=" * 110)
    p("  SUMMARY")
    p("=" * 110)
    p(f"    Passed    : {len(survives):>2} / {len(SCENARIOS)}")
    p(f"    Warnings  : {len(warnings):>2} / {len(SCENARIOS)}  (survived but DD > 4 %)")
    p(f"    Failed    : {len(fails):>2} / {len(SCENARIOS)}")
    pct = 100.0 * len(survives) / max(1, len(SCENARIOS))
    p(f"    Survival rate = {pct:.1f} %")
    # baseline reference
    bl = next((r for sc, r in results if sc.key == "baseline" and "error" not in r), None)
    if bl:
        p(f"\n    BASELINE (sanity-check) : PnL=${bl['net']:+,.0f}  "
          f"DD={bl['dd_pct']:.2f}%  WR={bl['wr']*100:.1f}%  "
          f"(expected ~$23,311 / 2.06 % DD)")
    p("=" * 110)

    # ------------------------------------------------------------------
    #  Save
    # ------------------------------------------------------------------
    out_dir = ROOT / "Results"; out_dir.mkdir(exist_ok=True)
    with open(out_dir / "stress_test_v24.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    json_data = {
        "bot": "v24 = v23 signal + Merton-GZ(0.110%, cap=5x, γ=3.0, DD_cap=4%)",
        "sizer_cfg": asdict(V24_SIZER_CFG),
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
    with open(out_dir / "stress_test_v24.json", "w") as f:
        json.dump(json_data, f, indent=2, default=str)
    p(f"\n  Saved: Results/stress_test_v24.txt + .json")


if __name__ == "__main__":
    main()
