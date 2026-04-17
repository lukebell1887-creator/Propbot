#!/usr/bin/env python3
"""
SHF v12 BUM CRUSHER — XAUUSD standalone 2-year out-of-sample validation.

XAUUSD has 729 days of M1 data (2024-02 to 2026-02) — the biggest sample in
the repo.  Running the engine against this alone tells us whether the Hurst+
3-of-4 confluence edge generalises across a genuinely long window of mixed
trending/MR/chop regimes, including the 2024 bull run, Q3 2024 ranging,
and the 2025-2026 push.
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


def load_m1(path: Path):
    out = []
    with open(path, "r", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try:
                t = datetime.fromisoformat(row["time"])
            except Exception:
                t = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            out.append((t, float(row["open"]), float(row["high"]),
                         float(row["low"]), float(row["close"])))
    return out


def run(balance, hurst, conf, kalman_z, amp_hurdle, out_dir):
    data_dir = ROOT / "data" / "historical"
    p = data_dir / "XAUUSD_M1.csv"
    if not p.exists():
        raise SystemExit(f"missing: {p}")

    bars = load_m1(p)
    print(f"\n  XAUUSD data: {bars[0][0]} -> {bars[-1][0]}  ({len(bars):,} bars)")

    cfg = BumCrusherConfig(
        hurst_trend_threshold=hurst,
        confluence_needed=conf,
        kalman_entry_z=kalman_z,
        amplitude_hurdle=amp_hurdle,
    )
    spec = BUMCRUSHER_UNIVERSE["XAUUSD"]
    engine = BumCrusherEngine(symbols=[spec], cfg=cfg, initial_equity=balance)

    print(f"\n  Engine: v12 BUM CRUSHER on XAUUSD standalone")
    print(f"  Gates:  Hurst >= {hurst}, conf >= {conf}/4, kalman_z >= {kalman_z}, "
           f"amp hurdle = {amp_hurdle}x")

    t0 = _time.time()
    for (t, o, h, l, c) in bars:
        engine.on_bar("XAUUSD", t.timestamp(), t.strftime("%Y-%m-%d"),
                      t.hour, t.minute, o, h, l, c)
        if engine.halted_permanently:
            print(f"  !!! HALTED at {t}")
            break
    elapsed = _time.time() - t0
    print(f"  Ran in {elapsed:.1f}s ({len(bars)/max(elapsed,1e-3):,.0f} bars/sec)\n")

    s = engine.summary()
    s["start_equity"] = balance
    s["halted_permanently"] = engine.halted_permanently

    out_dir.mkdir(parents=True, exist_ok=True)
    days = (bars[-1][0] - bars[0][0]).days
    with open(out_dir / f"v12_bumcrusher_xauusd_{int(balance)}.json", "w") as f:
        json.dump(s, f, indent=2, default=str)
    engine.dump_trades(str(out_dir / f"v12_bumcrusher_xauusd_{int(balance)}_trades.json"))

    months = max(days / 30.0, 1)
    _print(s, balance, days, months)


def _print(s, balance, days, months):
    print("=" * 80)
    print(f"  v12 BUM CRUSHER | XAUUSD STANDALONE | ${balance:,.0f} | {days} days")
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
        print(f"  Avg bars held     {s['avg_bars_held']:>12.1f}  (M15 bars)")
        print(f"  Max DD            {s['max_dd_pct']:>12.2f} %")
        print(f"  Commissions       ${s['gross_commissions']:>12,.2f}")
        print(f"  Monthly return    {s['pct_return']/months:>12.2f} %")
        print(f"  Trades/month      {s['trades']/months:>12.1f}")

        print("\n  By side:")
        for side, d in s['by_side'].items():
            label = "LONG" if side in ("1", 1) else "SHORT"
            print(f"    {label:<6} n={d['n']:>4} WR={d['wr']*100:>5.1f}%  "
                   f"expR={d['expR']:+.3f}  net=${d['net']:>+10,.2f}")

        print("\n  By Hurst bucket:")
        for h, d in sorted(s.get('by_hurst', {}).items()):
            print(f"    H={h}  n={d['n']:>4}  WR={d['wr']*100:>5.1f}%  net=${d['net']:>+10,.2f}")

        print("\n  By confluence:")
        for c, d in sorted(s.get('by_confluence', {}).items()):
            print(f"    {c}/4   n={d['n']:>4}  WR={d['wr']*100:>5.1f}%  net=${d['net']:>+10,.2f}")

        print("\n  Exit reasons:")
        for r, cnt in sorted(s['by_exit_reason'].items(), key=lambda x: -x[1]):
            print(f"    {r:<20} {cnt:>4}")

    print("\n  OOS acceptance:")
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
    ap.add_argument("--hurst", type=float, default=0.55)
    ap.add_argument("--conf", type=int, default=3)
    ap.add_argument("--kalman-z", type=float, default=1.5)
    ap.add_argument("--amp-hurdle", type=float, default=1.5)
    ap.add_argument("--out", type=Path, default=ROOT / "Results")
    args = ap.parse_args()
    run(args.balance, args.hurst, args.conf, args.kalman_z, args.amp_hurdle, args.out)


if __name__ == "__main__":
    main()
