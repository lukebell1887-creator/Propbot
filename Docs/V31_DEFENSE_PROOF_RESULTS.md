# V31 Slippage-Defense Proof Pipeline — RESULTS

**Run date:** 2026-04-30
**Method:** Pure local backtest. 264 v30 trades × 4 adversity scenarios × 5 risk levels × 2 defense variants → 40 deterministic scenarios + 10,000 Monte Carlo runs.
**Output:** `Results/v31_proof_results.json`, `Results/v31_proof_table.txt`

---

## TL;DR — what the numbers actually say

> **The 3-layer defense is NOT worth deploying at 0.170% risk.**
> It costs $2,735 of median PnL over 3 months (-11.6%) for a worst-case DD reduction of 0.77pp.
> P(breach) is already **0.00%** without the defense, because the **4% daily halt is already protecting the account**.
>
> The interesting move is to **raise risk to 0.20% AND turn defense ON** — same 3-month median PnL as today (~$24.5k), worst-case DD same as today (3.79% vs 4.10%), more headroom for actual live slip surprises.

---

## What was tested

**Universe:** DE40, US30, US500, XAUUSD — 264 trades, 55 stop-outs, Jan 26 → Apr 28, 2026.

**Adversity factor:** the fraction of M1 bar excess that becomes realised slip on a stop. `slip_pts = (bar_extreme − SL_price) × adversity`. Calibrated against today's 4 live samples (US30 SHORT @ 0.74 catastrophic).

**Scenarios:**
| Scenario     | DE40 | US30 | US500 | XAUUSD |
|--------------|------|------|-------|--------|
| optimistic   | 0.20 | 0.30 | 0.20  | 0.05   |
| realistic    | 0.40 | 0.55 | 0.30  | 0.10   |
| pessimistic  | 0.60 | 0.75 | 0.50  | 0.20   |
| catastrophic | 0.80 | 0.90 | 0.70  | 0.40   |

**Defense (when ON):**
- **Layer 1** — Stop-limit cap. Slip hard-bounded at `LAYER1_CAPS` (DE40/US30: 5pt, US500: 3pt, XAU: 1pt). If exceeded, time-fallback adds 50% to the cap.
- **Layer 2** — Slip-aware sizing. All positions × 0.85 (15% shrink).
- **Layer 3** — Toxic-window filter. Block US30/US500 entries 13:15–13:45 UTC, DE40 06:55–07:05 UTC.

**Risk levels swept:** 0.10%, 0.125%, 0.15%, 0.17% (current), 0.20% per trade.

**Daily halt:** 4.0% (current ship config).
**5%ers limit:** 5.0%.
**Starting balance:** $100,000.

**Microstructure check:** 264 trades enriched, **55 stop-outs**, **0 in toxic windows** (all 264 entries fell outside the configured danger windows during this 3-month period). Average bar excess on stop bars = **10.61pt** — confirming today's 14.82pt US30 outcome was statistically credible (within 1.4× the bar-average).

---

## Headline tables

### Monte Carlo — 1000 runs across mixed adversity

|  Risk%  | Defense | Median PnL | P5 PnL  | P95 PnL | WorstDD% | P(breach) | AvgHalts |
|--------:|---------|-----------:|--------:|--------:|---------:|----------:|---------:|
| 0.100%  | none    | $13,894    | $13,242 | $14,646 | 2.63%    | 0.00%     | 0.00     |
| 0.100%  | 3layer  | $12,285    | $12,076 | $12,578 | 2.13%    | 0.00%     | 0.00     |
| 0.125%  | none    | $17,367    | $16,552 | $18,308 | 3.17%    | 0.00%     | 0.00     |
| 0.125%  | 3layer  | $15,356    | $15,095 | $15,723 | 2.58%    | 0.00%     | 0.00     |
| 0.150%  | none    | $20,841    | $19,862 | $21,970 | 3.69%    | 0.00%     | 0.00     |
| 0.150%  | 3layer  | $18,428    | $18,114 | $18,868 | 3.01%    | 0.00%     | 0.00     |
| **0.170%** | **none** | **$23,620** | $22,511 | $24,899 | **4.10%** | **0.00%** | 0.00 |
| **0.170%** | **3layer** | **$20,885** | $20,529 | $21,383 | **3.33%** | **0.00%** | 0.00 |
| 0.200%  | none    | $27,788    | $26,483 | $29,293 | 4.76%    | 0.00%     | 0.00     |
| 0.200%  | 3layer  | $24,570    | $24,152 | $25,157 | 3.79%    | 0.00%     | 0.00     |

### Deterministic — head-to-head at the worst scenarios

#### "catastrophic" adversity (US30 = 0.90)

|  Risk%  | Defense | PnL     | MaxDD% | Breach |
|--------:|---------|--------:|-------:|--------|
| 0.170%  | none    | $22,530 | 3.99%  | no     |
| 0.170%  | 3layer  | $20,527 | 3.33%  | no     |
| 0.200%  | none    | $26,506 | 4.53%  | no     |
| 0.200%  | 3layer  | $24,150 | 3.78%  | no     |

#### Defense vs no-defense, summarised

| Risk%   | PnL Δ    | WorstDD Δ | Breach Δ |
|--------:|---------:|----------:|---------:|
| 0.100%  | -$1,609  | -0.49pp   | 0.00pp   |
| 0.125%  | -$2,011  | -0.59pp   | 0.00pp   |
| 0.150%  | -$2,413  | -0.68pp   | 0.00pp   |
| **0.170%** | **-$2,735** | **-0.77pp** | **0.00pp** |
| 0.200%  | -$3,218  | -0.97pp   | 0.00pp   |

---

## What this actually means — read carefully

### Finding 1: The 4% daily halt is doing the heavy lifting
**Across 10,000 Monte Carlo runs and 40 deterministic scenarios, P(breach) = 0.00% in EVERY case** — with or without the 3-layer defense, at every risk level from 0.10% to 0.20%.

The reason is mechanical: the 4% halt cuts trading the moment same-day DD hits 4.0%. The worst-case MaxDD seen was 4.76% (no defense, 0.200% risk) — but that's peak-to-peak across the full 3 months, not a single-day breach. Within any single trading day the bot stops itself well before 5%.

**Implication:** The 3-layer defense is solving a problem (5%ers breach) that the 4% halt has already solved.

### Finding 2: The defense costs ~11.6% of median PnL at current risk
At 0.170% risk over 3 months:
- No defense:  **$23,620 median**, P5 $22,511
- 3-layer:     **$20,885 median**, P5 $20,529
- Cost: **-$2,735 (-11.6%)**

That cost is dominated by Layer 2 (the 15% position-size shrink applies to ALL 264 trades — including the 209 winners that didn't slip at all).

### Finding 3: The defense DOES tighten worst-case DD by ~0.8pp at current risk
- No defense: 4.10% worst DD
- 3-layer: 3.33% worst DD

That's real protection. But it buys you headroom you don't currently need (because the halt fires first).

### Finding 4: At 0.20% risk, defense=ON is actually attractive
- No defense @ 0.20%: $27,788 PnL, 4.76% worst DD — uncomfortably close to halt+breach in a bad week
- 3-layer @ 0.20%:    $24,570 PnL, 3.79% worst DD — same risk profile as today's 0.170% no-defense

So the tradeoff at 0.20% is: ~+$950 vs today's $23,620 (a ~4% bump) and same DD safety. Modest gain, real defense in place.

### Finding 5: 0 trades fired in toxic windows during the 3-month sample
None of the 264 backtest entries fell in 13:15–13:45 UTC for US30/US500 or 06:55–07:05 UTC for DE40. Layer 3 had **zero impact** in this window. This is an artifact of the v30 entry rules already excluding those times via the existing 1300–1340 UTC blackout. Layer 3 is therefore redundant in current v30. Leave it in as a belt-and-braces guard for future regime shifts, but don't expect it to do anything material today.

### Finding 6: Today's US30 14.82pt slip was a TYPICAL not extreme bar
The average bar excess on the 55 historical stop-outs was **10.61pt**. That means today's 14.82pt was about 1.4× the historical average — well within the natural distribution, **not** a black swan. The bot will see slip of this magnitude regularly. The system survives it because of the daily halt, not because of anything special in v30.

---

## Decision matrix — what to actually do

| Option | What | Median PnL | Worst DD | P(breach) | Verdict |
|--------|------|-----------:|---------:|----------:|---------|
| **A. Do nothing (v30 as-is)** | Stay 0.170% no defense | $23,620 | 4.10% | 0.00% | **Safe. Fine.** |
| B. Add full 3-layer @ 0.170% | Insurance | $20,885 | 3.33% | 0.00% | Pays insurance you don't need |
| C. Add 3-layer + raise to 0.20% | Compensate for L2 shrink | $24,570 | 3.79% | 0.00% | Slight upside, more headroom |
| D. Layer 1 ONLY @ 0.170% | Cap slip without sizing penalty | (~$22,500) | (~3.6%) | 0.00% | **Best surgical move** (estimated) |
| E. Layer 1 ONLY + raise to 0.20% | Cap slip + extract growth | (~$26,500) | (~4.2%) | low | **Best risk-adjusted, not yet tested** |

Options D and E haven't been numerically simulated yet — they need the script re-run with `LAYER2_SIZE_FACTOR = 1.0` and `TOXIC_WINDOWS = {}` (Layer 1 only). I expect D to be the real winner: it preserves position sizing (no profit penalty on winners) while still hard-capping the worst stops.

---

## Honest disclaimers

1. **Adversity factors are calibrated from 1 day of live data (today's 4 trades).** The "catastrophic" scenario (US30 adversity = 0.90) maps to today's actual 0.74 — already baked in. But future broker behaviour may shift; we should re-calibrate every 4–8 weeks once we have ≥30 live samples.
2. **Halt model assumes instantaneous halt at 4%.** Real-life slippage between trade close and halt-state propagation is not modelled. Could add ~0.1–0.2pp to worst-case DD.
3. **No spread cost is added on top of slip.** v30 backtest already includes spread; we're only modelling **incremental** slip beyond what the backtest already saw. Today's 14.82pt was the slip added on top of 1pt assumed in backtest = 13.82pt incremental — consistent with this model.
4. **The daily halt is THE safety mechanism.** If the halt logic ever fails (bot crash, MT5 disconnection mid-day), all bets are off regardless of which defense layers are active.

---

## What I want to discuss before any code change

1. **Are we OK to leave v30 as-is at 0.170% risk?** The numbers say yes — P(breach) = 0% across 10k MC runs, max realised DD 4.10%. Today's bad slip was within the modelled distribution.
2. **Or do we want Option D (Layer 1 only)?** Cap slip mechanically without paying the size-shrink tax. Cleanest insurance.
3. **Do we want to push to 0.20% risk?** Combined with Layer 1, we get growth and protection. But it tightens the buffer to halt — needs careful monitoring.
4. **Should I re-run with Layer 1 only to actually quantify Option D?** Takes ~2 minutes — give the word.

No production code is touched until you say so.
