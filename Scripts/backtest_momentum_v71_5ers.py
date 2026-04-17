#!/usr/bin/env python3
"""
SHF v7.1 ORB MOMENTUM SCALPER - 5%ers MTB 3-MONTH BACKTEST (IDENTICAL GATE).

Same data, same fees, same prop rules, same acceptance bar as v7.0 — only
the trigger changes from CUSUM (proven unprofitable) to Opening-Range
Breakout + NR filter + all the existing PhD math (Kalman, HMM, EVT-GARCH,
Bayesian sizer, Shiryaev optstop).

This is the A/B test requested by the operator: keep everything that worked
in v7.0 (sizing, stops, safety rails) and swap only the trigger.

Usage:
    python Scripts/backtest_momentum_v71_5ers.py
    python Scripts/backtest_momentum_v71_5ers.py --require-nr
    python Scripts/backtest_momentum_v71_5ers.py --no-kalman-gate
    python Scripts/backtest_momentum_v71_5ers.py --months 3 --balance 5000
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time as _time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.momentum_engine import (                                   # noqa: E402
    MomentumEngine, SymbolConfig, EngineConfig,
)

# Re-use the 5%ers rule set + spec table from the v7.0 harness.
from Scripts.backtest_momentum_v7_5ers import (                     # noqa: E402
    FiveersMTBRules, FIVEERS_LEVELS, FIVEERS_SYMBOL_SPECS,
    load_m1, common_window,
)

logging.basicConfig(level=logging.WARNING, format="%(message)s")
LOG = logging.getLogger("v71_5ers_bt")


# ======================================================================
#  Driver
# ======================================================================

def run_backtest(balance: float, months: int,
                 account_type: str, out_dir: Path,
                 daily_dd: Optional[float],
                 total_dd: Optional[float],
                 require_nr: bool,
                 require_kalman_agree: bool,
                 orb_tp1_R: float,
                 orb_tp2_R: float,
                 orb_max_hold: int) -> dict:
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
            raise SystemExit(f"Missing data file: {p}")

    bt_start, bt_end = common_window(files, months)
    print(f"\n  Backtest window:  {bt_start}  ->  {bt_end}")
    print(f"                    ({(bt_end - bt_start).days} days)")

    bars_per_sym: dict[str, list] = {}
    for sym, p in files.items():
        bars_per_sym[sym] = load_m1(p, bt_start, bt_end)
        print(f"    {sym:<8}  {len(bars_per_sym[sym]):>7,} bars")

    symbols = [SymbolConfig(**FIVEERS_SYMBOL_SPECS[s]) for s in files]

    eng_cfg = EngineConfig(
        strategy_mode="orb",
        orb_require_nr=require_nr,
        orb_require_kalman_agree=require_kalman_agree,
        orb_tp1_R=orb_tp1_R,
        orb_tp2_R=orb_tp2_R,
        orb_max_hold_minutes=orb_max_hold,
        # Match v7.0 TP ladder so _manage_position uses the same R-multiples
        tp1_R=orb_tp1_R,
        tp2_R=orb_tp2_R,
        max_hold_minutes=orb_max_hold,
    )

    engine = MomentumEngine(
        symbols=symbols,
        cfg=eng_cfg,
        initial_equity=balance,
        daily_dd_limit=daily_dd,
        total_dd_limit=total_dd,
    )

    # Merge-sort across symbols
    merged: list[tuple[datetime, str, float, float, float, float]] = []
    for sym, bars in bars_per_sym.items():
        for (t, o, h, l, c) in bars:
            merged.append((t, sym, o, h, l, c))
    merged.sort(key=lambda x: x[0])

    t_start = _time.time()
    last_day_print = None
    profit_target_abs = balance * (1.0 + rules.profit_target_pct)
    total_floor_abs = balance * (1.0 - rules.max_total_loss_pct)
    hit_profit_target_on: Optional[datetime] = None
    hit_total_dd_on: Optional[datetime] = None
    passed = False
    failed = False

    for (t, sym, o, h, l, c) in merged:
        engine.on_bar(
            symbol=sym,
            time=t.timestamp(),
            day_key=t.strftime("%Y-%m-%d"),
            hour_utc=t.hour,
            minute_utc=t.minute,             # <-- NEW for ORB
            open_=o, high=h, low=l, close=c,
        )
        if last_day_print != t.strftime("%Y-%m-%d"):
            last_day_print = t.strftime("%Y-%m-%d")
            if not passed and engine.equity >= profit_target_abs:
                passed = True
                hit_profit_target_on = t
            if not failed and engine.equity <= total_floor_abs:
                failed = True
                hit_total_dd_on = t
                break
            if engine.halted_permanently:
                failed = True
                hit_total_dd_on = t
                break

    elapsed = _time.time() - t_start
    print(f"\n  Simulation ran in {elapsed:.1f}s "
          f"({len(merged)/max(elapsed,1e-3):,.0f} bars/sec)")

    summary = engine.summary()
    summary["start_equity"] = balance
    summary["final_equity"] = engine.equity
    summary["rules"] = asdict(rules)
    summary["rules"]["daily_dd_limit_used"] = daily_dd
    summary["rules"]["total_dd_limit_used"] = total_dd
    summary["passed_profit_target"] = passed
    summary["failed_total_dd"] = failed
    summary["profit_target_hit_on"] = (
        hit_profit_target_on.isoformat() if hit_profit_target_on else None)
    summary["total_dd_hit_on"] = (
        hit_total_dd_on.isoformat() if hit_total_dd_on else None)
    summary["bt_start"] = bt_start.isoformat()
    summary["bt_end"] = bt_end.isoformat()
    summary["bars_seen"] = len(merged)
    summary["strategy_mode"] = "orb"
    summary["orb_config"] = {
        "require_nr": require_nr,
        "require_kalman_agree": require_kalman_agree,
        "tp1_R": orb_tp1_R,
        "tp2_R": orb_tp2_R,
        "max_hold_minutes": orb_max_hold,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "v71_5ers_mtb_3month.json"
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n  JSON:   {json_path}")

    trades_path = out_dir / "v71_5ers_mtb_3month_trades.json"
    engine.dump_trades(str(trades_path))
    print(f"  Trades: {trades_path}  ({summary['trades']} rows)")

    return summary


# ======================================================================
#  Reporting
# ======================================================================

def print_summary(summary: dict) -> None:
    rules = summary["rules"]
    orb_cfg = summary.get("orb_config", {})
    print("\n" + "=" * 72)
    print(f"  SHF v7.1 ORB Scalper  |  5%ers {rules['name']}  |  3-month backtest")
    print("=" * 72)
    print(f"  Strategy          ORB (Opening Range Breakout)")
    print(f"  NR filter         {'ON (NR7)' if orb_cfg.get('require_nr') else 'off'}")
    print(f"  Kalman-agree gate {'ON' if orb_cfg.get('require_kalman_agree') else 'off'}")
    print(f"  TP ladder         {orb_cfg.get('tp1_R', 1.0)}R / {orb_cfg.get('tp2_R', 2.0)}R / trail")
    print("-" * 72)
    print(f"  Start equity      ${summary['start_equity']:>12,.2f}")
    print(f"  Final equity      ${summary['final_equity']:>12,.2f}")
    print(f"  Net P&L           ${summary['net_pnl']:>12,.2f}")
    print(f"  Return            {summary['pct_return']:>12.2f} %")
    print(f"  Trades            {summary['trades']:>12,}")
    if summary['trades'] > 0:
        print(f"  Win rate          {summary['win_rate']*100:>12.1f} %")
        print(f"  Profit factor     {summary['pf']:>12.2f}")
        print(f"  Expectancy (R)    {summary['expectancy_R']:>12.3f}")
        print(f"  Avg winner (R)    {summary['avg_winner_R']:>12.2f}")
        print(f"  Avg loser  (R)    {summary['avg_loser_R']:>12.2f}")
        print(f"  Max DD            {summary['max_dd_pct']:>12.2f} %")
        print(f"  Gross costs       ${summary['gross_costs']:>12,.2f}")
        print("\n  By symbol: " + ", ".join(
            f"{s}={n}" for s, n in summary['by_symbol'].items()))
    else:
        print("  No trades fired — ORB window never triggered or all filters vetoed.")
    print("\n  Acceptance (v7.0 same bar):")
    print(f"    Net P&L > 0             : {'YES' if summary['net_pnl'] > 0 else 'NO'}")
    print(f"    PF >= 1.3               : {'YES' if summary.get('pf', 0) >= 1.3 else 'NO'}")
    print(f"    Max DD < 5%             : {'YES' if summary.get('max_dd_pct', 100) < 5.0 else 'NO'}")
    print(f"    Trades >= 100           : {'YES' if summary.get('trades', 0) >= 100 else 'NO'}")
    if summary["failed_total_dd"]:
        print(f"    *** BLEW total DD on {summary['total_dd_hit_on']} ***")
    elif summary["passed_profit_target"]:
        print(f"    HIT profit target on {summary['profit_target_hit_on']}")
    print("=" * 72)


def emit_markdown_report(summary: dict, out_path: Path) -> None:
    rules = summary["rules"]
    orb_cfg = summary.get("orb_config", {})
    passed = summary["passed_profit_target"]
    failed = summary["failed_total_dd"]

    lines: list[str] = []
    lines.append(f"# SHF v7.1 ORB Scalper — 5%ers MTB 3-Month Backtest")
    lines.append("")
    lines.append(f"**Strategy:** Opening-Range Breakout (Crabel 1990; Zarattini 2023)")
    lines.append(f"**NR7 filter:** {'on' if orb_cfg.get('require_nr') else 'off'}")
    lines.append(f"**Kalman-agree gate:** {'on' if orb_cfg.get('require_kalman_agree') else 'off'}")
    lines.append(f"**TP ladder:** {orb_cfg.get('tp1_R', 1.0)}R / {orb_cfg.get('tp2_R', 2.0)}R / EVT-GARCH trail")
    lines.append(f"**Max hold:** {orb_cfg.get('max_hold_minutes', 180)} min")
    lines.append(f"**Account:** {rules['name']}")
    lines.append(f"**Balance:** ${rules['initial_balance']:,.0f}")
    lines.append(f"**Backtest window:** {summary['bt_start']} → {summary['bt_end']}")
    lines.append(f"**Bars processed:** {summary['bars_seen']:,}")
    lines.append("")
    lines.append("## Outcome")
    lines.append("")
    if failed:
        lines.append(f"- ❌ **Failed** — total DD hit on {summary['total_dd_hit_on']}")
    elif passed:
        lines.append(f"- ✅ **PASSED PROFIT TARGET** on {summary['profit_target_hit_on']}")
    else:
        lines.append(f"- ⚠️ Window ended still solvent at ${summary['final_equity']:.2f}")
    lines.append("")
    lines.append("## Headline metrics (net of all fees & slippage)")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---:|")
    lines.append(f"| Start equity | ${summary['start_equity']:,.2f} |")
    lines.append(f"| Final equity | ${summary['final_equity']:,.2f} |")
    lines.append(f"| Net P&L | ${summary['net_pnl']:,.2f} |")
    lines.append(f"| Return | {summary['pct_return']:.2f}% |")
    lines.append(f"| Trades | {summary['trades']} |")
    if summary['trades'] > 0:
        lines.append(f"| Win rate | {summary['win_rate']*100:.1f}% |")
        lines.append(f"| Profit factor | {summary['pf']:.2f} |")
        lines.append(f"| Expectancy (R) | {summary['expectancy_R']:.3f} |")
        lines.append(f"| Avg winner (R) | {summary['avg_winner_R']:.2f} |")
        lines.append(f"| Avg loser (R) | {summary['avg_loser_R']:.2f} |")
        lines.append(f"| Max draw-down | {summary['max_dd_pct']:.2f}% |")
        lines.append(f"| Gross costs | ${summary['gross_costs']:,.2f} |")
    lines.append("")
    lines.append("## Trades by symbol")
    lines.append("")
    lines.append("| Symbol | Trades |")
    lines.append("|---|---:|")
    for sym, n in summary["by_symbol"].items():
        lines.append(f"| {sym} | {n} |")
    lines.append("")
    lines.append("## v7.0 vs v7.1 acceptance gate")
    lines.append("")
    gate = [
        ("Net P&L > 0",            summary['net_pnl'] > 0),
        ("PF >= 1.3",              summary.get('pf', 0) >= 1.3),
        ("Max DD < 5%",            summary.get('max_dd_pct', 100) < 5.0),
        ("Trades >= 100",          summary.get('trades', 0) >= 100),
        ("No total DD blow",       not failed),
    ]
    for name, ok in gate:
        lines.append(f"- {'✅' if ok else '❌'} {name}")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    all_ok = all(ok for _, ok in gate)
    if all_ok:
        lines.append("**PASSED v7.0 acceptance bar.**  Proceed to paper-trade phase.")
    else:
        lines.append("**FAILED.**  Tune ORB parameters, re-test.  If still negative,"
                     " pivot to Proposal B (VWAP-reversion) per PIVOT_TO_PROFITABLE_v71.md.")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Report: {out_path}")


# ======================================================================
#  CLI
# ======================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=5000.0)
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--account-type", choices=list(FIVEERS_LEVELS), default="level1")
    ap.add_argument("--daily-dd", type=float, default=None)
    ap.add_argument("--total-dd", type=float, default=None)
    ap.add_argument("--require-nr", action="store_true",
                    help="Require NR7 filter (Crabel).")
    ap.add_argument("--no-kalman-gate", action="store_true",
                    help="Disable Kalman-drift agreement veto.")
    ap.add_argument("--tp1", type=float, default=1.0)
    ap.add_argument("--tp2", type=float, default=2.0)
    ap.add_argument("--max-hold", type=int, default=180,
                    help="Max hold minutes (default 180 = 3h).")
    ap.add_argument("--out", type=Path, default=ROOT / "Results")
    args = ap.parse_args()

    summary = run_backtest(
        balance=args.balance,
        months=args.months,
        account_type=args.account_type,
        out_dir=args.out,
        daily_dd=args.daily_dd,
        total_dd=args.total_dd,
        require_nr=args.require_nr,
        require_kalman_agree=not args.no_kalman_gate,
        orb_tp1_R=args.tp1,
        orb_tp2_R=args.tp2,
        orb_max_hold=args.max_hold,
    )
    print_summary(summary)
    emit_markdown_report(summary, args.out / "v71_5ers_mtb_3month_report.md")


if __name__ == "__main__":
    main()
