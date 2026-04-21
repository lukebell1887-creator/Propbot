#!/usr/bin/env python3
"""
backtest_v17_final.py  —  THE bot.  One config.  Zero flags.

v17 = v16 engine (dynamic Kelly per symbol × side + calendar blackouts)
      + FiversRiskGuard (5%ers-aware progressive brake + hard kill switch)

What it outputs:
    Results/v17_final_{balance}_{months}m.json          summary
    Results/v17_final_{balance}_{months}m_trades.json   full trade log

Usage:
    python Scripts/backtest_v17_final.py
    python Scripts/backtest_v17_final.py --months 6
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

from src.live.v15_live import load_v15_params                          # noqa: E402
from src.smartbb_engine import SMARTBB_UNIVERSE                        # noqa: E402
from src.smartbb_engine_v14 import SmartBBV14Config                    # noqa: E402
from src.smartbb_engine_v16 import SmartBBV16Engine                    # noqa: E402
from src.dynamic_sizer_v16 import DynamicSizerV16, SizerConfig         # noqa: E402
from src.trading_calendar import TradingCalendar                       # noqa: E402
from src.fivers_risk_guard import FiversRiskGuard                      # noqa: E402

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


def run(balance, months):
    print("=" * 78)
    print(f"  v17 FINAL  —  the single blessed config")
    print(f"  ${balance:,.0f} account  |  {months}-month OOS window")
    print(f"  PhD dynamic Kelly per (symbol × side) × 5%ers DD brake")
    print(f"  no ablation flags.  no dials.  this is what goes live.")
    print("=" * 78)

    # ── Load data (same feed the v15/v16 backtests use) ──
    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in TIER1}
    files = {s: p for s, p in files.items() if p.exists()}
    if not files:
        print("ERROR: no {SYMBOL}_M1.csv files in data/historical/")
        return 1
    print(f"  Symbols: {', '.join(sorted(files))}")


    tmin, tmax = common_window(files, months)
    print(f"  OOS window: {tmin.date()} → {tmax.date()}")

    # ── Engine + v15 tuned params + the three PhD layers ──
    tuning_path = ROOT / "Results" / "v15_ultimate_tuning.json"
    params = load_v15_params(str(tuning_path)) if tuning_path.exists() else None
    cfg    = SmartBBV14Config()

    sizer = DynamicSizerV16(cfg=SizerConfig(
        kelly_fractional     = 0.25,    # quarter-Kelly on TOP of per-symbol history
        target_ann_vol       = 0.15,
        min_risk_pct         = 0.001,   # 0.1 % absolute floor
        max_risk_pct         = 0.015,   # 1.5 % absolute ceiling per trade
        cold_start_risk_pct  = 0.0025,  # 0.25 % for symbols with <20 closed trades
        min_trades_for_kelly = 20,
    ))
    calendar = TradingCalendar()
    guard = FiversRiskGuard(
        start_equity=balance,
        # All thresholds hard-coded for 5%ers MTB $100k rules:
        #   daily cap = $4,000 (4 %)
        #   total cap = $10,000 (10 %)
        #   daily soft brake @ 50 % / hard stop @ 75 %
        #   total soft brake @ 50 % / hard stop @ 70 %
    )

    internal = sorted(files)
    specs    = [SMARTBB_UNIVERSE[s] for s in internal]
    eng = SmartBBV16Engine(
        symbols       = specs,
        params        = params,
        cfg           = cfg,
        initial_equity= balance,
        sizer         = sizer,
        calendar      = calendar,
        fivers_guard  = guard,
        use_dynamic_sizing = True,
        use_calendar       = True,
    )

    # ── Replay M1 bars (chronological merge across all symbols) ──
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


    # ── Summary ──
    summary = eng.summary()
    summary["start_equity"] = balance
    summary["elapsed_sec"]  = round(elapsed, 1)

    # Final guard state (use the last bar's wall-clock time)
    from datetime import timezone as _tz
    last_ts = merged[-1][0] if merged else datetime.utcnow()
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=_tz.utc)
    gs = guard.multiplier(eng.equity, now_utc=last_ts)

    summary["guard_state"] = {
        "today_dd_usd": round(gs.today_dd_usd, 2),
        "total_dd_usd": round(gs.total_dd_usd, 2),
        "phase":        gs.phase,
        "multiplier":   round(gs.multiplier, 3),
        "halted_today":        gs.halted_today,
        "halted_permanently":  gs.halted_permanently,
    }

    # Per-symbol×side trade breakdown (the actual per-ticker risk picture)
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

    # ── Save ──
    out_dir = ROOT / "Results"; out_dir.mkdir(exist_ok=True)
    summ_path   = out_dir / f"v17_final_{int(balance)}_{months}m.json"
    trades_path = out_dir / f"v17_final_{int(balance)}_{months}m_trades.json"
    with open(summ_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    # Universal serialisation — accepts any dataclass shape
    trades_out = [asdict(tr) for tr in eng.trades]

    with open(trades_path, "w") as f:
        json.dump(trades_out, f, indent=2, default=str)

    # ── Print results ──
    print()
    print("=" * 78)
    print("  RESULTS  (v17 FINAL)")
    print("=" * 78)
    print(f"  Final equity      ${summary.get('equity', balance):>12,.2f}")
    print(f"  Net P&L           ${summary.get('net_pnl', 0):>+12,.2f}")
    print(f"  Return            {summary.get('pct_return', 0):>+12.2f} %")
    print(f"  Trades            {summary.get('trades', 0):>12,}")
    print(f"  Profit factor     {summary.get('pf', 0):>12.2f}")
    print(f"  Win rate          {summary.get('win_rate', 0)*100:>12.1f} %")
    print(f"  Expectancy        {summary.get('expectancy_R', 0):>+12.3f} R")
    print(f"  Max DD            {summary.get('max_dd_pct', 0):>12.2f} %")

    print()
    print("  5%ers GUARD (end-of-backtest snapshot):")
    print(f"    today DD   ${summary['guard_state']['today_dd_usd']:>8,.0f} "
          f"(cap $4,000)")
    print(f"    total DD   ${summary['guard_state']['total_dd_usd']:>8,.0f} "
          f"(cap $10,000)")
    print(f"    phase      {summary['guard_state']['phase']}")
    print(f"    halted?    today={summary['guard_state']['halted_today']}   "
          f"forever={summary['guard_state']['halted_permanently']}")
    print()
    print("  PER-SYMBOL × SIDE TRADE PROFILE:")
    print(f"    {'Bucket':<20} {'N':>6} {'avg_R':>8} {'win%':>7}")
    for k, v in sorted(summary["per_symbol_side"].items(), key=lambda x: -x[1]["n"]):
        print(f"    {k:<20} {v['n']:>6} {v['avg_R']:>+8.3f} {v['win_pct']:>6.1f}")
    print()
    if summary.get("v16", {}).get("risk_pct_mean"):
        v = summary["v16"]
        print(f"  Dynamic sizing telemetry (mean over {v['n_risk_breakdowns_sampled']} entries):")
        print(f"    risk_pct  mean={v['risk_pct_mean']*100:.3f}%  "
              f"min={v['risk_pct_min']*100:.3f}%  max={v['risk_pct_max']*100:.3f}%")
    if summary.get("v16", {}).get("blackout_counts"):
        print(f"\n  Blackouts (calendar + guard):")
        for k, v in sorted(summary["v16"]["blackout_counts"].items(), key=lambda x: -x[1]):
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
    a = ap.parse_args()
    sys.exit(run(a.balance, a.months))
