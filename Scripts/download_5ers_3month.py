"""
Download last 3 months of M1 data from the 5%ers MT5 terminal
=============================================================
Source : 5%ers broker (server FivePercentOnline-Real)
Period : last 3 calendar months, ending NOW
Symbols: US30, NAS100, SP500, DAX40, UK100, JPN225, XAUUSD, XAGUSD (all 8)

Saves to ``data/historical/{internal_name}_M1.csv`` using the engine's
internal symbol names (US30 / US100 / US500 / DE40 / UK100 / JP225 /
XAUUSD / XAGUSD) so the backtest scripts pick them up directly. Existing
files are backed up first to ``data/historical_backup/`` (timestamped).

If a broker ticker is not found, we auto-probe common aliases before giving
up (e.g. UK100 -> UK100. / UK100.a / UK100pro / FTSE100 / FTSE100.).
A provenance sidecar ``data/historical/_provenance.json`` records server,
account, and download timestamp per symbol so we can always prove every
CSV came from 5%ers MTB.

Data columns match the existing CSV schema:
  time, open, high, low, close, tick_volume
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

# Force UTF-8 stdout on Windows cp1252 consoles
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DATA_DIR = Path("data/historical")
BACKUP_DIR = Path("data/historical_backup")
PROVENANCE = DATA_DIR / "_provenance.json"
MONTHS_BACK = 3
CHUNK_DAYS = 14           # 14-day chunks (~20k M1 bars per chunk, safe)

# Primary broker symbol -> engine-internal save name.
# If a primary name doesn't resolve on the terminal, we'll try the aliases
# listed in ``ALIASES`` below.
SYMBOLS: dict[str, str] = {
    "US30":    "US30",      # Dow
    "NAS100":  "US100",     # Nasdaq
    "SP500":   "US500",     # S&P 500
    "DAX40":   "DE40",      # DAX
    "UK100":   "UK100",     # FTSE 100
    "JPN225":  "JP225",     # Nikkei
    "XAUUSD":  "XAUUSD",    # Gold
    "XAGUSD":  "XAGUSD",    # Silver
}

# Fallback probes, in priority order. If the primary fails, we try each
# alias until one selects successfully. The chosen ticker is recorded in
# _provenance.json.
ALIASES: dict[str, list[str]] = {
    "US30":    ["US30.", "US30.a", "US30pro", "DJ30", "WALL30"],
    "NAS100":  ["NAS100.", "NAS100.a", "NAS100pro", "US100", "NDX100"],
    "SP500":   ["SP500.", "SP500.a", "SP500pro", "US500", "SPX500"],
    "DAX40":   ["DAX40.", "DAX40.a", "DAX40pro", "DE40", "GER40"],
    "UK100":   ["UK100.", "UK100.a", "UK100pro", "FTSE100", "FTSE100."],
    "JPN225":  ["JPN225.", "JPN225.a", "JPN225pro", "JP225", "NIKKEI225", "NIKKEI"],
    "XAUUSD":  ["XAUUSD.", "XAUUSD.a", "XAUUSDpro", "GOLD"],
    "XAGUSD":  ["XAGUSD.", "XAGUSD.a", "XAGUSDpro", "SILVER"],
}


def _select(sym: str) -> bool:
    ok = mt5.symbol_select(sym, True)
    _time.sleep(0.3)
    return bool(ok)


def _resolve_ticker(primary: str) -> str | None:
    """Return the first ticker name that symbol_select() accepts.

    Tries the primary name first, then each alias. Prints which one wins.
    """
    if _select(primary):
        print(f"    resolved: '{primary}' [primary]")
        return primary
    print(f"    primary '{primary}' not available "
          f"({mt5.last_error()}) — probing aliases...")
    for a in ALIASES.get(primary, []):
        if _select(a):
            print(f"    resolved: '{a}' [alias]")
            return a
    print(f"    [X] no working ticker for {primary} on this terminal")
    return None


def _download_one(ticker: str, start: datetime, end: datetime) -> pd.DataFrame | None:
    frames: list[pd.DataFrame] = []
    cur_end = end
    chunk = 0
    while cur_end > start:
        chunk += 1
        cur_start = max(start, cur_end - timedelta(days=CHUNK_DAYS))
        rates = mt5.copy_rates_range(ticker, mt5.TIMEFRAME_M1,
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
    print("=" * 78)
    print(f"  5%ers MT5 3-month M1 downloader  "
          f"(ending {datetime.now():%Y-%m-%d %H:%M})")
    print("=" * 78)

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
    print("-" * 78)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    summary: list[tuple[str, str, str, int, str, str]] = []
    provenance: dict[str, dict] = {}
    now_iso = datetime.now().isoformat(timespec="seconds")
    server = getattr(acc, "server", "unknown")
    login = getattr(acc, "login", "unknown")

    for primary, save_name in SYMBOLS.items():
        print(f"\n{primary:<8s} -> {save_name}_M1.csv")
        ticker = _resolve_ticker(primary)
        if ticker is None:
            summary.append((primary, "-", save_name, 0, "-", "-"))
            continue
        df = _download_one(ticker, start, end)
        if df is None or df.empty:
            print(f"    [X] no data downloaded for {ticker}")
            summary.append((primary, ticker, save_name, 0, "-", "-"))
            continue
        out = DATA_DIR / f"{save_name}_M1.csv"
        _backup(out)
        df.to_csv(out, index=False)
        first, last = df["time"].iloc[0], df["time"].iloc[-1]
        days = (last - first).days
        print(f"    [OK] saved {len(df):,} bars  ({first} -> {last})  "
              f"{days} days")
        summary.append((primary, ticker, save_name, len(df),
                        str(first), str(last)))
        provenance[save_name] = {
            "internal_name": save_name,
            "primary_broker_ticker": primary,
            "resolved_broker_ticker": ticker,
            "server": server,
            "login": str(login),
            "downloaded_utc": now_iso,
            "bars": int(len(df)),
            "first_bar": str(first),
            "last_bar":  str(last),
        }

    mt5.shutdown()

    # persist provenance
    if provenance:
        with open(PROVENANCE, "w", encoding="utf-8") as f:
            json.dump(provenance, f, indent=2)
        print(f"\n  Provenance written: {PROVENANCE}")

    print("\n" + "=" * 78)
    print(f"  {'primary':<8s} {'ticker':<10s} {'save':<8s} "
          f"{'bars':>10s}  {'first':<20s} {'last':<20s}")
    print("-" * 78)
    for primary, ticker, sn, n, first, last in summary:
        print(f"  {primary:<8s} {ticker:<10s} {sn:<8s} {n:>10,}  "
              f"{first:<20s} {last:<20s}")
    ok = sum(1 for s in summary if s[3] > 0)
    print("=" * 78)
    print(f"  {ok}/{len(summary)} symbols updated with fresh 5%ers MTB data.")
    if ok == len(summary):
        print("  Next:  python Scripts\\final_answer_sweep.py")
    return 0 if ok == len(summary) else 3


if __name__ == "__main__":
    raise SystemExit(main())
