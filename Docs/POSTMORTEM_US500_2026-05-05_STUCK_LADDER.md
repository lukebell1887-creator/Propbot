# POSTMORTEM — US500 #547550971 phantom-close, 2026-05-05 (FINAL — root cause confirmed from logs)

## TL;DR — actual root cause, smoking-gun confirmed from `v30_live.log`

**The bot fired `POS_CLOSED_BY_BROKER` 15 milliseconds after the entry was confirmed**, while the position was actually still open at the broker. The bot then synthesised a fake "TP1 hit" outcome (`close_px_src=snap_tp1`), wiped its in-memory state, and ignored the real position for the next 2 hours.

This is **NOT** the partial-close ladder bug I originally hypothesised. It is a **post-entry reconciliation race** in `v30_live.py`.

### The 15-millisecond death spiral (verbatim from your log)

```
13:45:02,793  Sending order: SP500 ORDER_TYPE_BUY 26.18 lots
13:45:02,965  Order executed: ticket=547550971 price=0.0      ← suspect: price=0.0
13:45:02,965  [SLIP]  US500 LONG  intended=7233.47  fill=7233.47   slip=0.0t
13:45:02,980  [ENTRY] US500 LONG  lots=26.18  SL=7227.15  TP1=7235.24  TP2=7237.01
13:45:02,980  [event] POS_CLOSED_BY_BROKER  {ticket: 547550971}    ← FIRED 15ms LATER
13:45:02,980  [event] SIZER_FEEDBACK  reason=broker_close  realised_R=0.28  pnl_approx=46.34  close_px=7235.24  close_px_src=snap_tp1
```

Three lines of evidence prove this was a phantom close:

1. **Time delta = 15 ms.** The market cannot move from entry to TP1 in 15 ms with a stationary broker; price was 7233.47 → 7235.24 would be a 1.77-point instant gap. Impossible.
2. **`close_px_src=snap_tp1`** — this is the bot's own admission that it didn't observe a real fill price; it synthesised one by snapping to TP1.
3. **Every heartbeat for the next 2 hours shows `open=0`** while you confirmed the broker held the position the entire time, exiting at 7245.94 only when you manually clicked close.

## Where the bug lives — exact lines in `src/live/v30_live.py`

```python
open_by_broker = {p.ticket: p for p in self._broker_positions()}
for sym, st in self.states.items():
    if st.open_ticket is not None and st.open_ticket not in open_by_broker:
        self._log_event("POS_CLOSED_BY_BROKER", symbol=sym, side=st.open_side)
        self._feed_sizer_on_close(sym, reason="broker_close")
        self._clear_state(sym)
```

This block runs on the main loop tick. Right after `_register_entry()` sets `st.open_ticket = 547550971`, the loop calls `_broker_positions()`. If that call returns a list **not containing 547550971**, the bot fires the phantom close.

### Two prime suspects for why the lookup was empty

**A. Symbol mismatch in `_broker_positions()`.** Bot's logical symbol is `US500`; broker's symbol is `SP500` (you can see it on the order line: `Sending order: SP500 ...`). If `_broker_positions()` calls `mt5.positions_get(symbol="US500")`, the broker returns nothing. We need to read the body of `_broker_positions` to confirm — but the symptom matches perfectly.

**B. MT5 position-list propagation lag.** The deal at 13:45:02.965 may exist in deal history before it appears in `mt5.positions_get()`. With the reconciliation loop running on the very next tick (15 ms later), `positions_get()` may legitimately return an empty list for that one beat, even though the position will appear ~50 ms later.

Either way, the fix is the same: **never reconcile against the broker for at least N seconds after a confirmed entry**.

## The fix (v31.2 — to implement next session with fresh context)

### Layer 1 — entry grace period (3 lines, this is THE fix)

In `_register_entry()` (where `st.open_ticket` is set), also set `st.opened_at = time.time()`. Then in the reconciliation loop:

```python
GRACE_S = 30  # seconds
for sym, st in self.states.items():
    if st.open_ticket is None: continue
    if (time.time() - st.opened_at) < GRACE_S:
        continue  # too fresh to reconcile against broker — trust the entry
    if st.open_ticket not in open_by_broker:
        self._log_event("POS_CLOSED_BY_BROKER", ...)
```

This single guard would have prevented today's bug. With a 30 s grace period the bot trusts its own entry confirmation for the first half-minute, by which point any propagation lag is gone.

### Layer 2 — symbol-correct lookup

Audit `_broker_positions()` to ensure it iterates `mt5.positions_get(magic=BOT_MAGIC)` (no symbol filter) — let MT5 return ALL positions for our magic, then we match by ticket. **Never** filter by bot-symbol when looking up a broker position; ticket numbers are the universal key.

### Layer 3 — hard re-confirm before phantom close

Even after the grace period, before firing `POS_CLOSED_BY_BROKER` do **one extra explicit lookup**:

```python
explicit = mt5.positions_get(ticket=st.open_ticket)
if explicit and len(explicit) > 0:
    return  # broker confirms still open — false alarm, do nothing
self._log_event("POS_CLOSED_BY_BROKER", ...)
```

This catches the rare case where `_broker_positions()` (which fetches the full list) misses one due to filter/race, but a direct ticket lookup succeeds.

### Layer 4 — ban `snap_tp1` from real-money runs

The `snap_tp1` heuristic in `_infer_broker_close_px` is fundamentally dangerous: it assumes any unobserved close was a TP1 hit. When the close was actually a phantom (this bug) or an SL hit by ticks slightly outside our M1 close, we synthesise wrong PnL telemetry that poisons the sizer's R-distribution, which scales lots up over time on imaginary winners. **Replace `snap_tp1` with `unknown` and use the broker's actual deal price when available** (it is — `mt5.history_deals_get(position=ticket)` will return the OUT deal with a real price).

## Test that must pass before redeploy

`tests/test_phantom_close_grace.py`:

```python
def test_phantom_close_blocked_by_grace_period():
    bot = make_bot(grace_s=30)
    # Step 1: register entry
    bot._register_entry("US500", ticket=12345, side="LONG", entry=7233.47, ...)
    assert bot.states["US500"].open_ticket == 12345
    # Step 2: simulate broker returning empty positions (the race)
    bot._broker_positions = lambda: []
    # Step 3: tick reconciliation loop within grace window
    bot._reconcile_open_positions(now=bot.states["US500"].opened_at + 5)
    # Step 4: assert the bot did NOT fire the phantom close
    assert bot.states["US500"].open_ticket == 12345
    assert "POS_CLOSED_BY_BROKER" not in [e["kind"] for e in bot.event_log]
    # Step 5: tick AFTER grace expires while broker really shows nothing
    bot._reconcile_open_positions(now=bot.states["US500"].opened_at + 31)
    # NOW the close should fire (real broker close)
    assert bot.states["US500"].open_ticket is None
```

Plus a second test (`test_phantom_close_real_after_grace`) confirming the close does fire after the grace window when the broker actually shows the position gone.

## Detector results — historical bug rate

`Scripts/diag_did_tp1_get_touched.py` v2 cross-checked the last 10 days of trades against MT5 M1 bars:

| bucket                  | n  | meaning                                              |
|-------------------------|----|-------------------------------------------------------|
| `CORRECT_TP1_FIRED`     | 8  | TP1 touched in market AND `TP1_PARTIAL` event logged |
| `CORRECT_NO_TP1`        | 10 | TP1 never reached → SL'd cleanly                     |
| **`STUCK_LADDER_BUG`**  | **1** | DE40 SHORT 543990333 on 04-28 — TP1 touched, no event |
| `STILL_OPEN_NO_TP1`     | 1  | today's US500 547550971 (still open at script run)   |
| `NO_MT5_DATA`           | 8  | mostly old-account tickets that aged out             |

The DE40 04-28 instance is plausibly **the same bug** (phantom close near entry → in-memory state wiped → ladder never runs). Now that we know what to look for, the fix above will catch both it and US500 today.

## What to do right now

1. **Bot stays stopped.** Same as before.
2. **Take the win** — your manual exit at 7245.94 was actually better than the bot's intended ladder would have produced.
3. **Next session, fresh context, I implement v31.2:** Layers 1+2+3+4 in `src/live/v30_live.py` plus `tests/test_phantom_close_grace.py`. Estimated ~80 lines of changes.
4. **Re-deploy + monitor for 1 day.** Watch heartbeats for `open >= 1` while a position is actually open at the broker, and grep events.log for the new `RECONCILE_GRACE_SKIPPED` event the patch will emit, confirming the guard fired during real entries.

---

*Generated 2026-05-05 from forensic analysis of `v30_live.log` lines 13:45:02.793 → 13:45:02.980. Root cause: 15-ms reconciliation race producing a phantom `POS_CLOSED_BY_BROKER` immediately after entry, with `snap_tp1` falsely closing the position in the bot's internal state while the broker held it open for 2 more hours.*
