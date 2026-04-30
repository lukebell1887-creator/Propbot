"""
extract_today_sl_slip.py — Pull SL price + exit price from MT5 for every closed
position today, compute the EXIT slippage in points and dollars.

Run on the VPS (or anywhere with MT5 installed and the same login that traded).

v2: Wider time window (server-time tolerant), full diagnostic mode that prints
    raw deals so we can see if any are being filtered out.
"""

import MetaTrader5 as mt5
from datetime import datetime, timedelta, timezone


def main():
    if not mt5.initialize():
        print(f"[FATAL] mt5.initialize() failed: {mt5.last_error()}")
        return

    info = mt5.account_info()
    print(f"\nAccount: {info.login}  server={info.server}  equity=${info.equity:,.2f}")

    # Use a WIDE window — last 48 hours — so server-time vs UTC mismatches don't
    # filter anything out. We'll filter to "today" later by deal time.
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=48)

    deals = mt5.history_deals_get(start, now)
    if deals is None:
        print(f"[FATAL] history_deals_get returned None: {mt5.last_error()}")
        mt5.shutdown()
        return
    if len(deals) == 0:
        print("[INFO] No deals found in last 48h.")
        mt5.shutdown()
        return

    print(f"\n=== RAW DEALS (last 48h, total {len(deals)}) ===")
    print(f"{'time':<20} {'ticket':>10} {'pos_id':>10} {'symbol':<10} {'type':<6} "
          f"{'entry':<10} {'volume':>7} {'price':>14} {'sl_set':>14} {'profit':>10} {'comm':>8} {'swap':>8}")
    print("-" * 150)

    type_names = {0: "BUY", 1: "SELL", 2: "BAL", 3: "CRED", 4: "CHRG", 5: "CORR", 6: "BONUS"}
    entry_names = {0: "IN", 1: "OUT", 2: "INOUT", 3: "OUT_BY"}

    # Print all deals raw
    for d in deals:
        t = datetime.fromtimestamp(d.time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        type_str = type_names.get(d.type, str(d.type))
        entry_str = entry_names.get(d.entry, str(d.entry))
        sl_str = f"{getattr(d, 'sl', 0):.2f}" if hasattr(d, 'sl') else "-"
        print(f"{t:<20} {d.ticket:>10} {d.position_id:>10} {d.symbol:<10} {type_str:<6} "
              f"{entry_str:<10} {d.volume:>7.2f} {d.price:>14.2f} {sl_str:>14} "
              f"{d.profit:>+10.2f} {d.commission:>+8.2f} {d.swap:>+8.2f}")

    # Now pair them up by position_id
    print()
    print("=== PAIRED POSITIONS WITH EXIT SLIPPAGE ===")

    by_position = {}
    for d in deals:
        if d.position_id == 0:
            continue  # skip non-position deals (balance/credit/etc.)
        by_position.setdefault(d.position_id, []).append(d)

    print(f"{'Symbol':<10} {'Side':<6} {'Lots':>7} {'Entry':>14} {'Exit':>14} "
          f"{'PlannedSL':>14} {'ExitSlip(pts)':>14} {'ExitSlip($)':>13} {'Total$':>10}")
    print("-" * 130)

    grand_total_slip_dollars = 0.0
    grand_total_pnl = 0.0
    rows = []

    for pid, ds in by_position.items():
        # IN = open, OUT or OUT_BY = close
        in_deals = [x for x in ds if x.entry == mt5.DEAL_ENTRY_IN]
        out_deals = [x for x in ds if x.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY)]
        if not in_deals or not out_deals:
            continue

        d_in = in_deals[0]
        d_out = out_deals[-1]

        symbol = d_in.symbol
        side = "BUY " if d_in.type == mt5.DEAL_TYPE_BUY else "SELL"
        lots = d_in.volume
        entry_px = d_in.price
        exit_px = d_out.price

        pnl = sum(x.profit + x.swap + x.commission for x in ds)

        # Find the planned SL from the position-history orders.
        orders = mt5.history_orders_get(position=pid)
        planned_sl = None
        if orders:
            for o in orders:
                if o.sl and o.sl > 0:
                    planned_sl = o.sl
                    break

        # Sometimes SL is stored on the deal itself
        if not planned_sl:
            for x in ds:
                if hasattr(x, 'sl') and x.sl and x.sl > 0:
                    planned_sl = x.sl
                    break

        if planned_sl:
            if side.strip() == "BUY":
                slip_pts = planned_sl - exit_px  # +ve means broker filled BELOW SL = worse
            else:
                slip_pts = exit_px - planned_sl
        else:
            slip_pts = None

        sym_info = mt5.symbol_info(symbol)
        if sym_info and slip_pts is not None:
            tick_size = sym_info.trade_tick_size
            tick_value = sym_info.trade_tick_value
            slip_ticks = slip_pts / tick_size if tick_size else 0
            slip_dollars = slip_ticks * tick_value * lots
        else:
            slip_dollars = None

        if slip_dollars is not None:
            grand_total_slip_dollars += slip_dollars
        grand_total_pnl += pnl

        rows.append((symbol, side, lots, entry_px, exit_px, planned_sl, slip_pts, slip_dollars, pnl))

    rows.sort(key=lambda r: r[0])
    for r in rows:
        symbol, side, lots, entry_px, exit_px, planned_sl, slip_pts, slip_dollars, pnl = r
        sl_str = f"{planned_sl:>14.2f}" if planned_sl else f"{'(missing)':>14}"
        slip_pts_str = f"{slip_pts:+14.2f}" if slip_pts is not None else f"{'?':>14}"
        slip_d_str = f"${slip_dollars:+12.2f}" if slip_dollars is not None else f"{'?':>13}"
        print(f"{symbol:<10} {side:<6} {lots:>7.2f} {entry_px:>14.2f} {exit_px:>14.2f} "
              f"{sl_str} {slip_pts_str} {slip_d_str} {pnl:>+10.2f}")

    print("-" * 130)
    print(f"{'TOTALS':<78} hidden_slip=${grand_total_slip_dollars:+8.2f}  pnl=${grand_total_pnl:+8.2f}")

    print("\nREADING THIS TABLE:")
    print("  ExitSlip(pts) = how many points WORSE than planned SL the broker filled at.")
    print("                  +ve = worse fill (broker filled past your SL, costs you money).")
    print("                  -ve = better fill (broker gave you a price BETTER than SL).")
    print("                   0  = filled exactly at SL (perfect).")

    mt5.shutdown()


if __name__ == "__main__":
    main()
