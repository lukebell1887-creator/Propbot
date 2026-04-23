#!/usr/bin/env python3
"""
hunt_real_edge_v19.py
=====================
Exhaustive edge hunt now that we KNOW the BB-anchored SL was inverted in 77 %
of historical "profitable" trades. Goal: find a configuration of the SmartBB
mean-reversion engine that produces HONEST positive expectancy with
broker-valid stops (ALWAYS correct side of entry).

Methodology (no bullshit):
  - Run SmartBBV14Engine directly with a FIXED 0.5 % risk per trade (no
    Grossman-Zhou amplification — we want to measure raw edge)
  - Load the v15 tuning as a baseline, then mutate:
      sl_mode            ∈ {"bb_floored", "atr_fixed"}
      stop_atr_mult      ∈ {1.0, 1.5, 2.0, 2.5}
      z_max_abs          ∈ {3.0, 3.5, 4.0, 5.0}   (cap to avoid deep overshoots)
      tp_frac            ∈ {0.5, 0.75, 1.0}
  - Real 5%ers MTB cost model (same as backtest_v18)
  - Same 3-month OOS window as the existing benchmarks
  - Report per-config: trades / win_rate / expectancy_R / pf / max_dd / net
  - Rank by expectancy_R (stable across trade counts)

Outputs:
  Results/edge_hunt_v19.json   — full config × metrics table
  stdout                       — ranked top-20 configs
"""
from __future__ import annotations

import csv
import json
import sys
import time as _time
from copy import deepcopy
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
from src.smartbb_engine import SMARTBB_UNIVERSE              # noqa: E402
from src.smartbb_engine_v14 import (                          # noqa: E402
    SmartBBV14Engine, SmartBBV14Config, SymbolParams,
)

TIER1 = ["US30", "US100", "US500", "DE40", "XAUUSD"]

# --- sweep grid ------------------------------------------------------------
SL_MODES      = ["bb_floored", "atr_fixed"]
STOP_ATR_MULT = [1.0, 1.5, 2.0, 2.5]
Z_MAX         = [3.0, 3.5, 4.0, 5.0]
TP_FRACS      = [0.5, 0.75, 1.0]
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


def mutate_params(base_params: dict[str, SymbolParams],
                   sl_mode: str, stop_atr_mult: float,
                   z_max_abs: float, tp_frac: float) -> dict[str, SymbolParams]:
    """Apply the variant to every per-symbol SymbolParams."""
    out = {}
    for sym, p in base_params.items():
        out[sym] = replace(
            p,
            sl_mode=sl_mode,
            stop_atr_mult=stop_atr_mult,
            z_max_abs=z_max_abs,
            tp_frac=tp_frac,
        )
    return out


def run_one(merged, symbols_in_feed, base_params, cfg,
             sl_mode, stop_atr_mult, z_max_abs, tp_frac,
             balance) -> dict:
    """Run a single config variant through the merged M1 feed."""
    params = mutate_params(base_params, sl_mode, stop_atr_mult,
                             z_max_abs, tp_frac) if base_params else None
    specs = [SMARTBB_UNIVERSE[s] for s in symbols_in_feed]
    eng = SmartBBV14Engine(
        symbols=specs, params=params, cfg=cfg, initial_equity=balance,
    )
    # FIXED 0.5 % risk regardless of edge (no Grossman-Zhou amplification)
    eng.cfg = replace(cfg,
                        base_risk_pct=0.005,
                        min_risk_pct=0.005,
                        max_risk_pct=0.005)

    for t, s, o, h, l, c in merged:
        eng.on_bar(s, t.timestamp(), t.strftime("%Y-%m-%d"),
                   t.hour, t.minute, o, h, l, c)

    s = eng.summary()
    # Add SL-direction audit: any 0-bar winning stop_loss trades left?
    zero_bar_win_sl = sum(
        1 for tr in eng.trades
        if tr.exit_reason == "stop_loss"
           and tr.realised_R > 0 and tr.bars_held == 0
    )
    s["zero_bar_winning_SL"] = zero_bar_win_sl
    return s


def main(months: int = 3, balance: float = 100_000.0,
         out_path: str = "Results/edge_hunt_v19.json"):
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
    cfg         = SmartBBV14Config()

    internal = sorted(files)
    print(f"Symbols: {', '.join(internal)}")

    streams = {s: load_m1(files[s], tmin, tmax) for s in internal}
    merged  = []
    for s, bars in streams.items():
        merged.extend((t, s, o, h, l, c) for (t, o, h, l, c) in bars)
    merged.sort(key=lambda r: r[0])
    print(f"Total M1 bars: {len(merged):,}")

    combos = list(product(SL_MODES, STOP_ATR_MULT, Z_MAX, TP_FRACS))
    print(f"Testing {len(combos)} configurations ...\n")

    results = []
    t0 = _time.time()
    for i, (sl_mode, stop_mult, z_max, tp_frac) in enumerate(combos, 1):
        s = run_one(merged, internal, base_params, cfg,
                     sl_mode, stop_mult, z_max, tp_frac, balance)
        row = {
            "sl_mode": sl_mode,
            "stop_atr_mult": stop_mult,
            "z_max_abs": z_max,
            "tp_frac": tp_frac,
            "trades": s["trades"],
            "net_pnl": s["net_pnl"],
            "pct_return": s["pct_return"],
            "pf": s["pf"] if s["pf"] != float("inf") else 999.0,
            "win_rate": s["win_rate"],
            "expectancy_R": s["expectancy_R"],
            "max_dd_pct": s["max_dd_pct"],
            "zero_bar_win_SL": s["zero_bar_winning_SL"],
            "elapsed": round(_time.time() - t0, 1),
        }
        results.append(row)
        print(f"  [{i:3d}/{len(combos)}] sl={sl_mode:<10} "
              f"atr={stop_mult:.1f} zmax={z_max:.1f} tpfrac={tp_frac:.2f} "
              f"N={row['trades']:3d} WR={row['win_rate']*100:5.1f}% "
              f"E[R]={row['expectancy_R']:+.3f} "
              f"PF={row['pf']:5.2f} "
              f"PnL=${row['net_pnl']:+9,.0f} "
              f"DD={row['max_dd_pct']:4.2f}% "
              f"|bug0b|={row['zero_bar_win_SL']:3d}")

    # Persist
    out_file = ROOT / out_path
    out_file.parent.mkdir(parents=True, exist_ok=True)
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2)

    print()
    print("=" * 100)
    print("TOP 15 by expectancy_R (filtered: trades >= 15, zero_bar_win_SL == 0)")
    print("=" * 100)
    clean = [r for r in results if r["trades"] >= 15 and r["zero_bar_win_SL"] == 0]
    clean.sort(key=lambda x: x["expectancy_R"], reverse=True)
    print(f"{'sl_mode':<12}{'atr':<5}{'zmax':<6}{'tp':<6}"
          f"{'N':<5}{'WR%':<7}{'E[R]':<8}{'PF':<7}{'PnL':<12}{'DD%':<6}")
    for r in clean[:15]:
        print(f"{r['sl_mode']:<12}{r['stop_atr_mult']:<5.1f}{r['z_max_abs']:<6.1f}"
              f"{r['tp_frac']:<6.2f}{r['trades']:<5}"
              f"{r['win_rate']*100:<7.1f}{r['expectancy_R']:<+8.3f}"
              f"{r['pf']:<7.2f}${r['net_pnl']:<+10,.0f}"
              f"{r['max_dd_pct']:<6.2f}")

    print()
    print("=" * 100)
    print("TOP 10 by net_pnl (honest — trades >= 15 AND zero_bar_win_SL == 0)")
    print("=" * 100)
    clean.sort(key=lambda x: x["net_pnl"], reverse=True)
    print(f"{'sl_mode':<12}{'atr':<5}{'zmax':<6}{'tp':<6}"
          f"{'N':<5}{'WR%':<7}{'E[R]':<8}{'PF':<7}{'PnL':<12}{'DD%':<6}")
    for r in clean[:10]:
        print(f"{r['sl_mode']:<12}{r['stop_atr_mult']:<5.1f}{r['z_max_abs']:<6.1f}"
              f"{r['tp_frac']:<6.2f}{r['trades']:<5}"
              f"{r['win_rate']*100:<7.1f}{r['expectancy_R']:<+8.3f}"
              f"{r['pf']:<7.2f}${r['net_pnl']:<+10,.0f}"
              f"{r['max_dd_pct']:<6.2f}")

    print(f"\nSaved: {out_file}")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--balance", type=float, default=100_000.0)
    ap.add_argument("--out", default="Results/edge_hunt_v19.json")
    a = ap.parse_args()
    sys.exit(main(a.months, a.balance, a.out))
