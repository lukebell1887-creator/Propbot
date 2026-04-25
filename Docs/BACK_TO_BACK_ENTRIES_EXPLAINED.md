# Why Did The Bot Fire A Trade The Instant One Closed?

**Short answer: That's by design — the concurrency cap is a gatekeeper, not a cooldown. When US500 closed at 13:45 UTC it freed up one of the 2 slots, so the next queued breakout (US30 short) filled immediately. Statistically this matches the backtest's behaviour, but the resulting trade is a "chase" entry that's worth understanding.**

---

## 1. Timeline of what happened

From your log:

| UTC | Event | Open positions | `entries_today` |
|---|---|---|---|
| 11:46 | US500 SHORT entry | 1 | 1 |
| 12:07 | XAUUSD SHORT entry | 2 | 2 |
| 12:08–13:44 | Both slots full → every US30/DE40 tick gets **`BLOCKED=concurrency_cap(2)`** (`cap_hits` climbs 0 → 194) | 2 | 2 |
| **13:45:00** | US500 CLOSE (`window_expiry`, +1.10R) → slot frees | 1 | 2 |
| **13:45:05** | US30 SHORT entry (fills instantly — 5 seconds after US500 closes) | 2 | **3** |
| 13:45–14:00 | US30 sits for 15 minutes | 2 | 3 |
| 14:00:00 | US30 + XAUUSD both CLOSE at trade-window end | 0 | 3 |

So the "2→3 entries in 5 seconds" is the bot releasing a queued breakout the moment a slot opens.

---

## 2. Why it happens — the code logic

In `src/live/v23_live.py` the entry flow for each symbol on every new bar is:

1. Has the OR window closed? → if yes, continue
2. Is the current close **outside** the OR range (i.e. breakout condition met)? → if yes, continue
3. Am I already in a position on this symbol? → if yes, skip
4. News/calendar/daily-halt blocks? → if yes, skip
5. **Concurrency cap full (≥ 2 open)?** → if yes, `cap_hits++`, **stay in `WAIT_BREAK` state, retry next bar**
6. If all gates pass → fill the trade

Crucially step 5 does NOT consume the breakout signal. The `break_short_triggered` flag only gets set **after** the trade actually fills. So every subsequent bar that still satisfies "close < OR_low" retries the entry. The moment the cap frees (a different symbol closes), the next bar's breakout check passes and the trade fills.

This is why we see **`cap_hits=194` build up** over 90 minutes. That's 194 blocked entry attempts — one per ~28 seconds on average.

---

## 3. Is this a problem?

**Mathematically no, practically it's a known ORB quirk.** Here's the honest breakdown.

### The good side
- **No missed signals.** If the market breaks out hard and the cap is full because of two other winning trades, you don't miss the third opportunity — it fills as soon as a slot opens.
- **Risk is still correct.** The sizer uses the current SL distance (OR_high to current close), so the bot sizes down to keep risk-per-trade at $110. US30 entered at 49139.73 with SL at 49424.36 = 285 pts risk → lots=0.3 (versus a fresh entry at OR low that would have been ~143 pts risk → lots=0.6). $110 risk either way.
- **Backtest tested this behaviour.** The 65% win rate and drawdown figures already include these late-queue fills.

### The not-so-good side
- **The US30 entry at 49139.73 was 141 pts BELOW the OR low** — the breakout had already played out for 30+ minutes. ORB edge comes from "momentum following the initial break"; a chase entry misses the initial burst and can get mean-reverted.
- **Trades bunch in time.** All 3 trades today happened in a ~2-hour window, zero temporal diversification.
- **TP1 is further away in absolute terms** (287 pts vs 143), so the probability of hitting TP1 drops.

Result of today's chase trade: US30 held 15 minutes, price barely moved (±0.1R), closed at time-stop for **−$0.75** (essentially flat). Not a disaster, but also no edge realised.

---

## 4. Is the backtest honest about this?

Yes — per `Docs/AUDIT_V23_REPORT.md` and the ledger (`Scripts/accountability_ledger.py`), the backtest replays the exact same "queue and release when cap frees" logic. The 65% WR / 1.77R avg win / $23k 3-month return in V23_LOCKED_RESULTS.md is **net** of this behaviour — including the chase trades that only get 15 minutes.

So figuratively speaking, today's US30 trade was **a −$0.75 sample of a known −$5–$10 expected loss distribution** from late queue-release entries. It's priced in.

---

## 5. Optional improvement (for v24 / later)

If you want to be more defensive, add a **breakout freshness filter**:

```python
# Reject entries where price is already more than N% of the OR range
# beyond the breakout level.
or_high, or_low = st.or_tracker.or_high, st.or_tracker.or_low
or_range = or_high - or_low
if direction == "short":
    depth_below_low = or_low - float(bar.close)
    if depth_below_low > 0.5 * or_range:    # i.e. price is already > 0.5R below OR low
        self.counters["block_stale_break"] += 1
        return  # skip — breakout is stale
```

**Expected impact** based on today's trade: US30 would have been rejected (depth was 98% of OR range below OR_low). We'd have traded 2 instead of 3. Lost P/L: minimal (the stale trade was basically breakeven anyway).

**Whether to add this to v24**: defer until 10+ stale-entry examples exist. A single trade isn't enough to justify adding filter complexity.

---

## 6. Bottom line

| Question | Answer |
|---|---|
| Is "2→3 entries in 5 seconds" a bug? | **No.** It's the concurrency cap releasing a queued signal. |
| Is chasing a 30-min-old breakout a risk? | **Mild.** The backtest tested this and still achieved 65% WR. |
| Should I change anything before live-micro? | **No.** Let the 1-day $1-risk test run as-is. |
| Should we add a staleness filter in v24? | **Maybe.** Collect 2 weeks of live data first, then decide. |

The bot is doing exactly what the backtest did. Your intuition is good — chase entries *are* statistically weaker than fresh-break entries — but the framework already accounts for them. **Proceed with confidence.**
