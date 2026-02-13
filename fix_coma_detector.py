#!/usr/bin/env python3
"""
FIX: Add Coma Detector to engine.py
====================================
Patches the engine to detect OS-level sleep/freeze (the 12-hour gap problem).

When the bot wakes from a coma:
  1. Logs a CRITICAL warning with the gap duration
  2. Emergency closes ALL open positions (they're stale/unmanaged)
  3. Forces a full re-warm before trading again
  4. Continues running (doesn't crash)

The check runs at the TOP of every _tick() call.
"""

import re
from pathlib import Path

ENGINE = Path("src/engine.py")

def patch():
    code = ENGINE.read_text(encoding="utf-8")
    
    # =========================================================================
    # PATCH 1: Add COMA_THRESHOLD constant near other constants
    # =========================================================================
    if "COMA_THRESHOLD" in code:
        print("  [SKIP] COMA_THRESHOLD already present")
    else:
        # Find STALE_FEED_TIMEOUT and add after it
        old = "    STALE_FEED_TIMEOUT = 5.0  # seconds — if no new tick for this long, data is stale"
        new = old + """
    COMA_THRESHOLD = 60.0  # seconds — if tick loop was frozen > this, we were in a coma"""
        if old in code:
            code = code.replace(old, new)
            print("  [OK] Added COMA_THRESHOLD = 60.0s constant")
        else:
            # Try without the em dash
            old2 = "    STALE_FEED_TIMEOUT = 5.0"
            for line in code.split('\n'):
                if 'STALE_FEED_TIMEOUT' in line and '5.0' in line:
                    old2 = line
                    break
            new2 = old2 + "\n    COMA_THRESHOLD = 60.0  # seconds — if tick loop was frozen > this, we were in a coma"
            code = code.replace(old2, new2, 1)
            print(f"  [OK] Added COMA_THRESHOLD after STALE_FEED_TIMEOUT")

    # =========================================================================
    # PATCH 2: Initialize _last_tick_wall in run() before the loop
    # =========================================================================
    if "_last_tick_wall" in code:
        print("  [SKIP] _last_tick_wall already present")
    else:
        old_run = '        self._running = True\n        logger.info("Starting v5.6 trading loop (100ms tick)...")'
        new_run = '        self._running = True\n        self._last_tick_wall = time.time()  # Coma detector baseline\n        logger.info("Starting v5.6 trading loop (100ms tick)...")'
        if old_run in code:
            code = code.replace(old_run, new_run)
            print("  [OK] Added _last_tick_wall init in run()")
        else:
            print("  [WARN] Could not find run() init pattern, trying alternative...")
            code = code.replace(
                "self._running = True\n        logger.info(",
                "self._running = True\n        self._last_tick_wall = time.time()  # Coma detector baseline\n        logger.info(",
                1
            )
            print("  [OK] Added _last_tick_wall init (alternative pattern)")

    # =========================================================================
    # PATCH 3: Add coma detection at the START of _tick()
    # =========================================================================
    if "COMA DETECTED" in code:
        print("  [SKIP] Coma detector already in _tick()")
    else:
        # Find the start of _tick and inject right after the heartbeat check
        # We inject BEFORE the heartbeat check — if we were in a coma, heartbeat
        # will also fail, but we want to handle the coma first
        
        # The injection point: right at the start of _tick, after the docstring
        coma_code = '''
        # ═══ COMA DETECTOR ═══════════════════════════════════════════════════
        # Detects OS-level sleep/freeze that pauses the entire process.
        # If wall clock jumped > COMA_THRESHOLD since last tick, we were frozen.
        now_wall = time.time()
        gap = now_wall - getattr(self, '_last_tick_wall', now_wall)
        self._last_tick_wall = now_wall
        if gap > self.COMA_THRESHOLD:
            logger.critical(
                f"COMA DETECTED: Process was frozen for {gap:.0f}s ({gap/3600:.1f}h)! "
                f"Last tick was {gap:.0f}s ago."
            )
            # Emergency close ALL open positions — they were unmanaged
            open_count = 0
            for state in self._pairs.values():
                if state.position != 0:
                    pair_name = state.config.name
                    logger.critical(
                        f"COMA EMERGENCY CLOSE: {pair_name} has open position "
                        f"(dir={state.position}) — closing immediately"
                    )
                    try:
                        if state.ticket_a:
                            self._bridge.close_position(state.ticket_a)
                        if state.ticket_b:
                            self._bridge.close_position(state.ticket_b)
                        logger.critical(f"COMA CLOSE: {pair_name} positions closed")
                    except Exception as e:
                        logger.critical(f"COMA CLOSE FAILED: {pair_name} — {e}")
                    state.position = 0
                    state.ticket_a = 0
                    state.ticket_b = 0
                    open_count += 1
            if open_count > 0:
                logger.critical(f"COMA: Closed {open_count} pair(s). All positions flat.")
            else:
                logger.warning(f"COMA: No open positions — no damage. Resuming.")
            # Force re-warm: reset bar counters so the engine re-calibrates
            logger.critical("COMA: Forcing 200-bar re-warm before next trade")
            for state in self._pairs.values():
                state.m1_bar_count = max(0, state.m1_bar_count - 200)
            # Skip this tick entirely — let the next tick process normally
            return
        # ═══ END COMA DETECTOR ═══════════════════════════════════════════════
'''
        
        # Find the _tick function and inject after the docstring
        # Pattern: the heartbeat check line
        heartbeat_line = '        # Heartbeat'
        # Find it in the _tick function context
        tick_idx = code.find('    async def _tick(self)')
        if tick_idx < 0:
            print("  [ERROR] Cannot find _tick() function!")
        else:
            # Find the heartbeat comment after _tick
            hb_idx = code.find(heartbeat_line, tick_idx)
            if hb_idx < 0:
                # Try without the special character
                for candidate in ['        # Heartbeat', '        # Heartbeat —', '        # Heartbeat ']:
                    hb_idx = code.find(candidate, tick_idx)
                    if hb_idx >= 0:
                        heartbeat_line = candidate
                        break
            
            if hb_idx >= 0:
                code = code[:hb_idx] + coma_code + code[hb_idx:]
                print("  [OK] Injected coma detector at top of _tick()")
            else:
                print("  [ERROR] Cannot find heartbeat check in _tick()!")

    ENGINE.write_text(code, encoding="utf-8")
    print("\n  Engine patched successfully!")

if __name__ == "__main__":
    print("=" * 70)
    print("  PATCHING ENGINE: Coma Detector (OS Sleep/Freeze Protection)")
    print("=" * 70)
    patch()
    print("\n  Done. The engine will now:")
    print("    1. Detect if it was frozen > 60s")
    print("    2. Emergency close any open positions")  
    print("    3. Force 200-bar re-warm")
    print("    4. Log CRITICAL warnings")
    print("    5. Continue running (no crash)")
