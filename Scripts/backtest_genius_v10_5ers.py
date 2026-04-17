#!/usr/bin/env python3
"""
SHF v10 GENIUS — 5%ers MTB backtest over 9 commission-friendly symbols.

Runs 3 probes × 9 symbols with Bayesian edge tracking + GZ-DD shrinkage.
Reports honest per-symbol + per-probe breakdown so we see which pairs are
actually trustworthy.

Usage:
    python Scripts/backtest_genius_v10_5ers.py                      # defaults: $100k, 3 months
    python Scripts/backtest_genius_v10_5ers.py --balance 5000       # MTB Level 1 $5k
    python Scripts/backtest_genius_v10_5ers.py --months 2           # last 2 months only
"""
from __future__ import annotations

import argparse, csv, json, sys, time as _time
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import asdict
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.genius_engine import GeniusEngine, GeniusConfig, FIVEERS_SPECS  # noqa: E402

SYMBOLS = ["DE40", "US100", "US500", "US30", "UK100", "JP225",
            "USOIL", "XTIUSD", "XBRUSD"]


def load_m1(path: Path, tmin: Optional[datetime], tmax: Optional[datetime]):
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
                         float(row["low"]), float(row["close"])))
    return out


def common_window(files: dict[str, Path], months: int):
    firsts, lasts = {}, {}
    for sym, p in files.items():
        with open(p, "r", newline="") as f:
            rdr = csv.reader(f); next(rdr)
            rows = [row for row in rdr if row]
        if not rows: raise RuntimeError(f"{sym} empty")
        try:
            firsts[sym] = datetime.fromisoformat(rows[0][0])
            lasts[sym] = datetime.fromisoformat(rows[-1][0])
        except Exception:
            firsts[sym] = datetime.strptime(rows[0][0], "%Y-%m-%d %H:%M:%S")
            lasts[sym] = datetime.strptime(rows[-1][0], "%Y-%m-%d %H:%M:%S")
    end = min(lasts.values())
    start = max(max(firsts.values()), end - timedelta(days=months * 30))
    return start, end


def run(balance: float, months: int, base_risk: float,
         total_dd: float, daily_dd: float, out_dir: Path):
    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in SYMBOLS}
    missing = [s for s, p in files.items() if not p.exists()]
    if missing:
        raise SystemExit(f"missing data: {missing}")

    bt_start, bt_end = common_window(files, months)
    print(f"\n  Window: {bt_start} -> {bt_end} ({(bt_end-bt_start).days} days)")

    bars_per_sym = {}
    for sym, p in files.items():
        b = load_m1(p, bt_start, bt_end)
        bars_per_sym[sym] = b
        print(f"    {sym:<8}  {len(b):>7,} bars")

    cfg = GeniusConfig(
        base_risk_pct=base_risk,
        total_dd_limit=total_dd,
        daily_dd_limit=daily_dd,
    )
    symbols = [FIVEERS_SPECS[s] for s in SYMBOLS]
    engine = GeniusEngine(symbols=symbols, cfg=cfg, initial_equity=balance)

    print(f"\n  Engine: v10 GENIUS | 9 symbols × 3 probes = 45 (symbol,probe) edges")
    print(f"  Risk:   base={base_risk*100:.2f}%, DD gates: daily={daily_dd*100}%, total={total_dd*100}%")

    # Merge-sort across symbols
    merged = []
    for sym, bars in bars_per_sym.items():
        for (t, o, h, l, c) in bars:
            merged.append((t, sym, o, h, l, c))
    merged.sort(key=lambda x: x[0])

    t0 = _time.time()
    for (t, sym, o, h, l, c) in merged:
        engine.on_bar(
            symbol=sym, time=t.timestamp(),
            day_key=t.strftime("%Y-%m-%d"),
            hour_utc=t.hour, minute_utc=t.minute,
            open_=o, high=h, low=l, close=c,
        )
        if engine.halted_permanently:
            break
    elapsed = _time.time() - t0
    print(f"\n  Ran in {elapsed:.1f}s ({len(merged)/max(elapsed,1e-3):,.0f} bars/sec)")

    summ = engine.summary()
    summ["start_equity"] = balance
    summ["bt_start"] = bt_start.isoformat()
    summ["bt_end"] = bt_end.isoformat()
    summ["halted_permanently"] = engine.halted_permanently

    out_dir.mkdir(parents=True, exist_ok=True)
    jpath = out_dir / f"v10_genius_5ers_{int(balance)}_{months}m.json"
    with open(jpath, "w") as f:
        json.dump(summ, f, indent=2, default=str)
    engine.dump_trades(str(out_dir / f"v10_genius_5ers_{int(balance)}_{months}m_trades.json"))

    _print_summary(summ, balance, months)
    return summ


def _print_summary(s: dict, balance: float, months: int):
    print("\n" + "=" * 80)
    print(f"  v10 GENIUS | 5%ers MTB | ${balance:,.0f} | {months} months")
    print("=" * 80)
    print(f"  Start equity      ${balance:>12,.2f}")
    print(f"  Final equity      ${s.get('equity', balance):>12,.2f}")
    print(f"  Net P&L           ${s.get('net_pnl', 0):>12,.2f}")
    print(f"  Return            {s.get('pct_return', 0):>12.2f} %")
    print(f"  Trades            {s.get('trades', 0):>12,}")
    if s.get('trades', 0):
        print(f"  Win rate          {s['win_rate']*100:>12.1f} %")
        print(f"  Profit factor     {s['pf']:>12.2f}")
        print(f"  Expectancy (R)    {s['expectancy_R']:>12.3f}")
        print(f"  Avg winner (R)    {s['avg_winner_R']:>12.2f}")
        print(f"  Avg loser  (R)    {s['avg_loser_R']:>12.2f}")
        print(f"  Max DD            {s['max_dd_pct']:>12.2f} %")
        print(f"  Commissions paid  ${s['gross_commissions']:>12,.2f}")
        print(f"\n  By symbol: " + ", ".join(f"{k}={v}" for k, v in s['by_symbol'].items()))
        print("\n  By probe (pooled across symbols):")
        print(f"    {'probe':<14} {'n':>4} {'WR':>7} {'expR':>8} {'net$':>11}")
        for pid, d in sorted(s['by_probe'].items(), key=lambda x: -x[1]['n']):
            print(f"    {pid:<14} {d['n']:>4} {d['wr']*100:>6.1f}% {d['expR']:>+8.3f} {d['net']:>+11,.2f}")
        print("\n  Top 10 (symbol,probe) edges by trade count:")
        print(f"    {'pair':<24} {'n':>3} {'WR':>6} {'expR':>7} {'net$':>10}")
        pairs = sorted(s['by_sym_probe'].items(), key=lambda x: -x[1]['n'])[:10]
        for pair, d in pairs:
            print(f"    {pair:<24} {d['n']:>3} {d['wr']*100:>5.1f}% {d['expR']:>+7.3f} {d['net']:>+10,.2f}")
        if s.get('muted_pairs'):
            print(f"\n  Bayesian-muted pairs: {list(s['muted_pairs'].keys())}")
    print("\n  Acceptance:")
    print(f"    Net P&L > 0       : {'YES' if s.get('net_pnl', 0) > 0 else 'NO'}")
    print(f"    PF >= 1.3         : {'YES' if s.get('pf', 0) >= 1.3 else 'NO'}")
    print(f"    Max DD < 5%       : {'YES' if s.get('max_dd_pct', 100) < 5.0 else 'NO'}")
    print(f"    Trades >= 30      : {'YES' if s.get('trades', 0) >= 30 else 'NO'}")
    print(f"    Monthly return    : {s.get('pct_return', 0)/max(months,1):>6.2f} %   (target: 3% for £3k/$100k)")
    if s.get("halted_permanently"):
        print("    *** HALTED: account blew total DD ***")
    print("=" * 80)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=100_000.0)
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--risk", type=float, default=0.005)
    ap.add_argument("--total-dd", type=float, default=0.05)
    ap.add_argument("--daily-dd", type=float, default=0.04)
    ap.add_argument("--out", type=Path, default=ROOT / "Results")
    args = ap.parse_args()
    run(args.balance, args.months, args.risk, args.total_dd, args.daily_dd, args.out)


if __name__ == "__main__":
    main()
