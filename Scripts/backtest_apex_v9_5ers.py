#!/usr/bin/env python3
"""
SHF v9 Apex - 5%ers MTB 3-month backtest.

Same data + prop rules as v7/v8.  Strategy: OR-breakout with stratified
filter stack validated in Results/deep_dive_orb.txt.
"""

from __future__ import annotations

import argparse
import json
import sys
import time as _time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.apex_engine import ApexEngine, SymbolConfig, ApexConfig

from Scripts.backtest_momentum_v7_5ers import (
    FiveersMTBRules, FIVEERS_LEVELS, FIVEERS_SYMBOL_SPECS,
    load_m1, common_window,
)


def run_backtest(balance, months, account_type, out_dir,
                  daily_dd, total_dd, risk_per_trade):
    rules = FIVEERS_LEVELS[account_type]
    rules.initial_balance = balance
    daily_dd = daily_dd if daily_dd is not None else rules.max_daily_loss_pct
    total_dd = total_dd if total_dd is not None else rules.max_total_loss_pct

    data_dir = ROOT / "data" / "historical"
    files = {
        "US100":  data_dir / "US100_M1.csv",
        "DE40":   data_dir / "DE40_M1.csv",
        "XAUUSD": data_dir / "XAUUSD_M1.csv",
    }
    for sym, p in files.items():
        if not p.exists():
            raise SystemExit(f"missing {p}")

    bt_start, bt_end = common_window(files, months)
    print(f"\n  Window: {bt_start} -> {bt_end} ({(bt_end-bt_start).days} days)")

    bars_per_sym = {}
    for sym, p in files.items():
        bars_per_sym[sym] = load_m1(p, bt_start, bt_end)
        print(f"    {sym:<8}  {len(bars_per_sym[sym]):>7,} bars")

    print(f"\n  Engine:   v9 apex  |  risk={risk_per_trade*100:.2f}%/trade")
    print(f"  Account:  ${balance:,.0f}  daily_dd={daily_dd*100:.1f}% total_dd={total_dd*100:.1f}%")

    _sc_fields = set(SymbolConfig.__dataclass_fields__)
    symbols = [SymbolConfig(**{k: v for k, v in FIVEERS_SYMBOL_SPECS[s].items()
                                if k in _sc_fields}) for s in files]

    cfg = ApexConfig(
        risk_per_trade=risk_per_trade,
        daily_dd_limit=daily_dd,
        total_dd_limit=total_dd,
    )
    engine = ApexEngine(symbols=symbols, cfg=cfg, initial_equity=balance)

    # Merge-sort bars across symbols
    merged = []
    for sym, bars in bars_per_sym.items():
        for (t, o, h, l, c) in bars:
            merged.append((t, sym, o, h, l, c))
    merged.sort(key=lambda x: x[0])

    profit_target_abs = balance * (1.0 + rules.profit_target_pct)
    total_floor_abs = balance * (1.0 - total_dd)
    hit_target_on: Optional[datetime] = None
    hit_dd_on: Optional[datetime] = None
    passed = False; failed = False
    last_day = None

    t0 = _time.time()
    for (t, sym, o, h, l, c) in merged:
        engine.on_bar(
            symbol=sym,
            time=t.timestamp(),
            day_key=t.strftime("%Y-%m-%d"),
            hour_utc=t.hour,
            minute_utc=t.minute,
            open_=o, high=h, low=l, close=c,
        )
        dk = t.strftime("%Y-%m-%d")
        if last_day != dk:
            last_day = dk
            if not passed and engine.equity >= profit_target_abs:
                passed = True; hit_target_on = t
            if not failed and engine.equity <= total_floor_abs:
                failed = True; hit_dd_on = t; break
            if engine.halted_permanently:
                failed = True; hit_dd_on = t; break

    elapsed = _time.time() - t0
    print(f"\n  Ran in {elapsed:.1f}s ({len(merged)/max(elapsed,1e-3):,.0f} bars/sec)")

    summary = engine.summary()
    summary["start_equity"] = balance
    summary["final_equity"] = engine.equity
    summary["bt_start"] = bt_start.isoformat()
    summary["bt_end"] = bt_end.isoformat()
    summary["bars_seen"] = len(merged)
    summary["passed_profit_target"] = passed
    summary["failed_total_dd"] = failed
    summary["profit_target_hit_on"] = hit_target_on.isoformat() if hit_target_on else None
    summary["total_dd_hit_on"] = hit_dd_on.isoformat() if hit_dd_on else None
    summary["rules"] = asdict(rules)

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"v9_apex_{account_type}_{int(balance)}.json"
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n  JSON:   {json_path}")

    trades_path = out_dir / f"v9_apex_{account_type}_{int(balance)}_trades.json"
    engine.dump_trades(str(trades_path))
    print(f"  Trades: {trades_path} ({summary['trades']} rows)")

    return summary


def print_summary(summary, balance):
    rules = summary["rules"]
    print("\n" + "=" * 72)
    print(f"  SHF v9 APEX  |  5%ers {rules['name']}  |  ${balance:,.0f}  |  3-month")
    print("=" * 72)
    print(f"  Start equity      ${summary['start_equity']:>12,.2f}")
    print(f"  Final equity      ${summary['final_equity']:>12,.2f}")
    print(f"  Net P&L           ${summary['net_pnl']:>12,.2f}")
    print(f"  Return            {summary['pct_return']:>12.2f} %")
    print(f"  Trades            {summary['trades']:>12,}")
    if summary['trades']:
        print(f"  Win rate          {summary['win_rate']*100:>12.1f} %")
        print(f"  Profit factor     {summary['pf']:>12.2f}")
        print(f"  Expectancy (R)    {summary['expectancy_R']:>12.3f}")
        print(f"  Avg winner (R)    {summary['avg_winner_R']:>12.2f}")
        print(f"  Avg loser  (R)    {summary['avg_loser_R']:>12.2f}")
        print(f"  Max DD            {summary['max_dd_pct']:>12.2f} %")
        print("\n  Trades by symbol: " + ", ".join(f"{s}={n}" for s, n in summary['by_symbol'].items()))
        print("\n  Per-filter breakdown:")
        print(f"    {'filter':<35} {'n':>3}  {'WR':>6}  {'exp_R':>7}  {'net_pnl':>10}")
        for name, d in sorted(summary['by_filter'].items(), key=lambda x: -x[1]['n']):
            print(f"    {name:<35} {d['n']:>3}  {d['wr']*100:>5.1f}%  {d['expR']:>+6.3f}  ${d['net']:>+9.2f}")
    print("\n  Acceptance:")
    print(f"    Net P&L > 0          : {'YES' if summary['net_pnl'] > 0 else 'NO'}")
    print(f"    PF >= 1.3            : {'YES' if summary.get('pf', 0) >= 1.3 else 'NO'}")
    print(f"    Max DD < 5%          : {'YES' if summary.get('max_dd_pct', 100) < 5.0 else 'NO'}")
    print(f"    Trades >= 30         : {'YES' if summary.get('trades', 0) >= 30 else 'NO'}")
    print(f"    Monthly return       : {summary['pct_return']/3:>6.2f} %  (target for £3k/$100k: 3.75%)")
    if summary["failed_total_dd"]:
        print(f"    *** BLEW total DD on {summary['total_dd_hit_on']} ***")
    elif summary["passed_profit_target"]:
        print(f"    HIT profit target on {summary['profit_target_hit_on']}")
    print("=" * 72)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=5000.0)
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--account-type", choices=list(FIVEERS_LEVELS), default="level1")
    ap.add_argument("--daily-dd", type=float, default=None)
    ap.add_argument("--total-dd", type=float, default=None)
    ap.add_argument("--risk", type=float, default=0.010, help="risk per trade as fraction (default 1%)")
    ap.add_argument("--out", type=Path, default=ROOT / "Results")
    args = ap.parse_args()

    summary = run_backtest(
        balance=args.balance,
        months=args.months,
        account_type=args.account_type,
        out_dir=args.out,
        daily_dd=args.daily_dd,
        total_dd=args.total_dd,
        risk_per_trade=args.risk,
    )
    print_summary(summary, args.balance)


if __name__ == "__main__":
    main()
