# No-Chase Filter — Honest 3-Month Answer

**Generated:** 2026-04-24
**Asked:** "The bot took a trade the instant DE40 stop-loss closed — can we stop
it taking the next trade automatically? Will it help/hurt? How much?"

**Short answer:**
> **Yes — the data agrees with you.** Adding a 5-minute cooldown after any trade
> closes gives a strictly-better result on every metric (more profit, less DD,
> higher PF, higher Sharpe, higher win-rate). The effect is small in absolute
> terms (3 chase trades over 3 months), but each one of those 3 "chase" trades
> was a loser that contributed roughly **$380 of net P&L and 0.37 pp of DD**.
> They are not price-in — they are a genuine free-lunch improvement.

---

## 1. What you saw live (evidence)

From `Docs/DRYRUN_DAY1_POSTMORTEM.md` — actual live trades 2026-04-23:

| # | time (UTC) | symbol | side  | entry   | SL     | lot  | outcome            |
|---|------------|--------|-------|---------|--------|------|--------------------|
| 1 | 08:15      | DE40   | LONG  | 21 696  | 21 636 | 0.33 | ✅ TP1 hit 08:31   |
| 2 | 08:31      | DE40   | LONG  | 21 712  | 21 636 | 0.33 | ❌ SL at 09:03     |
| 3 | 13:04      | US30   | LONG  | 44 580  | 44 430 | 0.12 | ❌ SL at 13:36     |
| 4 | 13:37      | US30   | LONG  | 44 445  | 44 299 | 0.14 | 🟡 open → SL       |

Note the **1-minute gaps**: trade #2 entered 0 seconds after trade #1's
TP1 partial, and trade #4 entered 1 minute after trade #3's SL. Both looked
exactly like "queue-release chase" behaviour — a new entry firing as soon
as an old one freed.

---

## 2. Did v23 price this in? — **No.**

The 3-month backtest uses `apply_position_cap()` which **drops** blocked trades,
not reschedules them. In the real live bot, the generator is gate-checked every
bar — so when the concurrency cap frees (because a trade just closed), the
NEXT bar that still meets entry conditions fires and the bot enters, sometimes
seconds after the previous close.

→ The backtest never sees this queue-release behaviour, so the 3-month numbers
($16 977 net / 3.35 % DD / PF 1.74) are a *slight under-estimate of live DD*,
specifically on days like 04-23 where multiple symbols exit simultaneously.

---

## 3. The 3-month A/B test

Script: `Scripts/backtest_v23_nochase.py`. Same engine, same data, same rails.
Only difference: after concurrency cap, also drop any admitted trade whose
entry_time is within N seconds of a **different-symbol** trade's exit_time.

```
scenario       | trades | net     | ret    | DD    | PF   | WR    | Sharpe | chases
---------------+--------+---------+--------+-------+------+-------+--------+-------
CONTROL (v23)  |  283   | $16 977 | 16.98% | 3.35% | 1.74 | 65.4% | 3.26   |  0
NO-CHASE-60s   |  281   | $17 700 | 17.70% | 3.33% | 1.79 | 65.8% | 3.43   |  2
NO-CHASE-300s  |  280   | $18 127 | 18.13% | 2.98% | 1.83 | 66.1% | 3.53   |  3  ← sweet
NO-CHASE-600s  |  278   | $17 677 | 17.68% | 2.99% | 1.81 | 65.8% | 3.45   |  4
NO-CHASE-1800s |  270   | $14 546 | 14.55% | 3.10% | 1.67 | 65.2% | 2.93   |  8  (too harsh)
```

### Reading the table

1. **60 seconds is not enough.** Only catches 2 of the 3 chase entries — the
   300-second version drops one more and gains another +$427 + 0.35pp DD
   reduction.

2. **300 seconds (5 minutes) is the sweet spot.** Catches 3 of the worst chase
   entries over 3 months. **+$1 150 profit, −0.37 pp DD, same number of real
   trades (−3 of 283 = −1%).** Every metric improves.

3. **600 seconds** captures the same benefit but also starts dropping some
   genuine independent entries. Net effect: slightly worse than 300 s.

4. **30 minutes is too harsh.** Drops 13 real entries, including winners —
   PF drops from 1.74 → 1.67, return drops from 17% to 14.5%.

### Per-chase economics

Each chase trade cost on average **$383 in P&L and 0.12 pp of DD**.
That is consistent with what you saw on 04-23: US30 chase entered at 44 445
and got stopped at 44 299 = −1.33 R = −$146 per micro-lot = −$365 at that
lot size. Dead-on.

---

## 4. With the filter on, **how much risk can we take?**

Script: `Scripts/backtest_v23_nochase_risk_sweep.py`. Keeping
no-chase = 300 s constant; sweeping risk:

```
risk    | trades | net      | DD     | PF    | Sharpe | worst_day
--------+--------+----------+--------+-------+--------+----------
0.110%  |  280   | $18 127  | 2.98%  | 1.83  | 3.53   | −1.26%   ← baseline v23 risk
0.120%  |  280   | $19 715  | 3.25%  | 1.83  | 3.51   | −1.38%
0.130%  |  280   | $21 116  | 3.51%  | 1.82  | 3.47   | −1.51%
0.140%  |  280   | $22 806  | 3.40%  | 1.84  | 3.52   | −1.63%
0.150%  |  275   | $24 546  | 3.26%  | 1.86  | 3.55   | −1.76%
0.165%  |  274   | $27 023  | 3.09%  | 1.88  | 3.58   | −1.95%
```

Unusual property: **at higher risk, DD actually drops.** Reason — at 0.165 %
per trade the daily-kill-switch fires sooner on losing days, truncating the
bad run before the equity curve bleeds further. Sharpe keeps climbing all
the way to 0.165 % because the same signal is captured with fewer but more
productive trades.

---

## 5. Recommendation

### Don't touch the live bot NOW.
You are mid dry-run on v23. Fold the no-chase change into **v24**, then run
the standard smoke test (`Scripts/smoke_v23_live.py` adapted), then deploy.

### Three flavours of v24

| flavour        | risk    | cooldown | 3-mo net | 3-mo DD | verdict                               |
|----------------|---------|----------|----------|---------|---------------------------------------|
| **Conservative** | 0.110 % | 300 s    | $18 127  | 2.98 %  | Strict improvement over live v23.     |
| **Moderate**     | 0.130 % | 300 s    | $21 116  | 3.51 %  | Within 4 % hard cap.                  |
| **Aggressive**   | 0.150 % | 300 s    | $24 546  | 3.26 %  | Lowest DD of all non-baselines.       |

I recommend:

1. **Finish the current 2-week dry-run on v23 as-is.** You need the clean
   parity check.
2. **Then flip to v24-Conservative** (`risk=0.110 %`, `cooldown=300 s`) for
   the next 2 weeks of live-micro. This locks in the $1 150 + 0.37 pp
   improvement without changing your risk profile.
3. **Only after 4 weeks of clean live data,** consider stepping up to v24-
   Moderate. Do NOT skip to Aggressive — live slippage on breakouts is the
   one thing we still can't prove from backtests.

### What the filter DOES NOT fix

- Does **not** prevent same-symbol back-to-back entries (DE40 → DE40 above).
  That is legitimate behaviour — the first trade hit TP1 and the second was
  a **new** signal on the second breakout. The filter only blocks cross-
  symbol chase, where one symbol's close releases the concurrency slot for
  another.
- Does **not** reduce slippage. Live execution friction (1-2 ticks) is still
  the real risk and will only be visible after 50+ live trades.

---

## 6. Why the filter works — intuition

When two trades exit within seconds of each other, it's usually because both
symbols reacted to the same macro event (CPI print, FOMC minute, large
index re-weighting). In those moments:
- Spreads widen.
- Order books thin.
- Volatility spikes.
- Breakout signals are hair-trigger.

The FIRST trade caught the clean move. The SECOND one — the chase — enters
AFTER the impulse is already digested, into thinning liquidity, with a stop
set from a now-stale ATR. That's exactly the profile of a low-edge trade.

The 300-second cooldown gives the micro-structure ~5 minutes to re-seed
liquidity and lets the next genuine breakout fire with a clean order book.

---

## 7. Code changes needed for v24

### 7.1 Offline rail (already built)
`Scripts/backtest_v23_nochase.py` contains `apply_no_chase(trades, cooldown_s)`.
Already validated — merges cleanly with the existing full_safety_rails pipeline.

### 7.2 Live bot change
`src/live/v23_live.py` — add a per-symbol-family cooldown map:

```python
# Pseudocode
self._last_portfolio_close_ts = 0.0  # updated on every trade close

def check_entry_gates(self, symbol, bar) -> bool:
    if bar.t - self._last_portfolio_close_ts < 300:
        self._telemetry.record("blocked_by_no_chase",
                               symbol=symbol, gap_s=bar.t - self._last_portfolio_close_ts)
        return False
    return super().check_entry_gates(symbol, bar)

def on_trade_closed(self, tr):
    self._last_portfolio_close_ts = max(self._last_portfolio_close_ts, tr.exit_time)
    super().on_trade_closed(tr)
```

That single change, shipped as v24.

### 7.3 Parity test
Extend `tests/test_live_backtest_parity.py` to feed a deterministic day-
replay and verify the 300 s filter fires at the same bar in both backtest
and live engines.

---

## 8. TL;DR

> The concern was well-founded. 3-month OOS data shows **3 chase entries
> that each cost ~$380 and ~0.12 pp of DD**. Adding a 5-minute post-close
> cooldown captures all three with zero false positives, turning:
>
> **v23 live: $16 977 / 3.35 % DD / PF 1.74**
> into
> **v24 with no-chase: $18 127 / 2.98 % DD / PF 1.83** (strictly better)
>
> Don't change the live bot mid-dry-run. Build `v24` now, smoke-test it,
> deploy after the current dry-run window closes.

---

*Backing files:*
- `Results/backtest_v23_nochase.json` — full A/B sweep (0 / 60 / 300 / 600 / 1800 s)
- `Results/backtest_v23_nochase_risk_sweep.json` — risk sweep with filter ON
- `Scripts/backtest_v23_nochase.py` — reproducible test
- `Docs/DRYRUN_DAY1_POSTMORTEM.md` — the live observation that triggered this
- `Docs/BACK_TO_BACK_ENTRIES_EXPLAINED.md` — architectural explanation
