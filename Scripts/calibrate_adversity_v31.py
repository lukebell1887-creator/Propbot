"""
calibrate_adversity_v31.py — STAGE 1 of the v31 slippage-defense proof.

What it does
============
For each of TODAY's 4 closed positions on the live MT5 account, this script:
  1. Pulls the entry, exit and SL from MT5 deal history (truth source).
  2. Pulls the 5 M1 bars surrounding the exit minute from MT5 live data.
  3. Identifies the "stop-out bar" — the M1 bar where price crossed the SL.
  4. Measures actual slip:        actual_slip = |fill_px − SL|       (pts)
  5. Measures bar-extreme excess: bar_excess  = |bar_extreme − SL|  (pts)
  6. Computes adversity factor:   adversity   = actual_slip / bar_excess
                                  (∈ [0, 1] — fraction of the bar's worst-case
                                   extension that the broker actually filled at)

The adversity factor characterises how aggressively this specific broker fills
stop orders relative to the M1 bar's intra-minute movement.  An adversity of:
  • 0.0  → broker fills exactly at SL (no slippage, idealised)
  • 0.5  → broker fills halfway between SL and the bar's extreme
  • 1.0  → broker fills at the bar's worst extreme (catastrophic)

Why this matters
================
The 3-month backtest (Results/v30_fresh_trades.json) assumed slip = 1 tick
on every stop-out.  That's a fiction.  By calibrating the adversity factor
against TODAY's 4 measured slips, we can replay the 264 historical trades
with bar-accurate slip estimates — giving us the HONEST P&L the bot would
have produced in real life.

Output
======
  Results/v31_adversity_calibration.json
  {
    "calibrated_at_utc": "2026-04-30T13:55:00Z",
    "samples": [
      {
        "ticket": 545278227,
        "symbol": "DAX40",
        "side": "SHORT",
        "exit_time_utc": "...",
        "sl_price": ...,
        "fill_price": ...,
        "bar_high": ...,
        "bar_low": ...,
        "actual_slip_pts": ...,
        "bar_excess_pts": ...,
        "adversity_factor": ...
      },
      ...
    ],
    "per_symbol": {
        "DE40":  {"adversity": ..., "n": ..., "actual_slip_pts": ...},
        "US30":  {...},
        "US500": {...},
        "XAUUSD":{...}
    },
    "fallback_adversity": ...     # weighted mean across all samples
  }

Usage
=====
    # Mode 1: pass position tickets (recommended — most reliable)
    python Scripts/calibrate_adversity_v31.py 545278227 545502968 545509924 545524760

    # Mode 2: no args — auto-discover all closed positions in last 24h
    python Scripts/calibrate_adversity_v31.py
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

try:
    import MetaTrader5 as mt5  # type: ignore
except ImportError:
    print("[FATAL] MetaTrader5 package not installed. Run: pip install MetaTrader5")
    sys.exit(1)


ROOT = Path(__file__).resolve().parents[1]
OUT_PATH = ROOT / "Results" / "v31_adversity_calibration.json"


# --- Map MT5 broker symbol names → our internal canonical names ------------
# (must match the canonical names used in src/live/v30_live.py)
_BROKER_TO_CANON = {
    "DAX40": "DE40",
    "GER40": "DE40",
    "DE40": "DE40",
    "US30": "US30",
    "US30.cash": "US30",
    "DJI30": "US30",
    "SP500": "US500",
    "US500": "US500",
    "SPX500": "US500",
    "XAUUSD": "XAUUSD",
    "GOLD": "XAUUSD",
}


def canon(broker_symbol: str) -> str:
    """Map broker name to canonical symbol; fall back to as-is."""
    return _BROKER_TO_CANON.get(broker_symbol, broker_symbol)


def fetch_position_details(pid: int) -> Optional[dict]:
    """Fetch entry/exit deals and SL for a closed position; return a dict
    with everything we need, or None if not found / not closed."""
    deals = mt5.history_deals_get(position=pid)
    if not deals or len(deals) < 2:
        # Wide fallback search
        now = datetime.now(timezone.utc)
        all_deals = mt5.history_deals_get(now - timedelta(days=14), now)
        if all_deals:
            deals = [d for d in all_deals if d.position_id == pid]

    if not deals or len(deals) < 2:
        print(f"[WARN] No deal pair found for position {pid}")
        return None

    in_deals  = [d for d in deals if d.entry == mt5.DEAL_ENTRY_IN]
    out_deals = [d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT,
                                                  mt5.DEAL_ENTRY_OUT_BY)]
    if not in_deals or not out_deals:
        print(f"[WARN] Position {pid} not yet closed (or missing entry).")
        return None

    d_in, d_out = in_deals[0], out_deals[-1]
    side = "BUY" if d_in.type == mt5.DEAL_TYPE_BUY else "SELL"

    # --- Find SL the bot actually placed ---
    orders = mt5.history_orders_get(position=pid)
    if not orders:
        now = datetime.now(timezone.utc)
        orders = mt5.history_orders_get(now - timedelta(days=14), now)
        orders = [o for o in (orders or []) if o.position_id == pid]

    sl_price = None
    for o in orders or []:
        if getattr(o, "sl", 0) and o.sl > 0:
            sl_price = o.sl
            break
    # Last fallback: take SL off any current/recently-modified order metadata
    if sl_price is None:
        for o in orders or []:
            if hasattr(o, "price_stoplimit") and o.price_stoplimit > 0:
                sl_price = o.price_stoplimit
                break

    return {
        "ticket": pid,
        "broker_symbol": d_in.symbol,
        "symbol": canon(d_in.symbol),
        "side": side,
        "lots": float(d_in.volume),
        "entry_time": int(d_in.time),
        "exit_time": int(d_out.time),
        "entry_price": float(d_in.price),
        "exit_price": float(d_out.price),
        "sl_price": float(sl_price) if sl_price else None,
    }


def fetch_bar_at(broker_symbol: str, ts_unix: int) -> Optional[dict]:
    """Fetch the M1 bar that contains exit-time ts_unix.

    We pull a window of ±3 minutes and pick the bar whose [open_time,
    open_time+60s) contains ts_unix.  If exit_time is on a 60-second boundary
    we take the bar whose start == exit_time.
    """
    bar_start_target = ts_unix - (ts_unix % 60)  # round down to minute
    rates = mt5.copy_rates_from(broker_symbol, mt5.TIMEFRAME_M1,
                                datetime.fromtimestamp(bar_start_target + 5,
                                                       tz=timezone.utc),
                                3)
    if rates is None or len(rates) == 0:
        return None
    # Pick the bar with start time exactly == bar_start_target
    for r in rates:
        if int(r["time"]) == bar_start_target:
            return {
                "time_utc": datetime.fromtimestamp(int(r["time"]),
                                                   tz=timezone.utc).isoformat(),
                "open":   float(r["open"]),
                "high":   float(r["high"]),
                "low":    float(r["low"]),
                "close":  float(r["close"]),
            }
    # Fall back to first returned bar
    r = rates[0]
    return {
        "time_utc": datetime.fromtimestamp(int(r["time"]),
                                           tz=timezone.utc).isoformat(),
        "open":   float(r["open"]),
        "high":   float(r["high"]),
        "low":    float(r["low"]),
        "close":  float(r["close"]),
    }


def compute_sample(pos: dict) -> Optional[dict]:
    """Combine position details + exit bar into one calibration sample."""
    if pos["sl_price"] is None:
        print(f"  [SKIP {pos['ticket']}] No SL price recorded.")
        return None

    bar = fetch_bar_at(pos["broker_symbol"], pos["exit_time"])
    if bar is None:
        print(f"  [SKIP {pos['ticket']}] Could not fetch exit bar.")
        return None

    sl   = pos["sl_price"]
    fill = pos["exit_price"]

    # Determine bar extreme on the adverse side of the stop:
    #   SHORT stop fires when price goes UP through SL → adverse extreme = bar_high
    #   LONG  stop fires when price goes DOWN through SL → adverse extreme = bar_low
    if pos["side"] == "SELL":
        bar_extreme = bar["high"]
        actual_slip = max(0.0, fill - sl)             # +ve = filled ABOVE SL = bad
        bar_excess  = max(0.0, bar_extreme - sl)
    else:  # BUY
        bar_extreme = bar["low"]
        actual_slip = max(0.0, sl - fill)             # +ve = filled BELOW SL = bad
        bar_excess  = max(0.0, sl - bar_extreme)

    if bar_excess <= 1e-9:
        # Bar didn't actually breach SL — this isn't a stop-out, skip.
        print(f"  [SKIP {pos['ticket']}] Bar extreme didn't breach SL — "
              f"likely a time-stop or trail exit, not a calibration point.")
        return None

    adversity = actual_slip / bar_excess
    return {
        **pos,
        "exit_bar": bar,
        "bar_extreme": bar_extreme,
        "actual_slip_pts": actual_slip,
        "bar_excess_pts":  bar_excess,
        "adversity_factor": adversity,
    }


def per_symbol_summary(samples: list[dict]) -> dict:
    """Group by canonical symbol and average adversity."""
    by_sym: dict[str, list[dict]] = {}
    for s in samples:
        by_sym.setdefault(s["symbol"], []).append(s)
    out = {}
    for sym, lst in by_sym.items():
        adv = [x["adversity_factor"] for x in lst]
        out[sym] = {
            "adversity": sum(adv) / len(adv),
            "n":         len(adv),
            "median_actual_slip_pts": sorted(x["actual_slip_pts"] for x in lst)[len(lst) // 2],
            "max_actual_slip_pts":    max(x["actual_slip_pts"] for x in lst),
        }
    return out


def main() -> int:
    if not mt5.initialize():
        print(f"[FATAL] mt5.initialize() failed: {mt5.last_error()}")
        return 1

    info = mt5.account_info()
    if info is None:
        print("[FATAL] No MT5 account info.")
        mt5.shutdown()
        return 1

    print("=" * 78)
    print(f"  v31 STAGE 1 — adversity factor calibration")
    print(f"  Account: {info.login}  server={info.server}  equity=${info.equity:,.2f}")
    print("=" * 78)

    args = sys.argv[1:]
    tickets: list[int] = []
    if args:
        for a in args:
            try:
                tickets.append(int(a))
            except ValueError:
                print(f"[WARN] Skipping non-numeric ticket: {a}")
    else:
        # Auto-discover: all closed positions in last 24h
        now = datetime.now(timezone.utc)
        deals = mt5.history_deals_get(now - timedelta(hours=24), now)
        if deals:
            seen = set()
            for d in deals:
                if d.position_id and d.position_id not in seen:
                    tickets.append(d.position_id)
                    seen.add(d.position_id)

    if not tickets:
        print("[ERROR] No tickets supplied and no recent positions found.")
        mt5.shutdown()
        return 1

    print(f"\nProcessing {len(tickets)} positions: {tickets}\n")

    samples: list[dict] = []
    for pid in tickets:
        pos = fetch_position_details(pid)
        if pos is None:
            continue
        sample = compute_sample(pos)
        if sample is None:
            continue
        samples.append(sample)

        # Pretty-print
        print(f"---  POSITION {pid}  ---")
        print(f"   Symbol            : {sample['broker_symbol']:8s} → {sample['symbol']}  ({sample['side']})")
        print(f"   SL price          : {sample['sl_price']}")
        print(f"   Fill price        : {sample['exit_price']}")
        print(f"   Bar high/low      : {sample['exit_bar']['high']:.2f} / {sample['exit_bar']['low']:.2f}")
        print(f"   Bar extreme used  : {sample['bar_extreme']:.2f}")
        print(f"   Actual slip       : {sample['actual_slip_pts']:.4f} pts")
        print(f"   Bar excess vs SL  : {sample['bar_excess_pts']:.4f} pts")
        print(f"   ADVERSITY FACTOR  : {sample['adversity_factor']:.4f}")
        print()

    if not samples:
        print("[ERROR] No usable samples produced.")
        mt5.shutdown()
        return 1

    # Per-symbol + global summaries
    per_sym  = per_symbol_summary(samples)
    all_advs = [s["adversity_factor"] for s in samples]
    fallback = sum(all_advs) / len(all_advs)

    payload = {
        "calibrated_at_utc": datetime.now(timezone.utc).isoformat(),
        "n_samples": len(samples),
        "samples": samples,
        "per_symbol": per_sym,
        "fallback_adversity": fallback,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2, default=str))

    print("=" * 78)
    print("  PER-SYMBOL ADVERSITY")
    print("=" * 78)
    print(f"   {'Symbol':8s}  {'n':>4s}  {'adv':>8s}  {'med_slip':>10s}  {'max_slip':>10s}")
    print("   " + "-" * 50)
    for sym in sorted(per_sym.keys()):
        s = per_sym[sym]
        print(f"   {sym:8s}  {s['n']:>4d}  {s['adversity']:>8.4f}  "
              f"{s['median_actual_slip_pts']:>10.3f}  {s['max_actual_slip_pts']:>10.3f}")
    print()
    print(f"   FALLBACK (cross-sym avg) adversity: {fallback:.4f}")
    print()
    print(f"   Saved to: {OUT_PATH.relative_to(ROOT)}")
    print()
    print("  → NEXT STAGE: run Scripts/build_slip_distribution_v31.py")

    mt5.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
