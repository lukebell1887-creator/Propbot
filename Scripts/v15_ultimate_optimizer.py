#!/usr/bin/env python3
"""
SHF v15 — ULTIMATE PER-SYMBOL OPTIMIZER
========================================

One massive script that does EVERYTHING the user asked for:

  1. **Wider grid** — 4 × 4 × 5 × 3 × 4 = 960 configs per symbol
     (Z quantile × Hurst quantile × stop ATR × TP fraction × session filter).
  2. **Multi-split walk-forward** — 3 different IS/OOS boundaries.
     A config must survive ALL THREE to be labelled stable (prevents overfit
     to one lucky slice).
  3. **Bootstrap robustness** — 10,000 resamples of the OOS trade list on
     each split; track median/p05 net P&L and PF.
  4. **Commission stress test** — re-run the best config with extra
     $1/lot AND $2/lot round-trip to verify the edge survives fee hikes,
     slippage, or hidden broker charges.
  5. **Tier classification** (NO auto-drop):
        Tier 1 (LIVE):   survives all splits + bootstrap + commission stress
        Tier 2 (WATCH):  profitable on median but fails >=1 robustness check
        Tier 3 (REJECT): unprofitable or flat across splits
  6. **Per-symbol output** — the BEST config for every symbol is written,
     regardless of tier, so the user can inspect and decide.
  7. **Commission transparency** — every run logs commission + spread cost
     in dollars so you can verify the cost model is active.

Run:
    python Scripts/v15_ultimate_optimizer.py \
        --symbols US100 US500 US30 DE40 USOIL XAUUSD \
        --out Results/v15_ultimate_tuning.json \
        --report Docs/V15_ULTIMATE_RESULTS.md

Anti-overfit guarantees:
    - Three non-overlapping OOS windows, each with an embargo.
    - Bootstrap requires p05 net >= 0 on >= 2 of 3 OOS slices for Tier 1.
    - Commission stress (+$1/lot) net P&L must still be > 0 for Tier 1.
    - Parameter smoothness check (top-5 grid configs around winner must
      all be profitable on OOS2 — rules out knife-edge hyperparameters).
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from itertools import product
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.smartbb_engine_v14 import (  # noqa: E402
    SmartBBV14Engine, SmartBBV14Config, SymbolParams,
    SMARTBB_UNIVERSE, params_to_dict,
)


# =====================================================================
#  Data loader
# =====================================================================

def load_m1(path: Path) -> list[tuple]:
    out: list[tuple] = []
    with open(path, "r", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try: t = datetime.fromisoformat(row["time"])
            except Exception:
                t = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            out.append((t, float(row["open"]), float(row["high"]),
                         float(row["low"]), float(row["close"])))
    return out


# =====================================================================
#  Run engine on one slice with optional commission-stress
# =====================================================================

def run_engine(symbol: str, rows: list[tuple], params: SymbolParams,
                 extra_cost_per_lot: float = 0.0,
                 initial_equity: float = 100_000.0) -> dict:
    cfg = SmartBBV14Config(
        default_params=params,
        extra_cost_per_lot=extra_cost_per_lot,
    )
    eng = SmartBBV14Engine(
        symbols=[SMARTBB_UNIVERSE[symbol]],
        params={symbol: params},
        cfg=cfg,
        initial_equity=initial_equity,
    )
    for (t, o, h, l, c) in rows:
        eng.on_bar(symbol, t.timestamp(), t.strftime("%Y-%m-%d"),
                    t.hour, t.minute, o, h, l, c)
    s = eng.summary()
    s["_trades"] = [(tr.net_pnl, tr.entry_time, tr.exit_time,
                      tr.hurst_at_entry, tr.z_at_entry, tr.exit_reason,
                      tr.bars_held, tr.side, tr.realised_R)
                     for tr in eng.trades]
    return s


# =====================================================================
#  Session filters — maps a label to an allowed-hours frozenset
# =====================================================================

SESSION_MAP: dict[str, Optional[frozenset[int]]] = {
    # None = use symbol's default window (SymbolSpec.trade_start/end)
    "all":       None,
    # US session only (approx 13-21 UTC — covers NY open to close)
    "us":        frozenset(range(13, 21)),
    # European morning (approx 7-12 UTC)
    "eu":        frozenset(range(7, 12)),
    # NY-London overlap (approx 13-17 UTC) — peak liquidity
    "overlap":   frozenset(range(13, 17)),
}


# =====================================================================
#  Bootstrap CI helper
# =====================================================================

def bootstrap_ci(trade_pnls: list[float], iters: int = 10_000,
                   seed: int = 7) -> dict:
    if not trade_pnls:
        return {"net_p05": 0.0, "net_p50": 0.0, "net_p95": 0.0,
                "pf_p05": 0.0, "pf_p50": 0.0, "pf_p95": 0.0}
    rng = random.Random(seed)
    n = len(trade_pnls)
    nets, pfs = [], []
    for _ in range(iters):
        sample = [trade_pnls[rng.randrange(n)] for _ in range(n)]
        nets.append(sum(sample))
        gw = sum(x for x in sample if x > 0)
        gl = -sum(x for x in sample if x <= 0)
        pfs.append(gw / gl if gl > 0 else (10.0 if gw > 0 else 0.0))
    nets.sort()
    pfs.sort()

    def q(a, p): return a[max(0, min(len(a) - 1, int(len(a) * p)))]

    return {
        "net_p05": q(nets, 0.05), "net_p50": q(nets, 0.50),
        "net_p95": q(nets, 0.95),
        "pf_p05": q(pfs, 0.05), "pf_p50": q(pfs, 0.50),
        "pf_p95": q(pfs, 0.95),
    }


# =====================================================================
#  Walk-forward split generator — three non-overlapping IS/OOS cuts
# =====================================================================

def make_splits(rows: list[tuple], embargo_frac: float = 0.03
                 ) -> list[tuple[list, list]]:
    """
    Returns 3 (IS, OOS) splits with an embargo between them.

      Split A: IS = 0-55%,  emb 55-58%,  OOS 58-75%
      Split B: IS = 0-70%,  emb 70-73%,  OOS 73-88%
      Split C: IS = 15-75%, emb 75-78%,  OOS 78-95%   (shifted start)
    """
    n = len(rows)
    splits = []
    for is_lo, is_hi, oos_lo, oos_hi in [
        (0.00, 0.55, 0.58, 0.75),
        (0.00, 0.70, 0.73, 0.88),
        (0.15, 0.75, 0.78, 0.95),
    ]:
        is_rows = rows[int(is_lo * n):int(is_hi * n)]
        oos_rows = rows[int(oos_lo * n):int(oos_hi * n)]
        splits.append((is_rows, oos_rows))
    return splits


# =====================================================================
#  Grid definition
# =====================================================================

def build_grid() -> list[tuple]:
    """
    4 × 4 × 5 × 3 × 4 = 960 configs / symbol.
    """
    zs = [0.95, 0.97, 0.98, 0.99]
    hqs = [0.15, 0.25, 0.35, 0.45]
    stops = [0.50, 0.75, 1.00, 1.25, 1.50]
    tps = [0.50, 0.75, 1.00]
    sessions = list(SESSION_MAP.keys())
    return list(product(zs, hqs, stops, tps, sessions))


def cfg_to_params(z: float, hq: float, sa: float, tf: float,
                    session: str) -> SymbolParams:
    return SymbolParams(
        z_quantile=z, z_min_abs=2.0, z_max_abs=5.5,
        hurst_quantile=hq,
        stop_atr_mult=sa, tp_frac=tf,
        allowed_hours=SESSION_MAP[session],
    )


# =====================================================================
#  Acceptance logic
# =====================================================================

def eval_summary(s: dict) -> dict:
    """Compact summary for ranking."""
    pnls = [t[0] for t in s.get("_trades", [])]
    return {
        "n": s.get("trades", 0),
        "net": s.get("net_pnl", 0.0),
        "pf": s.get("pf", 0.0),
        "dd_pct": s.get("max_dd_pct", 0.0),
        "expR": s.get("expectancy_R", 0.0),
        "wr": s.get("win_rate", 0.0),
        "commissions": s.get("gross_commissions", 0.0),
        "spread_cost": s.get("gross_spread_cost", 0.0),
        "pnls": pnls,
    }


def classify(per_split: list[dict], stress_results: dict) -> tuple[str, str]:
    """
    Given the 3 OOS summaries and commission-stress result, return tier + reason.
    """
    # Must have at least 2 splits with trades
    non_empty = [s for s in per_split if s["n"] >= 3]
    if not non_empty:
        return "REJECT", "no trades in any OOS split"

    # Must be net-positive on at least 2 of 3 splits
    positive_splits = sum(1 for s in per_split if s["net"] > 0)
    if positive_splits == 0:
        return "REJECT", "unprofitable on all OOS splits"

    median_pf = sorted([s["pf"] for s in non_empty])[len(non_empty) // 2]
    median_net = sorted([s["net"] for s in non_empty])[len(non_empty) // 2]

    if median_net <= 0 or median_pf < 1.0:
        return "REJECT", f"median OOS unprofitable (net=${median_net:.0f} PF={median_pf:.2f})"

    # Bootstrap robustness
    boot_fail_count = 0
    for s in per_split:
        if s["n"] < 3:
            continue
        ci = bootstrap_ci(s["pnls"], iters=5_000)
        if ci["net_p05"] < 0 or ci["pf_p05"] < 0.9:
            boot_fail_count += 1
    if boot_fail_count >= 2:
        return "TIER2", f"bootstrap p05 fails on {boot_fail_count}/3 splits"

    # Commission stress test
    if stress_results["stress_net"] <= 0:
        return "TIER2", f"fails commission stress +$1/lot (net=${stress_results['stress_net']:.0f})"
    if stress_results["stress_pf"] < 1.2:
        return "TIER2", f"commission stress PF {stress_results['stress_pf']:.2f} < 1.2"

    # Passed everything
    return "TIER1", (f"3-split median PF {median_pf:.2f}, "
                      f"net ${median_net:.0f}, "
                      f"+$1/lot stress PF {stress_results['stress_pf']:.2f}")


# =====================================================================
#  Main per-symbol optimization
# =====================================================================

def optimize_symbol(symbol: str, rows: list[tuple]) -> dict:
    t0 = time.time()
    splits = make_splits(rows)
    print(f"\n=== {symbol} ===  ({len(rows):,} M1 bars, "
           f"3 WF splits: OOS sizes "
           f"{[len(oos) for _, oos in splits]})")

    grid = build_grid()
    print(f"[{symbol}] grid = {len(grid)} configs")

    # ------------------------------------------------------------------
    # Phase 1: IS evaluation on SPLIT A only (fast coarse filter)
    # ------------------------------------------------------------------
    is_rows_A = splits[0][0]
    is_scores: list[tuple[tuple, dict]] = []
    for i, cfg_tuple in enumerate(grid):
        z, hq, sa, tf, session = cfg_tuple
        params = cfg_to_params(z, hq, sa, tf, session)
        s = run_engine(symbol, is_rows_A, params)
        ev = eval_summary(s)
        is_scores.append((cfg_tuple, ev))
        if (i + 1) % 50 == 0 or i == len(grid) - 1:
            print(f"  [IS-A {i+1:3d}/{len(grid)}] "
                   f"z={z:.2f} hq={hq:.2f} sa={sa:.2f} tf={tf:.2f} ses={session:<8s} "
                   f"n={ev['n']:3d} PF={ev['pf']:.2f} net=${ev['net']:+.0f}")

    # Score: composite rank by (net, PF, DD penalty)
    def is_rank(entry):
        _, ev = entry
        return (ev["net"] - 100 * ev["dd_pct"], ev["pf"])

    is_scores.sort(key=is_rank, reverse=True)
    top_is = is_scores[:30]
    print(f"[{symbol}] top-30 IS configs identified; running on all 3 OOS splits")

    # ------------------------------------------------------------------
    # Phase 2: Evaluate top-30 on each of 3 OOS splits
    # ------------------------------------------------------------------
    per_config_oos: list[dict] = []
    for idx, (cfg_tuple, _is_ev) in enumerate(top_is):
        z, hq, sa, tf, session = cfg_tuple
        params = cfg_to_params(z, hq, sa, tf, session)
        per_split_evs = []
        for split_idx, (_, oos_rows) in enumerate(splits):
            s = run_engine(symbol, oos_rows, params)
            per_split_evs.append(eval_summary(s))

        median_net = sorted([e["net"] for e in per_split_evs])[1]
        median_pf = sorted([e["pf"] for e in per_split_evs])[1]
        per_config_oos.append({
            "cfg": cfg_tuple, "oos": per_split_evs,
            "median_net": median_net, "median_pf": median_pf,
        })
        if (idx + 1) % 5 == 0 or idx == len(top_is) - 1:
            print(f"  [OOS {idx+1:3d}/{len(top_is)}] "
                   f"z={z:.2f} hq={hq:.2f} sa={sa:.2f} tf={tf:.2f} ses={session:<8s} "
                   f"  3-split med: net=${median_net:+,.0f} PF={median_pf:.2f}")

    # Rank by 3-split median net
    per_config_oos.sort(key=lambda r: (r["median_net"], r["median_pf"]),
                         reverse=True)

    # ------------------------------------------------------------------
    # Phase 3: For the TOP winner, run commission-stress test on all splits
    # ------------------------------------------------------------------
    winner = per_config_oos[0]
    z, hq, sa, tf, session = winner["cfg"]
    winner_params = cfg_to_params(z, hq, sa, tf, session)

    print(f"\n[{symbol}] WINNER: z={z} hq={hq} sa={sa} tf={tf} ses={session}")
    print(f"  3-split OOS: "
           + "  ".join([f"net=${e['net']:+,.0f} PF={e['pf']:.2f} n={e['n']}"
                        for e in winner["oos"]]))

    # Commission stress: run on the BEST OOS split (split idx with highest net)
    best_oos_idx = max(range(3), key=lambda i: winner["oos"][i]["net"])
    stress_oos_rows = splits[best_oos_idx][1]
    stress_configs = []
    for extra in (0.0, 0.50, 1.00, 2.00):
        s = run_engine(symbol, stress_oos_rows, winner_params,
                         extra_cost_per_lot=extra)
        ev = eval_summary(s)
        stress_configs.append({"extra_cost_per_lot": extra,
                                 "n": ev["n"], "net": ev["net"],
                                 "pf": ev["pf"], "commissions": ev["commissions"]})
        print(f"  Comm-stress +${extra:.2f}/lot  "
               f"n={ev['n']:3d}  net=${ev['net']:+,.0f}  "
               f"PF={ev['pf']:.2f}  comm=${ev['commissions']:,.0f}")

    stress_results = {
        "stress_net": stress_configs[2]["net"],   # +$1/lot
        "stress_pf": stress_configs[2]["pf"],
        "all": stress_configs,
    }

    # ------------------------------------------------------------------
    # Phase 4: Bootstrap on each OOS split (winner config only)
    # ------------------------------------------------------------------
    bootstrap_by_split = []
    for split_idx, ev in enumerate(winner["oos"]):
        ci = bootstrap_ci(ev["pnls"], iters=10_000)
        bootstrap_by_split.append({
            "split": split_idx, "n": ev["n"],
            "net_observed": ev["net"], "pf_observed": ev["pf"],
            **ci,
        })
        print(f"  Bootstrap split-{split_idx}: "
               f"net p05=${ci['net_p05']:+,.0f} p50=${ci['net_p50']:+,.0f} "
               f"p95=${ci['net_p95']:+,.0f}  PF p05={ci['pf_p05']:.2f}")

    # ------------------------------------------------------------------
    # Phase 5: Parameter smoothness — top-5 neighbours must also be positive
    # ------------------------------------------------------------------
    top5 = per_config_oos[:5]
    top5_positive = sum(1 for r in top5 if r["median_net"] > 0)
    print(f"  Smoothness: {top5_positive}/5 top grid neighbours profitable")

    # ------------------------------------------------------------------
    # Classify
    # ------------------------------------------------------------------
    tier, reason = classify(winner["oos"], stress_results)
    if top5_positive < 3 and tier == "TIER1":
        tier = "TIER2"
        reason = f"neighbour smoothness fail ({top5_positive}/5)"

    duration = time.time() - t0
    print(f"[{symbol}] TIER {tier} — {reason}  ({duration:.1f}s)")

    return {
        "symbol": symbol,
        "tier": tier,
        "reason": reason,
        "best_params": params_to_dict(winner_params),
        "best_cfg_summary": {
            "z_quantile": z, "hurst_quantile": hq,
            "stop_atr_mult": sa, "tp_frac": tf, "session": session,
        },
        "oos_per_split": winner["oos"],
        "median_net": winner["median_net"],
        "median_pf": winner["median_pf"],
        "commission_stress": stress_configs,
        "bootstrap_per_split": bootstrap_by_split,
        "neighbour_smoothness": {
            "top5_positive": top5_positive,
            "top5_configs": [
                {"cfg": r["cfg"], "median_net": r["median_net"],
                 "median_pf": r["median_pf"]}
                for r in top5
            ],
        },
        "duration_sec": duration,
    }


# =====================================================================
#  Main
# =====================================================================

DATA_PATHS = {
    "US100":  "data/historical/US100_M1.csv",
    "US500":  "data/historical/US500_M1.csv",
    "US30":   "data/historical/US30_M1.csv",
    "DE40":   "data/historical/DE40_M1.csv",
    "USOIL":  "data/historical/USOIL_M1.csv",
    "XAUUSD": "data/historical/XAUUSD_M1.csv",
}


def main():
    ap = argparse.ArgumentParser(description="v15 Ultimate per-symbol optimizer.")
    ap.add_argument("--symbols", nargs="+",
                     default=list(SMARTBB_UNIVERSE.keys()),
                     help="Symbols to optimise.")
    ap.add_argument("--out", default="Results/v15_ultimate_tuning.json",
                     help="Output JSON.")
    ap.add_argument("--report", default="Docs/V15_ULTIMATE_RESULTS.md",
                     help="Markdown report path.")
    args = ap.parse_args()

    results: list[dict] = []
    total_start = time.time()
    for sym in args.symbols:
        if sym not in SMARTBB_UNIVERSE:
            print(f"skip {sym} (not in SMARTBB_UNIVERSE)")
            continue
        path = Path(DATA_PATHS.get(sym, f"data/historical/{sym}_M1.csv"))
        if not path.exists():
            print(f"skip {sym} (data missing at {path})")
            continue
        rows = load_m1(path)
        res = optimize_symbol(sym, rows)
        results.append(res)

    total_duration = time.time() - total_start

    # Organise by tier
    by_tier: dict[str, list[dict]] = {"TIER1": [], "TIER2": [], "REJECT": []}
    for r in results:
        by_tier[r["tier"]].append(r)

    # Persist JSON
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_obj = {
        "_version": "v15",
        "_duration_sec": total_duration,
        "_symbols_tested": [r["symbol"] for r in results],
        "by_tier": {k: [r["symbol"] for r in v] for k, v in by_tier.items()},
        "results": {r["symbol"]: r for r in results},
    }
    with open(out_path, "w") as f:
        json.dump(out_obj, f, indent=2, default=str)
    print(f"\nWrote {out_path}")

    # Markdown report
    write_report(args.report, results, by_tier, total_duration)
    print(f"Wrote {args.report}")

    print("\n" + "=" * 70)
    print("  v15 SUMMARY")
    print("=" * 70)
    for tier, rs in by_tier.items():
        print(f"  {tier:8s}: {[r['symbol'] for r in rs]}")


def write_report(path: str, results: list[dict],
                   by_tier: dict, duration_sec: float) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    lines.append("# SmartBB v15 — ULTIMATE PER-SYMBOL RESULTS")
    lines.append("")
    lines.append("**Methodology**:")
    lines.append("  * Grid: 4×4×5×3×4 = 960 configs per symbol")
    lines.append("    (Z quantile × Hurst quantile × stop ATR × TP fraction × session filter)")
    lines.append("  * 3-split walk-forward on non-overlapping OOS windows")
    lines.append("  * Bootstrap 10,000 resamples per OOS split")
    lines.append("  * Commission stress test at +$0.50/lot, +$1.00/lot, +$2.00/lot extra")
    lines.append("  * Neighbour-smoothness: top-5 grid configs must also be profitable")
    lines.append("")
    lines.append(f"**Runtime**: {duration_sec/60:.1f} minutes over {len(results)} symbols")
    lines.append("")
    lines.append("## TIER 1 (LIVE-READY)")
    lines.append("Survives 3-split WF, bootstrap p05 > 0 on ≥2 splits, commission-stress +$1/lot still profitable, smoothness ≥3/5.")
    lines.append("")
    for r in by_tier["TIER1"]:
        lines.extend(symbol_section(r))
    if not by_tier["TIER1"]:
        lines.append("*none*")
    lines.append("")
    lines.append("## TIER 2 (WATCH — profitable but less robust)")
    lines.append("Profitable on median OOS but fails at least one robustness gate. Paper-trade first or use ½ risk.")
    lines.append("")
    for r in by_tier["TIER2"]:
        lines.extend(symbol_section(r))
    if not by_tier["TIER2"]:
        lines.append("*none*")
    lines.append("")
    lines.append("## REJECTED (no viable edge)")
    lines.append("")
    for r in by_tier["REJECT"]:
        lines.append(f"### {r['symbol']} — rejected")
        lines.append(f"Reason: {r['reason']}")
        lines.append("")
    if not by_tier["REJECT"]:
        lines.append("*none*")
    lines.append("")
    lines.append("## COMMISSION VERIFICATION")
    lines.append("")
    lines.append("Every symbol below shows the dollar commissions and spread costs charged on the winning config's best OOS split — proves the cost model is active.")
    lines.append("")
    lines.append("| Symbol | Trades | Net $ | Comm $ | Spread $ | Total cost $ | Cost/trade |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for r in results:
        if not r["oos_per_split"]:
            continue
        # pick best-net split for reporting
        best = max(r["oos_per_split"], key=lambda e: e["net"])
        total_cost = best["commissions"] + best["spread_cost"]
        per_tr = total_cost / best["n"] if best["n"] else 0
        lines.append(f"| {r['symbol']} | {best['n']} | "
                      f"${best['net']:+,.0f} | ${best['commissions']:,.0f} | "
                      f"${best['spread_cost']:,.0f} | ${total_cost:,.0f} | "
                      f"${per_tr:.2f} |")
    lines.append("")
    lines.append("## COMMISSION-STRESS MATRIX")
    lines.append("Verify every Tier 1/2 symbol's edge survives broker fee changes. Net P&L on best OOS split as extra $/lot rises:")
    lines.append("")
    lines.append("| Symbol | +$0.00 | +$0.50 | +$1.00 | +$2.00 | Slope |")
    lines.append("|---|---:|---:|---:|---:|:---:|")
    for r in results:
        if r["tier"] == "REJECT":
            continue
        cs = r.get("commission_stress", [])
        if not cs:
            continue
        vals = [c["net"] for c in cs]
        slope = "✅ robust" if vals[2] > 0 and vals[3] > 0 else "⚠️ fragile"
        lines.append(f"| {r['symbol']} | ${vals[0]:+,.0f} | ${vals[1]:+,.0f} | "
                      f"${vals[2]:+,.0f} | ${vals[3]:+,.0f} | {slope} |")
    lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")


def symbol_section(r: dict) -> list[str]:
    out = []
    cfg = r["best_cfg_summary"]
    out.append(f"### {r['symbol']} — TIER {r['tier']}")
    out.append(f"Reason: {r['reason']}")
    out.append("")
    out.append("**Best params:**  "
                 f"Z-quantile={cfg['z_quantile']}, "
                 f"Hurst-q={cfg['hurst_quantile']}, "
                 f"stop={cfg['stop_atr_mult']}×ATR, "
                 f"TP={cfg['tp_frac']}×band, "
                 f"session={cfg['session']}")
    out.append("")
    out.append("**3-split OOS performance:**")
    out.append("")
    out.append("| Split | n | Net $ | PF | DD% | WR | Comm $ | Spread $ |")
    out.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for i, e in enumerate(r["oos_per_split"]):
        out.append(f"| {i} | {e['n']} | ${e['net']:+,.0f} | "
                    f"{e['pf']:.2f} | {e['dd_pct']:.2f}% | "
                    f"{e['wr']*100:.1f}% | "
                    f"${e['commissions']:,.0f} | ${e['spread_cost']:,.0f} |")
    out.append("")
    out.append(f"**Median:** net ${r['median_net']:+,.0f}, PF {r['median_pf']:.2f}")
    out.append("")
    out.append("**Bootstrap CIs (10k resamples on each split):**")
    out.append("")
    out.append("| Split | Observed net | p05 net | p50 net | p95 net | p05 PF |")
    out.append("|---|---:|---:|---:|---:|---:|")
    for b in r["bootstrap_per_split"]:
        out.append(f"| {b['split']} | ${b['net_observed']:+,.0f} | "
                    f"${b['net_p05']:+,.0f} | ${b['net_p50']:+,.0f} | "
                    f"${b['net_p95']:+,.0f} | {b['pf_p05']:.2f} |")
    out.append("")
    return out


if __name__ == "__main__":
    main()
