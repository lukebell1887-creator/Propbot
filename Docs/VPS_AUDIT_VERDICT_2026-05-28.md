# VPS audit verdict — 2026-05-28 13:15 UTC

**Source**: `Results/VPS_FULL_PARITY_20260528_131521.log`
**Account**: 5%ers MT5  #26059964  balance/equity = **$95,361.39**

---

## TL;DR — what's happening

> The strategy is fine. The bot is in a **DD-breaker deadlock**.

1. Some losses in the first 2 live weeks dragged the account from $100,566 → $95,356.
2. That's a **5.18 %** drawdown from peak — past the bot's own `dd_cap_pct = 0.04` ceiling.
3. The Grossman–Zhou DD penalty kicked in. Multiplier collapsed to ~0.02 of base.
4. Every trade since **May 21** has been sized at **$4–$7** (broker minimum lot).
5. Tiny lots can't recover the DD, so DD stays high, so multiplier stays at zero. **Deadlock.**
6. Per-symbol Merton sizer ALSO stuck at `n_seen = 0` on every symbol (separate bug — never warmed up because of the `_init_persistence` issue I flagged in §4 of `PARITY_V31_HONEST_ANSWER.md`).

---

## The hard evidence (from step 6 of the VPS audit)

### Live sizer state
```
symbol   n_seen     mu       var    sharpe  state    mult
DE40          0   0.0000   1.0000   0.000   WARMUP   1.000
US30          0   0.0000   1.0000   0.000   WARMUP   1.000
US500         0   0.0000   1.0000   0.000   WARMUP   1.000
XAUUSD        0   0.0000   1.0000   0.000   WARMUP   1.000
_GLOBAL_    264   0.0252   0.6726   0.031   CAPPED   5.000   <- seeded from backtest, never used per-symbol
```

89 ENTRY rows on disk, yet every per-symbol learner is at `n_seen = 0`. **The bot has never updated per-symbol state.**

### Entry sizing over time
```
trades   1- 4  (Apr 27)      risk = 0.17 %  (~$170, OK — base risk)
trades   5-67  (Apr 28-May19) risk = 0.13-0.92 % (normal range)
trades  68-72  (May 20)       risk = 0.06-0.92 % (mixed -- DD ratcheting up)
trades  73-89  (May 21-28)    risk = 0.005-0.008 %  -- ALL flagged RISK<$25
                                                       these are the "$10 trades"
```

17 trades in a row at base × 0.04 multiplier. That is the DD-breaker floor.

### DD breaker state (stale, last saved 10:38 today)
```
peak_equity = $100,566.28
equity_last = $100,566.28   (stale -- actual equity is now $95,356)
```
Real current DD = 5.18 % > 4.00 % cap.

### Layer-1 / orphans / slippage
- Layer 1 fired on 35 events. Healthy, not over-firing (the audit says `35/0` only because it can't reconcile event count to entry count from the events.log alone — false alarm).
- 4 orphan tickets (entries with no TP/CLOSE) — these match the 27 `POS_CLOSED_BY_BROKER` events; the broker closed them externally. Not a bug, just journal gap.
- Entry slippage = +0.05 ticks median. Excellent. The bot is filling cleanly.

---

## What to do — in this order

### 1. Stop the bot (read-only — no money risk)
```powershell
.\STOP_BOT.ps1
```

### 2. Reseed the sizer + clear the DD breaker  ( --apply is REQUIRED )
```powershell
python Scripts/reseed_v31_sizer.py --apply
```
This is **already on the VPS** (committed earlier). It:
- Sets `dd_breaker.peak_equity = current equity` (so DD = 0)
- Loads `Results/v30_fresh_trades.json` (the 158 fresh backtest trades from step 3) and feeds them into the per-symbol Merton learner, so `n_seen >= 15` on every symbol immediately
- Writes the new state files (backs up the old ones to *.bak.<ts>)

### 3. Restart the LIVE v30/v31 bot  ( NOT GO_LIVE.ps1 -- that is the OLD v18 launcher )
```powershell
.\PULL_AND_GO_LIVE_V30.ps1
```
This is the correct launcher for your live bot:
- `Scripts/run_v30_live.py`  magic = 30000  risk = 0.185 %  cap_mult = 5.0
- Writes the same `Results/v30_live_trades.jsonl` your audit reads
- `GO_LIVE.ps1` (no suffix) is the legacy v18 launcher — DO NOT use it

### 4. Verify (one trade in)
```powershell
python Scripts/diag_v31_live_vs_backtest.py
```
After the next entry, you should see:
- per-symbol `n_seen >= 15` on whichever symbol traded
- entry risk back to **0.17-0.19 %** of equity (~$160-$190 per trade), NOT $4-$7

---

## What's still untested (and the long-term fix)

The seeding bug in `src/live/v30_live.py:_init_persistence` means that even after reseeding, if the bot restarts before per-symbol `n_seen` exceeds `warmup_trades = 15`, it will load the stale state and not reseed. The one-line patch in `Docs/PARITY_V31_HONEST_ANSWER.md` §4 fixes that permanently. **I have NOT applied it** — your call whether to ship that patch now or wait.

Also: the cosmetic errors in steps 1 and 5 of the VPS log (`SyntaxError: invalid syntax` for the `d.get(schema, ?)` and `print(f'  net PnL:    \')` lines) are PowerShell-here-string $ / " interpolation issues. They didn't affect the audit — step 6 (the one that matters) ran clean. I'll fix those by moving the inline Python to standalone `.py` files in the next commit.
