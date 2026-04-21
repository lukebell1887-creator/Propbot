#!/usr/bin/env python3
"""
SHF v14 — per-symbol walk-forward optimizer.

For each candidate symbol:

  1. Load M1 bars.
  2. Split: [IS 60%][embargo 5%][OOS1 15%][embargo 5%][OOS2 15%]
  3. Grid-search (z_quantile, hurst_quantile, stop_atr_mult, tp_frac) on IS.
  4. Rank by IS PF -> top 20.
  5. Re-test top 20 on OOS1 -> rank by OOS1 net P&L -> top 5.
  6. Re-test top 5 on OOS2.
  7. Filter by acceptance: PF>=1.3 AND trades>=5 AND max_dd<2.5%.
  8. Bootstrap the OOS2 trade list (10k resamples); keep only if
     5th-percentile PF > 1.0.
  9. Phase 2 — hour-of-day mask: on the IS trade list of the winner,
     compute WR per hour. Mask = {h: WR >= 0.55 AND n >= 3}. Re-test
     OOS1/OOS2 with the mask; keep the better of (masked, unmasked).
 10. Write per-symbol winners + CIs to Results/v14_per_symbol_tuning.json.

Any symbol with no surviving config is DROPPED from the live universe.
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
#  Data loader (single symbol)
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
#  Run v14 on a single-symbol slice
# =====================================================================

def run_v14(symbol: str, rows: list[tuple], params: SymbolParams,
             initial_equity: float = 100_000.0) -> dict:
    cfg = SmartBBV14Config(default_params=params)
    eng = SmartBBV14Engine(
        symbols=[SMARTBB_UNIVERSE[symbol]],
        params={symbol: params},
        cfg=cfg,
        initial_equity=initial_equity,
    )
    for (t, o, h, l, c) in rows:
        eng.on_bar(symbol, t.timestamp(), t.strftime("%Y-%m-%d"),
                    t.hour, t.minute, o, h, l, c)
        if eng.halted_permanently: break

    s = eng.summary()
    # Attach trades (list of dicts) for bootstrap / hour mask
    s["_trades"] = [asdict(t) for t in eng.trades]
    return s


# =====================================================================
#  Bootstrap CI on a trade list
# =====================================================================

def bootstrap_ci(trades: list[dict], n_iters: int = 10_000,
                  seed: int = 42) -> dict:
    if not trades:
        return {"median_net": 0, "p05_net": 0, "p95_net": 0,
                "median_pf": 0, "p05_pf": 0, "p95_pf": 0,
                "median_dd": 0, "p95_dd": 0,
                "n_iters": 0}
    rng = random.Random(seed)
    pnls = [float(t["net_pnl"]) for t in trades]
    n = len(pnls)

    nets: list[float] = []
    pfs: list[float] = []
    dds: list[float] = []
    for _ in range(n_iters):
        sample = [pnls[rng.randrange(n)] for _ in range(n)]
        net = sum(sample)
        gw = sum(x for x in sample if x > 0)
        gl = -sum(x for x in sample if x <= 0)
        pf = gw / gl if gl > 0 else (float("inf") if gw > 0 else 0.0)
        # DD path
        peak = 0.0; cum = 0.0; dd = 0.0
        for x in sample:
            cum += x
            peak = max(peak, cum)
            cur_dd = peak - cum
            if cur_dd > dd: dd = cur_dd
        nets.append(net); pfs.append(pf); dds.append(dd)

    nets.sort(); pfs.sort(); dds.sort()

    def q(xs, p):
        idx = int(p * (len(xs) - 1))
        return xs[idx]

    # pf has inf values possibly — cap for percentile reporting
    pfs_cap = [min(x, 1e6) for x in pfs]
    return {
        "median_net": q(nets, 0.50),
        "p05_net": q(nets, 0.05),
        "p95_net": q(nets, 0.95),
        "median_pf": q(pfs_cap, 0.50),
        "p05_pf": q(pfs_cap, 0.05),
        "p95_pf": q(pfs_cap, 0.95),
        "median_dd": q(dds, 0.50),
        "p95_dd": q(dds, 0.95),
        "n_iters": n_iters,
    }


# =====================================================================
#  Hour-of-day mask from IS trades
# =====================================================================

def derive_hour_mask(trades: list[dict], min_wr: float = 0.55,
                       min_n: int = 3) -> Optional[frozenset]:
    if not trades:
        return None
    from collections import defaultdict
    by_hour = defaultdict(lambda: {"n": 0, "wins": 0})
    for t in trades:
        # entry_time is a unix timestamp (float) or ISO string (defaultdict in asdict)
        et = t.get("entry_time")
        try:
            if isinstance(et, (int, float)):
                hr = datetime.fromtimestamp(float(et)).hour
            else:
                hr = datetime.fromisoformat(str(et)).hour
        except Exception:
            continue
        b = by_hour[hr]
        b["n"] += 1
        if float(t.get("net_pnl", 0.0)) > 0:
            b["wins"] += 1
    kept = []
    for h, b in by_hour.items():
        if b["n"] >= min_n and b["wins"] / b["n"] >= min_wr:
            kept.append(h)
    if not kept:
        return None
    return frozenset(kept)


# =====================================================================
#  Grid
# =====================================================================

def build_grid() -> list[SymbolParams]:
    grid: list[SymbolParams] = []
    for zq, hq, sa, tf in product(
        [0.97, 0.98, 0.99],          # z_quantile
        [0.20, 0.30, 0.40],           # hurst_quantile
        [0.75, 1.00, 1.25],           # stop_atr_mult
        [0.50, 0.75, 1.00],           # tp_frac
    ):
        grid.append(SymbolParams(
            z_quantile=zq,
            hurst_quantile=hq,
            stop_atr_mult=sa,
            tp_frac=tf,
        ))
    return grid


# =====================================================================
#  Walk-forward for one symbol
# =====================================================================

def walk_forward(symbol: str, rows: list[tuple],
                   initial_equity: float,
                   is_frac: float = 0.60, emb_frac: float = 0.05,
                   top_is: int = 20, top_oos1: int = 5,
                   min_trades_oos: int = 5,
                   acceptance_pf: float = 1.3,
                   acceptance_dd: float = 2.5,
                   bootstrap_iters: int = 10_000) -> dict:

    n = len(rows)
    is_end = int(is_frac * n)
    emb_len = int(emb_frac * n)
    oos1_start = is_end + emb_len
    oos1_end = oos1_start + int(((1 - is_frac - 2 * emb_frac) / 2) * n)
    oos2_start = oos1_end + emb_len
    oos2_end = n

    is_rows = rows[:is_end]
    oos1_rows = rows[oos1_start:oos1_end]
    oos2_rows = rows[oos2_start:oos2_end]

    print(f"\n[{symbol}] splits: IS={len(is_rows):,}  "
           f"OOS1={len(oos1_rows):,}  OOS2={len(oos2_rows):,}", flush=True)

    grid = build_grid()
    print(f"[{symbol}] IS grid = {len(grid)} configs", flush=True)

    # --- Phase 1: IS grid ----------------------------------------------
    is_results = []
    t0 = time.time()
    for i, p in enumerate(grid):
        s = run_v14(symbol, is_rows, p, initial_equity)
        is_results.append((p, s))
        if (i + 1) % 10 == 0 or i == 0:
            print(f"  IS [{i+1}/{len(grid)}] last: "
                   f"n={s['trades']} PF={s['pf']:.2f} net=${s['net_pnl']:.0f}",
                   flush=True)
    dt1 = time.time() - t0
    print(f"[{symbol}] IS done in {dt1:.1f}s "
           f"({len(grid)*len(is_rows)/max(dt1,1e-3):,.0f} bars/sec)", flush=True)

    # Rank IS results by PF (tie-break: n_trades)
    is_results.sort(key=lambda r: (r[1].get("pf", 0) if r[1].get("pf") != float("inf") else 1e6,
                                      r[1].get("trades", 0)), reverse=True)
    # drop configs with 0 trades
    is_filtered = [r for r in is_results if r[1].get("trades", 0) >= 5]
    if not is_filtered:
        return {"symbol": symbol, "status": "DROP_IS_NO_TRADES",
                "is_best_trades": [r[1].get("trades", 0) for r in is_results[:5]]}
    top20 = is_filtered[:top_is]

    # --- Phase 2: OOS1 on top20 ---------------------------------------
    oos1_results = []
    for (p, _) in top20:
        s = run_v14(symbol, oos1_rows, p, initial_equity)
        oos1_results.append((p, s))
    oos1_results.sort(key=lambda r: r[1].get("net_pnl", 0), reverse=True)
    top5 = oos1_results[:top_oos1]

    # --- Phase 3: OOS2 on top5 ----------------------------------------
    oos2_results = []
    for (p, _) in top5:
        s = run_v14(symbol, oos2_rows, p, initial_equity)
        oos2_results.append((p, s))

    # --- Acceptance filter ---------------------------------------------
    def _passes(oos_m: dict) -> bool:
        return (oos_m.get("trades", 0) >= min_trades_oos and
                 oos_m.get("pf", 0) >= acceptance_pf and
                 oos_m.get("max_dd_pct", 100) < acceptance_dd)

    survivors = [(p, oos2) for (p, oos2) in oos2_results if _passes(oos2)]
    print(f"[{symbol}] Top5 OOS2 results:", flush=True)
    for (p, m) in oos2_results:
        print(f"    z={p.z_quantile} hq={p.hurst_quantile} "
               f"sa={p.stop_atr_mult} tf={p.tp_frac}  "
               f"n={m.get('trades',0)} PF={m.get('pf',0):.2f} "
               f"net=${m.get('net_pnl',0):.0f} "
               f"DD={m.get('max_dd_pct',0):.2f}%  "
               f"{'PASS' if _passes(m) else 'fail'}", flush=True)

    if not survivors:
        return {"symbol": symbol, "status": "DROP_OOS2_NONE_PASS",
                "oos2_results": [{
                    "params": params_to_dict(p),
                    "metrics": {k: v for k, v in m.items() if k != "_trades"},
                } for (p, m) in oos2_results]}

    # Pick the survivor with best OOS2 net
    best_p, best_oos2 = max(survivors, key=lambda r: r[1].get("net_pnl", 0))

    # --- Bootstrap CI on OOS2 trades ----------------------------------
    ci = bootstrap_ci(best_oos2.get("_trades", []), n_iters=bootstrap_iters)
    print(f"[{symbol}] Bootstrap CI on OOS2: "
           f"net_p05=${ci['p05_net']:.0f} pf_p05={ci['p05_pf']:.2f} "
           f"dd_p95=${ci['p95_dd']:.0f}", flush=True)

    if ci["p05_pf"] < 1.0:
        return {"symbol": symbol, "status": "DROP_BOOTSTRAP_FAIL",
                "best_params": params_to_dict(best_p),
                "oos2_metrics": {k: v for k, v in best_oos2.items() if k != "_trades"},
                "bootstrap": ci}

    # --- Phase 4: hour-of-day mask ------------------------------------
    # Re-run on IS with the winning params to collect the IS trade set,
    # then derive mask and re-test on OOS1/OOS2.
    is_with_best = run_v14(symbol, is_rows, best_p, initial_equity)
    mask = derive_hour_mask(is_with_best.get("_trades", []), min_wr=0.55, min_n=3)
    masked_result = None
    if mask is not None and len(mask) >= 1:
        p_mask = SymbolParams(**{**asdict(best_p), "allowed_hours": mask})
        oos2_masked = run_v14(symbol, oos2_rows, p_mask, initial_equity)
        if _passes(oos2_masked) and oos2_masked.get("net_pnl", 0) > best_oos2.get("net_pnl", 0):
            masked_result = {
                "params": params_to_dict(p_mask),
                "oos2_metrics": {k: v for k, v in oos2_masked.items() if k != "_trades"},
                "bootstrap": bootstrap_ci(oos2_masked.get("_trades", []),
                                             n_iters=bootstrap_iters),
                "hour_mask": sorted(mask),
            }
            print(f"[{symbol}] Hour mask {sorted(mask)} IMPROVES OOS2 "
                   f"(${oos2_masked.get('net_pnl',0):.0f} > ${best_oos2.get('net_pnl',0):.0f})",
                   flush=True)

    return {
        "symbol": symbol,
        "status": "KEEP_MASKED" if masked_result else "KEEP",
        "best_params": params_to_dict(best_p),
        "oos2_metrics": {k: v for k, v in best_oos2.items() if k != "_trades"},
        "bootstrap": ci,
        "masked_alternative": masked_result,
        "is_trades_with_best": is_with_best.get("trades", 0),
        "is_net_with_best": is_with_best.get("net_pnl", 0.0),
    }


# =====================================================================
#  CLI
# =====================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", nargs="+", default=[
        "US100", "US500", "US30", "DE40", "UK100", "JP225", "USOIL", "XAUUSD"])
    ap.add_argument("--balance", type=float, default=100_000.0)
    ap.add_argument("--out", type=Path,
                      default=ROOT / "Results" / "v14_per_symbol_tuning.json")
    ap.add_argument("--bootstrap-iters", type=int, default=10_000)
    ap.add_argument("--acceptance-pf", type=float, default=1.3)
    ap.add_argument("--acceptance-dd", type=float, default=2.5)
    ap.add_argument("--min-trades-oos", type=int, default=5)
    a = ap.parse_args()

    out = {"results": {}, "summary": {}}
    kept = []
    dropped = []

    for sym in a.symbols:
        if sym not in SMARTBB_UNIVERSE:
            print(f"[{sym}] SKIP — not in SMARTBB_UNIVERSE "
                   f"(no verified low-commission spec)", flush=True)
            out["results"][sym] = {"status": "NOT_IN_UNIVERSE"}
            dropped.append(sym)
            continue
        path = ROOT / "data" / "historical" / f"{sym}_M1.csv"
        if not path.exists():
            print(f"[{sym}] SKIP — no data file", flush=True)
            out["results"][sym] = {"status": "NO_DATA"}
            dropped.append(sym)
            continue
        rows = load_m1(path)
        print(f"\n=== {sym} ===  ({len(rows):,} M1 bars)", flush=True)
        res = walk_forward(
            symbol=sym, rows=rows, initial_equity=a.balance,
            min_trades_oos=a.min_trades_oos,
            acceptance_pf=a.acceptance_pf,
            acceptance_dd=a.acceptance_dd,
            bootstrap_iters=a.bootstrap_iters,
        )
        out["results"][sym] = res
        if res["status"].startswith("KEEP"):
            kept.append(sym)
        else:
            dropped.append(sym)

        # Save incrementally in case of crash
        a.out.parent.mkdir(parents=True, exist_ok=True)
        with open(a.out, "w") as f:
            json.dump(out, f, indent=2, default=str)

    out["summary"] = {"kept": kept, "dropped": dropped,
                       "total_candidates": len(a.symbols)}
    with open(a.out, "w") as f:
        json.dump(out, f, indent=2, default=str)

    print("\n" + "=" * 70, flush=True)
    print(f"  v14 OPTIMIZER SUMMARY", flush=True)
    print("=" * 70, flush=True)
    print(f"  KEPT:     {kept}", flush=True)
    print(f"  DROPPED:  {dropped}", flush=True)
    print(f"  Written to: {a.out}", flush=True)


if __name__ == "__main__":
    main()
