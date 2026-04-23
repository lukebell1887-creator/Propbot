#!/usr/bin/env python3
"""
edge_hunt_z_window_v19.py
=========================
Proper edge hunt.  We keep the smart BB+ATR stop (the calculus part) and only
move the Z gate so every trade lands in the geometrically valid region

        |z| < 2 + stop_atr_mult * ATR / sigma_bb

The in-engine SL-direction guard means any trade that would still slip past
this inequality (e.g. when ATR collapses right at entry) is rejected outright.

Grid:
    z_min_abs       ∈ {1.8, 2.0, 2.2}
    z_max_abs       ∈ {2.2, 2.4, 2.6, 2.8}
    stop_atr_mult   ∈ {0.4, 0.5, 0.6, 0.75}
    tp_frac         ∈ {0.5, 0.7, 1.0}

Total: 144 configs (but many (z_min >= z_max) combos get skipped).

Outputs:
    Results/edge_hunt_z_window_v19.json
    stdout: top-20 ranked by expectancy_R (filtered: ≥20 trades, 0 phantoms)
"""
from __future__ import annotations

import csv
import json
import sys
import time as _time
from dataclasses import replace
from datetime import datetime, timedelta
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.live.v15_live import load_v15_params                 # noqa: E402
from src.smartbb_engine import SMARTBB_UNIVERSE               # noqa: E402
from src.smartbb_engine_v14 import (                          # noqa: E402
    SmartBBV14Engine, SmartBBV14Config,
)

TIER1 = ["US30", "US100", "US500", "DE40", "XAUUSD"]

# --- sweep grid ------------------------------------------------------------
Z_MIN    = [1.8, 2.0, 2.2]
Z_MAX    = [2.2, 2.4, 2.6, 2.8]
STOP_MLT = [0.4, 0.5, 0.6, 0.75]
TP_FRACS = [0.5, 0.7, 1.0]
# ---------------------------------------------------------------------------


def load_m1(path, tmin, tmax):
    out = []
    with open(path, "r", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try:
                t = datetime.fromisoformat(row["time"])
            except Exception:
                t = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            if tmin and t < tmin: continue
            if tmax and t > tmax: continue
            out.append((t, float(row["open"]), float(row["high"]),
                           float(row["low"]),  float(row["close"])))
    return out


def common_window(files, months):
    firsts, lasts = {}, {}
    for s, p in files.items():
        with open(p, "r", newline="") as f:
            rdr = csv.reader(f); next(rdr)
            rows = [r for r in rdr if r]
        firsts[s] = datetime.fromisoformat(rows[0][0])
        lasts[s]  = datetime.fromisoformat(rows[-1][0])
    end   = min(lasts.values())
    start = max(max(firsts.values()), end - timedelta(days=months * 31))
    return start, end


def mutate(base_params, z_min, z_max, stop_mult, tp_frac):
    """Return a fresh params dict with overrides.

    We ALSO relax the rolling-quantile gates + the Hurst absolute ceiling
    so the absolute-z window we are sweeping is the real binding constraint.
    Otherwise the v15 tuning's z_quantile=0.99 + hurst_max_abs≈0.45 keeps
    rejecting almost every entry and the sweep is uninformative.

    What stays ON:
      - Absolute-z window (this is what we are sweeping)
      - BB + ATR smart stop
      - OU half-life gate (the fundamental mean-reversion condition)
      - Hurst absolute ceiling relaxed to 0.60 (still rejects deep trends)
    """
    out = {}
    for sym, p in base_params.items():
        out[sym] = replace(
            p,
            z_min_abs=z_min,
            z_max_abs=z_max,
            stop_atr_mult=stop_mult,
            tp_frac=tp_frac,
            # relax rolling-quantile gates (they double-filter the z window)
            # RollingQuantile requires q ∈ (0,1) — use endpoints
            z_quantile=0.01,
            hurst_quantile=0.99,

            # allow slight trending too (0.45 → 0.60)
            hurst_max_abs=0.60,
        )
    return out



def run_one(merged, symbols_in_feed, base_params, cfg,
             z_min, z_max, stop_mult, tp_frac, balance):
    params = mutate(base_params, z_min, z_max, stop_mult, tp_frac)
    specs = [SMARTBB_UNIVERSE[s] for s in symbols_in_feed]
    eng = SmartBBV14Engine(
        symbols=specs, params=params, cfg=cfg, initial_equity=balance,
    )
    # fixed 0.5 % risk per trade — isolates raw edge, no sizing amplification
    eng.cfg = replace(cfg,
                        base_risk_pct=0.005,
                        min_risk_pct=0.005,
                        max_risk_pct=0.005)

    for t, s, o, h, l, c in merged:
        eng.on_bar(s, t.timestamp(), t.strftime("%Y-%m-%d"),
                   t.hour, t.minute, o, h, l, c)

    s = eng.summary()
    phantoms = sum(1 for tr in eng.trades
                      if tr.exit_reason == "stop_loss"
                         and tr.realised_R > 0 and tr.bars_held == 0)
    s["phantoms"] = phantoms

    # Per-symbol trade counts (for diagnostic)
    per_sym = {}
    for tr in eng.trades:
        per_sym.setdefault(tr.symbol, {"n": 0, "R": 0.0})
        per_sym[tr.symbol]["n"] += 1
        per_sym[tr.symbol]["R"] += tr.realised_R
    s["per_symbol"] = {k: {"n": v["n"], "avg_R": round(v["R"]/v["n"], 3)}
                         for k, v in per_sym.items()}
    return s


def main(months=3, balance=100_000.0, out_path="Results/edge_hunt_z_window_v19.json"):
    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in TIER1}
    files = {s: p for s, p in files.items() if p.exists()}
    if not files:
        print("ERROR: no {SYMBOL}_M1.csv files in data/historical/")
        return 1

    tmin, tmax = common_window(files, months)
    print(f"OOS window: {tmin.date()} -> {tmax.date()}")

    tuning_path = ROOT / "Results" / "v15_ultimate_tuning.json"
    base_params = load_v15_params(str(tuning_path)) if tuning_path.exists() else {}
    if not base_params:
        print("ERROR: no v15 tuning found")
        return 1

    cfg = SmartBBV14Config()

    internal = sorted(files)
    print(f"Symbols: {', '.join(internal)}")

    streams = {s: load_m1(files[s], tmin, tmax) for s in internal}
    merged  = []
    for s, bars in streams.items():
        merged.extend((t, s, o, h, l, c) for (t, o, h, l, c) in bars)
    merged.sort(key=lambda r: r[0])
    print(f"Total M1 bars: {len(merged):,}")

    combos = [
        (zmn, zmx, sm, tp)
        for (zmn, zmx, sm, tp) in product(Z_MIN, Z_MAX, STOP_MLT, TP_FRACS)
        if zmn < zmx
    ]
    print(f"Testing {len(combos)} configs (z_min < z_max only)...\n")

    results = []
    t0 = _time.time()
    for i, (zmn, zmx, sm, tp) in enumerate(combos, 1):
        s = run_one(merged, internal, base_params, cfg,
                      zmn, zmx, sm, tp, balance)
        row = {
            "z_min": zmn, "z_max": zmx,
            "stop_atr_mult": sm, "tp_frac": tp,
            "trades":       s["trades"],
            "net_pnl":      s["net_pnl"],
            "pct_return":   s["pct_return"],
            "pf":           s["pf"] if s["pf"] != float("inf") else 999.0,
            "win_rate":     s["win_rate"],
            "expectancy_R": s["expectancy_R"],
            "max_dd_pct":   s["max_dd_pct"],
            "phantoms":     s["phantoms"],
            "per_symbol":   s["per_symbol"],
        }
        results.append(row)
        print(f"  [{i:3d}/{len(combos)}] zmin={zmn} zmax={zmx} "
              f"atr_m={sm} tp={tp} "
              f"N={row['trades']:3d} WR={row['win_rate']*100:5.1f}% "
              f"E[R]={row['expectancy_R']:+.3f} "
              f"PF={row['pf']:5.2f} "
              f"PnL=${row['net_pnl']:+9,.0f} "
              f"DD={row['max_dd_pct']:4.2f}% "
              f"phantoms={row['phantoms']:3d}",
              flush=True)

    out_file = ROOT / out_path
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)

    # --- filtered ranking ---
    print()
    clean = [r for r in results if r["trades"] >= 20 and r["phantoms"] == 0]
    print(f"{len(clean)} configs pass filters (N>=20 AND phantoms==0)")
    print()
    print("=" * 108)
    print("TOP 20 by expectancy_R (honest only)")
    print("=" * 108)
    clean.sort(key=lambda x: x["expectancy_R"], reverse=True)
    hdr = f"{'zmin':<6}{'zmax':<6}{'atr_m':<7}{'tp':<6}{'N':<5}{'WR%':<7}{'E[R]':<8}{'PF':<7}{'PnL':<12}{'DD%':<6}"
    print(hdr)
    for r in clean[:20]:
        print(f"{r['z_min']:<6}{r['z_max']:<6}{r['stop_atr_mult']:<7}"
              f"{r['tp_frac']:<6}{r['trades']:<5}"
              f"{r['win_rate']*100:<7.1f}{r['expectancy_R']:<+8.3f}"
              f"{r['pf']:<7.2f}${r['net_pnl']:<+10,.0f}"
              f"{r['max_dd_pct']:<6.2f}")

    print()
    print("=" * 108)
    print("TOP 10 by net_pnl (honest only)")
    print("=" * 108)
    clean.sort(key=lambda x: x["net_pnl"], reverse=True)
    print(hdr)
    for r in clean[:10]:
        print(f"{r['z_min']:<6}{r['z_max']:<6}{r['stop_atr_mult']:<7}"
              f"{r['tp_frac']:<6}{r['trades']:<5}"
              f"{r['win_rate']*100:<7.1f}{r['expectancy_R']:<+8.3f}"
              f"{r['pf']:<7.2f}${r['net_pnl']:<+10,.0f}"
              f"{r['max_dd_pct']:<6.2f}")

    print()
    print(f"Saved: {out_file}")
    print(f"Elapsed: {_time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--balance", type=float, default=100_000.0)
    a = ap.parse_args()
    sys.exit(main(a.months, a.balance))
