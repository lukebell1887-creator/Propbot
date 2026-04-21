"""
Download last 3 months of M1 data from the 5%ers MT5 terminal
=============================================================
Source : 5%ers broker (server FivePercentOnline-Real)
Period : last 3 calendar months, ending NOW
Symbols: US30, NAS100, SP500, DAX40, XAUUSD

Saves to `data/historical/{internal_name}_M1.csv` using the engine's
internal symbol names (US30 / US100 / US500 / DE40 / XAUUSD) so the backtest
scripts pick them up directly. Existing files are backed up first to
`data/historical_backup/` (timestamped).

Data columns match the existing CSV schema:
  time, open, high, low, close, tick_volume
"""
from __future__ import annotations

import shutil
import sys
import time as _time
from datetime import datetime, timedelta
from pathlib import Path

import MetaTrader5 as mt5
import pandas as pd

# Force UTF-8 stdout on Windows cp1252 consoles
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DATA_DIR = Path("data/historical")
BACKUP_DIR = Path("data/historical_backup")
MONTHS_BACK = 3
CHUNK_DAYS = 14           # 14-day chunks (~20k M1 bars per chunk, safe)

# Broker symbol -> engine-internal save name
SYMBOLS: dict[str, str] = {
    "US30":    "US30",      # Dow
    "NAS100":  "US100",     # Nasdaq
    "SP500":   "US500",     # S&P 500
    "DAX40":   "DE40",      # DAX
    "XAUUSD":  "XAUUSD",    # Gold
}


def _select(sym: str) -> bool:
    ok = mt5.symbol_select(sym, True)
    _time.sleep(0.3)
    if not ok:
        print(f"    [X] symbol_select({sym}) failed: {mt5.last_error()}")
    return ok


def _download_one(broker_sym: str, save_name: str,
                  start: datetime, end: datetime) -> pd.DataFrame | None:
    if not _select(broker_sym):
        return None
    frames = []
    cur_end = end
    chunk = 0
    while cur_end > start:
        chunk += 1
        cur_start = max(start, cur_end - timedelta(days=CHUNK_DAYS))
        rates = mt5.copy_rates_range(broker_sym, mt5.TIMEFRAME_M1,
                                      cur_start, cur_end)
        if rates is None or len(rates) == 0:
            err = mt5.last_error()
            print(f"    chunk {chunk}: {cur_start.date()} -> {cur_end.date()}  "
                  f"no data ({err})")
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
    full = (pd.concat(frames, ignore_index=True)
              .drop_duplicates(subset=["time"])
              .sort_values("time")
              .reset_index(drop=True))
    return full


def _backup(path: Path) -> None:
    if not path.exists():
        return
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"{path.stem}_{stamp}{path.suffix}"
    shutil.copy2(path, dst)
    print(f"    backup: {path.name} -> {dst.name}")


def main() -> int:
    print("=" * 76)
    print(f"  5%ers MT5 3-month M1 downloader  "
          f"(ending {datetime.now():%Y-%m-%d %H:%M})")
    print("=" * 76)

    if not mt5.initialize():
        print(f"  [X] MT5 init failed: {mt5.last_error()}")
        print(f"      Make sure the 5%ers MT5 terminal is open and logged in.")
        return 2

    term = mt5.terminal_info()
    acc = mt5.account_info()
    print(f"  terminal : {term.name}")
    print(f"  server   : {acc.server}")
    print(f"  account  : {acc.login}  balance=${acc.balance:,.2f}  "
          f"equity=${acc.equity:,.2f}")

    end = datetime.now()
    start = end - timedelta(days=MONTHS_BACK * 31)
    print(f"  window   : {start:%Y-%m-%d} -> {end:%Y-%m-%d}  "
          f"({MONTHS_BACK} months)")
    print("-" * 76)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    summary: list[tuple[str, str, int, str, str]] = []

    for broker_sym, save_name in SYMBOLS.items():
        print(f"\n{broker_sym:<8s} -> {save_name}_M1.csv")
        df = _download_one(broker_sym, save_name, start, end)
        if df is None or df.empty:
            print(f"    [X] no data downloaded for {broker_sym}")
            summary.append((broker_sym, save_name, 0, "-", "-"))
            continue
        out = DATA_DIR / f"{save_name}_M1.csv"
        _backup(out)
        df.to_csv(out, index=False)
        first, last = df["time"].iloc[0], df["time"].iloc[-1]
        days = (last - first).days
        print(f"    [OK] saved {len(df):,} bars  "
              f"({first} -> {last})  {days} days")
        summary.append((broker_sym, save_name, len(df),
                        str(first), str(last)))

    mt5.shutdown()

    print("\n" + "=" * 76)
    print(f"  {'broker':<8s} {'save':<8s} {'bars':>10s}  "
          f"{'first':<20s} {'last':<20s}")
    print("-" * 76)
    for bs, sn, n, first, last in summary:
        print(f"  {bs:<8s} {sn:<8s} {n:>10,}  "
              f"{first:<20s} {last:<20s}")
    ok = sum(1 for s in summary if s[2] > 0)
    print("=" * 76)
    print(f"  {ok}/{len(summary)} symbols updated with fresh 5%ers data.")
    if ok == len(summary):
        print("  Next:  python Scripts\\backtest_v15_oos_3month.py")
    return 0 if ok == len(summary) else 3


if __name__ == "__main__":
    raise SystemExit(main())
