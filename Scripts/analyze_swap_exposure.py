"""Analyse swap exposure for SmartBB trades.

Uses v13 + v14 trade logs (these have real entry/exit timestamps) as proxy
for v15's trade behaviour — same engine family, same session rules.

5%ers swap (from user's US30 MT5 spec, 2026-04-18):
    Swap type: In points
    Swap long  = -720
    Swap short = -720
    Weekend multiplier: x3

Swap-in-dollars conversion for 5%ers indices:
    contract_size = 1 (1 lot = 1 contract)
    point = 0.01 price units
    point value at 1 lot = $1 per 1.0 price move = $0.01 per 0.01 (per point)
    Swap per lot per night = 720 points * $0.01 / point = $7.20 per lot
    Friday roll = 3x = $21.60 per lot

For XAUUSD we don't have the exact 5%ers swap spec, so we use conservative
$5/lot/night (typical industry level for gold with negative carry).
"""
from __future__ import annotations
import json
import sys
import datetime as dt
from pathlib import Path
from collections import defaultdict

ROOT = Path(__file__).resolve().parent.parent

SWAP_PER_LOT_NIGHT = {
    "US30":   7.20,
    "US100":  7.20,
    "US500":  7.20,
    "DE40":   7.20,
    "UK100":  7.20,
    "JP225":  7.20,
    "XAUUSD": 5.00,
    "XAGUSD": 3.00,
    "USOIL": 10.00,
    "UKOIL": 10.00,
}

TRADE_FILES = [
    "Results/v13_smartbb_100000_3m_trades.json",
    "Results/v14_smartbb_100000_3m_trades.json",
]

def swap_nights(entry_ts: float, exit_ts: float) -> int:
    """Count 20:00 UTC rollovers between entry and exit.  Friday = 3x (weekend)."""
    if exit_ts <= entry_ts:
        return 0
    entry = dt.datetime.fromtimestamp(entry_ts, tz=dt.timezone.utc)
    exit_ = dt.datetime.fromtimestamp(exit_ts, tz=dt.timezone.utc)
    rollover = entry.replace(hour=20, minute=0, second=0, microsecond=0)
    if rollover <= entry:
        rollover += dt.timedelta(days=1)
    nights = 0
    while rollover < exit_:
        weekday = rollover.weekday()
        if weekday == 4:
            nights += 3
        elif weekday in (5, 6):
            pass
        else:
            nights += 1
        rollover += dt.timedelta(days=1)
    return nights

def load_trades() -> list[dict]:
    all_trades = []
    for rel in TRADE_FILES:
        path = ROOT / rel
        if not path.exists():
            continue
        with open(path) as f:
            data = json.load(f)
        trades = data if isinstance(data, list) else data.get("trades", [])
        for t in trades:
            t["_source"] = rel
        all_trades.extend(trades)
    return all_trades

def main() -> None:
    trades = load_trades()
    if not trades:
        print("No trades found")
        return
    print("=" * 90)
    print(f"SWAP EXPOSURE ANALYSIS — {len(trades)} trades from v13+v14 backtest logs")
    print("=" * 90)
    print()
    by_sym = defaultdict(list)
    for t in trades:
        by_sym[t["symbol"]].append(t)
    totals_pnl = 0.0
    totals_swap = 0.0
    totals_over = 0
    totals_n = 0
    for sym in sorted(by_sym.keys()):
        syms = by_sym[sym]
        n = len(syms)
        rate = SWAP_PER_LOT_NIGHT.get(sym, 5.0)
        durations_min = []
        n_overnight = 0
        total_nights = 0
        total_swap_cost = 0.0
        total_pnl = 0.0
        for t in syms:
            e, x = t["entry_time"], t["exit_time"]
            dur = (x - e) / 60.0
            durations_min.append(dur)
            total_pnl += t.get("net_pnl", 0.0)
            nights = swap_nights(e, x)
            if nights > 0:
                n_overnight += 1
            total_nights += nights
            total_swap_cost += nights * t["lots"] * rate
        dur_mean = sum(durations_min) / max(1, n)
        dur_p50  = sorted(durations_min)[len(durations_min) // 2] if durations_min else 0
        dur_p95  = sorted(durations_min)[int(0.95 * len(durations_min))] if durations_min else 0
        dur_max  = max(durations_min) if durations_min else 0
        pct_ov   = 100.0 * n_overnight / max(1, n)
        swap_pct = 100.0 * total_swap_cost / max(1.0, abs(total_pnl))
        net_after_swap = total_pnl - total_swap_cost
        print(f"  {sym:<8s}  n={n:<4d}  dur(min)  mean={dur_mean:>6.1f}  med={dur_p50:>6.1f}  p95={dur_p95:>7.1f}  max={dur_max:>9.1f}")
        print(f"  {'':8s}  overnight: {n_overnight:>3d} trades ({pct_ov:>5.1f}%),  {total_nights:>3d} nights total")
        print(f"  {'':8s}  net PnL:  ${total_pnl:>10,.0f}   swap cost:  ${total_swap_cost:>8,.2f}  ({swap_pct:>5.1f}% of gross PnL)")
        print(f"  {'':8s}  NET AFTER SWAP:  ${net_after_swap:>10,.0f}")
        print()
        totals_pnl += total_pnl
        totals_swap += total_swap_cost
        totals_over += n_overnight
        totals_n += n
    pct_ov = 100.0 * totals_over / max(1, totals_n)
    print("=" * 90)
    print(f"TOTAL across all symbols:")
    print(f"  {totals_n} trades, {totals_over} overnight ({pct_ov:.1f}%)")
    print(f"  PnL before swap: ${totals_pnl:>10,.0f}")
    print(f"  Swap cost:       ${totals_swap:>10,.2f}")
    print(f"  PnL AFTER swap:  ${totals_pnl - totals_swap:>10,.0f}")
    print(f"  Swap drag:       {100.0 * totals_swap / max(1.0, abs(totals_pnl)):.2f}% of PnL")
    print()
    print("Swap rates used (per lot per night): US30/US100/US500/DE40 = $7.20, XAUUSD = $5.00")
    print("Assumes 20:00 UTC rollover (5%ers EET-based), Friday = 3x weekend multiplier.")

if __name__ == "__main__":
    main()
