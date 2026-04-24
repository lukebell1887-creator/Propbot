# UK-time trading windows for v23 (4-pair ORB)

> **⚠ Corrected 2026-04-24.** An earlier version of this file claimed the
> windows in `V23_ORB_CONFIGS` were UTC-hours. They are not — they are
> **broker-server hours** (MT5 server clock, UTC+2 in GMT, UTC+3 in DST).
> The error in the previous doc would shift every window 2–3 h later than
> reality. The bot itself was always correct — only this doc was wrong.
> Proof in §"How I know" below.

The bot trades **4 symbols, not 5**. An older universe carried USDJPY; v23
dropped it because it wasn't earning its keep in the 3-month OOS.

All `or_start_hour` values in `src/live/v23_live.py::V23_ORB_CONFIGS`
are **MT5-broker-server-clock** hours. 5%ers' MT5 server runs on:

* **UTC+2** when the server is in its winter offset (roughly Nov → Mar)
* **UTC+3** when the server is in its summer offset (roughly Apr → Oct)

UK clocks ALSO shift (GMT = UTC+0 in winter, BST = UTC+1 in summer), but
the two DST changeovers don't land on the same weekend. Between the two
transitions there's a ~2-week window where UK↔broker offset briefly = +1 h
instead of the usual +2 h. Don't be surprised if your windows slide an
hour in late March / late October.

The bot pulls the live broker offset from `bridge.get_server_time()` at
startup (`[broker-offset] +10800 s` = +3 h, DST in force), so the code is
always correct. Only humans reading the clock need to do arithmetic.

---

## Summer (BST = UTC+1, broker = UTC+3) — CURRENTLY IN FORCE

Broker is 2 h ahead of UK. All four symbols use a **2-hour entry window**
(`trade_window_minutes=120` measured from OR close). OR length differs:
US500 is 15-min, everything else is 30-min. That's the only asymmetry.

| Symbol   | OR builds (BST) | Entries can fire (BST) | Window closes (BST) | Entry window | Session theme                    |
|----------|-----------------|------------------------|---------------------|--------------|----------------------------------|
| DE40     | 06:00 – 06:30   | **06:30 – 08:30**      | 08:30               | **2 h**      | NY previous-day close echo / Asia→EU handover |
| US500    | 12:30 – 12:45   | **12:45 – 14:45**      | 14:45               | **2 h**      | NY **pre-open** (NYSE opens 14:30 BST) |
| US30     | 12:30 – 13:00   | **13:00 – 15:00**      | 15:00               | **2 h**      | NY **pre-open** (NYSE opens 14:30 BST) |
| XAUUSD   | 12:30 – 13:00   | **13:00 – 15:00**      | 15:00               | **2 h**      | London→NY metals handover        |

Why 2 hours and not 1? The v23 3-month grid-search bumped every symbol
from the original 60 to 120: a 1-hour window caught the first break fine
but forced time exits on moves that were still extending. Lengthening to
2 h added ~+18 % total-PnL at ~+0.6 trades/mo/symbol average with no
material DD change.

> **Note on session names** — the independent audit flagged that the
> anchor fires ~2 h *before* NYSE cash open, not *at* it. The original
> docstring comments on `or_start_hour` saying "NYSE open" are therefore
> misleading. What the bot actually trades is the London-close-into-NY
> build-up, not the 14:30 BST NYSE bell itself. This is a known edge-risk
> flagged as Risk #1 in `AUDIT_INDEPENDENT_2026-04-23.md`.

---

## Winter (GMT = UTC+0, broker = UTC+2) — takes over at 25 Oct 2026 01:00 UK

Broker offset drops from +3 h to +2 h on the Sunday morning when Europe
leaves DST. UK clocks go back the same weekend, so the NET UK↔broker gap
stays at 2 h and the table below matches the summer one hour-for-hour.
(It's only in late-March / late-October that the offset is temporarily
weird — see above.)

| Symbol   | OR builds (GMT) | Entries can fire (GMT) | Window closes (GMT) | Entry window |
|----------|-----------------|------------------------|---------------------|--------------|
| DE40     | 06:00 – 06:30   | **06:30 – 08:30**      | 08:30               | 2 h          |
| US500    | 12:30 – 12:45   | **12:45 – 14:45**      | 14:45               | 2 h          |
| US30     | 12:30 – 13:00   | **13:00 – 15:00**      | 15:00               | 2 h          |
| XAUUSD   | 12:30 – 13:00   | **13:00 – 15:00**      | 15:00               | 2 h          |

---

## Weekly schedule at a glance

```
Mon - Thu:   DE40 fires in the morning (06:30-08:30 BST),
             US set in the early afternoon (12:45-15:00 BST)
Fri:         same, but by 15:00 BST nothing more can fire
Sat 00:00 UTC → Mon ~07:00 UTC:  TradingCalendar.can_enter() returns (False, "weekend")
Mon 06:00 BST (summer) / 06:00 GMT (winter):  DE40 OR rebuilds, new week begins
```

---

## Hard blocks that override the table above

Even inside the windows above, entries are silently suppressed by any of:

1. **Weekend** — `TradingCalendar` blocks Sat/Sun fully (incl. late Friday
   rollover 21:55–22:10 UTC).
2. **News ±15 min** — Tier-1 events from `data/news/tier1_2026.csv` block
   entries for 15 min either side. Positions are *flattened* 2 min **before**
   the event. After the tz-fix in `v23_live.py`, the news rails now compare
   `bar.time → real UTC` against the CSV's real-UTC timestamps.
3. **Concurrency cap** — max 2 open positions across the portfolio. So if
   DE40 is still open past 08:30 BST and both XAUUSD + US30 break at 13:00
   BST, only two of the three can be taken (first-to-signal wins).
4. **Daily halt (2 %)** — if today's DD ≥ 2 % of start-of-day equity, no
   new entries today. Open positions keep their broker SL/TP.
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

Your current restart was **08:57 UTC (09:57 BST)** with all warmup green.
Because 09:57 BST is **1 h 27 min AFTER the DE40 window closed (08:30 BST)**,
DE40 shows `state=WINDOW_CLOSED` correctly and will next re-arm on Monday
at 06:00 BST.

| Time today (BST) | What happens                                            |
|------------------|---------------------------------------------------------|
| 06:00–06:30      | DE40 OR built (already done, we weren't running)        |
| 06:30–08:30      | DE40 trade window (already ended, so `WINDOW_CLOSED`)   |
| 12:30–12:45      | US500 OR builds (15-min OR)                             |
| 12:30–13:00      | US30 + XAUUSD OR build (30-min OR)                      |
| **12:45–14:45**  | US500 entry window (2 h) — **first real test of fix**   |
| **13:00–15:00**  | US30 + XAUUSD entry windows (2 h)                       |
| 14:45            | US500 time-stop if still open                           |
| 15:00            | US30 + XAUUSD time-stop if still open; no more entries Fri |
| **Sat + Sun**    | Weekend block — no entries, heartbeat only              |
| Mon 06:00 BST    | Cycle repeats (DE40 first)                              |

---

## How I know (proof the broker-time interpretation is correct)

At 09:57 BST / 08:57 UTC the live heartbeat said:

```
US30   OR=n/a  state=PRE_OR  close=49127.31  t-153m→OR_open
```

**153 minutes from 09:57 BST = 12:30 BST.**

- If the `or_start_hour=14` value in `US30`'s ORBConfig meant **UTC**,
  then `bar.time.hour == 14` would occur at UTC 14:00 = BST 15:00, and
  the log would say `t-303m` (not 153).
- If the value means **broker-time** (broker = UTC+3 in DST), then
  `bar.time.hour == 14` occurs at broker 14:00 = real UTC 11:00 =
  BST 12:00. The OR then closes at broker 14:30 = BST 12:30. The
  countdown from 09:57 BST to 12:30 BST = **153 min**. ✅

The bot, the audit, and the live countdown all agree. The previous
version of this doc (that said "windows are UTC") was written against
the stale docstring in `src/momentum/orb.py`, not against the code's
actual behaviour.

---

## How to verify live

On the VPS, `tail -f Results/v23_live_events.log` will show JSON rows like:

```json
{"ts_utc":"2026-04-24T12:30:02","kind":"ENTRY","symbol":"US30","side":"LONG",...}
{"ts_utc":"2026-04-24T15:00:15","kind":"CLOSE","symbol":"US30","reason":"window_expiry"}
```

Those `ts_utc` fields are **real UTC** (wall clock), not broker. To get UK
time, add 1 h in BST or 0 h in GMT.

Heartbeat telemetry is also one-shot-readable at any time:

```powershell
Get-Content Results\v23_live_telemetry.json | ConvertFrom-Json |
  Select-Object ts_utc, equity, dd_pct_total, open_count
```
