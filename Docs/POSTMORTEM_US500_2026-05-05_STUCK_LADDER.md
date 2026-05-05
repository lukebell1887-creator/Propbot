# POSTMORTEM — US500 ticket #547550971 stuck-ladder, 2026-05-05

## TL;DR — read this first
- **The bot is mostly working.** Out of 9 closed positions in the last 7 days that
  *looked* like stuck-ladder events in my first audit, 8 were actually the bot's
  trail-SL working correctly — my diag classifier didn't know about the moving SL,
  so it false-flagged them. The bot's TP1/TP2/TRAIL_SL ladder has fired on **8
  separate positions in 7 days** (DE40 ×2, US500 ×3, US30 ×1, XAUUSD ×2).
- **There is exactly ONE confirmed real bug:** US500 ticket #547550971, opened
  2026-05-05 14:45:02 UTC, held for 120 minutes, ran past both TP1 and TP2, and
  the partial-close ladder fired **zero** events. You manually closed it at
  7245.94 for +$321.23 (~12 points past TP2).
- **Cost:** vs. the bot's intended ladder, your manual rescue was actually
  *better* than the design (because price kept running far past TP2 and the bot
  would have only kept the last 25 % running). But the bug is real and the next
  time price hits TP2 then reverses, **we lose the entire profit.**

---

## What happened — minute by minute

| UTC time | event | source |
|---|---|---|
| 14:45:00 | Bot fires US500 LONG signal, writes ENTRY to trades.jsonl | trades.jsonl |
| 14:45:02 | MT5 confirms BUY 26.18 lots @ 7233.67 | MT5 deal history |
| 14:45:02 | Bot logged plan: SL=7227.15, TP1=7235.24, TP2=7237.01 | trades.jsonl |
| 14:45 → 16:45 | Position lives 120 minutes. Price **crosses TP1 (7235.24)** at some point, **crosses TP2 (7237.01)**, runs all the way to 7245.94 | inferred from final price |
| 14:45 → 16:45 | **Zero TP1_PARTIAL / TP2_PARTIAL / TRAIL_SL events for ticket 547550971** | events.log |
| 16:45:09 | You manually click-close. MT5 fills SELL @ 7245.94. +$321.23 | MT5 deal history |

---

## Why the ladder didn't fire

In `src/live/v30_live.py` the ladder code is gated like this (paraphrased):

```python
# Per-symbol bar-close handler
if st.open_ticket is not None and st.partial_state is not None:
    res = self.partial_mgr.update(state=st.partial_state, bar_high=bar.high, ...)
    if res.tp1_fired: ...
    if res.tp2_fired: ...
    if res.sl_moved: ...
```

If **either** `st.open_ticket` **or** `st.partial_state` is `None` when a bar closes
on US500, the ladder is silently skipped — no exception, no log line, nothing.
That's exactly the symptom we observed for ticket 547550971 across 120 bar-closes.

### How either field could become / stay None despite an open position

There are four plausible failure modes:

1. **Registration race.** The order_send returns success and writes the ENTRY row
   to trades.jsonl *before* the code that assigns `st.open_ticket` and
   `st.partial_state`. If an exception is raised between those two writes (e.g.
   `PartialState.__init__` raising on a NaN ATR for US500 around 14:45 UTC), the
   ticket is in MT5 but the in-memory state is empty. The bot logs ENTRY and then
   moves on; nothing ever ties US500 back to the ladder.

2. **Bot restart.** If the bot was restarted at any point during the 120 minutes
   the position was open (manual restart, deploy script, crash + auto-restart),
   `st.open_ticket` and `st.partial_state` are reinitialised to `None`. The
   re-init code reconciles open positions against the broker but **does not
   re-create `partial_state`** for already-open tickets.

3. **Per-symbol bar-close starvation.** The ladder only fires on US500 *bar
   closes*, not on a global poll. If the M1 bar fetch loop for US500 specifically
   stopped advancing (e.g. MT5 returned the same `last_bar_time` repeatedly, or
   `mt5.copy_rates_from_pos` returned None for an extended period for that one
   symbol), the ladder never gets to run. **The heartbeat count of 0 from
   `findstr` today is a hint here — either the bot doesn't write a HEARTBEAT
   `kind` (likely; the events.log may use a different field name) or the loop
   was fully stalled.**

4. **Layer-1 deploy disturbance.** You have `PULL_AND_TEST_V31_LAYER1.ps1` and
   freshly-modified Layer-1 code in your tree from this session. If a redeploy
   landed during the 14:45–16:45 window, the in-memory state was wiped on
   restart. This is hypothesis #2 with a concrete trigger.

We can't tell which of the four it was without process-level evidence (no
heartbeat lines, and no exceptions visible in the events log, which itself is a
red flag — exceptions inside the bar-close handler may not be reaching the
event log).

---

## The fix — three layers of defence, none of which require us to find the exact root cause

### Layer A: persist `partial_state` to disk (survives restart)

After every change to `st.partial_state` (creation, TP1 fire, TP2 fire, trail
move, position close → None), write the dict to `Results/v30_partial_state.json`.
On bot startup, load that file *before* the first bar-close handler runs. Cost:
one ~200-byte JSON write per minute when something actually changes; trivial.

### Layer B: broker-source-of-truth reconciliation (every bar close)

Before the ladder check on every bar close, run this sanity pass per symbol:

```python
broker_pos = mt5.positions_get(symbol=sym, magic=BOT_MAGIC)
broker_ticket = broker_pos[0].ticket if broker_pos else None

if broker_ticket is not None and st.partial_state is None:
    # Broker has an open position the bot has lost track of.
    # Rebuild partial_state from trades.jsonl ENTRY row.
    entry_row = self._lookup_entry_row(broker_ticket)
    if entry_row is not None:
        st.open_ticket = broker_ticket
        st.partial_state = PartialState.from_entry_row(entry_row)
        self._log_event("LADDER_RECOVERED", symbol=sym, ticket=broker_ticket,
                        reason="state_was_None_but_position_exists")
```

This single block makes the bot **self-healing**. Whatever caused
`partial_state` to be lost (race, restart, exception, GC), the next bar close on
that symbol restores it — and we get a `LADDER_RECOVERED` event in the log so
we can quantify the bug going forward.

### Layer C: stuck-ladder watchdog

Every M1 poll, regardless of in-memory state: for each open position with bot
magic, if **broker price has been beyond TP1 for ≥3 consecutive M1 closes** AND
no TP1_PARTIAL has been logged for that ticket, force the partial close anyway.

```python
# Run this BEFORE the per-symbol bar-close handler
for pos in mt5.positions_get(magic=BOT_MAGIC):
    entry = self._lookup_entry_row(pos.ticket)
    if entry is None: continue
    bars_beyond_tp1 = self._count_recent_bars_beyond(pos.symbol, entry["tp1"], pos.type, 3)
    has_logged_tp1 = self._has_logged_event(pos.ticket, "TP1_PARTIAL")
    if bars_beyond_tp1 >= 3 and not has_logged_tp1:
        self._force_partial_close(pos, fraction=0.5,
                                  reason="watchdog_TP1_never_fired")
```

This is the belt-and-braces line of defence. Even if A and B both fail, the
watchdog catches us within 3 minutes of price clearing TP1.

### Layer D (cosmetic but important): emit a real per-minute heartbeat

Add a `kind: "HEARTBEAT"` event every 60 s containing:

```json
{"ts_utc": "...", "kind": "HEARTBEAT",
 "open_positions": [547550971, ...],
 "partial_state_keys": {"US500": true, "DE40": false, ...},
 "broker_state_mismatches": 0,
 "ladder_recoveries_today": 0}
```

That gives us live visibility on whether the in-memory state matches the broker,
and a 1-line tripwire: any time `partial_state_keys[X]=false` while `X` is in
`open_positions`, the watchdog should already be acting — but at minimum the
operator (you) sees it instantly.

---

## Diag-tool fix (small but important)

`Scripts/diag_ticket_dealprice.py` currently false-flags TRAIL_SL hits as
`MANUAL/STUCK` because it doesn't read the events log. Patch v2:

1. Read every TRAIL_SL event for the ticket from `Results/v30_live_events.log`
2. If exit price is within tolerance of the **last** TRAIL_SL value → bucket as
   `TRAIL_SL_HIT` (not `MANUAL/STUCK`)
3. Only flag `MANUAL/STUCK` when exit price is far from every level the bot
   ever set for that ticket

This is a 30-line change. I'll do it next session.

---

## What you should do right now

1. **Leave the bot stopped.** No new positions = no new stuck-ladder risk. The
   bug only bites when a position is held long enough to cross TP1 and the
   in-memory state has been lost.
2. **Take the win on today's manual close** — your $321 was actually *better*
   than the bot's design would have produced.
3. **Wait for the v31.1 patch** (Layers A + B + D — Layer C is a stretch goal).
   I'll write it in the next session with fresh context.
4. **Re-deploy and verify** with a 1-day monitoring run before resuming full
   live trading. Specifically watch for any `LADDER_RECOVERED` events — those
   tell us the bug is happening but the fix is catching it.

---

## What to tell yourself when the panic sets in

- The bot is **not** silently bleeding profit. 8 of 9 "MANUAL/STUCK" rows my
  v1 diag flagged were the bot working perfectly.
- The bug is **real but rare**: 1 confirmed instance in 7 days.
- The bug is **fixable** without rewriting the engine. Layers A+B+D = ~150
  lines of Python plus one new persistence file.
- Once the fix is in, every future occurrence will be **logged** as a
  `LADDER_RECOVERED` event, so we can measure the underlying race rate and
  decide whether we need to chase it deeper.

---

*Generated 2026-05-05 by Cline diagnostic session. Bot kept stopped pending fix.*
