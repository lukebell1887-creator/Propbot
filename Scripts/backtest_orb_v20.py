#!/usr/bin/env python3
"""
backtest_orb_v20.py — honest 3-month 5%ers backtest of ORB v20.

Runs ORBEngineV20 on US30 + US100 + US500 + DE40 + XAUUSD with full
5%ers cost modelling (spread + commission per asset class) over the
same 3-month window used by backtest_v18.py / backtest_v19_honest.py.

Tests three configurations:
  1. BASELINE            — ATR-trail, no NR7 filter, amp_hurdle 2.5
  2. + NR7 filter        — only trade days after narrow-range days
  3. + higher amp_hurdle — only the thickest, most cost-friendly breakouts

Each run reports per-symbol breakdown, exit-reason breakdown, and the
overall PnL / PF / DD / WR.
"""
from __future__ import annotations

import csv
import sys
import time as _time
from datetime import datetime, timedelta
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


# Per-symbol ORB configs (UTC hours — winter DST)
ORB_CONFIGS = {
    # US indices: NY cash open 14:30 UTC (winter)
    "US30":   ORBConfig(or_start_hour=14, or_start_minute=30,
                        or_minutes=15, trade_window_minutes=90,
                        tp1_range_mult=1.0, tp2_range_mult=2.0),
    "US100":  ORBConfig(or_start_hour=14, or_start_minute=30,
                        or_minutes=15, trade_window_minutes=90,
                        tp1_range_mult=1.0, tp2_range_mult=2.0),
    "US500":  ORBConfig(or_start_hour=14, or_start_minute=30,
                        or_minutes=15, trade_window_minutes=90,
                        tp1_range_mult=1.0, tp2_range_mult=2.0),
    # DE40: Xetra open 08:00 UTC (winter)
    "DE40":   ORBConfig(or_start_hour=8, or_start_minute=0,
                        or_minutes=15, trade_window_minutes=90,
                        tp1_range_mult=1.0, tp2_range_mult=2.0),
    # XAUUSD: NY open spike, 15-min OR
    "XAUUSD": ORBConfig(or_start_hour=14, or_start_minute=30,
                        or_minutes=15, trade_window_minutes=90,
                        tp1_range_mult=1.0, tp2_range_mult=2.0),
}


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
                           float(row["low"]),  float(row["close"])))
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
            lasts[s]  = datetime.fromisoformat(rows[-1][0])
        except Exception:
            firsts[s] = datetime.strptime(rows[0][0], "%Y-%m-%d %H:%M:%S")
            lasts[s]  = datetime.strptime(rows[-1][0], "%Y-%m-%d %H:%M:%S")
    end = min(lasts.values())
    start = max(max(firsts.values()), end - timedelta(days=months * 31))
    return start, end


def run_config(name: str, cfg: ORBEngineConfig, specs, merged, balance: float):
    eng = ORBEngineV20(
        symbols        = specs,
        cfg            = cfg,
        orb_configs    = ORB_CONFIGS,
        initial_equity = balance,
    )
    t0 = _time.time()
    for t, s, o, h, l, c in merged:
        eng.on_bar(s, t.timestamp(), t.strftime("%Y-%m-%d"),
                   t.hour, t.minute, o, h, l, c)
    elapsed = _time.time() - t0
    summ = eng.summary()
    summ["name"] = name
    summ["elapsed_sec"] = elapsed
    return summ


def print_summary(s):
    print(f"  {s['name']:<30} | entries={s.get('entries', 0):>3}"
          f" partials={s['trades']:>4}"
          f" WR={s['win_rate']*100:>4.1f}%"
          f" PnL=${s['net_pnl']:>+9,.0f}"
          f" ({s['pct_return']:>+5.1f}%)"
          f" PF={s['pf']:>4.2f}"
          f" DD={s['max_dd_pct']:>4.2f}%"
          f" cost=${s.get('gross_commissions', 0) + s.get('gross_spread_cost', 0):>5,.0f}"
          f" ({s['elapsed_sec']:.1f}s)")


def print_breakdown(s):
    if s['trades'] == 0:
        return
    by_sym = s.get('by_symbol', {})
    print(f"    by_symbol:")
    for sym in sorted(by_sym.keys()):
        d = by_sym[sym]
        print(f"      {sym:<7} N={d['n']:>3} WR={d['wr']*100:>4.1f}% net=${d['net']:>+8,.0f}")
    by_exit = s.get('by_exit', {})
    print(f"    by_exit:  " + ", ".join(
        f"{k}={v['n']}(${v['net']:+,.0f})" for k, v in sorted(by_exit.items())
    ))


def main():
    balance = 100_000.0
    months = 3
    print("=" * 110)
    print("  ORB v20 — HONEST 3-month 5%ers backtest")
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

    # ---- Three configurations ----------------------------------------
    cfg_baseline = ORBEngineConfig(
        risk_pct=0.005,
        amp_hurdle=2.5,
        require_nr7=False,
        trail_atr_mult=0.8,
        tp1_close_frac=0.50, tp2_close_frac=0.25,
    )
    cfg_nr7 = ORBEngineConfig(
        risk_pct=0.005,
        amp_hurdle=2.5,
        require_nr7=True,  nr_lookback=7,
        trail_atr_mult=0.8,
        tp1_close_frac=0.50, tp2_close_frac=0.25,
    )
    cfg_strict = ORBEngineConfig(
        risk_pct=0.005,
        amp_hurdle=4.0,       # much higher cost-to-edge hurdle
        require_nr7=False,
        trail_atr_mult=0.8,
        tp1_close_frac=0.50, tp2_close_frac=0.25,
    )

    configs = [
        ("BASELINE (amp=2.5, no NR7)",    cfg_baseline),
        ("+ NR7 filter",                   cfg_nr7),
        ("+ strict amp hurdle (4.0)",      cfg_strict),
    ]

    print("  Running 3 configurations on same feed...\n")
    results = []
    for name, cfg in configs:
        print(f"  [{name}] ...")
        r = run_config(name, cfg, specs, merged, balance)
        results.append(r)

    # ---- Report ------------------------------------------------------
    print()
    print("=" * 150)
    print("  RESULTS")
    print("=" * 150)
    for r in results:
        print_summary(r)
        print_breakdown(r)
        print()
    print("-" * 150)

    # ---- Interpretation ---------------------------------------------
    best = max(results, key=lambda r: r['net_pnl'])
    print()
    print(f"BEST: {best['name']}")
    print(f"  N entries    = {best.get('entries', 0)}")
    print(f"  Net PnL      = ${best['net_pnl']:+,.0f}  ({best['pct_return']:+.1f}%)")
    print(f"  PF           = {best['pf']:.2f}")
    print(f"  WR           = {best['win_rate']*100:.1f}%")
    print(f"  Max DD       = {best['max_dd_pct']:.2f}%")
    print(f"  Total costs  = ${best.get('gross_commissions', 0) + best.get('gross_spread_cost', 0):,.0f}")
    print()

    # Verdict
    n = best.get('entries', 0)
    if best['net_pnl'] > 0 and best['pf'] >= 1.3 and n >= 30 and best['max_dd_pct'] < 5.0:
        verdict = "(i)  EDGE IS REAL — ORB passes honest 3-month test. Ready for portfolio build."
    elif best['net_pnl'] > 0 and best['pf'] >= 1.1 and n >= 20:
        verdict = "(ii) EDGE IS MARGINAL — positive but thin. Tune tp1_range_mult / amp_hurdle first."
    elif best['net_pnl'] > 0:
        verdict = "(iii) EDGE IS PROBABLY NOISE — positive but stats insufficient. Need more data."
    else:
        verdict = "(iv) ORB DOES NOT PAY AT 5%ERS COSTS ON THESE SYMBOLS. Pivot to FX or different edge."
    print(f"VERDICT: {verdict}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
