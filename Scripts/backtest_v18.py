#!/usr/bin/env python3
"""
backtest_v18.py  —  Grossman-Zhou dynamic sizing, genuine PhD stack

Runs the v15 signals with the v18 sizer (GZ × Bayesian shrinkage ×
conviction × safety-only 5%ers guard × 2 % hard cap) on the same 5%ers
3-month OOS feed.

Output:
    Results/v18_{balance}_{months}m.json          summary + telemetry
    Results/v18_{balance}_{months}m_trades.json   full trade log
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time as _time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.live.v15_live import load_v15_params                            # noqa: E402
from src.smartbb_engine import SMARTBB_UNIVERSE                          # noqa: E402
from src.smartbb_engine_v14 import SmartBBV14Config                      # noqa: E402
from src.smartbb_engine_v18 import SmartBBV18Engine                      # noqa: E402
from src.dynamic_sizer_v18 import DynamicSizerV18, SizerV18Config        # noqa: E402
from src.trading_calendar import TradingCalendar                         # noqa: E402

TIER1 = ["US30", "US100", "US500", "DE40", "XAUUSD"]


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
        try:
            firsts[s] = datetime.fromisoformat(rows[0][0])
            lasts[s]  = datetime.fromisoformat(rows[-1][0])
        except Exception:
            firsts[s] = datetime.strptime(rows[0][0], "%Y-%m-%d %H:%M:%S")
            lasts[s]  = datetime.strptime(rows[-1][0], "%Y-%m-%d %H:%M:%S")
    end   = min(lasts.values())
    start = max(max(firsts.values()), end - timedelta(days=months * 31))
    return start, end


def warmup_sizer(sizer: DynamicSizerV18, warmup_path: Path) -> int:
    """Seed the sizer's per-bucket R history from an existing trades JSON."""
    if not warmup_path.exists():
        return 0
    try:
        trades = json.loads(warmup_path.read_text())
    except Exception:
        return 0
    n = 0
    for t in trades:
        sym = t.get("symbol")
        side = t.get("side")
        R = t.get("realised_R")
        if sym is None or side is None or R is None:
            continue
        sizer.record_trade(sym, int(side), float(R))
        n += 1
    return n


def run(balance: float, months: int, warmup: bool):
    print("=" * 92)
    print(f"  v18  —  GROSSMAN-ZHOU dynamic sizing  (genuine PhD stack)")
    print(f"  ${balance:,.0f} account  |  {months}-month OOS window")
    print(f"  per (symbol × side) Grossman-Zhou × Bayesian shrinkage × conviction")
    print(f"  SAFETY-ONLY 5%ers guard (no pre-emptive haircuts)  ×  2 % hard cap")
    print("=" * 92)

    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in TIER1}
    files = {s: p for s, p in files.items() if p.exists()}
    if not files:
        print("ERROR: no {SYMBOL}_M1.csv files in data/historical/")
        return 1

    tmin, tmax = common_window(files, months)
    print(f"  Symbols: {', '.join(sorted(files))}")
    print(f"  OOS window: {tmin.date()} → {tmax.date()}")

    tuning_path = ROOT / "Results" / "v15_ultimate_tuning.json"
    params = load_v15_params(str(tuning_path)) if tuning_path.exists() else None
    cfg    = SmartBBV14Config()

    sizer_cfg = SizerV18Config(
        alpha_cap              = 0.10,
        kelly_fractional       = 0.50,
        min_trades_for_bucket  = 20,
        min_risk_pct           = 0.0020,   # 0.20 %
        max_risk_pct           = 0.0200,   # 2.00 %
        cold_start_risk_pct    = 0.0050,   # 0.50 %
        daily_cap_usd          = 4_000.0,
        total_cap_usd          = 10_000.0,
        daily_safety_frac      = 0.75,
        total_safety_frac      = 0.70,
        kill_losing_buckets    = True,
    )
    sizer = DynamicSizerV18(cfg=sizer_cfg)

    warmup_n = 0
    if warmup:
        w = ROOT / "Results" / "v17_final_100000_3m_trades.json"
        warmup_n = warmup_sizer(sizer, w)
        print(f"  Kelly warm-up: {warmup_n} historical R-values seeded from "
              f"{w.name}")

    calendar = TradingCalendar()

    internal = sorted(files)
    specs    = [SMARTBB_UNIVERSE[s] for s in internal]
    eng = SmartBBV18Engine(
        symbols       = specs,
        params        = params,
        cfg           = cfg,
        initial_equity= balance,
        sizer         = sizer,
        calendar      = calendar,
        use_calendar  = True,
    )

    streams = {s: load_m1(files[s], tmin, tmax) for s in internal}
    merged  = []
    for s, bars in streams.items():
        merged.extend((t, s, o, h, l, c) for (t, o, h, l, c) in bars)
    merged.sort(key=lambda r: r[0])
    print(f"  Total M1 bars: {len(merged):,}")

    t0 = _time.time()
    for t, s, o, h, l, c in merged:
        eng.on_bar(s, t.timestamp(), t.strftime("%Y-%m-%d"),
                   t.hour, t.minute, o, h, l, c)
    elapsed = _time.time() - t0

    summary = eng.summary()
    summary["start_equity"]  = balance
    summary["elapsed_sec"]   = round(elapsed, 1)
    summary["warmup_trades"] = warmup_n

    # Per-symbol × side trade breakdown
    per_key: dict = {}
    for tr in eng.trades:
        k = f"{tr.symbol}_{'long' if tr.side>0 else 'short'}"
        d = per_key.setdefault(k, {"n":0,"sumR":0.0,"wins":0})
        d["n"] += 1
        d["sumR"] += tr.realised_R
        if tr.realised_R > 0: d["wins"] += 1
    summary["per_symbol_side"] = {
        k: {"n": v["n"],
            "avg_R":   round(v["sumR"]/v["n"], 3) if v["n"] else 0,
            "win_pct": round(100*v["wins"]/v["n"], 1) if v["n"] else 0}
        for k,v in per_key.items()
    }

    out_dir = ROOT / "Results"; out_dir.mkdir(exist_ok=True)
    summ_path   = out_dir / f"v18_{int(balance)}_{months}m.json"
    trades_path = out_dir / f"v18_{int(balance)}_{months}m_trades.json"
    with open(summ_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    trades_out = [asdict(tr) for tr in eng.trades]
    with open(trades_path, "w") as f:
        json.dump(trades_out, f, indent=2, default=str)

    # ── Printed report ───────────────────────────────────────────────────
    print()
    print("=" * 92)
    print("  RESULTS  (v18 — Grossman-Zhou)")
    print("=" * 92)
    print(f"  Final equity      ${summary.get('equity', balance):>13,.2f}")
    print(f"  Net P&L           ${summary.get('net_pnl', 0):>+13,.2f}")
    print(f"  Return            {summary.get('pct_return', 0):>+13.2f} %")
    print(f"  Trades            {summary.get('trades', 0):>13,}")
    print(f"  Profit factor     {summary.get('pf', 0):>13.2f}")
    print(f"  Win rate          {summary.get('win_rate', 0)*100:>13.1f} %")
    print(f"  Expectancy        {summary.get('expectancy_R', 0):>+13.3f} R")
    print(f"  Max DD            {summary.get('max_dd_pct', 0):>13.2f} %")

    v18 = summary.get("v18", {})
    if v18.get("risk_pct_mean") is not None:
        print()
        print("  DYNAMIC SIZING — Grossman-Zhou pipeline (mean over "
              f"{v18.get('n_risk_breakdowns_sampled', 0)} entries):")
        print(f"    f_base (G-Z fraction)    mean = {v18['f_base_mean']*100:.3f} %")
        print(f"    shrinkage               mean = {v18['shrink_mean']:.3f}")
        print(f"    conviction              mean = {v18['conviction_mean']:.3f}")
        print(f"    safety-guard mult       mean = {v18['guard_mean']:.3f}")
        print(f"    FINAL risk_pct          mean = {v18['risk_pct_mean']*100:.3f} %"
              f"  (min {v18['risk_pct_min']*100:.3f} % / "
              f"max {v18['risk_pct_max']*100:.3f} %)")
        print()
        print("  Source of f_base:")
        for src, n in sorted(v18.get("source_counts", {}).items(),
                              key=lambda x: -x[1]):
            print(f"    {src:<25} {n}")

    print()
    print("  PER-SYMBOL × SIDE TRADE PROFILE:")
    print(f"    {'Bucket':<20} {'N':>6} {'avg_R':>8} {'win%':>7}")
    for k, v in sorted(summary["per_symbol_side"].items(), key=lambda x: -x[1]["n"]):
        print(f"    {k:<20} {v['n']:>6} {v['avg_R']:>+8.3f} {v['win_pct']:>6.1f}")

    if v18.get("blackout_counts"):
        print()
        print("  Blackouts (calendar + guard):")
        for k, v in sorted(v18["blackout_counts"].items(),
                            key=lambda x: -x[1]):
            print(f"    {k:<32} {v:>6}")

    print()
    print(f"  elapsed {elapsed:.1f}s")
    print(f"  summary -> {summ_path}")
    print(f"  trades  -> {trades_path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=100_000)
    ap.add_argument("--months",  type=int,   default=3)
    ap.add_argument("--no-warmup", action="store_true",
                    help="skip Kelly history seeding (cold-start everything)")
    a = ap.parse_args()
    sys.exit(run(a.balance, a.months, warmup=not a.no_warmup))
