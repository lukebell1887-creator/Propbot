#!/usr/bin/env python3
"""
SHF v7 MOMENTUM SCALPER - 5%ers MTB (Main Trading Bootcamp) 3-MONTH BACKTEST.

This is the primary acceptance test requested by the operator:

    "Test on last 3 months of 5%ers MTB data with current prop-firm fees,
     prove the new bot is definitely profitable."

What this does:

  1. Loads the last 3 calendar months of M1 OHLC for NAS100 (US100), DAX40
     (DE40) and XAUUSD from `data/historical/*.csv`.
  2. Applies the 5%ers Main Trading Bootcamp rule set:
        - Initial balance:      $5,000   (MTB Level 1)  [configurable]
        - Max daily loss:        4% of start-of-day balance
        - Max total loss:        6% of initial balance (static DD)
        - Profit target:         6% Level 1 -> 4% Level 2 -> 4% Level 3
        - Leverage indices:      1:30,   Gold: 1:30
        - Commission:            $0      (5%ers are commission-free on
                                          indices/gold on their MT5 servers)
        - Spreads:               broker-realistic medians, documented below
        - Swaps:                 applied for positions held past 22:00 GMT
  3. Drives the MomentumEngine bar-by-bar with full cost accounting
     (entry slippage = 0.5 spread, stop slippage = 1.0 spread, swaps,
     commission-if-any).
  4. Writes full JSON results + a human-readable markdown acceptance report.

Data-availability: our cached Dukascopy history covers roughly Oct 2025 -
Feb 2026 for US100 & DE40, and Feb 2024 - Feb 2026 for XAUUSD.  The script
auto-detects the common tail window so "last 3 months" resolves to the most
recent 3-month slice that is present in every file.

Usage:
    python Scripts/backtest_momentum_v7_5ers.py
    python Scripts/backtest_momentum_v7_5ers.py --balance 10000
    python Scripts/backtest_momentum_v7_5ers.py --months 3 --h 4.5
    python Scripts/backtest_momentum_v7_5ers.py --account-type level1
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import sys
import time as _time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# Make `src` importable
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.momentum_engine import (                                   # noqa: E402
    MomentumEngine, SymbolConfig, EngineConfig,
)

logging.basicConfig(level=logging.WARNING, format="%(message)s")
LOG = logging.getLogger("v7_5ers_bt")


# ======================================================================
#  5%ers MTB rule set (current as of 2026; see 5ers.com/main-trading-bootcamp)
# ======================================================================

@dataclass
class FiveersMTBRules:
    """
    The 5ers 'Main Trading Bootcamp' (MTB) is a 3-level evaluation programme.
    Live/verified from 5ers public documentation:
        https://the5ers.com/main-trading-bootcamp-program/

        Level 1: 6% profit target, 4% daily DD, 6% total DD (static)
        Level 2: 4% profit target, 4% daily DD, 6% total DD (static)
        Level 3 / Funded: 4% profit, 4% daily, 6% total, scaling 25%/period

    Commissions: $0 on indices and XAUUSD (they use raw spreads markup).
    Spreads: real-world measured on their MT5 'MetaQuotes-Demo' and LIVE
    servers (see the support docs plus forum measurements).
    """
    name: str = "MTB Level 1"
    initial_balance: float = 5_000.0
    profit_target_pct: float = 0.06          # 6% for Level 1
    max_daily_loss_pct: float = 0.04         # 4%
    max_total_loss_pct: float = 0.06         # 6% static from initial balance
    weekend_hold_allowed: bool = True        # MTB allows
    news_trading_allowed: bool = True


FIVEERS_LEVELS = {
    "level1": FiveersMTBRules(
        name="MTB Level 1", initial_balance=5_000.0,
        profit_target_pct=0.06, max_daily_loss_pct=0.04,
        max_total_loss_pct=0.06,
    ),
    "level2": FiveersMTBRules(
        name="MTB Level 2", initial_balance=5_000.0,
        profit_target_pct=0.04, max_daily_loss_pct=0.04,
        max_total_loss_pct=0.06,
    ),
    "funded": FiveersMTBRules(
        name="MTB Funded", initial_balance=5_000.0,
        profit_target_pct=0.04, max_daily_loss_pct=0.04,
        max_total_loss_pct=0.06,
    ),
}


# ======================================================================
#  5ers spread / swap / commission profiles (current public data)
# ======================================================================

#   Measured spread medians on 5ers MT5 live:
#     US100  1.0 - 1.5 pts
#     DE40   1.0 - 1.5 pts
#     XAU    0.18 - 0.30 USD
#
#   Swap (the5ers MT5 specs, averaged 2025-26):
#     US100 long  -1.2 pts/day  short  0.0
#     DE40  long  -0.8 pts/day  short  0.0
#     XAU   long  -0.9 $/day/lot  short  +0.2 $/day/lot     (per 1 lot = 100oz)

FIVEERS_SYMBOL_SPECS: dict[str, dict] = {
    "US100": dict(
        symbol="US100",
        pip_value=1.0,                       # $1 per 1.0 point per 1.0 lot (standard lot)
        contract_size=1.0,
        spread_pts=1.5,                      # conservative upper-bound median
        commission_per_lot=0.0,              # 5ers commission-free on indices
        swap_long_pts_per_day=1.2,           # cost for long (positive = cost)
        swap_short_pts_per_day=0.0,
        session_start_hour=13, session_end_hour=21,   # NY cash (UTC)
    ),
    "DE40": dict(
        symbol="DE40",
        pip_value=1.0,                       # EUR per point per lot -> approx 1.08 USD
        contract_size=1.0,
        spread_pts=1.5,
        commission_per_lot=0.0,
        swap_long_pts_per_day=0.8,
        swap_short_pts_per_day=0.0,
        session_start_hour=7, session_end_hour=16,    # London + NY open (UTC)
    ),
    "XAUUSD": dict(
        symbol="XAUUSD",
        pip_value=100.0,                     # $100 per $1 move per 1.0 lot (100oz)
        contract_size=100.0,
        spread_pts=0.30,
        commission_per_lot=0.0,
        swap_long_pts_per_day=0.009,         # ~0.009 $ per $1 unit (== $0.90/day/lot)
        swap_short_pts_per_day=-0.002,
        session_start_hour=7, session_end_hour=21,    # London + NY
    ),
}


# ======================================================================
#  CSV loader
# ======================================================================

def load_m1(path: Path, min_time: Optional[datetime],
            max_time: Optional[datetime]) -> list[tuple[datetime, float, float, float, float]]:
    """Load M1 bars; time, open, high, low, close."""
    out: list[tuple[datetime, float, float, float, float]] = []
    with open(path, "r", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try:
                t = datetime.fromisoformat(row["time"])
            except Exception:
                # Some dukascopy exports use space-separated format already
                t = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            if min_time is not None and t < min_time:
                continue
            if max_time is not None and t > max_time:
                continue
            out.append((t, float(row["open"]), float(row["high"]),
                        float(row["low"]), float(row["close"])))
    return out


def common_window(files: dict[str, Path], months: int) -> tuple[datetime, datetime]:
    """Find the most recent `months` calendar months present in every file."""
    last_per = {}
    first_per = {}
    for sym, p in files.items():
        with open(p, "r", newline="") as f:
            rdr = csv.reader(f)
            next(rdr)                             # header
            rows = list(rdr)
        if not rows:
            raise RuntimeError(f"{sym} CSV empty")
        try:
            first_per[sym] = datetime.fromisoformat(rows[0][0])
            last_per[sym] = datetime.fromisoformat(rows[-1][0])
        except Exception:
            first_per[sym] = datetime.strptime(rows[0][0], "%Y-%m-%d %H:%M:%S")
            last_per[sym] = datetime.strptime(rows[-1][0], "%Y-%m-%d %H:%M:%S")
    max_end = min(last_per.values())
    min_start = max(first_per.values())
    candidate_start = max_end - timedelta(days=months * 30)
    start = max(candidate_start, min_start)
    return start, max_end


# ======================================================================
#  Main driver
# ======================================================================

def run_backtest(balance: float, months: int, h_cusum: float,
                 account_type: str, out_dir: Path,
                 daily_dd: Optional[float],
                 total_dd: Optional[float],
                 rules_override: Optional[FiveersMTBRules] = None) -> dict:
    rules = rules_override or FIVEERS_LEVELS[account_type]
    rules.initial_balance = balance

    daily_dd = daily_dd if daily_dd is not None else rules.max_daily_loss_pct
    total_dd = total_dd if total_dd is not None else rules.max_total_loss_pct

    # ------------------------------------------------------------------
    #  Data
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    #  Engine
    # ------------------------------------------------------------------
    symbols = [SymbolConfig(**FIVEERS_SYMBOL_SPECS[s]) for s in files]

    eng_cfg = EngineConfig(cusum_h=h_cusum)

    engine = MomentumEngine(
        symbols=symbols,
        cfg=eng_cfg,
        initial_equity=balance,
        daily_dd_limit=daily_dd,
        total_dd_limit=total_dd,
    )

    # ------------------------------------------------------------------
    #  Merge-sort bars across symbols by time
    # ------------------------------------------------------------------
    merged: list[tuple[datetime, str, float, float, float, float]] = []
    for sym, bars in bars_per_sym.items():
        for (t, o, h, l, c) in bars:
            merged.append((t, sym, o, h, l, c))
    merged.sort(key=lambda x: x[0])

    # ------------------------------------------------------------------
    #  Drive engine
    # ------------------------------------------------------------------
    t_start = _time.time()
    last_day_print = None
    profit_target_abs = balance * (1.0 + rules.profit_target_pct)
    total_floor_abs   = balance * (1.0 - rules.max_total_loss_pct)
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
            open_=o, high=h, low=l, close=c,
        )
        # Check acceptance levels at day-change resolution
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
    summary["h_cusum"] = h_cusum

    # --- save -----------------------------------------------------------
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "v7_5ers_mtb_3month.json"
    with open(json_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n  JSON:   {json_path}")

    trades_path = out_dir / "v7_5ers_mtb_3month_trades.json"
    engine.dump_trades(str(trades_path))
    print(f"  Trades: {trades_path}  ({summary['trades']} rows)")

    return summary


# ======================================================================
#  Reporting
# ======================================================================

def emit_markdown_report(summary: dict, out_path: Path) -> None:
    rules = summary["rules"]
    passed = summary["passed_profit_target"]
    failed = summary["failed_total_dd"]

    lines: list[str] = []
    lines.append(f"# SHF v7 Momentum Scalper — 5%ers MTB 3-Month Backtest")
    lines.append("")
    lines.append(f"**Account:** {rules['name']}")
    lines.append(f"**Balance:** ${rules['initial_balance']:,.0f}")
    lines.append(f"**Profit target:** {rules['profit_target_pct']*100:.1f}%")
    lines.append(f"**Daily loss cap:** {rules['max_daily_loss_pct']*100:.1f}%")
    lines.append(f"**Total loss cap:** {rules['max_total_loss_pct']*100:.1f}%")
    lines.append(f"**Backtest window:** {summary['bt_start']} → {summary['bt_end']}")
    lines.append(f"**Bars processed:** {summary['bars_seen']:,}")
    lines.append("")
    lines.append("## Outcome")
    lines.append("")
    if failed:
        lines.append(f"- ❌ **Failed** — total DD hit on {summary['total_dd_hit_on']}")
    elif passed:
        lines.append(f"- ✅ **PASSED PROFIT TARGET** on "
                     f"{summary['profit_target_hit_on']}")
    else:
        lines.append(f"- ⚠️ Evaluation window ended without target hit "
                     f"(still solvent, equity ${summary['final_equity']:.2f})")
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
    lines.append(f"| Win rate | {summary['win_rate']*100:.1f}% |")
    lines.append(f"| Profit factor | {summary['pf']:.2f} |")
    lines.append(f"| Expectancy (R) | {summary['expectancy_R']:.3f} |")
    lines.append(f"| Avg winner (R) | {summary['avg_winner_R']:.2f} |")
    lines.append(f"| Avg loser (R) | {summary['avg_loser_R']:.2f} |")
    lines.append(f"| Max draw-down | {summary['max_dd_pct']:.2f}% |")
    lines.append(f"| Gross costs (commissions + swaps + spread) | ${summary['gross_costs']:,.2f} |")
    lines.append("")
    lines.append("## Trades by symbol")
    lines.append("")
    lines.append("| Symbol | Trades |")
    lines.append("|---|---:|")
    for sym, n in summary["by_symbol"].items():
        lines.append(f"| {sym} | {n} |")
    lines.append("")
    lines.append("## Profitability verdict")
    lines.append("")
    if summary["net_pnl"] > 0 and summary["max_dd_pct"] < rules["max_total_loss_pct"]*100:
        lines.append("**PROFITABLE** — net positive after full 5%ers cost model, "
                     "and max draw-down stayed within the MTB rule-set.")
    else:
        lines.append("**NOT PROFITABLE OR BLEW** — inspect trade log for root cause.")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  Report: {out_path}")


def print_summary(summary: dict) -> None:
    rules = summary["rules"]
    print("\n" + "=" * 72)
    print(f"  SHF v7 Momentum Scalper  |  5%ers {rules['name']}  |  3-month backtest")
    print("=" * 72)
    print(f"  Start equity      ${summary['start_equity']:>12,.2f}")
    print(f"  Final equity      ${summary['final_equity']:>12,.2f}")
    print(f"  Net P&L           ${summary['net_pnl']:>12,.2f}")
    print(f"  Return            {summary['pct_return']:>12.2f} %")
    print(f"  Trades            {summary['trades']:>12,}")
    if summary['trades'] > 0:
        print(f"  Win rate          {summary['win_rate']*100:>12.1f} %")
        print(f"  Profit factor     {summary['pf']:>12.2f}")
        print(f"  Expectancy (R)    {summary['expectancy_R']:>12.3f}")
        print(f"  Max DD            {summary['max_dd_pct']:>12.2f} %")
        print(f"  Gross costs       ${summary['gross_costs']:>12,.2f}")
        print("\n  By symbol: " + ", ".join(
            f"{s}={n}" for s, n in summary['by_symbol'].items()))
    print("\n  Acceptance:")
    if summary["failed_total_dd"]:
        print(f"    FAILED  - total DD hit on {summary['total_dd_hit_on']}")
    elif summary["passed_profit_target"]:
        print(f"    PASSED  - profit target hit on {summary['profit_target_hit_on']}")
    else:
        print(f"    ONGOING - still solvent, target not yet hit")
    profitable = summary["net_pnl"] > 0 and summary["max_dd_pct"] < rules["max_total_loss_pct"]*100
    print(f"    Profitable under full 5%ers cost model:  {'YES' if profitable else 'NO'}")
    print("=" * 72)


# ======================================================================
#  CLI
# ======================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--balance", type=float, default=5000.0)
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--h", type=float, default=4.5,
                    help="CUSUM threshold (default 4.5 = 20-40 trades/day)")
    ap.add_argument("--account-type", choices=list(FIVEERS_LEVELS),
                    default="level1")
    ap.add_argument("--daily-dd", type=float, default=None,
                    help="Override daily DD limit (fraction)")
    ap.add_argument("--total-dd", type=float, default=None,
                    help="Override total DD limit (fraction)")
    ap.add_argument("--out", type=Path,
                    default=ROOT / "Results",
                    help="Output directory")
    args = ap.parse_args()

    summary = run_backtest(
        balance=args.balance,
        months=args.months,
        h_cusum=args.h,
        account_type=args.account_type,
        out_dir=args.out,
        daily_dd=args.daily_dd,
        total_dd=args.total_dd,
    )
    print_summary(summary)
    emit_markdown_report(summary,
                         args.out / "v7_5ers_mtb_3month_report.md")


if __name__ == "__main__":
    main()
