#!/usr/bin/env python3
"""
SHF v14 — portfolio backtest using the per-symbol walk-forward winners.

Reads Results/v14_per_symbol_tuning.json, constructs SymbolParams for the
KEPT symbols only, runs the v14 engine in portfolio mode on the common
3-month window (same window as v13 for an apples-to-apples comparison),
and writes full results + bootstrap CIs.

Usage:
    python Scripts/backtest_smartbb_v14.py
    python Scripts/backtest_smartbb_v14.py --months 6
    python Scripts/backtest_smartbb_v14.py --tuning Results/v14_per_symbol_tuning.json
"""

from __future__ import annotations
import argparse, csv, json, random, sys, time as _time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.smartbb_engine_v14 import (  # noqa: E402
    SmartBBV14Engine, SmartBBV14Config, SymbolParams,
    SMARTBB_UNIVERSE, params_from_dict,
)


# =====================================================================
#  Helpers copied from v13 backtest for like-for-like comparison
# =====================================================================

def load_m1(path: Path, tmin, tmax):
    out = []
    with open(path, "r", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try: t = datetime.fromisoformat(row["time"])
            except Exception:
                t = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            if tmin and t < tmin: continue
            if tmax and t > tmax: continue
            out.append((t, float(row["open"]), float(row["high"]),
                         float(row["low"]), float(row["close"])))
    return out


def common_window(files, months):
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
    start = max(max(firsts.values()), end - timedelta(days=months * 30))
    return start, end


# =====================================================================
#  Bootstrap on portfolio-level trade list
# =====================================================================

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
        peak = 0.0; cum = 0.0; dd = 0.0
        for x in sample:
            cum += x
            peak = max(peak, cum)
            if peak - cum > dd: dd = peak - cum
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
        "p05_pf": q(pfs_cap, 0.05),
        "p95_pf": q(pfs_cap, 0.95),
        "median_dd": q(dds, 0.50),
        "p95_dd": q(dds, 0.95),
        "n_iters": n_iters,
    }


# =====================================================================
#  Main
# =====================================================================

def run(balance, months, tuning_path, out_dir, bootstrap_iters, prefer_masked):
    with open(tuning_path) as f:
        tuning = json.load(f)

    kept_raw = tuning.get("summary", {}).get("kept", [])
    if not kept_raw:
        raise SystemExit(f"No KEPT symbols in {tuning_path}")

    # Build per-symbol SymbolParams
    params_by_symbol: dict[str, SymbolParams] = {}
    for sym in kept_raw:
        r = tuning["results"][sym]
        # Prefer masked alternative if it exists AND user asked for it
        if prefer_masked and r.get("masked_alternative"):
            params_by_symbol[sym] = params_from_dict(
                r["masked_alternative"]["params"])
            print(f"  [{sym}] using MASKED params "
                   f"(hours={r['masked_alternative']['hour_mask']})")
        else:
            params_by_symbol[sym] = params_from_dict(r["best_params"])
            print(f"  [{sym}] using UNMASKED params")

    symbols = list(params_by_symbol.keys())

    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in symbols}
    miss = [s for s, p in files.items() if not p.exists()]
    if miss: raise SystemExit(f"missing: {miss}")

    start, end = common_window(files, months)
    print(f"\n  Window: {start} -> {end} ({(end-start).days} days)")

    bars = {s: load_m1(p, start, end) for s, p in files.items()}
    total_bars = sum(len(b) for b in bars.values())
    print(f"  Loaded {total_bars:,} M1 bars across {len(symbols)} symbols")

    cfg = SmartBBV14Config()
    syms = [SMARTBB_UNIVERSE[s] for s in symbols]
    eng = SmartBBV14Engine(symbols=syms, params=params_by_symbol,
                             cfg=cfg, initial_equity=balance)

    print(f"\n  Engine: v14 PhD-ADAPTIVE SMART BOLLINGER")
    print(f"  Symbols kept: {symbols}\n")

    merged = []
    for s, b in bars.items():
        for (t, o, h, l, c) in b:
            merged.append((t, s, o, h, l, c))
    merged.sort(key=lambda x: x[0])

    t0 = _time.time()
    for (t, sym, o, h, l, c) in merged:
        eng.on_bar(sym, t.timestamp(), t.strftime("%Y-%m-%d"),
                    t.hour, t.minute, o, h, l, c)
        if eng.halted_permanently: break
    el = _time.time() - t0
    print(f"  Ran in {el:.1f}s ({len(merged)/max(el,1e-3):,.0f} bars/sec)\n")

    s = eng.summary()
    s["start_equity"] = balance
    s["halted_permanently"] = eng.halted_permanently
    s["kept_symbols"] = symbols
    s["tuning_source"] = str(tuning_path)

    # Bootstrap on the portfolio trade list
    trade_dicts = [asdict(t) for t in eng.trades]
    s["bootstrap"] = bootstrap_portfolio(trade_dicts, n_iters=bootstrap_iters)

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"v14_smartbb_{int(balance)}_{months}m"
    with open(out_dir / f"{stem}.json", "w") as f:
        json.dump(s, f, indent=2, default=str)
    eng.dump_trades(str(out_dir / f"{stem}_trades.json"))

    _print(s, balance, months)
    _v13_comparison(s, balance, months, out_dir)
    return s


def _print(s, balance, months):
    print("=" * 80)
    print(f"  v14 PhD-ADAPTIVE SMART BOLLINGER | 5%ers MTB | ${balance:,.0f} | {months} months")
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
        print(f"    {'symbol':<8} {'n':>4} {'WR':>7} {'expR':>8} {'bars':>6} {'net$':>11}")
        for sym, d in sorted(s["by_symbol"].items(), key=lambda x: -x[1]["net"]):
            print(f"    {sym:<8} {d['n']:>4} {d['wr']*100:>6.1f}% {d['expR']:>+8.3f} "
                   f"{d['avg_bars']:>6.1f} {d['net']:>+11,.2f}")

        print("\n  By side:")
        for side, d in s["by_side"].items():
            lbl = "LONG" if side in ("1", 1) else "SHORT"
            print(f"    {lbl:<6} n={d['n']:>4} WR={d['wr']*100:>5.1f}% "
                   f"expR={d['expR']:+.3f} net=${d['net']:>+10,.2f}")

        print("\n  Exit reasons:")
        for r, c in sorted(s["by_exit_reason"].items(), key=lambda x: -x[1]):
            print(f"    {r:<22} {c:>4}")

        boot = s.get("bootstrap", {})
        if boot.get("n_iters", 0):
            print("\n  Bootstrap CI (10k resamples of trade sequence):")
            print(f"    Net P&L    median=${boot['median_net']:>10,.0f}  "
                   f"p05=${boot['p05_net']:>10,.0f}  p95=${boot['p95_net']:>10,.0f}")
            print(f"    PF         median={boot['median_pf']:>9.2f}  "
                   f"p05={boot['p05_pf']:>9.2f}  p95={boot['p95_pf']:>9.2f}")
            print(f"    Max DD$    median=${boot['median_dd']:>10,.0f}  "
                   f"p95=${boot['p95_dd']:>10,.0f}")

    print("\n  Acceptance (v14 gates):")
    boot = s.get("bootstrap", {})
    print(f"    Net P&L > 0            : {'YES' if s.get('net_pnl', 0) > 0 else 'NO'}")
    print(f"    PF >= 1.5              : {'YES' if s.get('pf', 0) >= 1.5 else 'NO'}")
    print(f"    Max DD < 3%            : {'YES' if s.get('max_dd_pct', 100) < 3.0 else 'NO'}")
    print(f"    Bootstrap p05 net > 0  : {'YES' if boot.get('p05_net', 0) > 0 else 'NO'}")
    print(f"    Bootstrap p05 PF > 1.0 : {'YES' if boot.get('p05_pf', 0) > 1.0 else 'NO'}")
    if s.get("halted_permanently"):
        print("    *** HALTED ***")
    print("=" * 80)


def _v13_comparison(s14, balance, months, out_dir):
    v13_path = out_dir / f"v13_smartbb_{int(balance)}_{months}m.json"
    if not v13_path.exists():
        print("\n  (v13 baseline not found, skipping comparison)")
        return
    with open(v13_path) as f:
        s13 = json.load(f)
    print("\n" + "=" * 80)
    print(f"  v14 vs v13 COMPARISON (${balance:,.0f}, {months}m)")
    print("=" * 80)
    print(f"                    {'v13':>14}   {'v14':>14}   {'delta':>12}")
    def row(label, k, fmt="{:>12,.2f}", pct=False):
        v13v = s13.get(k, 0) or 0
        v14v = s14.get(k, 0) or 0
        d = v14v - v13v
        if pct:
            print(f"  {label:<18} {v13v:>12.2f} %   {v14v:>12.2f} %   "
                   f"{d:>+10.2f} %")
        else:
            print(f"  {label:<18} " + fmt.format(v13v) + "     " +
                   fmt.format(v14v) + f"   {d:>+12,.2f}")
    row("Trades           ", "trades", "{:>14,d}")
    row("Net P&L          ", "net_pnl")
    row("Return (%)       ", "pct_return", pct=True)
    row("PF               ", "pf", "{:>14.2f}")
    row("Win rate         ", "win_rate", "{:>14.3f}")
    row("Expectancy (R)   ", "expectancy_R", "{:>14.3f}")
    row("Max DD (%)       ", "max_dd_pct", pct=True)
    print("=" * 80)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=100_000.0)
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--tuning", type=Path,
                      default=ROOT / "Results" / "v14_per_symbol_tuning.json")
    ap.add_argument("--out", type=Path, default=ROOT / "Results")
    ap.add_argument("--bootstrap-iters", type=int, default=10_000)
    ap.add_argument("--prefer-masked", action="store_true",
                      help="If masked alternative exists, use it over unmasked.")
    a = ap.parse_args()
    run(a.balance, a.months, a.tuning, a.out, a.bootstrap_iters, a.prefer_masked)


if __name__ == "__main__":
    main()
