"""
extract_today_sl_slip.py — Pull SL price + entry/exit prices from MT5 for the
exact positions you give it. Computes ENTRY slippage and EXIT slippage.

Usage:
    # mode 1: by position ticket (most reliable — uses 5ers UI ticket numbers)
    python Scripts/extract_today_sl_slip.py 545278227 545502968 545509924 545524760

    # mode 2: no args — falls back to all deals in last 48h
    python Scripts/extract_today_sl_slip.py
"""

import sys
import MetaTrader5 as mt5
from datetime import datetime, timedelta, timezone


def fetch_deals_for_position(pid):
    """Return all deals for a given position id, even if outside default time window."""
    # First try the direct lookup
    deals = mt5.history_deals_get(position=pid)
    if deals and len(deals) > 0:
        return list(deals)

    # Fallback: search wide time window (last 14 days) and filter
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=14)
    all_deals = mt5.history_deals_get(start, now)
    if all_deals:
        return [d for d in all_deals if d.position_id == pid]
    return []


def fetch_orders_for_position(pid):
    orders = mt5.history_orders_get(position=pid)
    if orders and len(orders) > 0:
        return list(orders)
    # fallback wide search
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=14)
    all_orders = mt5.history_orders_get(start, now)
    if all_orders:
        return [o for o in all_orders if o.position_id == pid]
    return []


def analyse_position(pid):
    """Pull all info we need for ONE position id and print a detailed forensic report."""
    deals = fetch_deals_for_position(pid)
    orders = fetch_orders_for_position(pid)

    print(f"\n========== POSITION {pid} ==========")

    if not deals:
        print(f"[ERROR] No deals found for position {pid}.")
        return

    in_deals = [d for d in deals if d.entry == mt5.DEAL_ENTRY_IN]
    out_deals = [d for d in deals if d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]

    if not in_deals:
        print(f"[ERROR] No entry deal found for position {pid}.")
        return
    if not out_deals:
        print(f"[ERROR] No exit deal found for position {pid}. Position may still be open.")
        return

    d_in = in_deals[0]
    symbol = d_in.symbol
    side = "BUY" if d_in.type == mt5.DEAL_TYPE_BUY else "SELL"
    lots = d_in.volume
    actual_entry_px = d_in.price
    entry_time_utc = datetime.fromtimestamp(d_in.time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # --- Find INTENDED entry price (from the original entry order) ---
    intended_entry_px = None
    entry_order = None
    for o in orders:
        # Entry orders have type matching position direction
        if (side == "BUY" and o.type in (mt5.ORDER_TYPE_BUY, mt5.ORDER_TYPE_BUY_STOP, mt5.ORDER_TYPE_BUY_LIMIT)) or \
           (side == "SELL" and o.type in (mt5.ORDER_TYPE_SELL, mt5.ORDER_TYPE_SELL_STOP, mt5.ORDER_TYPE_SELL_LIMIT)):
            if o.position_id == pid and entry_order is None:
                entry_order = o
                # For stop/limit orders, price_open is the trigger; for market orders, price_open is the request price
                intended_entry_px = o.price_open
                break

    # --- Find SL price the bot set ---
    planned_sl = None
    for o in orders:
        if o.sl and o.sl > 0:
            planned_sl = o.sl
            break

    # --- Find ACTUAL exit price ---
    d_out = out_deals[-1]
    actual_exit_px = d_out.price
    exit_time_utc = datetime.fromtimestamp(d_out.time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # --- Get symbol metadata for $ conversion ---
    sym_info = mt5.symbol_info(symbol)
    tick_size = sym_info.trade_tick_size if sym_info else 1.0
    tick_value = sym_info.trade_tick_value if sym_info else 1.0

    # --- Compute ENTRY slippage ---
    if intended_entry_px and intended_entry_px > 0:
        if side == "BUY":
            entry_slip_pts = actual_entry_px - intended_entry_px  # +ve = paid more = worse
        else:  # SELL
            entry_slip_pts = intended_entry_px - actual_entry_px  # +ve = sold for less = worse
    else:
        entry_slip_pts = None

    # --- Compute EXIT slippage (vs planned SL) ---
    if planned_sl:
        if side == "BUY":
            exit_slip_pts = planned_sl - actual_exit_px  # +ve = filled below SL = worse
        else:
            exit_slip_pts = actual_exit_px - planned_sl  # +ve = filled above SL = worse
    else:
        exit_slip_pts = None

    # --- Convert slip to dollars ---
    def to_dollars(slip_pts):
        if slip_pts is None or tick_size == 0:
            return None
        return (slip_pts / tick_size) * tick_value * lots

    entry_slip_d = to_dollars(entry_slip_pts)
    exit_slip_d = to_dollars(exit_slip_pts)

    pnl = sum(d.profit + d.swap + d.commission for d in deals)

    # --- Print forensic report ---
    print(f"  Symbol               : {symbol}  ({side})")
    print(f"  Lots                 : {lots}")
    print(f"  Entry time (UTC)     : {entry_time_utc}")
    print(f"  Exit  time (UTC)     : {exit_time_utc}")
    print(f"  -- ENTRY --")
    print(f"  Intended entry price : {intended_entry_px if intended_entry_px else '(missing)'}")
    print(f"  Actual entry fill    : {actual_entry_px}")
    if entry_slip_pts is not None:
        print(f"  Entry slippage       : {entry_slip_pts:+.4f} pts  =  ${entry_slip_d:+.2f}")
    else:
        print(f"  Entry slippage       : (cannot compute — intended price missing)")
    print(f"  -- EXIT vs SL --")
    print(f"  Bot's planned SL     : {planned_sl if planned_sl else '(no SL was set!)'}")
    print(f"  Actual exit price    : {actual_exit_px}")
    if exit_slip_pts is not None:
        print(f"  Exit slippage vs SL  : {exit_slip_pts:+.4f} pts  =  ${exit_slip_d:+.2f}")
        print(f"     (positive = broker filled BEYOND your SL = costs you money)")
        print(f"     (negative = broker filled BEFORE your SL = you closed via time/manual)")
    print(f"  -- RESULT --")
    print(f"  Real PnL (incl swap) : ${pnl:+.2f}")
    print(f"  Tick size / value    : {tick_size} / ${tick_value}")


def main():
    if not mt5.initialize():
        print(f"[FATAL] mt5.initialize() failed: {mt5.last_error()}")
        return

    info = mt5.account_info()
    print(f"\nAccount: {info.login}  server={info.server}  equity=${info.equity:,.2f}")

    args = sys.argv[1:]
    if args:
        # Mode 1: explicit ticket numbers
        for arg in args:
            try:
                pid = int(arg)
            except ValueError:
                print(f"[WARN] '{arg}' is not a valid ticket number, skipping.")
                continue
            analyse_position(pid)
    else:
        # Mode 2: all positions in last 48h
        now = datetime.now(timezone.utc)
        start = now - timedelta(hours=48)
        deals = mt5.history_deals_get(start, now)
        if not deals:
            print("[INFO] No deals in last 48h.")
            mt5.shutdown()
            return
        seen_pids = set()
        for d in deals:
            if d.position_id and d.position_id not in seen_pids:
                seen_pids.add(d.position_id)
        for pid in sorted(seen_pids):
            analyse_position(pid)

    mt5.shutdown()


if __name__ == "__main__":
    main()
