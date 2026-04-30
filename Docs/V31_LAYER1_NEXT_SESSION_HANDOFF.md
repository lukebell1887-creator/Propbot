# V31 Layer 1 — Next-Session Handoff

**Why this file exists:** the previous session built and proved the Layer 1 math
(81 unit tests green on local + VPS, commit `671c7a1`) but ran out of context
window before wiring it into the live engine.  The user explicitly asked for the
live bot to behave **exactly like the backtest**, with both `0.185 %` risk and
Layer 1 active.

This document is the resume-point.  Open it as the **first read** in the next
session, then follow the order below.

---

## What is already done (commit `671c7a1`)

- ✅ `0.185 %` base risk → live (`GO_LIVE_V30.ps1` passes `--risk 0.00185`)
- ✅ No-chase 300 s, slippage tracker → live in v30
- ✅ `src/execution/layer1.py` — pure-Python helpers + `Layer1Decision`
- ✅ `src/execution/layer1_tracker.py` — stateful envelope tracker
- ✅ `tests/test_layer1.py` (70) + `tests/test_layer1_tracker.py` (11) — **81/81 pass on VPS**
- ✅ `Docs/V31_LAYER1_INTEGRATION_GUIDE.md` — full design + signatures
- ✅ `PULL_AND_TEST_V31_LAYER1.ps1` — one-shot VPS verifier

## What is NOT done (the gap)

- ❌ `src/live/v30_live.py` does **not** import `layer1` or `layer1_tracker`
- ❌ Live `send_order` still places the broker SL at the original level (no `cap × 1.5` buffer)
- ❌ Live poll loop has no Layer 1 intercept — broker SL trigger = uncapped slip
- ❌ `tests/test_live_backtest_parity.py` does not yet assert Layer 1 parity
- ❌ No verification script that prints a one-shot "EVERYTHING IS WIRED" report

> **Bottom line:** if you start `GO_LIVE_V30.ps1` right now, it sizes at 0.185 %
> (good) but Layer 1 is dormant — today's US30 14.82 pt slip can happen again.

---

## The plan (single focused session)

### Order of work

1. **Read** the two files cold:
   - `src/live/v30_live.py` (whole file)
   - `src/execution/mt5_bridge.py` (whole file — focus on `send_order`, `modify_sl`, `close_position`, `get_positions`)

2. **Read** the contract:
   - `Docs/V31_LAYER1_INTEGRATION_GUIDE.md` — sections "Live wiring (4 tasks)"
     and "Acceptance criteria"

3. **Edit** `src/live/v30_live.py` — exactly four anchor points:

   **(a) `__init__` / engine setup**
   ```python
   from src.execution.layer1 import (
       LAYER1_CAPS, FALLBACK_MULT, ENVELOPE_S,
       emergency_sl_offset,
   )
   from src.execution.layer1_tracker import Layer1Tracker

   self.layer1 = Layer1Tracker()           # one tracker per engine
   self.layer1_original_sl: dict[int, float] = {}   # ticket -> original SL
   ```

   **(b) `send_order` (right before MT5 send)**
   - Compute `cap = LAYER1_CAPS[symbol]` (default 5.0 if unknown)
   - Compute `widened_sl = original_sl ± cap * 1.5` (sign depends on side)
   - Send the order with `sl=widened_sl` to the broker
   - On fill, store `self.layer1_original_sl[ticket] = original_sl`
   - **Critical:** every other downstream calc (R, partials, BE, trail) must
     keep using `original_sl`, NOT the widened one.  Do an explicit grep for
     `position.sl` / `pos.sl` in v30_live.py and route them through the stored
     original.

   **(c) Poll loop (every cycle, for each open position)**
   ```python
   for pos in mt5_bridge.get_positions(magic=MAGIC):
       orig_sl = self.layer1_original_sl.get(pos.ticket)
       if orig_sl is None:
           continue          # not ours / pre-Layer1 trade
       decision = self.layer1.update_and_decide(
           ticket=pos.ticket,
           symbol=pos.symbol,
           side=pos.side,            # +1 long, -1 short
           original_sl=orig_sl,
           current_price=pos.current_price,
           now=time.time(),
       )
       if decision.action in ("CLOSE_NOW", "FALLBACK_CLOSE"):
           mt5_bridge.close_position(pos.ticket, reason=f"layer1_{decision.action}")
           self.layer1.clear(pos.ticket)
           self.layer1_original_sl.pop(pos.ticket, None)
           # log decision.to_jsonable() to telemetry
   ```

   **(d) Position close cleanup**
   - When MT5 reports a position closed (any reason), call
     `self.layer1.clear(ticket)` and pop from `layer1_original_sl`.

4. **Edit** `Scripts/backtest_v23_final.py` (5 lines)
   - Already uses the same `LAYER1_CAPS` numbers conceptually — make it call
     `apply_layer1_slip()` from `src/execution/layer1.py` so the backtest and
     live share the **exact same Python function**.  This is the parity proof.

5. **Extend** `tests/test_live_backtest_parity.py`
   - Add a parametrized test that feeds the same `(symbol, original_sl, fill_price)`
     pair to both the backtest accounting and a stub of the live tracker, and
     asserts identical realised slip.

6. **Build** `Scripts/verify_v31_live_wiring.py` — the one-shot green-light report
   - Imports `src.live.v30_live`, asserts `Layer1Tracker` is on the engine class
   - Asserts `--risk` default in launchers is `0.00185`
   - Asserts `LAYER1_CAPS` matches `Docs/V31_DEFENSE_PROOF_RESULTS.md`
   - Runs all 81 unit tests + the new parity test
   - Prints a banner the user can copy-paste:
     ```
     ============================================================
       V31 LIVE WIRING REPORT  (commit <sha>)
     ============================================================
       [OK]  Risk:           0.185 %
       [OK]  Layer 1 caps:   US30=5  US500=3  DE40=5  XAUUSD=1
       [OK]  Envelope:       60 s
       [OK]  Fallback mult:  1.5
       [OK]  Live engine:    Layer1Tracker imported
       [OK]  Live engine:    send_order widens SL by cap*1.5
       [OK]  Live engine:    poll loop calls update_and_decide
       [OK]  Backtest:       calls apply_layer1_slip from same module
       [OK]  Parity tests:   N/N pass
       [OK]  Unit tests:     81/81 pass
     ------------------------------------------------------------
       RESULT: LIVE BOT MATCHES BACKTEST  ✅
     ============================================================
     ```

7. **Smoke test locally**
   ```powershell
   .\GO_DRYRUN_V30.ps1                       # no real orders, watch logs
   python Scripts\verify_v31_live_wiring.py  # the green-light report
   ```

8. **Commit + push**
   - Single commit, message:
     `v31: wire Layer 1 into live v30 — backtest ↔ live parity proven`

9. **VPS deploy + verify**
   ```powershell
   git pull
   python Scripts\verify_v31_live_wiring.py    # paste output back to me
   .\PULL_AND_TEST_V31_LAYER1.ps1              # 81/81 still green
   .\GO_DRYRUN_V30.ps1                         # overnight in dry-run first
   ```
   Only after the dry-run shows Layer 1 firing correctly on at least one breach
   event do we graduate to `.\GO_LIVE_V30.ps1`.

---

## Acceptance criteria (next session is "done" when…)

- [ ] `verify_v31_live_wiring.py` prints all `[OK]` and `RESULT: ✅`
- [ ] `pytest tests/test_layer1.py tests/test_layer1_tracker.py tests/test_live_backtest_parity.py` → 100 % pass
- [ ] Dry-run on VPS shows at least one Layer 1 breach event with `action=CLOSE_NOW` or `WAIT` in logs
- [ ] User pastes the green-light banner back, gets the explicit go-ahead before live mode
- [ ] User starts `.\GO_LIVE_V30.ps1` knowing live behaviour ≡ backtest

## Hard rules for the next session

1. **Do NOT modify `src/execution/layer1.py` or `layer1_tracker.py`** — they're
   locked.  All 81 tests must continue to pass without changes.
2. **Do NOT change `--risk 0.00185`** — that ship has sailed and is verified.
3. **Do NOT touch the EA (`MQL5/Experts/SHF_Bridge.mq5`)** — Layer 1 is
   client-side only.
4. **Every edit in `v30_live.py` must be paired with a unit test** in either
   `test_layer1_tracker.py` (already covers tracker) or
   `test_live_backtest_parity.py` (new lines for SL widening + close routing).
5. If anything is ambiguous, **stop and ask before editing live code**.

---

*Written end of session 671c7a1.  Resume here.*
