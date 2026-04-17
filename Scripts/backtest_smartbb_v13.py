#!/usr/bin/env python3
"""SHF v13 SMART BOLLINGER — backtest on 5%ers MTB cheap symbols."""

from __future__ import annotations
import argparse, csv, json, sys, time as _time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.smartbb_engine import (  # noqa: E402
    SmartBBEngine, SmartBBConfig, SMARTBB_UNIVERSE,
)


def load_m1(path: Path, tmin, tmax):
    out = []
    with open(path, "r", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try: t = datetime.fromisoformat(row["time"])
            except Exception: t = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
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


def run(balance, months, symbols, hurst_max, z_min, z_max, amp_hurdle, out_dir):
    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in symbols}
    miss = [s for s, p in files.items() if not p.exists()]
    if miss: raise SystemExit(f"missing: {miss}")

    start, end = common_window(files, months)
    print(f"\n  Window: {start} -> {end} ({(end-start).days} days)")

    bars = {s: load_m1(p, start, end) for s, p in files.items()}

    cfg = SmartBBConfig(
        hurst_max_for_trade=hurst_max,
        min_z_entry=z_min, max_z_entry=z_max,
        amplitude_hurdle=amp_hurdle,
    )
    syms = [SMARTBB_UNIVERSE[s] for s in symbols]
    eng = SmartBBEngine(symbols=syms, cfg=cfg, initial_equity=balance)

    print(f"  Engine: v13 SMART BOLLINGER (M5, BB(20,2) + Hurst<{hurst_max} + Kalman exit)")
    print(f"  Gates : Z in [{z_min},{z_max}], Hurst < {hurst_max}, "
           f"amp hurdle {amp_hurdle}x, 1x ATR stop, middle-band TP\n")

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

    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"v13_smartbb_{int(balance)}_{months}m"
    with open(out_dir / f"{stem}.json", "w") as f:
        json.dump(s, f, indent=2, default=str)
    eng.dump_trades(str(out_dir / f"{stem}_trades.json"))

    _print(s, balance, months)
    return s


def _print(s, balance, months):
    print("=" * 80)
    print(f"  v13 SMART BOLLINGER | 5%ers MTB | ${balance:,.0f} | {months} months")
    print("=" * 80)
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
        print(f"  Avg bars held     {s['avg_bars_held']:>12.1f}  (M5)")
        print(f"  Max DD            {s['max_dd_pct']:>12.2f} %")
        print(f"  Gross commissions ${s['gross_commissions']:>12,.2f}")
        print(f"  Gross spread cost ${s['gross_spread_cost']:>12,.2f}")
        total_cost = s['gross_commissions'] + s['gross_spread_cost']
        gross_pnl_est = s['net_pnl'] + s['gross_commissions']
        print(f"  TOTAL trading fees${total_cost:>12,.2f}   "
               f"(={total_cost/max(s['trades'],1):,.2f}/trade)")
        print(f"  Monthly return    {s['pct_return']/max(months,1):>12.2f} %")
        print(f"  Trades/month      {s['trades']/max(months,1):>12.1f}")

        print("\n  By symbol:")
        print(f"    {'symbol':<8} {'n':>4} {'WR':>7} {'expR':>8} {'bars':>6} {'net$':>11}")
        for sym, d in sorted(s['by_symbol'].items(), key=lambda x:-x[1]['net']):
            print(f"    {sym:<8} {d['n']:>4} {d['wr']*100:>6.1f}% {d['expR']:>+8.3f} "
                   f"{d['avg_bars']:>6.1f} {d['net']:>+11,.2f}")

        print("\n  By side:")
        for side, d in s['by_side'].items():
            lbl = "LONG" if side in ("1", 1) else "SHORT"
            print(f"    {lbl:<6} n={d['n']:>4} WR={d['wr']*100:>5.1f}% "
                   f"expR={d['expR']:+.3f} net=${d['net']:>+10,.2f}")

        print("\n  By Hurst (MR regime quality):")
        for h, d in sorted(s.get('by_hurst', {}).items()):
            print(f"    H={h}  n={d['n']:>4}  WR={d['wr']*100:>5.1f}%  net=${d['net']:>+10,.2f}")

        print("\n  By |Z| at entry:")
        for z, d in sorted(s.get('by_z', {}).items()):
            print(f"    |Z|={z}  n={d['n']:>4}  WR={d['wr']*100:>5.1f}%  net=${d['net']:>+10,.2f}")

        print("\n  Exit reasons:")
        for r, c in sorted(s['by_exit_reason'].items(), key=lambda x:-x[1]):
            print(f"    {r:<22} {c:>4}")

    print("\n  Acceptance:")
    print(f"    Net P&L > 0       : {'YES' if s.get('net_pnl', 0) > 0 else 'NO'}")
    print(f"    PF >= 1.3         : {'YES' if s.get('pf', 0) >= 1.3 else 'NO'}")
    print(f"    Max DD < 5%       : {'YES' if s.get('max_dd_pct', 100) < 5.0 else 'NO'}")
    print(f"    Trades >= 50      : {'YES' if s.get('trades', 0) >= 50 else 'NO'}")
    if s.get("halted_permanently"):
        print("    *** HALTED ***")
    print("=" * 80)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=100_000.0)
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--symbols", nargs="+",
                     default=["US100", "US500", "US30", "USOIL", "DE40"])
    ap.add_argument("--hurst-max", type=float, default=0.50)
    ap.add_argument("--z-min", type=float, default=2.0)
    ap.add_argument("--z-max", type=float, default=4.5)
    ap.add_argument("--amp-hurdle", type=float, default=1.5)
    ap.add_argument("--out", type=Path, default=ROOT / "Results")
    a = ap.parse_args()
    run(a.balance, a.months, a.symbols, a.hurst_max, a.z_min, a.z_max,
         a.amp_hurdle, a.out)


if __name__ == "__main__":
    main()
