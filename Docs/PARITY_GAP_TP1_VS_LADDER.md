# V30 Parity Gap: TP1-only (live) vs Full Ladder (backtest)

**Date:** 2026-04-28
**Trigger:** User asked "is the live bot trading exactly the way the backtest did?"
**Answer:** No — but the gap is smaller than feared.

---

## The bug, in one paragraph

`src/orb_engine_v20.py` (used by the backtest) places trades with a 3-stage
exit ladder: **50%** closes at TP1, **25%** at TP2 (twice as far), **25%**
trails 0.8×ATR(14) from peak.

`src/live/v30_live.py` (the live runner) places **TP1 only** at the broker.
There's a comment that says *"TP2 managed by us"*, but **the management code
was never written**. When TP1 hits, the broker closes the entire position.
TP2 and the trailing stop never execute.

This has been quietly carried from v23 → v24 → v25.1 → v30.

---

## Numerical impact (149 entries, 8 weeks of fresh 5%ers data)

| Metric | Full Ladder (backtest) | TP1-only (live reality) | Δ |
|--------|----------------------:|------------------------:|--:|
| Net P/L | **$26,021** | **$22,108** | **−$3,913 (−15%)** |
| Net P/L (% of $100k) | 26.02% | 22.11% | −3.9 pp |
| Win rate | 57.72% | 57.72% | identical |
| Profit factor | 1.82 | 1.70 | −7% |
| Avg winner | $670 | $625 | −7% |
| Avg loser | −$502 | −$502 | identical |
| **Max drawdown** | **$4,710** | **$4,338** | **−$372 (live is SAFER)** |
| Worst single day | −$2,018 | −$2,018 | identical |
| Sharpe (approx) | 5.04 | 4.77 | −5% |

### Per-symbol cost

| Symbol | Ladder $ | TP1-only $ | Δ |
|--------|---------:|-----------:|--:|
| DE40   | $8,187 | $5,592 | **−$2,595** |
| US30   | $9,273 | $8,346 | −$927 |
| XAUUSD | $5,868 | $5,494 | −$374 |
| US500  | $2,693 | $2,677 | −$16 |

DE40 (which has the widest TP2 = 3.0×OR_range) loses the most when
runners are cut off — over half the parity gap is DE40 alone.

### Trade-flow reality check

Of 149 entries:
- **70 (47%)** stopped at SL — never touched TP1 → **identical** in both modes
- **43 (29%)** reached TP1 but reversed before TP2 → **TP1-only is BETTER** here (full 100% banked at TP1; ladder only banks 50% then loses some on the runner)
- **36 (24%)** reached TP2 / ran with the trail → **ladder is better** here

The ladder wins on the *meaty trends* (24% of trades) and loses on the
*medium pops that mean-revert* (29%). Net of those two effects across
this sample: ladder beats TP1-only by 15%.

---

## The Merton sizer impact is tiny

| | mean_R | var_R | r* (γ=3) |
|---|------:|------:|---------:|
| Ladder seed | 0.244 | 1.532 | **5.31%** |
| TP1-only reality | 0.207 | 1.270 | **5.44%** |

Difference: **2.3% relative**. Because variance shrinks roughly in
proportion to mean when you cut off both upside (TP2) and the
trail-back-down, the **Merton ratio (mean / variance) is preserved**.

→ The 264-trade seed in `live_state/sizer_state.json` is ~2% off
   target. Negligible. The sizer doesn't need re-seeding.

---

## What this means for live results so far

- **Today's $271 DE40 win** is comparable in shape to a **TP1-only** outcome.
  In ladder-mode the same trade would have paid more (~$420–500) if TP2 hit, or
  slightly less (~$200) if the runner reversed. The R-multiple is the same.

- **The bot's edge is real either way.** TP1-only still produces:
  - +22% annualised in this 2-month sample
  - 4.34% max DD (well inside the 8% account-killer)
  - 2.0% worst day (well inside the 5%ers daily 5% rule)
  - 57.7% win rate, 1.70 profit factor, Sharpe ~4.77

- **The bot is *more conservative* than the backtest claimed.** Lower
  drawdown, lower variance, lower upside. Sharpe is essentially preserved.

---

## Three options for the user

### Option A — Add TP2 + trail to live (close the gap)
- **Effort:** ~½ day + tests + a clean v30.3 release.
- **Upside:** Match the backtest. Recover the ~$4k/year that's currently being left on the runners.
- **Risk:** New code paths in the live engine (partial-close orders, trail computation, race conditions with broker). Adds operational complexity.
- **Verdict:** Worth doing — the backtest results you've trusted assume this.

### Option B — Re-baseline the backtest as TP1-only (accept current behaviour)
- **Effort:** ~30 min: change `tp1_close_frac=1.0` in the backtest engine, re-run, update docs.
- **Upside:** All your forward-looking docs become honest. Sizer is already well-calibrated.
- **Risk:** None. But you give up ~$4k/year of demonstrated edge.
- **Verdict:** Cheap and honest, but suboptimal.

### Option C — Do both
1. Re-baseline the backtest as TP1-only (Option B) **today** so all docs are honest.
2. Schedule v30.3 (Option A) to add TP2/trail and capture the missing edge.
- **Verdict:** Recommended.

---

## Files

- `Scripts/compare_tp1_only_vs_full_ladder.py` — this analysis
- `Results/tp1_vs_ladder_compare.json` — machine-readable output
- `Results/tp1_vs_ladder_compare.txt` — human-readable output
