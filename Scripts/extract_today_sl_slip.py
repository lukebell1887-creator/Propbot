"""
extract_today_sl_slip.py — Pull SL price + exit price from MT5 for every closed
position today, compute the EXIT slippage in points and dollars.

Run on the VPS (or anywhere with MT5 installed and the same login that traded).
"""

import MetaTrader5 as mt5
from datetime import datetime, timedelta, timezone


def main():
    if not mt5.initialize():
        print(f"[FATAL] mt5.initialize() failed: {mt5.last_error()}")
        return

    info = mt5.account_info()
    print(f"\nAccount: {info.login}  server={info.server}  equity=${info.equity:,.2f}")
    print("=" * 100)

    # Window: from start of "today" UTC to now (covers all 4 trades)
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    # All deals closed today
    deals = mt5.history_deals_get(start, now)
    if deals is None or len(deals) == 0:
        print("[INFO] No deals found today.")
        mt5.shutdown()
        return

    # Group by position ID
    by_position = {}
    for d in deals:
        by_position.setdefault(d.position_id, []).append(d)

    print(f"{'Symbol':<10} {'Side':<6} {'Lots':>7} {'Entry':>14} {'Exit':>14} "
          f"{'PlannedSL':>14} {'ExitSlip(pts)':>14} {'ExitSlip($)':>13} {'Total$':>10}")
    print("-" * 120)

    grand_total_slip_dollars = 0.0
    grand_total_pnl = 0.0
    rows = []

    for pid, ds in by_position.items():
        in_deals = [x for x in ds if x.entry == mt5.DEAL_ENTRY_IN]
        out_deals = [x for x in ds if x.entry == mt5.DEAL_ENTRY_OUT]
        if not in_deals or not out_deals:
            continue

        d_in = in_deals[0]
        d_out = out_deals[-1]

        symbol = d_in.symbol
        side = "BUY " if d_in.type == mt5.DEAL_TYPE_BUY else "SELL"
        lots = d_in.volume
        entry_px = d_in.price
        exit_px = d_out.price

        # Real PnL = sum of profit + swap + commission across deals
        pnl = sum(x.profit + x.swap + x.commission for x in ds)

        # Find the planned SL — fetch the orders for this position
        orders = mt5.history_orders_get(position=pid)
        planned_sl = None
        if orders:
            # The opening order should have the SL stored (it's an OPEN order with sl set)
            for o in orders:
                if o.sl and o.sl > 0:
                    planned_sl = o.sl
                    break

        # Compute exit slippage in points
        # For BUY: SL is below entry. If exit < SL, exit was WORSE (more loss). slip = SL - exit.
        # For SELL: SL is above entry. If exit > SL, exit was WORSE. slip = exit - SL.
        if planned_sl:
            if side.strip() == "BUY":
                slip_pts = planned_sl - exit_px  # +ve means worse fill
            else:
                slip_pts = exit_px - planned_sl
        else:
            slip_pts = None

        # Convert to dollars
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

        row = (symbol, side, lots, entry_px, exit_px, planned_sl, slip_pts, slip_dollars, pnl)
        rows.append(row)

    # Sort by symbol for readability
    rows.sort(key=lambda r: r[0])
    for r in rows:
        symbol, side, lots, entry_px, exit_px, planned_sl, slip_pts, slip_dollars, pnl = r
        sl_str = f"{planned_sl:>14.2f}" if planned_sl else f"{'(missing)':>14}"
        slip_pts_str = f"{slip_pts:+14.2f}" if slip_pts is not None else f"{'?':>14}"
        slip_d_str = f"${slip_dollars:+12.2f}" if slip_dollars is not None else f"{'?':>13}"
        print(f"{symbol:<10} {side:<6} {lots:>7.2f} {entry_px:>14.2f} {exit_px:>14.2f} "
              f"{sl_str} {slip_pts_str} {slip_d_str} {pnl:>+10.2f}")

    print("-" * 120)
    print(f"{'TOTALS':<78} hidden_slip=${grand_total_slip_dollars:+8.2f}  pnl=${grand_total_pnl:+8.2f}")
    print()
    print("READING THIS TABLE:")
    print("  ExitSlip(pts) = how many points WORSE than planned SL the broker filled at.")
    print("                  +ve = worse fill (broker filled past your SL, costs you money).")
    print("                  -ve = better fill (broker gave you a price BETTER than SL, free money).")
    print("                   0  = filled exactly at SL (perfect).")
    print("  ExitSlip($)   = the same in dollar terms, multiplied by lots × $/point.")
    print("  Total$        = your real PnL incl. commission, swap. This is what 5ers shows.")

    mt5.shutdown()


if __name__ == "__main__":
    main()
