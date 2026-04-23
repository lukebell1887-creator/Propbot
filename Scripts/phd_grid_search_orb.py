#!/usr/bin/env python3
"""
phd_grid_search_orb.py — PhD-grade exhaustive grid search for ORB v20.

Tests every combination of:
    or_minutes     ∈ {5, 15, 30}                       (OR duration)
    tp_preset      ∈ {(0.5,1.0), (1.0,2.0), (1.5,3.0)} (TP ladder ratios)
    sl_buffer      ∈ {0.0, 0.3}                        (wider stop)
    amp_hurdle     ∈ {2.5, 4.0}                        (cost-edge gate)
    require_nr7    ∈ {False, True}                     (Crabel filter)
    hurst_regime   ∈ {none, trending_only}             (H>0.55 regime)

= 3 × 3 × 2 × 2 × 2 × 2 = 144 full backtests.

Data loaded ONCE (merged 376k M1 bars), each backtest ~1.5s → ~4 minutes total.

Output:
    - TOP 20 overall configurations by PhD score J
    - BEST per-symbol configuration (with all other symbols masked out
      so each symbol can pick its own optimal setup)
    - Saved JSON: Results/phd_grid_search_orb_v20.json

PhD scoring function:
    J = PnL_$  ×  √N   ×  max(PF-1, 0)   /   (1 + DD%)

    which rewards: absolute profit, sample size, edge strength (PF over 1),
    and penalises drawdown.  Filters out configs with N<20 or DD>8% (5%ers).
"""
from __future__ import annotations

import csv
import json
import math
import sys
import time as _time
from datetime import datetime, timedelta
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.smartbb_engine import SMARTBB_UNIVERSE                      # noqa: E402
from src.orb_engine_v20 import ORBEngineV20, ORBEngineConfig         # noqa: E402
from src.momentum.orb import ORBConfig                               # noqa: E402


SYMBOLS = ["US30", "US100", "US500", "DE40", "XAUUSD"]


def load_m1(path, tmin, tmax):
    out = []
    with open(path, "r", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try:
                t = datetime.fromisoformat(row["time"])
            except Exception:
                t = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            if tmin and t < tmin:
                continue
            if tmax and t > tmax:
                continue
            out.append((t, float(row["open"]), float(row["high"]),
                        float(row["low"]), float(row["close"])))
    return out


def common_window(files, months):
    firsts, lasts = {}, {}
    for s, p in files.items():
        with open(p, "r", newline="") as f:
            rdr = csv.reader(f)
            next(rdr)
            rows = [r for r in rdr if r]
        try:
            firsts[s] = datetime.fromisoformat(rows[0][0])
            lasts[s] = datetime.fromisoformat(rows[-1][0])
        except Exception:
            firsts[s] = datetime.strptime(rows[0][0], "%Y-%m-%d %H:%M:%S")
            lasts[s] = datetime.strptime(rows[-1][0], "%Y-%m-%d %H:%M:%S")
    end = min(lasts.values())
    start = max(max(firsts.values()), end - timedelta(days=months * 31))
    return start, end


def build_orb_configs(or_minutes: int, tp1: float, tp2: float,
                      sl_buf: float) -> dict[str, ORBConfig]:
    """Build per-symbol ORBConfig with the given global params."""
    cfgs: dict[str, ORBConfig] = {}
    for sym in SYMBOLS:
        # DE40 uses Xetra open (08:00 UTC), others use NY (14:30 UTC).
        if sym == "DE40":
            h, m = 8, 0
        else:
            h, m = 14, 30
        cfgs[sym] = ORBConfig(
            or_start_hour=h, or_start_minute=m,
            or_minutes=or_minutes, trade_window_minutes=90,
            tp1_range_mult=tp1, tp2_range_mult=tp2,
            sl_buffer_range_mult=sl_buf,
        )
    return cfgs


def run_one(label: dict, specs, merged, balance: float,
            engine_cfg: ORBEngineConfig,
            orb_configs: dict[str, ORBConfig]):
    eng = ORBEngineV20(
        symbols=specs,
        cfg=engine_cfg,
        orb_configs=orb_configs,
        initial_equity=balance,
    )
    for t, s, o, h, l, c in merged:
        eng.on_bar(s, t.timestamp(), t.strftime("%Y-%m-%d"),
                   t.hour, t.minute, o, h, l, c)
    summ = eng.summary()
    summ["cfg"] = label
    return summ


def j_score(r: dict) -> float:
    """PhD composite score — NaN for invalid configs."""
    n = r.get("entries", 0)
    if n < 20:
        return float("-inf")
    if r["max_dd_pct"] >= 8.0:
        return float("-inf")
    pf = r.get("pf", 0.0)
    if not math.isfinite(pf):
        pf = 3.0  # cap infinite PF (rare, when no losers)
    pnl = r["net_pnl"]
    dd = r["max_dd_pct"]
    return pnl * math.sqrt(n) * max(pf - 1.0, 0.0) / (1.0 + dd)


def main():
    balance = 100_000.0
    months = 3
    print("=" * 110)
    print("  ORB v20 — PhD-GRADE GRID SEARCH (144 configurations)")
    print(f"  ${balance:,.0f} account  |  5 symbols  |  full per-asset-class commission")
    print("=" * 110)

    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in SYMBOLS}
    files = {s: p for s, p in files.items() if p.exists()}
    if not files:
        print("ERROR: no {SYMBOL}_M1.csv files in data/historical/")
        return 1

    tmin, tmax = common_window(files, months)
    print(f"  Symbols: {', '.join(sorted(files))}")
    print(f"  Window : {tmin.date()} -> {tmax.date()}")

    internal = sorted(files)
    specs = [SMARTBB_UNIVERSE[s] for s in internal]

    streams = {s: load_m1(files[s], tmin, tmax) for s in internal}
    merged = []
    for s, bars in streams.items():
        merged.extend((t, s, o, h, l, c) for (t, o, h, l, c) in bars)
    merged.sort(key=lambda r: r[0])
    print(f"  Total M1 bars: {len(merged):,}")
    print()

    # ---- Build 144-cell grid -----------------------------------------
    grid_or_minutes = [5, 15, 30]
    grid_tp = [(0.5, 1.0), (1.0, 2.0), (1.5, 3.0)]
    grid_sl = [0.0, 0.3]
    grid_amp = [2.5, 4.0]
    grid_nr = [False, True]
    grid_hurst = [(0.0, 1.0), (0.55, 1.0)]   # (min, max); (0.55,1.0) = trending only

    configs = list(product(grid_or_minutes, grid_tp, grid_sl,
                           grid_amp, grid_nr, grid_hurst))
    total = len(configs)
    print(f"  Grid cells: {total}")
    print(f"  Estimated runtime: ~{total * 1.5 / 60:.1f} min")
    print()

    t_start = _time.time()
    results = []
    for i, (orm, (tp1, tp2), slb, amp, nr, (hmin, hmax)) in enumerate(configs, 1):
        label = {
            "or_minutes": orm,
            "tp1": tp1, "tp2": tp2,
            "sl_buffer": slb,
            "amp_hurdle": amp,
            "require_nr7": nr,
            "hurst_min": hmin, "hurst_max": hmax,
        }
        eng_cfg = ORBEngineConfig(
            risk_pct=0.005,
            amp_hurdle=amp,
            require_nr7=nr, nr_lookback=7,
            trail_atr_mult=0.8,
            tp1_close_frac=0.50, tp2_close_frac=0.25,
            hurst_min=hmin, hurst_max=hmax,
            hurst_window=200,
        )
        orb_cfgs = build_orb_configs(orm, tp1, tp2, slb)

        t0 = _time.time()
        r = run_one(label, specs, merged, balance, eng_cfg, orb_cfgs)
        elapsed = _time.time() - t0
        r["elapsed_sec"] = elapsed
        r["J"] = j_score(r)
        results.append(r)

        if i % 12 == 0 or i == total:
            eta = (_time.time() - t_start) / i * (total - i)
            best_so_far = max((x for x in results if math.isfinite(x["J"])),
                              key=lambda x: x["J"], default=None)
            best_str = (f"best J={best_so_far['J']:.3g} "
                        f"PnL=${best_so_far['net_pnl']:+,.0f} "
                        f"N={best_so_far.get('entries',0)}"
                        if best_so_far else "no valid yet")
            print(f"  [{i:>3}/{total}]  ETA {eta:>4.0f}s  —  {best_str}")

    total_elapsed = _time.time() - t_start
    print()
    print(f"  Finished {total} runs in {total_elapsed:.0f}s "
          f"({total_elapsed/total:.2f}s/run)")
    print()

    # ---- TOP 20 ------------------------------------------------------
    ranked = sorted(results, key=lambda r: r["J"], reverse=True)
    valid = [r for r in ranked if math.isfinite(r["J"])]
    print("=" * 140)
    print(f"  TOP 20 CONFIGURATIONS BY PhD SCORE J  ({len(valid)}/{total} valid)")
    print("=" * 140)
    print(f"  {'rank':>4}  {'J':>9}  {'N':>4}  {'WR':>5}  {'PF':>5}  "
          f"{'PnL':>10}  {'DD%':>6}  |  or  tp1/tp2    slb  amp  nr  hurst")
    print("-" * 140)
    for i, r in enumerate(valid[:20], 1):
        c = r["cfg"]
        hurst_str = f"{c['hurst_min']:.2f}-{c['hurst_max']:.2f}"
        nr_str = "NR7" if c["require_nr7"] else "off"
        print(f"  {i:>4}  {r['J']:>9.0f}  {r.get('entries',0):>4}  "
              f"{r['win_rate']*100:>4.1f}%  {r['pf']:>5.2f}  "
              f"${r['net_pnl']:>+8,.0f}  {r['max_dd_pct']:>5.2f}%  |  "
              f"{c['or_minutes']:>2}  {c['tp1']:.1f}/{c['tp2']:.1f}    "
              f"{c['sl_buffer']:.1f}  {c['amp_hurdle']:.1f}  {nr_str:<3}  {hurst_str}")
    print()

    # ---- BEST PER SYMBOL --------------------------------------------
    print("=" * 140)
    print("  BEST CONFIGURATION PER SYMBOL (where that symbol was profitable)")
    print("=" * 140)
    print(f"  {'symbol':<7}  {'best J':>9}  {'N':>3}  {'WR':>5}  {'PF':>5}  "
          f"{'PnL':>10}  |  or  tp1/tp2  slb  amp  nr  hurst")
    print("-" * 140)
    best_per_sym = {}
    for sym in sorted(SYMBOLS):
        sym_best = None
        sym_best_J = float("-inf")
        sym_best_stats = None
        for r in results:
            d = r.get("by_symbol", {}).get(sym)
            if d is None or d["n"] < 5:
                continue
            pf_est = max(d["net"] / max(-d["net"], 1), 0)  # placeholder
            sym_net = d["net"]
            sym_wr = d["wr"]
            sym_n = d["n"]
            if sym_net <= 0:
                continue
            # Simpler per-symbol score: net × √N × WR (capped)
            s = sym_net * math.sqrt(sym_n) * min(sym_wr, 0.8)
            if s > sym_best_J:
                sym_best_J = s
                sym_best = r["cfg"]
                sym_best_stats = d
        if sym_best is None:
            print(f"  {sym:<7}  (no profitable config found)")
            continue
        c = sym_best
        d = sym_best_stats
        hurst_str = f"{c['hurst_min']:.2f}-{c['hurst_max']:.2f}"
        nr_str = "NR7" if c["require_nr7"] else "off"
        print(f"  {sym:<7}  {sym_best_J:>9.0f}  {d['n']:>3}  "
              f"{d['wr']*100:>4.1f}%  {'—':>5}  ${d['net']:>+8,.0f}  |  "
              f"{c['or_minutes']:>2}  {c['tp1']:.1f}/{c['tp2']:.1f}  "
              f"{c['sl_buffer']:.1f}  {c['amp_hurdle']:.1f}  {nr_str:<3}  {hurst_str}")
        best_per_sym[sym] = {"cfg": c, "stats": d, "score": sym_best_J}
    print()

    # ---- Save full results ------------------------------------------
    out_path = ROOT / "Results" / "phd_grid_search_orb_v20.json"
    out_path.parent.mkdir(exist_ok=True, parents=True)
    with open(out_path, "w") as f:
        # Strip the non-serialisable bits
        serialisable = []
        for r in ranked:
            rs = dict(r)
            if not math.isfinite(rs.get("J", 0.0)):
                rs["J"] = None
            if not math.isfinite(rs.get("pf", 1.0)):
                rs["pf"] = None
            serialisable.append(rs)
        json.dump({
            "total_runs": total,
            "valid_runs": len(valid),
            "balance": balance,
            "window": {"start": str(tmin), "end": str(tmax)},
            "top20": serialisable[:20],
            "all": serialisable,
            "best_per_symbol": best_per_sym,
            "elapsed_sec": total_elapsed,
        }, f, indent=2, default=str)
    print(f"  Saved full results → {out_path.relative_to(ROOT)}")
    print()

    # ---- Summary verdict --------------------------------------------
    if not valid:
        print("VERDICT: No config met the minimum N≥20 / DD<8% gates. Edge is not robust.")
        return 0
    best = valid[0]
    print("=" * 110)
    print(f"OVERALL BEST: J={best['J']:.0f}")
    c = best["cfg"]
    print(f"  OR minutes       = {c['or_minutes']}")
    print(f"  TP ladder (×OR)  = {c['tp1']:.1f} / {c['tp2']:.1f}")
    print(f"  SL buffer        = {c['sl_buffer']:.2f} × OR_range beyond OR-mirror")
    print(f"  Amp hurdle       = {c['amp_hurdle']:.1f} × round-trip cost")
    print(f"  NR7 filter       = {'ON' if c['require_nr7'] else 'OFF'}")
    print(f"  Hurst gate       = [{c['hurst_min']:.2f}, {c['hurst_max']:.2f}]"
          + (" (trending only)" if c['hurst_min'] >= 0.55 else " (disabled)"))
    print()
    print(f"  Entries          = {best.get('entries',0)}")
    print(f"  Win rate         = {best['win_rate']*100:.1f}%")
    print(f"  Profit factor    = {best['pf']:.2f}")
    print(f"  Net PnL          = ${best['net_pnl']:+,.0f} ({best['pct_return']:+.1f}%)")
    print(f"  Max drawdown     = {best['max_dd_pct']:.2f}%")
    print(f"  Total costs      = ${best.get('gross_commissions',0) + best.get('gross_spread_cost',0):,.0f}")
    print("=" * 110)

    return 0


if __name__ == "__main__":
    sys.exit(main())
