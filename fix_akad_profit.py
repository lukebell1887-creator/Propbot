"""Fix _close_spread to use ACTUAL MT5 profit for win/loss (not spread proxy)"""

with open('src/engine.py', 'r', encoding='utf-8') as f:
    eng = f.read()

# Replace the old spread_pnl logic with actual MT5 profit query
old = '''    async def _close_spread(self, state: PairState, reason: str) -> None:
        """Close both legs of a spread position."""
        cfg = state.config

        # Close both legs
        closed_a = self._bridge.close_position(state.ticket_a) if state.ticket_a else True
        closed_b = self._bridge.close_position(state.ticket_b) if state.ticket_b else True

        # Determine win/loss from ACTUAL spread P&L (not Z-score proxy!)
        # spread_pnl = (exit_spread - entry_spread) * direction
        # This matches the backtest: pnl = (spread - es) * pos * elots * notional
        spread_pnl = (state.last_spread - state.entry_spread) * state.position
        is_win = spread_pnl > 0'''

new = '''    async def _close_spread(self, state: PairState, reason: str) -> None:
        """Close both legs of a spread position."""
        cfg = state.config

        # Query ACTUAL MT5 profit BEFORE closing (broker P&L includes spread costs)
        actual_profit = 0.0
        try:
            positions = self._bridge.get_positions()
            for pos in positions:
                if pos.ticket in (state.ticket_a, state.ticket_b):
                    actual_profit += pos.profit + pos.swap
        except Exception as e:
            logger.warning(f"Could not query MT5 profit before close: {e}")

        # Close both legs
        closed_a = self._bridge.close_position(state.ticket_a) if state.ticket_a else True
        closed_b = self._bridge.close_position(state.ticket_b) if state.ticket_b else True

        # Determine win/loss from ACTUAL MT5 profit (includes spreads, swaps, commissions)
        # Falls back to spread math only if MT5 query failed
        if actual_profit != 0.0:
            is_win = actual_profit > 0
        else:
            # Fallback: use spread math
            spread_pnl = (state.last_spread - state.entry_spread) * state.position
            actual_profit = spread_pnl  # For logging only
            is_win = spread_pnl > 0'''

if old in eng:
    eng = eng.replace(old, new)
    print("Replaced _close_spread profit logic")
else:
    print("ERROR: Could not find old _close_spread block")
    # Show what's there
    idx = eng.find('async def _close_spread')
    if idx >= 0:
        print(f"Found at pos {idx}:")
        print(eng[idx:idx+600])

# Also update the logging to show actual_profit instead of spread_pnl
old_log = '''        logger.info(
            f"EXIT {cfg.name} | {'WIN' if is_win else 'LOSS'} | {reason} | "
            f"SpreadPnL={spread_pnl:+.6f} (entry={state.entry_spread:.6f} exit={state.last_spread:.6f}) | "'''

new_log = '''        logger.info(
            f"EXIT {cfg.name} | {'WIN' if is_win else 'LOSS'} | {reason} | "
            f"MT5 P&L=${actual_profit:+.2f} | Spread={state.entry_spread:.6f}->{state.last_spread:.6f} | "'''

if old_log in eng:
    eng = eng.replace(old_log, new_log)
    print("Updated EXIT log format")
else:
    print("WARNING: Could not find old EXIT log format")

with open('src/engine.py', 'w', encoding='utf-8') as f:
    f.write(eng)

print("Done!")
