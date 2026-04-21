#!/usr/bin/env python3
"""
SmartBB v15 — TRUE OUT-OF-SAMPLE 3-month backtest on fresh 5%ers data.

What's new vs `backtest_smartbb_v14.py`:
  * Uses the v15 Tier-1 per-symbol params  (`Results/v15_ultimate_tuning.json`)
  * Runs on the freshly-downloaded 5%ers M1 data (2026-01-19 -> 2026-04-21)
  * Tier-1 universe: US30 / US100 / US500 / DE40 / XAUUSD
  * Writes `Results/v15_oos_3month.{json,_trades.json}`

The v15 optimiser was trained on data ending mid-Feb 2026.  Roughly **60 %**
of this window is post-training (late-Feb → now) so this is a proper OOS
robustness check.

Usage:
    python Scripts\\backtest_v15_oos_3month.py
    python Scripts\\backtest_v15_oos_3month.py --balance 100000 --months 3
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

# Force UTF-8 stdout on Windows cp1252 consoles
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.live.v15_live import load_v15_params                   # noqa: E402
from src.smartbb_engine import SMARTBB_UNIVERSE                 # noqa: E402
from src.smartbb_engine_v14 import (                            # noqa: E402
    SmartBBV14Config, SmartBBV14Engine,
)

TIER1 = ["US30", "US100", "US500", "DE40", "XAUUSD"]


# ----------------------------------------------------------------------
#  Data loading (copied from v14 backtest, like-for-like)
# ----------------------------------------------------------------------
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
            out.append((t, float(row["open"]), float(row["high"]),
                        float(row["low"]), float(row["close"])))
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


# ----------------------------------------------------------------------
#  Bootstrap (same impl as v14)
# ----------------------------------------------------------------------
def bootstrap_portfolio(trades: list[dict], n_iters: int = 10_000,
                          seed: int = 42) -> dict:
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
        "p05_net": q(nets, 0.05),
        "p95_net": q(nets, 0.95),
        "median_pf": q(pfs_cap, 0.50),
        "p05_pf":    q(pfs_cap, 0.05),
        "p95_pf":    q(pfs_cap, 0.95),
        "median_dd": q(dds, 0.50),
        "p95_dd":    q(dds, 0.95),
        "n_iters":   n_iters,
    }


# ----------------------------------------------------------------------
#  Runner
# ----------------------------------------------------------------------
def run(balance: float, months: int, tuning_path: Path, out_dir: Path,
        bootstrap_iters: int):
    print("=" * 80)
    print(f"  v15 OUT-OF-SAMPLE backtest  |  ${balance:,.0f}  |  {months}-month window")
    print("=" * 80)

    # 1. Per-symbol v15 params
    params = load_v15_params(tuning_path, tier="TIER1", symbols=TIER1)
    if not params:
        raise SystemExit(f"No TIER1 params found in {tuning_path}")
    symbols = list(params.keys())
    print(f"  Tier-1 symbols     : {symbols}")
    for sym, p in params.items():
        print(f"    {sym:<8s} z_q={p.z_quantile:.2f} "
              f"[{p.z_min_abs:.1f},{p.z_max_abs:.1f}] "
              f"stop={p.stop_atr_mult:.2f} tp={p.tp_frac:.2f} "
              f"hurst_q={p.hurst_quantile:.2f}<{p.hurst_max_abs:.2f} "
              f"ou_hl<{p.ou_max_halflife:.0f} risk_x={p.risk_multiplier:.2f}")

    # 2. Find fresh CSVs
    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in symbols}
    missing = [s for s, p in files.items() if not p.exists()]
    if missing:
        raise SystemExit(f"missing CSVs: {missing} — run "
                          f"`python Scripts\\download_5ers_3month.py` first")

    start, end = common_window(files, months)
    print(f"\n  Window             : {start}  ->  {end}  "
          f"({(end - start).days} days)")

    # 3. Load bars
    bars = {s: load_m1(p, start, end) for s, p in files.items()}
    total = sum(len(b) for b in bars.values())
    for s, b in bars.items():
        if b:
            print(f"    {s:<8s} {len(b):>7,} bars  "
                  f"({b[0][0]} -> {b[-1][0]})")
        else:
            print(f"    {s:<8s}     0 bars  -- EMPTY!")
    print(f"  Total loaded       : {total:,} bars")

    # 4. Build engine
    cfg = SmartBBV14Config()
    syms = [SMARTBB_UNIVERSE[s] for s in symbols]
    eng = SmartBBV14Engine(symbols=syms, params=params, cfg=cfg,
                            initial_equity=balance)
    print(f"\n  Engine             : v14 core + v15 per-symbol params")
    print(f"  Initial equity     : ${balance:,.2f}")
    print(f"  Risk base          : {cfg.base_risk_pct*100:.2f}% per trade\n")

    # 5. Merge and stream
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

    # 6. Summarise
    s = eng.summary()
    s["start_equity"] = balance
    s["halted_permanently"] = eng.halted_permanently
    s["tier1_symbols"] = symbols
    s["tuning_source"] = str(tuning_path)
    s["data_window"] = {"start": str(start), "end": str(end),
                        "days": (end - start).days}

    trade_dicts = [asdict(t) for t in eng.trades]
    s["bootstrap"] = bootstrap_portfolio(trade_dicts,
                                           n_iters=bootstrap_iters)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"v15_oos_{int(balance)}_{months}m"
    with open(out_dir / f"{stem}.json", "w") as f:
        json.dump(s, f, indent=2, default=str)
    eng.dump_trades(str(out_dir / f"{stem}_trades.json"))

    _print(s, balance, months)
    _v14_comparison(s, balance, months, out_dir)
    return s


# ----------------------------------------------------------------------
#  Printing
# ----------------------------------------------------------------------
def _print(s, balance, months):
    print("=" * 80)
    print(f"  v15 OOS RESULT  |  5%ers MTB  |  ${balance:,.0f}  |  "
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
        print(f"  Monthly return    {s['pct_return']/max(months,1):>12.2f} %")
        print(f"  Trades/month      {s['trades']/max(months,1):>12.1f}")

        print("\n  By symbol:")
        print(f"    {'symbol':<8} {'n':>4} {'WR':>7} {'expR':>8} "
              f"{'bars':>6} {'net$':>12}")
        for sym, d in sorted(s["by_symbol"].items(),
                               key=lambda x: -x[1]["net"]):
            print(f"    {sym:<8} {d['n']:>4} {d['wr']*100:>6.1f}% "
                  f"{d['expR']:>+8.3f} {d['avg_bars']:>6.1f} "
                  f"{d['net']:>+12,.2f}")

        print("\n  By side:")
        for side, d in s["by_side"].items():
            lbl = "LONG" if side in ("1", 1) else "SHORT"
            print(f"    {lbl:<6} n={d['n']:>4} WR={d['wr']*100:>5.1f}% "
                  f"expR={d['expR']:+.3f} net=${d['net']:>+10,.2f}")

        print("\n  Exit reasons:")
        for r, c in sorted(s["by_exit_reason"].items(),
                            key=lambda x: -x[1]):
            print(f"    {r:<22} {c:>4}")

        boot = s.get("bootstrap", {})
        if boot.get("n_iters", 0):
            print("\n  Bootstrap CI (10k resamples of trade sequence):")
            print(f"    Net P&L    median=${boot['median_net']:>10,.0f}  "
                  f"p05=${boot['p05_net']:>10,.0f}  "
                  f"p95=${boot['p95_net']:>10,.0f}")
            print(f"    PF         median={boot['median_pf']:>9.2f}  "
                  f"p05={boot['p05_pf']:>9.2f}  "
                  f"p95={boot['p95_pf']:>9.2f}")
            print(f"    Max DD$    median=${boot['median_dd']:>10,.0f}  "
                  f"p95=${boot['p95_dd']:>10,.0f}")

    print("\n  OOS acceptance gates:")
    boot = s.get("bootstrap", {})
    def tick(ok): return "PASS" if ok else "FAIL"
    print(f"    Net P&L > 0            : "
          f"{tick(s.get('net_pnl', 0) > 0)}")
    print(f"    PF >= 1.5              : "
          f"{tick(s.get('pf', 0) >= 1.5)}")
    print(f"    Max DD < 4% (daily)    : "
          f"{tick(s.get('max_dd_pct', 100) < 4.0)}")
    print(f"    Bootstrap p05 net > 0  : "
          f"{tick(boot.get('p05_net', 0) > 0)}")
    print(f"    Bootstrap p05 PF > 1.0 : "
          f"{tick(boot.get('p05_pf', 0) > 1.0)}")
    if s.get("halted_permanently"):
        print("\n    *** ENGINE HALTED PERMANENTLY ***")
    print("=" * 80)


def _v14_comparison(s15, balance, months, out_dir):
    v14_path = out_dir / f"v14_smartbb_{int(balance)}_{months}m.json"
    if not v14_path.exists():
        return
    with open(v14_path) as f:
        s14 = json.load(f)
    print("\n" + "=" * 80)
    print(f"  v15 vs v14 OOS COMPARISON  (${balance:,.0f}, {months}m)")
    print("=" * 80)
    print(f"                    {'v14':>14}   {'v15':>14}   {'delta':>12}")
    def row(label, k, fmt="{:>14,.2f}", pct=False):
        a = s14.get(k, 0) or 0
        b = s15.get(k, 0) or 0
        d = b - a
        if pct:
            print(f"  {label:<18} {a:>12.2f} %   {b:>12.2f} %   "
                  f"{d:>+10.2f} %")
        else:
            print(f"  {label:<18} " + fmt.format(a) + "     "
                  + fmt.format(b) + f"   {d:>+12,.2f}")
    row("Trades           ", "trades", "{:>14,d}")
    row("Net P&L          ", "net_pnl")
    row("Return (%)       ", "pct_return", pct=True)
    row("PF               ", "pf", "{:>14.2f}")
    row("Win rate         ", "win_rate", "{:>14.3f}")
    row("Expectancy (R)   ", "expectancy_R", "{:>14.3f}")
    row("Max DD (%)       ", "max_dd_pct", pct=True)
    print("=" * 80)


# ----------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=100_000.0)
    ap.add_argument("--months",  type=int,   default=3)
    ap.add_argument("--tuning",  type=Path,
                     default=ROOT / "Results" / "v15_ultimate_tuning.json")
    ap.add_argument("--out",     type=Path, default=ROOT / "Results")
    ap.add_argument("--bootstrap-iters", type=int, default=10_000)
    a = ap.parse_args()
    run(a.balance, a.months, a.tuning, a.out, a.bootstrap_iters)


if __name__ == "__main__":
    main()
