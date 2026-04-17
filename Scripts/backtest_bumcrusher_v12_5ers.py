#!/usr/bin/env python3
"""
SHF v12 BUM CRUSHER — backtest on 5%ers MTB low-commission universe.

Runs the Hurst-gated 3-of-4 confluence momentum engine on 4 symbols,
last 3 months, reports trades + exit reasons + Hurst/confluence attribution.

Usage:
    python Scripts/backtest_bumcrusher_v12_5ers.py
    python Scripts/backtest_bumcrusher_v12_5ers.py --months 3 --hurst 0.55 --conf 3
"""
from __future__ import annotations

import argparse, csv, json, sys, time as _time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.bumcrusher_engine import (  # noqa: E402
    BumCrusherEngine, BumCrusherConfig, BUMCRUSHER_UNIVERSE,
)

SYMBOLS = ["US100", "US500", "US30", "USOIL", "DE40", "XAUUSD"]


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


def run(balance, months, hurst_thresh, conf_needed, kalman_z,
         amplitude_hurdle, out_dir):
    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in SYMBOLS}
    missing = [s for s, p in files.items() if not p.exists()]
    if missing:
        raise SystemExit(f"missing data: {missing}")

    bt_start, bt_end = common_window(files, months)
    print(f"\n  Window: {bt_start} -> {bt_end} ({(bt_end-bt_start).days} days)")

    bars_per_sym = {s: load_m1(p, bt_start, bt_end) for s, p in files.items()}

    cfg = BumCrusherConfig(
        hurst_trend_threshold=hurst_thresh,
        confluence_needed=conf_needed,
        kalman_entry_z=kalman_z,
        amplitude_hurdle=amplitude_hurdle,
    )
    symbols = [BUMCRUSHER_UNIVERSE[s] for s in SYMBOLS]
    engine = BumCrusherEngine(symbols=symbols, cfg=cfg, initial_equity=balance)

    print(f"\n  Engine: v12 BUM CRUSHER (Kalman+CUSUM+Hawkes+Z-velocity)")
    print(f"  Gates:  Hurst >= {hurst_thresh}, confluence >= {conf_needed}/4, "
           f"kalman_z >= {kalman_z}, amp hurdle = {amplitude_hurdle}x cost")

    # Merge-sort M1 bars
    merged = []
    for sym, bars in bars_per_sym.items():
        for (t, o, h, l, c) in bars:
            merged.append((t, sym, o, h, l, c))
    merged.sort(key=lambda x: x[0])

    t0 = _time.time()
    for (t, sym, o, h, l, c) in merged:
        engine.on_bar(sym, t.timestamp(), t.strftime("%Y-%m-%d"),
                      t.hour, t.minute, o, h, l, c)
        if engine.halted_permanently:
            break
    elapsed = _time.time() - t0
    print(f"  Ran in {elapsed:.1f}s ({len(merged)/max(elapsed,1e-3):,.0f} bars/sec)\n")

    summ = engine.summary()
    summ["start_equity"] = balance
    summ["bt_start"] = bt_start.isoformat()
    summ["bt_end"] = bt_end.isoformat()
    summ["halted_permanently"] = engine.halted_permanently

    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / f"v12_bumcrusher_5ers_{int(balance)}_{months}m.json", "w") as f:
        json.dump(summ, f, indent=2, default=str)
    engine.dump_trades(str(out_dir /
        f"v12_bumcrusher_5ers_{int(balance)}_{months}m_trades.json"))

    _print_summary(summ, balance, months)
    return summ


def _print_summary(s, balance, months):
    print("=" * 80)
    print(f"  v12 BUM CRUSHER | 5%ers MTB | ${balance:,.0f} | {months} months")
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
        print(f"  Avg bars held     {s['avg_bars_held']:>12.1f}  (M15 bars; x15 = min held)")
        print(f"  Max DD            {s['max_dd_pct']:>12.2f} %")
        print(f"  Commissions paid  ${s['gross_commissions']:>12,.2f}")
        print(f"  Monthly return    {s['pct_return']/max(months,1):>12.2f} %")

        print("\n  By symbol:")
        print(f"    {'symbol':<8} {'n':>4} {'WR':>7} {'expR':>8} {'bars':>6} {'net$':>11}")
        for sym, d in sorted(s['by_symbol'].items(), key=lambda x: -x[1]['net']):
            print(f"    {sym:<8} {d['n']:>4} {d['wr']*100:>6.1f}% {d['expR']:>+8.3f} "
                   f"{d['avg_bars']:>6.1f} {d['net']:>+11,.2f}")

        print("\n  By side:")
        print(f"    {'side':<6} {'n':>4} {'WR':>7} {'expR':>8} {'net$':>11}")
        for side, d in s['by_side'].items():
            label = "LONG" if side in ("1", 1) else "SHORT"
            print(f"    {label:<6} {d['n']:>4} {d['wr']*100:>6.1f}% "
                   f"{d['expR']:>+8.3f} {d['net']:>+11,.2f}")

        print("\n  By Hurst bucket (at entry):")
        print(f"    {'H':<6} {'n':>4} {'WR':>7} {'net$':>11}")
        for h, d in sorted(s.get('by_hurst', {}).items()):
            print(f"    {h:<6} {d['n']:>4} {d['wr']*100:>6.1f}% {d['net']:>+11,.2f}")

        print("\n  By confluence (signals agreeing):")
        print(f"    {'conf':<6} {'n':>4} {'WR':>7} {'net$':>11}")
        for c, d in sorted(s.get('by_confluence', {}).items()):
            print(f"    {c:<6} {d['n']:>4} {d['wr']*100:>6.1f}% {d['net']:>+11,.2f}")

        print("\n  Exit reasons:")
        for reason, cnt in sorted(s['by_exit_reason'].items(), key=lambda x: -x[1]):
            print(f"    {reason:<20} {cnt:>4}")

    print("\n  Acceptance:")
    print(f"    Net P&L > 0       : {'YES' if s.get('net_pnl', 0) > 0 else 'NO'}")
    print(f"    PF >= 1.3         : {'YES' if s.get('pf', 0) >= 1.3 else 'NO'}")
    print(f"    Max DD < 5%       : {'YES' if s.get('max_dd_pct', 100) < 5.0 else 'NO'}")
    print(f"    Trades >= 50      : {'YES' if s.get('trades', 0) >= 50 else 'NO'}")
    if s.get("halted_permanently"):
        print("    *** HALTED: account blew total DD ***")
    print("=" * 80)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=100_000.0)
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--hurst", type=float, default=0.55)
    ap.add_argument("--conf", type=int, default=3)
    ap.add_argument("--kalman-z", type=float, default=1.5)
    ap.add_argument("--amp-hurdle", type=float, default=1.5)
    ap.add_argument("--out", type=Path, default=ROOT / "Results")
    args = ap.parse_args()
    run(args.balance, args.months, args.hurst, args.conf, args.kalman_z,
         args.amp_hurdle, args.out)


if __name__ == "__main__":
    main()
