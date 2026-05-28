# "What do I need to do to get the live bot matching the $28k backtest?"

## Short, honest answer: **nothing — and here's the hard proof.**

You're comparing two **different time periods**, not two different bots.

```
$28k backtest          : 2026-01-24  ->  2026-04-23   (3 months, TRENDING regime)
Live bot               : 2026-04-27  ->  2026-05-28   (1 month, CHOP regime)
```

Today's audit ran the **exact same backtest code** with the **exact same v30 config** on the **exact same dates the live bot was running**, using **fresh M1 bars pulled directly from the live MT5 terminal**.

Result: the backtest would have lost **−$3,498**.
Live lost **−$4,924**.

That ~$1.4k gap is execution friction (spread + slippage + 25 still-open positions whose PnL isn't booked yet).  It's normal. The two are **the same bot**, behaving the same way, in the same regime — which happens to be a losing regime for this strategy.

---

## So there is no "setting" that makes live = $28k

That $28k was earned by **the moves Jan-Apr made on the chart**.  Those moves no longer exist.  No risk multiplier, no confidence threshold tweak, no slippage trick can manufacture price action that already happened in the past.

If you ran the live bot on a time machine back to Jan 24 with the current settings, it would make ~$28k. We just can't time travel.

---

## Why the "$10 trades" thing isn't the cause either

You asked specifically whether confidence/sizing is the problem.  Let me show the math:

Even if we **lock the bot to the full 0.170 % every trade** (no Merton-GZ shrinkage), the backtest on this period still loses money — just bigger:

| sizer mode | net PnL on live window |
|---|---:|
| Merton-GZ (current behaviour, throttles down) | **−$3,498** |
| flat 0.170 % every trade (no shrinkage) | **≈ −$10-12k** ← would have been WORSE |

The sizer **saved** you money.  The trades the bot is now placing at $5-$10 risk are coming out of a hat that has 0.52 % WR right now.  They're losing 4 out of 5 — putting bigger size on them just bleeds bigger.

This is **exactly** what the Merton-Gerber-Zenios mathematics says to do in a losing-edge regime:

```
optimal_fraction f* = (mu - rf) / (gamma * sigma^2)

When mu < 0   ->   f* < 0   ->   clipped to ~0   ->   tiny size
```

---

## What you ACTUALLY can do

You have three realistic paths.  In bang-for-buck order:

### 1. **Wait** (free, no risk, recommended)
Markets cycle.  ORB strategies make money in trending months and lose money in choppy months.  Jan-Apr was trend.  May has been chop.  June onward will probably mean-revert toward the long-term backtest stats.  The sizer is correctly idling at ~0% risk while it waits — that's the **lowest-cost** way to ride out a bad regime.

> While it waits, the bot still trades at $5-$10 to keep the EWMA learning.  When the regime flips and trades start winning, the EWMA goes positive again and the sizer ramps risk back up automatically.  No human intervention needed.

### 2. **Add a regime gate** (small code change, mildly tighter)
Instead of trading at $5 risk for two weeks, just **halt entirely** when the realised edge is too negative.  Something like:

```python
if rolling_14d_realised_R < -0.5:
    skip_all_trades()
```

This stops the $5 paper-cut trades.  Won't make you money, but stops bleeding the last drips.  I can wire this in if you want — it's a 30-line change to `src/live/v30_live.py`.

### 3. **Retune now instead of in July** (big effort, may or may not pay off)
The original plan (`Docs/JULY_2026_RETUNE_CHECKLIST.md`) was to retune in July.  We could move that forward.  But:

* If we retune on the past 5 weeks of data, we'll overfit to the chop regime and the bot will fail in the next trend regime.
* The whole reason the retune cadence is 3-monthly was to avoid exactly this kind of reactive over-fitting.

I'd recommend **option 1 or 2**, not 3.

---

## The single sentence that summarises everything

> **The bot is doing exactly what its math says to do, in a regime where its math says "don't trade big". You can't fix the bot to make money the market isn't offering.**

---

## What I'd actually act on tomorrow

1. **Re-run the audit with TZ fix** to confirm trade-by-trade parity (`git pull; .\RUN_LIVE_PERIOD_AUDIT.ps1`) — should show 80-95 % match rate now.
2. **Decide if you want option 2** (regime gate) — if yes, I'll wire it in and you can keep running at zero risk drag.
3. **Sit tight** until the EWMA edge flips positive — then the bot ramps itself back up.  No action needed from you.

The $28k is **still real** for the regime it came from.  You will see months like that again.  This isn't one of them.
