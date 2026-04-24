# UK-time trading windows for v23 (4-pair ORB)

The bot trades **4 symbols, not 5**. An older universe carried USDJPY; v23
dropped it because it wasn't earning its keep in the 3-month OOS.

All windows in `src/live/v23_live.py::V23_ORB_CONFIGS` are expressed in
**UTC**, which means the *UK wall-clock time* shifts by 1 hour between summer
(BST) and winter (GMT). The bot itself is unaffected — only your clock is.

---

## Summer (BST = UTC+1) — CURRENTLY IN FORCE

| Symbol   | OR builds (BST) | Entries can fire (BST) | Window closes (BST) | Session theme                    |
|----------|-----------------|------------------------|---------------------|----------------------------------|
| DE40     | 09:00 – 09:30   | **09:30 – 11:30**      | 11:30               | Frankfurt cash / Xetra open      |
| US500    | 15:30 – 15:45   | **15:45 – 16:45**      | 16:45               | NYSE cash open (short OR)        |
| US30     | 15:30 – 16:00   | **16:00 – 17:00**      | 17:00               | NYSE cash open                   |
| XAUUSD   | 15:30 – 16:00   | **16:00 – 17:00**      | 17:00               | NY metals pit (COMEX)            |

*(US500 uses a 15-min OR + 120 min trade window = closes 45 min earlier than
US30/XAUUSD which use a 30-min OR + 120 min trade window.)*

---

## Winter (GMT = UTC+0) — takes over at 25 Oct 2026 01:00 UK

| Symbol   | OR builds (GMT) | Entries can fire (GMT) | Window closes (GMT) |
|----------|-----------------|------------------------|---------------------|
| DE40     | 08:00 – 08:30   | **08:30 – 10:30**      | 10:30               |
| US500    | 14:30 – 14:45   | **14:45 – 15:45**      | 15:45               |
| US30     | 14:30 – 15:00   | **15:00 – 16:00**      | 16:00               |
| XAUUSD   | 14:30 – 15:00   | **15:00 – 16:00**      | 16:00               |

*(Note: NYSE cash open also shifts by 1 hour in US DST vs UK DST. The
UTC-anchored config keeps the bot exactly on the NYSE open regardless.)*

---

## Weekly schedule at a glance

```
Mon - Thu:   full slate trades (DE40 AM, US set PM)
Fri:         full slate trades; by 18:00 BST nothing more can fire
Sat 00:00 UTC → Mon ~07:00 UTC:  TradingCalendar.can_enter() returns (False, "weekend")
Mon 09:00 BST (summer) / 08:30 GMT (winter):  DE40 OR rebuilds, new week begins
```

---

## Hard blocks that override the table above

Even inside the windows above, entries are silently suppressed by any of:

1. **Weekend** — `TradingCalendar` blocks Sat/Sun fully (incl. late Friday
   rollover 21:55–22:10 UTC).
2. **News ±15 min** — Tier-1 events from `data/news/tier1_2026.csv` block
   entries for 15 min either side. Positions are *flattened* 2 min **before**
   the event.
3. **Concurrency cap** — max 2 open positions across the portfolio. So if DE40
   is still open at 11:00 BST and XAUUSD + US30 both break at 16:00 BST, only
   two of the three can be taken (first-to-signal wins).
4. **Daily halt (2 %)** — if today's DD ≥ 2 % of start-of-day equity, no new
   entries today. Open positions keep their broker SL/TP.
5. **Static 4 % daily kill-switch** — if the rolling total DD against the
   day-start equity hits 4 %, the bot flattens and halts.
6. **Total 4 % DD breaker** — peak-to-trough 4 % → flatten all + permanent
   session kill (no day-rollover reset).
7. **Account kill (8 %)** — legacy belt-and-braces on top of the 4 % breaker.
8. **Already-taken this day** — ORB tracker has a per-day first-touch flag;
   once DE40 breaks long today, the next long-break *this day* is ignored.
   You get at most ONE entry per symbol per day.

---

## Today (Fri 2026-04-24) specifically

Your restart was at **06:54 UTC (07:54 BST)** with all warmup green:

| Window (BST)   | What happens                                     |
|----------------|---------------------------------------------------|
| 09:00–09:30    | DE40 OR builds (state `BUILDING_OR`)              |
| **09:30–11:30**| DE40 can fire ONE entry on first 1-min break      |
| 11:30          | If still open, time-stop `reason=window_expiry`   |
| 12:00–15:30    | Nothing — DE40 done, US set pre-OR                |
| 15:30–15:45    | US500 OR builds                                   |
| 15:30–16:00    | US30 + XAUUSD OR build (30-min OR)                |
| **15:45–16:45**| US500 entry window                                |
| **16:00–17:00**| US30 + XAUUSD entry windows                       |
| 17:00          | All windows closed; no more entries Fri           |
| **Sat + Sun**  | Weekend block — no entries, heartbeat only        |
| Mon 09:00 BST  | Cycle repeats                                     |

---

## How to verify live

On the VPS, `tail -f Results/v23_live_events.log` will show JSON rows like:

```json
{"ts_utc":"2026-04-24T08:30:02","kind":"ENTRY","symbol":"DE40","side":"LONG",...}
{"ts_utc":"2026-04-24T10:30:15","kind":"CLOSE","symbol":"DE40","reason":"window_expiry"}
```

If instead you see `POS_CLOSED_BY_BROKER` 1-5 seconds after an ENTRY — the
fix hasn't pulled. `git log --oneline -1` should show `bb928b2`.

Heartbeat telemetry is also one-shot-readable at any time:

```powershell
Get-Content Results\v23_live_telemetry.json | ConvertFrom-Json |
  Select-Object ts_utc, equity, dd_pct_total, open_count
```
