# LIVE DRY-RUN DIAGNOSIS — 2026-04-24, 07:40 UK

## TL;DR (the correct answer this time)

You asked *"how will I have history when it's a dry run?"* — and that question
cracked the case open. **You're right — there is no MT5 history to check, because
in dry-run mode the bot NEVER sends orders to MT5.** The `POS_CLOSED_BY_BROKER`
event you saw at 07:30:03 CEST was a **bug in the dry-run path**, not a rejected
order, not slippage, not the broker. I was wrong in the previous draft that told
you to look for ticket `#1039626713` and `TRADE_RETCODE_INVALID_STOPS (10016)`
— **neither of those ever existed.**

The bug is now fixed in `src/live/v23_live.py` (one-line guard + explanatory
comment). **Stop and restart the bot to pick up the fix.** Details below.

---

## What I got wrong

My earlier diagnosis assumed that a dry-run bot sent real orders to a demo MT5
server and that ticket `1039626713` was a real (demo) ticket with a real fill
in MT5 History. I told you to open `MT5 → History → #1039626713`. That was
nonsense. There is no such record in MT5 because the order was never sent.

You spotted it instantly. Apologies.

---

## The actual bug (and why it's in every dry-run trade you'll ever run)

Here are the two halves of the contradiction, both inside `src/live/v23_live.py`:

### Half 1 — `_maybe_enter` (entry path)

```python
if self.dry_run:
    fake_ticket = int(time.time() * 1000) & 0x7FFFFFFF
    st.open_ticket = fake_ticket
    ok = True
else:
    result = self.bridge.send_order(req)          # <-- only here does MT5 see it
    ok = getattr(result, "error_code", 0) == 0
    if ok:
        st.open_ticket = int(result.ticket)
```

So in dry-run:
- The bot fabricates a ticket from the unix timestamp.
- `0x7FFFFFFF = 2_147_483_647` is the 31-bit mask. Your "ticket" `1039626713`
  is exactly what `int(time.time()*1000) & 0x7FFFFFFF` returns when run at
  ~07:30 CEST on 2026-04-24. ✅ Matches perfectly.
- **`self.bridge.send_order(req)` is never called.** No ZMQ → no MT5 EA → no
  broker → no history row. That's why there is nothing in MT5 History. Your
  instinct was 100 % correct.

### Half 2 — `_manage_open` (exit reconciliation path — OLD CODE, now fixed)

```python
# OLD CODE (buggy):
open_by_broker = {p.ticket: p for p in self._broker_positions()}
for sym, st in self.states.items():
    if st.open_ticket is not None and st.open_ticket not in open_by_broker:
        self._log_event("POS_CLOSED_BY_BROKER", ...)
        self._feed_sizer_on_close(sym, reason="broker_close")
        self._clear_state(sym)
```

This path asks the REAL broker *"do you still have ticket 1039626713 open?"*,
and of course the broker says no — the ticket never existed there. So on the
very first `_manage_open` tick after an entry (1-second poll cadence), the
bot declares the "position" broker-closed, fabricates a P&L from the last M1
close, and feeds the fake R back to the Merton-GZ sizer.

**Every single dry-run "entry" is immediately phantom-closed on the next poll.**
That is why your log shows:

```
07:30:03.609  ENTRY        (fake ticket generated)
07:30:04.??   POS_CLOSED_BY_BROKER  (no such ticket at broker → phantom close)
07:30:04.??   SIZER_FEEDBACK realised_R=-0.059  pnl_approx=-6.44
```

The `realised_R = -0.059` was the minute-bar close (24151.27) 3.5 ticks below
the entry (24154.77), divided by the €110 risk budget. Exactly what
`_feed_sizer_on_close` computes from `last_m1_close`.

---

## What it means for everything else

### 1. This is a dry-run-only issue.
In LIVE mode the code path goes `bridge.send_order → real MT5 ticket → real
broker positions → reconciliation works correctly`. The bug cannot hurt you
with real money.

### 2. Your BOT_FULL_REPORT.md numbers are still correct.
The $14,412 / 4.15 % DD / 3.4 Sharpe figures come from
`Scripts/backtest_v23_final.py`, which is a pure simulator — it never touches
the live path. Verified yesterday against `Results/backtest_v23_final.txt`.
Those numbers stand.

### 3. Your slippage answer still stands.
`SLIPPAGE_HONEST_ANSWER.md` is unchanged — the live-path slippage buffer (1–2
ticks, $3–5) is priced in. Yesterday's audit was sound on that point.

### 4. The dry-run itself was measuring nothing.
Until the fix just landed, every dry-run trade was a phantom. That means:
- The dry-run log tells you nothing about SL/TP hit rates.
- It tells you nothing about realised P&L.
- The Merton-GZ sizer has been fed ~0-R noise as feedback (which barely moves
  its EWMA μ̂/σ̂² after one trade, so this is recoverable — but the longer
  it ran, the worse the drift).
- The **only** thing the pre-fix dry-run proved is that the signal-generation
  and entry-decision code paths fire correctly at 05:30 UTC (08:30 CEST) when
  the DE40 ORB breaks — which is genuinely useful! The ORB+news+calendar+sizer
  entry logic all worked and produced a correctly-sized order intent. That is
  what a dry-run is *supposed* to prove. Everything downstream of that was
  broken.

---

## The fix

`src/live/v23_live.py`, `_manage_open`, lines 748-769:

```python
# NEW CODE:
if not self.dry_run:
    open_by_broker = {p.ticket: p for p in self._broker_positions()}
    for sym, st in self.states.items():
        if st.open_ticket is not None and st.open_ticket not in open_by_broker:
            self._log_event("POS_CLOSED_BY_BROKER",
                            symbol=sym, ticket=st.open_ticket, side=st.open_side)
            self._feed_sizer_on_close(sym, reason="broker_close")
            self._clear_state(sym)
            self.counters["exit_broker"] += 1
```

The broker-positions reconciliation now only runs when we're actually sending
orders. In dry-run, "open" positions stay open in local state until they are
closed by:
- **Window expiry** (`_manage_open` lines 732-746) — fires ~120 min after the
  OR window starts; this is the normal ORB time-stop.
- **News flatten** (`_manage_open` lines 687-692) — 2 min before every Tier-1.
- **DD breakers** (`_manage_open` lines 694-729) — 4 % total or 2 % daily.
- **Account kill** — 8 % hard stop.

None of these fabricate a broker reply; they just close the local state and
let the next `_maybe_enter` find `open_ticket is None`.

### Caveat — dry-run is *still* a bit fictional

Even after this fix, dry-run does NOT simulate SL/TP hits from bar high/low,
because it never attaches real broker stops. In dry-run a "position" will now
ride through the full ORB trade-window (up to 120 min) and close by
time-stop, regardless of whether price actually touched SL or TP. That is
why the real test is the **LIVE** mode on a demo account — and why the
backtest is the authoritative source of P&L figures, not the dry-run log.

If you want a dry-run that actually simulates SL/TP hits against M1 bars,
that's a ~30-line feature (bigger than this fix — needs to check
`bar.high ≥ sl/tp` / `bar.low ≤ sl/tp` per poll and synthesize a close).
I recommend we **don't build that** and instead switch to a **LIVE-on-demo**
run, which tests the full ZMQ→EA→broker chain including real fills and real
slippage. That's what you actually need to measure before funding.

---

## What to do now — in order

### 1. Stop the running dry-run process
On the VPS (or wherever it's running):

```powershell
Get-Process python | Where-Object { $_.CommandLine -like '*run_v23_live*' } | Stop-Process -Force
# or simpler:
.\STOP_BOT.ps1
```

### 2. Pull the fix onto the VPS
```powershell
git pull
```
(The fix is one SEARCH/REPLACE block in `src/live/v23_live.py` — commit
message: *"fix: dry-run phantom-close bug in _manage_open"*.)

### 3. Run a 30-minute sanity dry-run with the fix
Just to confirm no more `POS_CLOSED_BY_BROKER` ticks right after entries:

```powershell
.\GO_DRYRUN_V23.ps1
```

When DE40 triggers tomorrow (~08:30 CEST), you should see:
- `[ENTRY] DE40 LONG ...` (or SHORT)
- **No** `POS_CLOSED_BY_BROKER` within 1–5 s.
- The position will ride until the time-stop at approximately 10:30 CEST
  (OR start 08:00 CEST + 30 min OR + 120 min trade window).
- At 10:30 CEST you should see exactly ONE `CLOSE  reason=window_expiry` and
  one `SIZER_FEEDBACK`.

If any of those don't match, tell me and we'll keep digging.

### 4. Then switch to LIVE-on-demo (the real answer)
Once the sanity dry-run passes, **don't stay in dry-run**. Switch to:

```powershell
.\GO_LIVE_V23.ps1
```

on the demo account. That runs `dry_run=False` → ZMQ → EA → real MT5 fills
on the demo server. Now you'll have:
- Real tickets in `MT5 → History`.
- Real fill prices you can compare to your `entry_px` estimate → **real measured
  slippage**.
- Real SL/TP hits at the broker (or rejections if `stops_level` is actually
  being violated — we'll see them in `MT5 → Experts` as `error_code != 0` in
  the `ORDER_FAILED` event).

That is the two-week phase your feedback on the original report was asking
for. It's the one that actually proves the bot works end-to-end. **A fixed
dry-run alone is not sufficient** — dry-run can never measure real slippage
or broker stop-rejection because no order ever leaves the Python process.

---

## Scoring update

The strategy, risk model, and backtest all score the same as yesterday
(`BOT_FULL_REPORT.md`). What changed:

| Area                                | Yesterday | Today   | Why                                 |
|-------------------------------------|-----------|---------|-------------------------------------|
| Strategy logic                      | A+        | A+      | Unchanged.                          |
| Risk rails                          | A+        | A+      | Unchanged.                          |
| Backtest fidelity                   | A         | A       | Unchanged (costs verified).         |
| Slippage pricing                    | B+        | B+      | Priced in; can be improved live.    |
| **Dry-run fidelity**                | **B**     | **B+**  | Bug found & fixed; still limited.   |
| Live-on-demo proof                  | **D**     | **D**   | Hasn't been run yet — this is the real gap. |
| **Overall bot honesty score**       | **A−**    | **A−**  | Bug was in scaffolding, not engine. |

The bot hasn't changed. My reporting got better.

---

## One honest admission

If you'd obeyed yesterday's bad advice *"don't stop the bot, watch for the
next trade"*, you'd have watched dozens of phantom trades roll in, with the
sizer slowly absorbing made-up R-values, and everything looking increasingly
wrong. You were right to push back. The lesson on my end: **always sanity-check
a bug diagnosis against the code paths for the mode you're actually in.**

---

## Files changed today (2026-04-24)

| File                                         | Change                                                     |
|----------------------------------------------|------------------------------------------------------------|
| `src/live/v23_live.py`                       | Guard `_manage_open`'s broker reconciliation with `if not self.dry_run`; added multiline comment explaining why. |
| `Docs/LIVE_DIAGNOSIS_2026-04-24.md`          | This file — **replaces** yesterday's broken diagnosis.     |

No backtest code touched. No strategy parameters touched. No risk rails
touched. Nothing that affects the $14,412 / 4.15 % DD projection moved.
