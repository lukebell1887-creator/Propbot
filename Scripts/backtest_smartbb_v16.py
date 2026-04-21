#!/usr/bin/env python3
"""
SmartBB v16 - TRUE OOS 3-month backtest with Dynamic Kelly + Calendar.

Runs on the SAME fresh 5%ers M1 data as the v15 OOS backtest, uses the
SAME per-symbol tuned thresholds (v15_ultimate_tuning.json), but sizes
every trade with DynamicSizerV16 and gates entries with TradingCalendar.

Output files:
    Results/v16_SC_{balance}_{months}m.json          summary (sizer + calendar ON)
    Results/v16_SC_{balance}_{months}m_trades.json   full trade log
    Results/v16_compare_{balance}_{months}m.md       v15 vs v16 side-by-side MD

Ablation flags:
    --no-sizer      disable dynamic Kelly (back to v14 fixed sizing)
    --no-calendar   disable blackout windows

Usage:
    python Scripts/backtest_smartbb_v16.py
    python Scripts/backtest_smartbb_v16.py --balance 100000 --months 3
    python Scripts/backtest_smartbb_v16.py --no-calendar     (ablation)
    python Scripts/backtest_smartbb_v16.py --no-sizer        (ablation)
"""
from __future__ import annotations

import argparse
import csv
import json
import random
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

from src.live.v15_live import load_v15_params                       # noqa: E402
from src.smartbb_engine import SMARTBB_UNIVERSE                     # noqa: E402
from src.smartbb_engine_v14 import SmartBBV14Config                 # noqa: E402
from src.smartbb_engine_v16 import SmartBBV16Engine                 # noqa: E402
from src.dynamic_sizer_v16 import DynamicSizerV16, SizerConfig      # noqa: E402
from src.trading_calendar import TradingCalendar                    # noqa: E402

TIER1 = ["US30", "US100", "US500", "DE40", "XAUUSD"]


# ---------------------------------------------------------------------
#  CSV + window helpers (copied from v15 backtest for identical semantics)
# ---------------------------------------------------------------------
def load_m1(path: Path, tmin: datetime, tmax: datetime):
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
            out.append((t,
                         float(row["open"]),  float(row["high"]),
                         float(row["low"]),   float(row["close"])))
    return out


def common_window(files: dict[str, Path], months: int):
    firsts, lasts = {}, {}
    for s, p in files.items():
        with open(p, "r", newline="") as f:
            rdr = csv.reader(f); next(rdr)
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


# ---------------------------------------------------------------------
#  Bootstrap (same as v15)
# ---------------------------------------------------------------------
def bootstrap_portfolio(trades, n_iters=10_000, seed=42):
    if not trades:
        return {"n_iters": 0}
    rng = random.Random(seed)
    pnls = [float(t["net_pnl"]) for t in trades]
    n = len(pnls)
    nets, pfs, dds = [], [], []
    for _ in range(n_iters):
        sample = [pnls[rng.randrange(n)] for _ in range(n)]
        net = sum(sample)
        gw = sum(x for x in sample if x > 0)
        gl = -sum(x for x in sample if x <= 0)
        pf = gw / gl if gl > 0 else (float("inf") if gw > 0 else 0.0)
        peak = cum = dd = 0.0
        for x in sample:
            cum += x
            peak = max(peak, cum)
            if peak - cum > dd:
                dd = peak - cum
        nets.append(net); pfs.append(pf); dds.append(dd)
    nets.sort(); pfs.sort(); dds.sort()
    pfs_cap = [min(x, 1e6) for x in pfs]

    def q(xs, p):
        return xs[int(p * (len(xs) - 1))]

    return {
        "median_net": q(nets, 0.50),
        "p05_net":    q(nets, 0.05),
        "p95_net":    q(nets, 0.95),
        "median_pf":  q(pfs_cap, 0.50),
        "p05_pf":     q(pfs_cap, 0.05),
        "p95_pf":     q(pfs_cap, 0.95),
        "median_dd":  q(dds, 0.50),
        "p95_dd":     q(dds, 0.95),
        "n_iters":    n_iters,
    }


# ---------------------------------------------------------------------
#  Runner
# ---------------------------------------------------------------------
def run(balance, months, tuning_path, out_dir, bootstrap_iters,
        use_sizer=True, use_calendar=True,
        kelly_frac=0.25, dd_max=0.06, target_vol=0.15,
        min_risk_pct=0.001, max_risk_pct=0.010,
        cold_start=0.0025, min_trades_kelly=20):

    print("=" * 80)
    tag = (("S" if use_sizer else "s") + ("C" if use_calendar else "c"))
    print(f"  v16 backtest [{tag}]  |  ${balance:,.0f}  |  {months}-month OOS window")
    print(f"  dynamic_sizing = {use_sizer}   calendar = {use_calendar}")
    if use_sizer:
        print(f"  kelly_frac = {kelly_frac:.2f}   dd_max = {dd_max:.2f}   "
              f"target_vol = {target_vol:.2f}")
        print(f"  risk bounds = [{min_risk_pct*100:.2f} %, {max_risk_pct*100:.2f} %]")
    print("=" * 80)

    # 1. Per-symbol params (v15 tuning)
    params = load_v15_params(tuning_path, tier="TIER1", symbols=TIER1)
    if not params:
        raise SystemExit(f"no TIER1 params in {tuning_path}")
    symbols = list(params.keys())
    print(f"  Tier-1 symbols : {symbols}")
    for sym, p in params.items():
        print(f"    {sym:<8s} z_q={p.z_quantile:.2f} "
              f"[{p.z_min_abs:.1f},{p.z_max_abs:.1f}] "
              f"stop={p.stop_atr_mult:.2f} tp={p.tp_frac:.2f} "
              f"hurst_q={p.hurst_quantile:.2f}<{p.hurst_max_abs:.2f} "
              f"ou_hl<{p.ou_max_halflife:.0f}")

    # 2. CSVs
    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in symbols}
    missing = [s for s, p in files.items() if not p.exists()]
    if missing:
        raise SystemExit(
            f"missing CSVs: {missing} - run "
            f"`python Scripts/download_5ers_3month.py` first")
    start, end = common_window(files, months)
    print(f"\n  Window         :  {start}  ->  {end}  "
          f"({(end - start).days} days)")

    # 3. Load bars
    bars = {s: load_m1(p, start, end) for s, p in files.items()}
    total = sum(len(b) for b in bars.values())
    for s, b in bars.items():
        if b:
            print(f"    {s:<8s} {len(b):>7,} bars")
    print(f"  Total          :  {total:,} M1 bars")

    # 4. Build engine
    cfg = SmartBBV14Config()
    syms = [SMARTBB_UNIVERSE[s] for s in symbols]

    sizer_cfg = SizerConfig(
        kelly_fractional=kelly_frac,
        dd_max=dd_max,
        target_ann_vol=target_vol,
        min_risk_pct=min_risk_pct,
        max_risk_pct=max_risk_pct,
        cold_start_risk_pct=cold_start,
        min_trades_for_kelly=min_trades_kelly,
    )
    sizer = DynamicSizerV16(cfg=sizer_cfg)
    calendar = TradingCalendar()

    eng = SmartBBV16Engine(
        symbols=syms, params=params, cfg=cfg,
        initial_equity=balance,
        sizer=sizer, calendar=calendar,
        use_dynamic_sizing=use_sizer,
        use_calendar=use_calendar,
    )
    print(f"\n  Engine         :  v16 (sizer={use_sizer}, calendar={use_calendar})\n")

    # 5. Merge + stream
    merged = []
    for s, b in bars.items():
        for (t, o, h, l, c) in b:
            merged.append((t, s, o, h, l, c))
    merged.sort(key=lambda x: x[0])

    t0 = _time.time()
    for (t, sym, o, h, l, c) in merged:
        eng.on_bar(sym, t.timestamp(), t.strftime("%Y-%m-%d"),
                   t.hour, t.minute, o, h, l, c)
        if eng.halted_permanently:
            break
    elapsed = _time.time() - t0
    print(f"  Processed {len(merged):,} bars in {elapsed:.1f}s "
          f"({len(merged)/max(elapsed,1e-3):,.0f} bars/sec)\n")

    # 6. Summary
    s = eng.summary()
    s["start_equity"] = balance
    s["halted_permanently"] = eng.halted_permanently
    s["tier1_symbols"] = symbols
    s["tuning_source"] = str(tuning_path)
    s["data_window"] = {"start": str(start), "end": str(end),
                        "days": (end - start).days}
    trade_dicts = [asdict(t) for t in eng.trades]
    s["bootstrap"] = bootstrap_portfolio(trade_dicts, n_iters=bootstrap_iters)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"v16_{tag}_{int(balance)}_{months}m"
    with open(out_dir / f"{stem}.json", "w") as f:
        json.dump(s, f, indent=2, default=str)
    eng.dump_trades(str(out_dir / f"{stem}_trades.json"))

    _print(s, balance, months, tag)
    _compare_vs_v15(s, balance, months, out_dir, tag)
    return s


# ---------------------------------------------------------------------
#  Printing
# ---------------------------------------------------------------------
def _print(s, balance, months, tag):
    print("=" * 80)
    print(f"  v16 [{tag}] RESULT  |  5%ers MTB  |  ${balance:,.0f}  |  "
          f"{months}-month window")
    print("=" * 80)
    print(f"  Final equity      ${s.get('equity', balance):>12,.2f}")
    print(f"  Net P&L           ${s.get('net_pnl', 0):>12,.2f}")
    print(f"  Return            {s.get('pct_return', 0):>12.2f} %")
    print(f"  Trades            {s.get('trades', 0):>12,}")
    if s.get("trades", 0):
        print(f"  Win rate          {s['win_rate']*100:>12.1f} %")
        print(f"  Profit factor     {s['pf']:>12.2f}")
        print(f"  Expectancy (R)    {s['expectancy_R']:>12.3f}")
        print(f"  Avg winner (R)    {s['avg_winner_R']:>12.2f}")
        print(f"  Avg loser  (R)    {s['avg_loser_R']:>12.2f}")
        print(f"  Avg bars held     {s['avg_bars_held']:>12.1f}  (M5)")
        print(f"  Max DD            {s['max_dd_pct']:>12.2f} %")
        print(f"  Gross commissions ${s['gross_commissions']:>12,.2f}")
        print(f"  Gross spread cost ${s['gross_spread_cost']:>12,.2f}")

        v16 = s.get("v16", {})
        bc = v16.get("blackout_counts", {})
        if bc:
            print("\n  Blackouts (new entries skipped):")
            for r, n in sorted(bc.items(), key=lambda x: -x[1]):
                print(f"    {r:<12}  {n:>6}")
        if "risk_pct_mean" in v16:
            print(f"\n  Dynamic risk profile (mean across {v16['n_risk_breakdowns_sampled']} entries):")
            print(f"    risk_pct     mean={v16['risk_pct_mean']*100:.3f}%  "
                  f"min={v16['risk_pct_min']*100:.3f}%  "
                  f"max={v16['risk_pct_max']*100:.3f}%")
            print(f"    kelly_f      mean={v16['kelly_mean']*100:.3f}%")
            print(f"    dd_mult      mean={v16['dd_mean']:.3f}")
            print(f"    vol_mult     mean={v16['vol_mean']:.3f}")
            print(f"    regime_mult  mean={v16['regime_mean']:.3f}")

        print("\n  By symbol:")
        print(f"    {'symbol':<8} {'n':>4} {'WR':>7} {'expR':>8} "
              f"{'bars':>6} {'net$':>12}")
        for sym, d in sorted(s["by_symbol"].items(),
                               key=lambda x: -x[1]["net"]):
            print(f"    {sym:<8} {d['n']:>4} {d['wr']*100:>6.1f}% "
                  f"{d['expR']:>+8.3f} {d['avg_bars']:>6.1f} "
                  f"{d['net']:>+12,.2f}")

        print("\n  By exit reason:")
        for r, c in sorted(s["by_exit_reason"].items(),
                             key=lambda x: -x[1]):
            print(f"    {r:<22} {c:>4}")

        boot = s.get("bootstrap", {})
        if boot.get("n_iters", 0):
            print("\n  Bootstrap (10k resamples):")
            print(f"    Net    median=${boot['median_net']:>10,.0f}  "
                  f"p05=${boot['p05_net']:>10,.0f}  "
                  f"p95=${boot['p95_net']:>10,.0f}")
            print(f"    PF     median={boot['median_pf']:>9.2f}  "
                  f"p05={boot['p05_pf']:>9.2f}  "
                  f"p95={boot['p95_pf']:>9.2f}")
            print(f"    DD$    p95=${boot['p95_dd']:>10,.0f}")

    print("\n  OOS acceptance gates:")
    boot = s.get("bootstrap", {})
    def tick(ok): return "PASS" if ok else "FAIL"
    print(f"    Net P&L > 0            : {tick(s.get('net_pnl', 0) > 0)}")
    print(f"    PF >= 1.5              : {tick(s.get('pf', 0) >= 1.5)}")
    print(f"    Max DD < 4% (daily)    : {tick(s.get('max_dd_pct', 100) < 4.0)}")
    print(f"    Bootstrap p05 net > 0  : {tick(boot.get('p05_net', 0) > 0)}")
    print(f"    Bootstrap p05 PF > 1.0 : {tick(boot.get('p05_pf', 0) > 1.0)}")
    if s.get("halted_permanently"):
        print("\n    *** ENGINE HALTED PERMANENTLY ***")
    print("=" * 80)


def _compare_vs_v15(s16, balance, months, out_dir, tag):
    v15_path = out_dir / f"v15_oos_{int(balance)}_{months}m.json"
    if not v15_path.exists():
        print(f"\n  (v15 comparison file not found: {v15_path})")
        return
    with open(v15_path) as f:
        s15 = json.load(f)

    lines = []
    lines.append(f"# v16[{tag}] vs v15 OOS COMPARISON")
    lines.append(f"Account: ${balance:,.0f}   Window: {months} months")
    lines.append("")
    lines.append("| metric | v15 | v16 | delta |")
    lines.append("|---|---:|---:|---:|")

    def row(label, k, pct=False, fmt=",.2f"):
        a = s15.get(k, 0) or 0
        b = s16.get(k, 0) or 0
        d = b - a
        if pct:
            return (f"| {label} | {a:.2f} % | {b:.2f} % | "
                    f"{d:+.2f} % |")
        return (f"| {label} | {a:{fmt}} | {b:{fmt}} | {d:+{fmt}} |")

    lines.append(row("Trades",        "trades", fmt=",d"))
    lines.append(row("Net P&L ($)",   "net_pnl"))
    lines.append(row("Return (%)",    "pct_return", pct=True))
    lines.append(row("Profit factor", "pf", fmt=".2f"))
    lines.append(row("Win rate",      "win_rate", fmt=".3f"))
    lines.append(row("Expectancy R",  "expectancy_R", fmt=".3f"))
    lines.append(row("Max DD (%)",    "max_dd_pct", pct=True))

    md = "\n".join(lines) + "\n"
    out_path = out_dir / f"v16_{tag}_vs_v15_{int(balance)}_{months}m.md"
    with open(out_path, "w") as f:
        f.write(md)

    # Console print
    print("\n" + "=" * 80)
    print(f"  v16[{tag}] vs v15 OOS COMPARISON   (${balance:,.0f}, {months}m)")
    print("=" * 80)
    print(f"                   {'v15':>14}   {'v16':>14}   {'delta':>14}")
    def prow(label, k, pct=False):
        a = s15.get(k, 0) or 0
        b = s16.get(k, 0) or 0
        d = b - a
        if pct:
            print(f"  {label:<16} {a:>12.2f} %   {b:>12.2f} %   "
                  f"{d:>+12.2f} %")
        else:
            print(f"  {label:<16} {a:>14,.2f}   {b:>14,.2f}   "
                  f"{d:>+14,.2f}")
    prow("Trades        ", "trades")
    prow("Net P&L       ", "net_pnl")
    prow("Return (%)    ", "pct_return", pct=True)
    prow("PF            ", "pf")
    prow("Win rate      ", "win_rate")
    prow("Expectancy R  ", "expectancy_R")
    prow("Max DD (%)    ", "max_dd_pct", pct=True)
    print("=" * 80)
    print(f"  Diff written to: {out_path.relative_to(ROOT)}")


# ---------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance",     type=float, default=100_000.0)
    ap.add_argument("--months",      type=int, default=3)
    ap.add_argument("--tuning",      type=Path,
                     default=ROOT / "Results" / "v15_ultimate_tuning.json")
    ap.add_argument("--out",         type=Path, default=ROOT / "Results")
    ap.add_argument("--bootstrap-iters", type=int, default=10_000)
    ap.add_argument("--no-sizer",    action="store_true")
    ap.add_argument("--no-calendar", action="store_true")
    ap.add_argument("--kelly-frac",  type=float, default=0.25)
    ap.add_argument("--dd-max",      type=float, default=0.06)
    ap.add_argument("--target-vol",  type=float, default=0.15)
    ap.add_argument("--min-risk",    type=float, default=0.001)
    ap.add_argument("--max-risk",    type=float, default=0.010)
    ap.add_argument("--cold-start",  type=float, default=0.0025,
                     help="risk %% per trade before Kelly has enough data")
    ap.add_argument("--min-trades-kelly", type=int, default=20,
                     help="trades per (symbol,side) before Kelly activates")
    a = ap.parse_args()
    run(a.balance, a.months, a.tuning, a.out, a.bootstrap_iters,
        use_sizer=not a.no_sizer,
        use_calendar=not a.no_calendar,
        kelly_frac=a.kelly_frac, dd_max=a.dd_max, target_vol=a.target_vol,
        min_risk_pct=a.min_risk, max_risk_pct=a.max_risk,
        cold_start=a.cold_start, min_trades_kelly=a.min_trades_kelly)


if __name__ == "__main__":
    main()
