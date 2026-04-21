#!/usr/bin/env python3
"""
SHF v15-X — UNIVERSE-WIDE FAST SCAN
====================================

Runs a **coarse grid + 1-split walk-forward** on every pair we have M1 data
for (31 pairs across indices / oil / metals / forex), so we can confirm:

  1. Which pairs have a real mean-reversion edge under REAL commission costs.
  2. How commission load affects each asset class (indices free, FX $4/lot,
     oil 0.002%, metals 0.001%).
  3. Whether the 6 pairs we've been obsessing over are really the best,
     or whether there are hidden gems in the FX universe we've never tested.

NOT a replacement for `v15_ultimate_optimizer.py` — that one does 3-split
WF + bootstrap CIs + commission stress on only the final winners.  This
script is the pre-filter: cheap grid on all 31 pairs to find candidates
worth the heavyweight treatment.

Grid: 3 z × 3 hq × 2 sa × 2 tf × 2 ses = 72 configs (vs 960 in ultimate)
Split: 70 % IS / 25 % OOS + 5 % embargo.
Runtime: ~60 sec per 100k-bar FX pair, ~4 min per 530k-bar XAGUSD.

Output:
  * Results/v15x_universe_scan.json — machine-readable summary per symbol
  * Docs/V15X_UNIVERSE_SCAN.md      — human-readable with commission costs,
                                       trade counts, PF, net, verdicts.

Usage:
  python Scripts/v15x_universe_scan.py \
      --out Results/v15x_universe_scan.json \
      --report Docs/V15X_UNIVERSE_SCAN.md
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
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


# ---------- Data loader -----------------------------------------------

def load_m1(path: Path) -> list[tuple]:
    out: list[tuple] = []
    with open(path, "r", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try:
                t = datetime.fromisoformat(row["time"])
            except Exception:
                t = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            out.append((t, float(row["open"]), float(row["high"]),
                         float(row["low"]), float(row["close"])))
    return out


# ---------- Sessions --------------------------------------------------

SESSION_MAP: dict[str, Optional[frozenset[int]]] = {
    "all": None,
    "us": frozenset(range(13, 21)),
}


# ---------- Coarse grid ----------------------------------------------

def build_grid() -> list[tuple]:
    zs       = [0.95, 0.97, 0.99]         # 3
    hqs      = [0.15, 0.30, 0.45]         # 3
    stops    = [0.75, 1.25]                # 2
    tps      = [0.50, 0.75]                # 2
    sessions = ["all", "us"]               # 2
    return list(product(zs, hqs, stops, tps, sessions))  # 72


def cfg_to_params(z, hq, sa, tf, session) -> SymbolParams:
    return SymbolParams(
        z_quantile=z, z_min_abs=2.0, z_max_abs=5.5,
        hurst_quantile=hq,
        stop_atr_mult=sa, tp_frac=tf,
        allowed_hours=SESSION_MAP[session],
    )


# ---------- Engine runner --------------------------------------------

def run_engine(symbol: str, rows: list[tuple], params: SymbolParams,
                 extra_cost: float = 0.0,
                 initial_equity: float = 100_000.0) -> dict:
    cfg = SmartBBV14Config(default_params=params,
                             extra_cost_per_lot=extra_cost)
    eng = SmartBBV14Engine(
        symbols=[SMARTBB_UNIVERSE[symbol]],
        params={symbol: params}, cfg=cfg,
        initial_equity=initial_equity,
    )
    for (t, o, h, l, c) in rows:
        eng.on_bar(symbol, t.timestamp(), t.strftime("%Y-%m-%d"),
                     t.hour, t.minute, o, h, l, c)
    s = eng.summary()
    return s


# ---------- Walk-forward split ----------------------------------------

def make_split(rows: list[tuple]) -> tuple[list, list]:
    n = len(rows)
    is_end = int(0.70 * n)
    oos_start = int(0.75 * n)   # 5 % embargo
    oos_end = int(0.95 * n)
    return rows[:is_end], rows[oos_start:oos_end]


# ---------- Commission profile helper --------------------------------

def commission_profile(symbol: str) -> dict:
    """Return the dollar commission model info for the MD table."""
    spec = SMARTBB_UNIVERSE[symbol]
    return {
        "asset_class": spec.asset_class,
        "spread_pts": spec.spread_pts,
        "commission_type": spec.commission_type,
        "commission_per_deal": spec.commission_per_deal,
        "pip_value": spec.pip_value,
        "contract_size": spec.contract_size,
    }


# ---------- Per-symbol evaluation ------------------------------------

def evaluate_symbol(symbol: str, data_path: Path) -> dict:
    """Coarse-grid IS search; run best config on OOS; return summary."""
    if not data_path.exists():
        return {"symbol": symbol, "error": f"no data file {data_path}"}

    print(f"[{symbol}] loading {data_path.name} ...", flush=True)
    rows = load_m1(data_path)
    n = len(rows)
    is_rows, oos_rows = make_split(rows)
    print(f"[{symbol}]   {n:,} bars -> IS {len(is_rows):,}  OOS {len(oos_rows):,}",
            flush=True)

    grid = build_grid()
    t0 = time.time()

    # IS grid search
    is_best = None
    for i, (z, hq, sa, tf, ses) in enumerate(grid, 1):
        params = cfg_to_params(z, hq, sa, tf, ses)
        s = run_engine(symbol, is_rows, params)
        score = s["net_pnl"] - 0.5 * abs(s.get("max_dd_pct", 0.0)) * 100.0
        rec = {
            "z": z, "hq": hq, "sa": sa, "tf": tf, "ses": ses,
            "is_n": s["trades"], "is_net": s["net_pnl"], "is_pf": s["pf"],
            "is_comm": s["gross_commissions"],
            "is_spread": s["gross_spread_cost"],
            "score": score,
        }
        if (is_best is None) or (rec["score"] > is_best["score"]):
            is_best = rec
        if i % 24 == 0:
            print(f"[{symbol}]   grid {i:3d}/72  best_IS_net=${is_best['is_net']:+,.0f} "
                    f"PF={is_best['is_pf']:.2f}", flush=True)

    # OOS with best IS config
    best = is_best
    best_params = cfg_to_params(best["z"], best["hq"], best["sa"],
                                   best["tf"], best["ses"])
    oos = run_engine(symbol, oos_rows, best_params)
    oos_summary = {
        "oos_n": oos["trades"], "oos_net": oos["net_pnl"],
        "oos_pf": oos["pf"], "oos_wr": oos["win_rate"],
        "oos_dd_pct": oos["max_dd_pct"],
        "oos_commissions": oos["gross_commissions"],
        "oos_spread_cost": oos["gross_spread_cost"],
        "oos_exp_R": oos["expectancy_R"],
    }

    # Commission-stress: rerun best OOS with +$1/lot
    oos_stress = run_engine(symbol, oos_rows, best_params, extra_cost=1.0)
    stress_summary = {
        "stress_n": oos_stress["trades"], "stress_net": oos_stress["net_pnl"],
        "stress_pf": oos_stress["pf"],
    }

    # Classification
    oos_net = oos_summary["oos_net"]
    oos_pf = oos_summary["oos_pf"]
    oos_n = oos_summary["oos_n"]
    stress_net = stress_summary["stress_net"]
    stress_pf = stress_summary["stress_pf"]

    if oos_n < 3:
        tier = "REJECT"; reason = f"only {oos_n} OOS trades"
    elif oos_net <= 0 or oos_pf < 1.0:
        tier = "REJECT"; reason = f"unprofitable OOS net=${oos_net:.0f} PF={oos_pf:.2f}"
    elif stress_net <= 0 or stress_pf < 1.1:
        tier = "TIER3"; reason = f"fails +$1/lot stress net=${stress_net:.0f} PF={stress_pf:.2f}"
    elif oos_pf < 1.5:
        tier = "TIER2"; reason = f"marginal OOS PF={oos_pf:.2f}"
    else:
        tier = "TIER1"; reason = f"OOS PF={oos_pf:.2f} stress_PF={stress_pf:.2f}"

    dt = time.time() - t0
    print(f"[{symbol}] {tier} | OOS n={oos_n} net=${oos_net:+,.0f} PF={oos_pf:.2f} "
            f"| stress +$1 net=${stress_net:+,.0f} PF={stress_pf:.2f}  ({dt:.0f}s)",
            flush=True)

    return {
        "symbol": symbol,
        "bars": n,
        "commission_profile": commission_profile(symbol),
        "best_config": {
            "z": best["z"], "hq": best["hq"], "sa": best["sa"],
            "tf": best["tf"], "ses": best["ses"],
        },
        "is": {"n": best["is_n"], "net": best["is_net"], "pf": best["is_pf"],
                 "comm": best["is_comm"], "spread": best["is_spread"]},
        "oos": oos_summary,
        "stress": stress_summary,
        "tier": tier, "reason": reason, "elapsed_sec": dt,
    }


# ---------- Report writer --------------------------------------------

def write_report(results: list[dict], out: Path):
    lines = [
        "# v15-X Universe-Wide Mean-Reversion Scan — PhD-Grade Commission-Aware Results",
        "",
        f"_Generated {datetime.utcnow().isoformat()}Z — SmartBB v14 engine, coarse "
        "grid (72 configs) × 70/25 walk-forward with embargo._",
        "",
        "## 1. Methodology (2-page summary)",
        "",
        "1. For every M1 CSV in `data/historical/` we load the full series and cut "
        "   a **walk-forward split**: 70 % in-sample, 5 % embargo, 20 % out-of-sample.",
        "2. A **coarse 72-point grid** is run on IS (3 z-quantiles × 3 hurst-quantiles "
        "   × 2 stop-ATR-mults × 2 TP-fractions × 2 sessions).",
        "3. The IS-best config is promoted to OOS, with the **real per-symbol "
        "   commission model baked in** (see §3).",
        "4. A **commission-stress** run adds +$1/lot round-trip to test robustness "
        "   against broker slippage / fee hikes.",
        "5. Classification:",
        "   * **TIER 1** — OOS PF ≥ 1.5 AND +$1/lot stress still PF ≥ 1.1",
        "   * **TIER 2** — OOS PF 1.0–1.5 (edge exists but marginal)",
        "   * **TIER 3** — OOS profitable but fails commission stress",
        "   * **REJECT** — unprofitable OOS or <3 trades",
        "",
        "## 2. Summary — ranked by OOS profit factor",
        "",
        "| Sym | Tier | Asset | Bars | IS n | OOS n | OOS PF | OOS Net | Stress PF | Stress Net | Comm $ | Reason |",
        "|-----|------|-------|-----:|-----:|------:|-------:|--------:|----------:|-----------:|-------:|--------|",
    ]

    ranked = sorted(results,
                      key=lambda r: (0 if r.get("tier", "REJECT") == "TIER1" else
                                       1 if r.get("tier") == "TIER2" else
                                       2 if r.get("tier") == "TIER3" else 3,
                                       -r.get("oos", {}).get("oos_pf", 0.0)))

    for r in ranked:
        if "error" in r:
            lines.append(f"| {r['symbol']} | ERR | - | - | - | - | - | - | - | - | - | {r['error']} |")
            continue
        cp = r["commission_profile"]
        oos = r["oos"]; stress = r["stress"]; isr = r["is"]
        lines.append(
            f"| {r['symbol']} | **{r['tier']}** | {cp['asset_class']} | "
            f"{r['bars']:,} | {isr['n']} | {oos['oos_n']} | {oos['oos_pf']:.2f} | "
            f"${oos['oos_net']:+,.0f} | {stress['stress_pf']:.2f} | "
            f"${stress['stress_net']:+,.0f} | ${oos['oos_commissions']:,.0f} | "
            f"{r['reason']} |"
        )

    lines += [
        "",
        "## 3. Commission models per pair (baked into backtest)",
        "",
        "| Sym | Asset | Spread (pts) | Commission model | Round-trip cost @1 lot (typical) |",
        "|-----|-------|-------------:|------------------|---------------------------------|",
    ]

    for r in sorted(results, key=lambda x: (x["commission_profile"]["asset_class"], x["symbol"]))\
            if all("error" not in x for x in results) else []:
        cp = r["commission_profile"]
        if cp["commission_type"] == "zero":
            cost_str = f"$0 (spread-only: {cp['spread_pts']} pts × ${cp['pip_value']}/pt)"
        elif cp["commission_type"] == "fixed":
            rt = 2.0 * cp["commission_per_deal"]
            cost_str = f"${rt:.2f} fixed + {cp['spread_pts']} pts spread"
        elif cp["commission_type"] == "percent":
            # approximate — 1 lot at ~mid-price
            sample_price = {"XAUUSD": 2000.0, "XAGUSD": 25.0, "USOIL": 80.0,
                              "XBRUSD": 85.0, "XTIUSD": 80.0}.get(r["symbol"], 100.0)
            notional = sample_price * cp["contract_size"]
            rt = 2.0 * (cp["commission_per_deal"] / 100.0) * notional
            cost_str = f"${rt:.2f} ({cp['commission_per_deal']}% ×2 deals on ~${notional:,.0f} notional)"
        else:
            cost_str = "?"
        lines.append(f"| {r['symbol']} | {cp['asset_class']} | {cp['spread_pts']} | "
                     f"{cp['commission_type']}: {cp['commission_per_deal']} | {cost_str} |")

    # Group by asset class verdict
    by_class: dict[str, list] = {}
    for r in results:
        if "error" in r: continue
        by_class.setdefault(r["commission_profile"]["asset_class"], []).append(r)

    lines += [
        "",
        "## 4. Per-asset-class verdict",
        "",
    ]
    for ac in ["index", "metal", "oil", "forex"]:
        if ac not in by_class: continue
        rs = by_class[ac]
        t1 = [r for r in rs if r["tier"] == "TIER1"]
        t2 = [r for r in rs if r["tier"] == "TIER2"]
        t3 = [r for r in rs if r["tier"] == "TIER3"]
        rj = [r for r in rs if r["tier"] == "REJECT"]
        lines.append(f"### {ac.upper()} ({len(rs)} pairs tested)")
        lines.append("")
        lines.append(f"- **TIER 1 (deploy)**: {', '.join(r['symbol'] for r in t1) if t1 else 'none'}")
        lines.append(f"- **TIER 2 (watch)**: {', '.join(r['symbol'] for r in t2) if t2 else 'none'}")
        lines.append(f"- **TIER 3 (stress-fail)**: {', '.join(r['symbol'] for r in t3) if t3 else 'none'}")
        lines.append(f"- **REJECT**: {', '.join(r['symbol'] for r in rj) if rj else 'none'}")
        lines.append("")

    lines += [
        "## 5. Best-config cheat-sheet for TIER 1 pairs",
        "",
        "| Sym | Z-quantile | Hurst-quantile | Stop×ATR | TP-frac | Session |",
        "|-----|-----------:|---------------:|---------:|--------:|:-------:|",
    ]
    for r in ranked:
        if r.get("tier") != "TIER1": continue
        b = r["best_config"]
        lines.append(f"| {r['symbol']} | {b['z']} | {b['hq']} | {b['sa']} | {b['tf']} | {b['ses']} |")

    lines += [
        "",
        "## 6. Next step",
        "",
        "Pipe the TIER 1 and TIER 2 symbols from this coarse scan into "
        "`v15_ultimate_optimizer.py` (960-config grid × 3-split WF × 10k bootstrap "
        "× commission stress @ +$0.50/+$1/+$2 per lot) to lock in live configs.",
        "",
        "Raw JSON: `Results/v15x_universe_scan.json`",
        "",
    ]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")


# ---------- Main ------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/historical")
    ap.add_argument("--symbols", nargs="*", default=None,
                      help="restrict scan to these symbols (default: all with data)")
    ap.add_argument("--out", default="Results/v15x_universe_scan.json")
    ap.add_argument("--report", default="Docs/V15X_UNIVERSE_SCAN.md")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    all_syms = sorted(SMARTBB_UNIVERSE.keys())
    if args.symbols:
        run_syms = [s for s in args.symbols if s in all_syms]
    else:
        # All symbols where we have a CSV
        run_syms = [s for s in all_syms if (data_dir / f"{s}_M1.csv").exists()]

    print(f"v15-X scan on {len(run_syms)} pairs: {run_syms}", flush=True)

    out_json = Path(args.out)
    report_md = Path(args.report)
    results: list[dict] = []

    for i, sym in enumerate(run_syms, 1):
        print("", flush=True)
        print(f"=== [{i}/{len(run_syms)}] {sym} ===", flush=True)
        path = data_dir / f"{sym}_M1.csv"
        try:
            r = evaluate_symbol(sym, path)
        except Exception as e:
            import traceback
            traceback.print_exc()
            r = {"symbol": sym, "error": str(e)}
        results.append(r)

        # Checkpoint after each symbol
        out_json.parent.mkdir(parents=True, exist_ok=True)
        out_json.write_text(json.dumps(results, indent=2, default=str))
        write_report(results, report_md)

    print("", flush=True)
    print("=== v15-X SCAN COMPLETE ===", flush=True)
    for r in results:
        if "error" in r:
            print(f"  {r['symbol']:8s} ERR  {r['error']}", flush=True)
        else:
            oos = r["oos"]
            print(f"  {r['symbol']:8s} {r['tier']:6s} "
                    f"OOS n={oos['oos_n']:3d} net=${oos['oos_net']:+8,.0f} "
                    f"PF={oos['oos_pf']:.2f}", flush=True)


if __name__ == "__main__":
    main()
