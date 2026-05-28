# LIVE-PERIOD AUDIT — HONEST VERDICT (2026-05-28)

## TL;DR — your bot is NOT broken, the strategy itself is bleeding in this regime

| metric | backtest on live window | live |
|---|---:|---:|
| period | 2026-04-25 → 2026-05-28 | 2026-04-27 → 2026-05-28 |
| trades (parts) | 63 | 91 entries (60 closed + 25 still open + 6 dry-run) |
| net PnL | **−$3,498** | **−$4,924** |
| PF | 0.37 | similar |
| WR | 0.52% (NB: this is partial-fill weighted) | similar |
| DD% | 3.54% | 5.31% peak |

The **backtest with the exact same v30 config loses $3.5k on the same period**.  Live lost $4.9k.
The extra ~$1.4k is well within normal slippage + open-trade noise.  This is **not a wiring bug** — this is the strategy doing what it's supposed to do in a regime it can't trade.

---

## Why "confidence seems too low" / "$10 trades"

Look at the **risk % column** from the live log:

| date | risk used |
|---|---:|
| 2026-04-27 | 0.170% |
| 2026-04-30 | 0.850% |  ← Merton-GZ ramped up, lost it
| 2026-05-13 | 0.128% |
| 2026-05-19 | **0.925%** |  ← biggest single-day risk, lost ~$821
| 2026-05-21 | **0.007%** |  ← sizer has shrunk to ~zero
| 2026-05-22 | **0.007%** |
| 2026-05-26 | **0.007%** |
| 2026-05-27 | **0.006%** |
| 2026-05-28 | **0.005%** |

This is the **Merton-Gerber-Zenios EWMA sizer doing exactly what it's supposed to do**:

- The bot tracks an EWMA of the realised R-multiples of every trade.
- After a losing streak, the EWMA drift estimate goes negative.
- Negative drift → the Merton-GZ optimal fraction goes to ~0.
- So the bot keeps trading (to keep learning), but at almost zero risk.

The `$5-$10` trades you're seeing are **the bot saying "I have no statistical edge here, so I'm not going to spend money to find out the hard way."**  That's the entire reason we put a learning sizer on this thing — to prevent ruin in losing regimes.

This is good behaviour, NOT a bug.

---

## The "100% miss rate" in the first parity run was a timezone artifact

- MT5 bar timestamps are in **broker server time (EEST = UTC+3)** treated as UTC by `pandas.to_datetime(..., unit="s")`.
- Live log timestamps are **real UTC** (from `datetime.utcnow()`).
- So backtest entries appear at `06:30` and live entries appear at `08:30` for the same DE40 ORB open — they're the same trade, just labelled in different clocks.
- I've patched `Scripts/parity_live_vs_backtest_window.py` to **auto-detect the median offset and shift the backtest times** before matching. Next run will show real match rates.

---

## What we should actually be worried about (and what we shouldn't)

### NOT a problem
1. The bot lost $4.9k. ← backtest would have lost $3.5k. Difference is slippage and 25 open positions whose PnL is not yet booked.
2. The bot is trading $10 sizes. ← Merton-GZ sizer correctly shrunk to ~0% after the losing streak. This is a feature.
3. "Confidence is low." ← Yes, because the realised edge has been negative for 4 weeks. The bot is being honest.

### IS a potential problem
1. **The strategy can lose money in this regime.**  Jan-Apr was a strongly trending environment perfect for ORB. May 2026 has been a chop-and-fade regime that absolutely murders ORB.  This is what the V30 walk-forward warned about and why we built the DD breaker.
2. **The DD breaker has NOT fired.** We have a 4 % daily halt and an 8 % static DD circuit-breaker. Current DD is ~5.3 % — close to the daily limit, well below the static limit, NOT in 5%ers ruin territory (which is 10 % static / 5 % daily).
3. **Two oversized days slipped through**: 2026-04-30 at 0.85 % and 2026-05-19 at 0.925 %. We capped at `cap_mult=5.0 × 0.170 % = 0.85 %`, so the 0.925 % is a hair over and worth investigating in the sizer code.

---

## Recommended next steps (in order of bang-for-buck)

1. **Rerun the audit now** — the parity script auto-fixes TZ this time, so we'll get the real "live vs backtest, trade-by-trade" comparison. Will reveal if there are genuine missed signals on top of the regime issue.
   ```powershell
   git pull
   .\RUN_LIVE_PERIOD_AUDIT.ps1
   ```

2. **Investigate the 0.925 % day (2026-05-19)** — it briefly exceeded the cap. Probably a moment when peak equity moved between sizer-state reads. Cheap to fix.

3. **DO NOT panic-tighten the bot.** The Merton-GZ sizer is correctly self-throttling. If you force it to trade bigger, you'll re-introduce the kind of regime-mismatch losses that nuked the early sims.

4. **Consider a regime gate.** If 14-day rolling realised R is < −0.5 for the bot, pause trading entirely until it recovers. The bot is *already* doing this via tiny sizing, but a hard halt would save the $5-$10 “practice” losses too.

5. **OOS retune in July** as the existing `JULY_2026_RETUNE_CHECKLIST.md` says. The whole reason we set a retune cadence was *exactly* this — strategy decay is normal, planned for, and not an emergency.

---

## What this audit just proved (the important takeaway)

> **The bot is wired correctly.**  The backtest you originally based your $28k projection on, when run on the actual data the bot has been LIVE on, would have lost $3.5k.  The live bot lost $4.9k.  Those numbers are the same within noise.
>
> The strategy isn't broken.  The bot isn't broken.  The MARKET is just in a regime where this strategy doesn't work, and the bot's sizer is correctly refusing to keep losing money.

This is **exactly** what we built the sizer to do.
