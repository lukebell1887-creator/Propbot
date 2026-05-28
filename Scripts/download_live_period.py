"""
download_live_period.py
=======================

Downloads M1 bars from the 5%ers MT5 terminal for an EXPLICIT date window
(default: April 20 2026 -> today).  This is for "what would the backtest
have done over the same period the live bot has been running?".

Saves to ``data/historical/{NAME}_M1.csv`` overwriting whatever is there,
but FIRST backs up the existing files to ``data/historical_backup_pre_liveperiod/``
so we can restore the Jan-Apr CSVs afterwards.

Usage:
    python Scripts/download_live_period.py
    python Scripts/download_live_period.py 2026-04-20 2026-05-28
"""
from __future__ import annotations

import json
import shutil
import sys
import time as _time
from datetime import datetime, timedelta
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DATA_DIR   = Path("data/historical")
BACKUP_DIR = Path("data/historical_backup_pre_liveperiod")
PROVENANCE = DATA_DIR / "_provenance.json"
CHUNK_DAYS = 7

# Only the 4 symbols the live v30 bot trades
SYMBOLS = {
    "US30":   "US30",
    "SP500":  "US500",
    "DAX40":  "DE40",
    "XAUUSD": "XAUUSD",
}
ALIASES = {
    "US30":   ["US30.", "US30.a", "US30pro", "DJ30", "WALL30"],
    "SP500":  ["SP500.", "SP500.a", "SP500pro", "US500", "SPX500"],
    "DAX40":  ["DAX40.", "DAX40.a", "DAX40pro", "DE40", "GER40"],
    "XAUUSD": ["XAUUSD.", "XAUUSD.a", "XAUUSDpro", "GOLD"],
}


def _select(sym: str) -> bool:
    ok = mt5.symbol_select(sym, True)
    _time.sleep(0.2)
    return bool(ok)


def _resolve_ticker(primary: str) -> str | None:
    if _select(primary):
        print(f"    resolved: '{primary}' [primary]"); return primary
    print(f"    primary '{primary}' failed -> probing aliases...")
    for a in ALIASES.get(primary, []):
        if _select(a):
            print(f"    resolved: '{a}' [alias]"); return a
    print(f"    [X] no ticker for {primary}")
    return None


def _download(ticker: str, start: datetime, end: datetime) -> pd.DataFrame | None:
    frames = []
    cur_end = end
    chunk = 0
    while cur_end > start:
        chunk += 1
        cur_start = max(start, cur_end - timedelta(days=CHUNK_DAYS))
        rates = mt5.copy_rates_range(ticker, mt5.TIMEFRAME_M1, cur_start, cur_end)
        if rates is None or len(rates) == 0:
            print(f"    chunk {chunk}: {cur_start.date()} -> {cur_end.date()}  "
                  f"no data ({mt5.last_error()})")
        else:
            df = pd.DataFrame(rates)
            df["time"] = pd.to_datetime(df["time"], unit="s")
            df = df[["time", "open", "high", "low", "close", "tick_volume"]]
            frames.append(df)
            print(f"    chunk {chunk}: {df['time'].iloc[0]} -> "
                  f"{df['time'].iloc[-1]}  {len(df):,} bars")
        cur_end = cur_start - timedelta(minutes=1)
        _time.sleep(0.2)
    if not frames:
        return None
    return (pd.concat(frames, ignore_index=True)
              .drop_duplicates(subset=["time"])
              .sort_values("time")
              .reset_index(drop=True))


def _backup_all_existing():
    """Move existing CSVs to BACKUP_DIR so we can restore later."""
    if not DATA_DIR.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    for sn in SYMBOLS.values():
        src = DATA_DIR / f"{sn}_M1.csv"
        if src.exists():
            dst = BACKUP_DIR / f"{sn}_M1.csv"
            shutil.copy2(src, dst)
            print(f"    backup: {src.name} -> {dst}")


def main():
    args = sys.argv[1:]
    if len(args) >= 2:
        start = datetime.strptime(args[0], "%Y-%m-%d")
        end   = datetime.strptime(args[1], "%Y-%m-%d") + timedelta(days=1)
    else:
        # Default: cover live bot window with some history for warmup
        start = datetime(2026, 4, 20)
        end   = datetime.now()

    print("=" * 78)
    print(f"  Download LIVE PERIOD bars from 5%ers MT5")
    print(f"  window : {start.date()} -> {end.date()}")
    print("=" * 78)

    if not mt5.initialize():
        print(f"  [X] MT5 init failed: {mt5.last_error()}")
        return 2
    term = mt5.terminal_info()
    acc  = mt5.account_info()
    print(f"  terminal : {term.name}")
    print(f"  server   : {acc.server}")
    print(f"  account  : {acc.login}  balance=${acc.balance:,.2f}")
    print("-" * 78)

    print("  backing up existing CSVs first...")
    _backup_all_existing()
    print("-" * 78)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    summary = []
    provenance = {}
    now_iso = datetime.now().isoformat(timespec="seconds")
    for primary, save_name in SYMBOLS.items():
        print(f"\n{primary:<8s} -> {save_name}_M1.csv")
        ticker = _resolve_ticker(primary)
        if ticker is None:
            summary.append((primary, "-", save_name, 0, "-", "-")); continue
        df = _download(ticker, start, end)
        if df is None or df.empty:
            print(f"    [X] no data"); summary.append((primary, ticker, save_name, 0, "-", "-")); continue
        out = DATA_DIR / f"{save_name}_M1.csv"
        df.to_csv(out, index=False)
        first, last = df['time'].iloc[0], df['time'].iloc[-1]
        print(f"    [OK] saved {len(df):,} bars  ({first} -> {last})")
        summary.append((primary, ticker, save_name, len(df), str(first), str(last)))
        provenance[save_name] = {
            "internal_name": save_name, "primary_broker_ticker": primary,
            "resolved_broker_ticker": ticker, "server": acc.server,
            "login": str(acc.login), "downloaded_utc": now_iso,
            "window_start": str(start), "window_end": str(end),
            "bars": int(len(df)),
            "first_bar": str(first), "last_bar": str(last),
        }
    mt5.shutdown()

    if provenance:
        with open(PROVENANCE, "w", encoding="utf-8") as f:
            json.dump(provenance, f, indent=2)

    print("\n" + "=" * 78)
    print(f"  {'primary':<8} {'ticker':<10} {'save':<8} {'bars':>10} {'first':<20} {'last':<20}")
    for primary, ticker, sn, n, first, last in summary:
        print(f"  {primary:<8} {ticker:<10} {sn:<8} {n:>10,}  {first:<20} {last:<20}")
    ok = sum(1 for s in summary if s[3] > 0)
    print(f"\n  {ok}/{len(summary)} symbols updated for the LIVE PERIOD window.")
    print("=" * 78)
    return 0 if ok == len(summary) else 3


if __name__ == "__main__":
    raise SystemExit(main())
