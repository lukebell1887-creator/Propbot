# 🔬 CORRECTED FORENSIC REPORT (v2) — Both agents were partly right

**Date:** 21 April 2026

## What my independent verification found

I re-ran my own classifier on the committed $78k trade log. Numbers match the previous agent's within noise:

| Category | N | % | Net $ |
|---|---:|---:|---:|
| **A — "Invalid-order win"** — SL on wrong side of entry, price reverted to it same bar | **146** | **78.5 %** | **+$79,514** |
| D — Valid SL loser (SL on correct side, price went against us) | 37 | 19.9 % | −$6,390 |
| E — Valid TP (price hit mid-band target) | 3 | 1.6 % | +$5,589 |
| **Total** | **186** | **100 %** | **+$78,712** |

**Without bucket A**, the strategy is **40 trades, net −$801**. The previous agent's core claim is correct: **≈78 % of trades have SL on the wrong side of entry, and those are where ~100 % of the profit comes from.** An MT5 broker will reject those orders at the ticket stage.

## Where my earlier report was wrong

My FORENSIC_REPORT_WHAT_BROKE_THE_BOT.md framed the v18.1 guard as "a well-intentioned fix that destroyed a profitable bot." The reality is more nuanced: **the v18.1 guard exposed a pre-existing live-viability problem.** The $78k backtest was never achievable on a real 5%ers account as currently coded. Revert = restores the *appearance* of the edge in the simulator, but those 146 orders would never live.

## Where the previous agent's conclusion goes too far

"The REAL strategy loses money" — this is only true under the assumption that the 146 "wrong-side SL" trades are completely unachievable in live trading. But look at what they actually are:

- **Engine intent:** mean-reversion. When z = −2.8 and price has overshot far below the lower BB band, the engine is saying "this is extreme; price should revert back up toward the band."
- **Chosen exit level** (`band − 0.5 × ATR`): in a deep overshoot, this level is actually **above entry**. Hitting it is a WIN, not a loss. In code it was wired into the `stop_loss` slot — that's the bug.
- **Backtest says** on 146 of those setups, price did revert to that level within the same 1-minute bar.

If we **re-code the exit** so that this level is sent to MT5 as a **take-profit (TP)** — or held as a python-side limit — those 146 trades are NOT fake. They represent real price reversion events. The only question is: **do they survive realistic live conditions** (no intrabar cheating, proper fill sequencing, spread/slippage)?

That question is **NOT answered yet**, and neither of us can claim to know until we actually re-backtest with correct semantics.

## What the 3 possible truths look like

| Scenario | What a corrected backtest would show | Verdict |
|---|---|---|
| **(i) Edge is real** — just mis-packaged | PnL stays close to +$78k when "wrong-side SL" is reimplemented as TP + a proper wide SL, with strict bar-forward exits (no same-bar fills) | Ship it with corrected exit logic |
| **(ii) Edge is half-real** | PnL falls to, say, +$10k–$30k once intrabar cheating is removed; WR drops from 78 % to, say, 55 % | Still tradeable, but expect 10-20 %/yr, not 300 %/yr |
| **(iii) Edge is spurious** | PnL flatlines to ~0 or negative when same-bar fills are disallowed | Previous agent is right — do not go live |

**I refuse to guess which one is true.** The data is ambiguous without the corrected backtest.

## Why the same-bar-exit question is the real pivot

`bars_held = 0` on 146 of 146 bucket-A trades means: price entered at the close of bar N and hit the target level within bar N+1 (or within the same bar N using the high/low). Live:

- If we enter at the **close of bar N** with a python-side limit-TP at the target, and the next tick prints at or through the target, we'd fill. **This could be legitimate.**
- But broker fill semantics matter: the backtest uses the bar's high/low as known data. Live only sees ticks one at a time, and spread may be wider exactly when price is extended.

So the answer depends on whether the bot can actually submit a limit order that fills at the stated reversion level under live spread/latency. It's an engineering question with a quantitative answer — we need to measure it.

## The correct next step (small, concrete, honest)

Do **not** revert v18.1 and go live (I was wrong about that). Do **not** shelve the bot as "no edge" (previous agent was too pessimistic). Do **this**:

1. **Add a new SL/TP logic** in `smartbb_engine_v14.py` that properly separates:
   - **Reversion target (TP)** = the old "sl" level computed from `band ± 0.5 × ATR` — used when on the profitable side of entry.
   - **Real stop-loss (SL)** = `entry ∓ 1.5 × ATR` (or optimised) — a safety floor that is ALWAYS on the losing side of entry.
   - **Order** exits when either TP or SL fires first, on bar-close ticks **after** the entry bar (no same-bar cheating).
2. **Re-run the 3-month backtest.** Record PnL, WR, DD, avg bars held, % of trades that exit same-bar-after (which is the bulk of the claimed edge).
3. **Only then** decide: scenario (i), (ii), or (iii).

This is work, not a revert. I estimate ~30 min of engine changes + 5 min of backtest time = ~35 min before we have an honest answer.

## What to tell your dry-run bot right now

**Leave it dry-running.** It cannot do damage (orders are not sent). The telemetry you're seeing shows the signal gates working correctly at the *signal* level. The question is only about exit mechanics, and that's backtest work, not live work.

**Do NOT go live tonight.** Whatever the final answer, we should only go live after the corrected backtest shows a real edge under proper semantics.

---

## Summary — who was right about what

| Claim | Verdict |
|---|---|
| "v18.1 patch changed backtest from +$78k to −$2k" | **True** (my finding; verified numerically) |
| "≈78 % of the $78k profit is from trades MT5 would reject as invalid" | **True** (previous agent's finding; verified independently) |
| "Just revert v18.1 and the bot is profitable for live" | **False** — I was wrong. The orders still can't be placed. |
| "The strategy loses money; cannot go live" | **Premature** — the 146 reversion events might be capturable by a correct TP-based exit, or might not. We need the corrected backtest to decide. |

So: **both of us contributed real evidence, neither of us had the full answer, and the path forward is neither "revert and ship" nor "give up" — it is "rebuild the exit logic correctly and re-measure."**
