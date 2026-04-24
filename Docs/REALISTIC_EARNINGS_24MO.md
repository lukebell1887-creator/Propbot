# Realistic 24-Month Earnings Forecast
**Date:** 2026-04-24
**Base evidence:** 3-month 5%ers backtest = +16.977 % ($100k → $116,977)
**Live haircut applied:** 60 % of backtest (middle of 50-70 % conservative band)

## Starting assumptions

| Parameter | Value | Why |
|---|---|---|
| Backtest monthly rate | +5.37 % compound | Measured: (1.16977)^(1/3) - 1 |
| Haircut | × 0.60 (= 60 % of backtest) | Midpoint of slippage + edge-decay band |
| **Live monthly rate (realistic)** | **+3.22 % compound** | 0.0537 × 0.60 |
| 5%ers profit split | 50 % trader / 50 % firm | Standard for High Stakes |
| Scaling trigger | +10 % equity gain | Account doubles |
| Challenge path | Step 1 +8 % then Step 2 +5 % | Then funded at $100k |

**Critical note:** these numbers assume the bot tracks the backtest at 60 % efficiency. It might do 100 % (hero-case, $310k+ equity gain in 24 months) or 30 % (dog-case, you scrape through the challenge then stall). **The first 4-6 weeks of paper trading is when we find out which universe we live in.** Treat everything below as a *target trajectory*, not a guarantee.

---

## Three strategies — pick one

You must choose before you go live. The bot is the same; the difference is what you do with monthly profits.

### Strategy A — **REINVEST EVERYTHING** (aggressive scaling)
Leave every $ on the account to trigger scaling as fast as possible. Zero cash until you decide to switch.

### Strategy B — **50/50 SPLIT** (balanced)
Withdraw 50 % of monthly profit as cash, leave 50 % on account to keep scaling (slower).

### Strategy C — **WITHDRAW ALL** (no scaling, cashflow-only)
Take all profit as monthly cash at the 50 % firm split. Account never grows beyond $100k.

---

## Month-by-month table — Strategy A (REINVEST, aggressive scaling)

| Month | Account at start | Status | Profit this month | Cash earned | Cum equity profit | Notes |
|:---:|---:|:---|---:|---:|---:|:---|
| **1** | $100k | CHALLENGE Step 1 | – | $0 | $0 | Buy eval, start trading |
| **2** | ~$103k | CHALLENGE Step 1 | – | $0 | $0 | +3.2 % progress |
| **3** | ~$106k | CHALLENGE Step 1 | – | $0 | $0 | +6.5 % progress |
| **4** | ~$108k | **Step 1 PASSED** → Step 2 | – | $0 | $0 | Account reset to $100k fresh |
| **5** | $100k | CHALLENGE Step 2 | – | $0 | $0 | Need +5 % now |
| **6** | ~$103k | **Step 2 PASSED** → FUNDED | – | $0 | $0 | Evaluation fee refunded |
| **7** | $100,000 | **FUNDED** — trading live | $3,220 | $0 | $3,220 | First funded month |
| **8** | $103,220 | Funded | $3,324 | $0 | $6,544 | |
| **9** | $106,544 | Funded | $3,431 | $0 | $9,975 | |
| **10** | $110,000 | **SCALE #1** → $200k | $0 | $0 | $10,000 | Account doubled by 5%ers |
| **11** | $200,000 | Funded @ $200k | $6,440 | $0 | $16,440 | Earning rate doubles |
| **12** | $206,440 | Funded | $6,647 | $0 | $23,087 | |
| **13** | $213,087 | Funded | $6,861 | $0 | $29,948 | |
| **14** | $220,000 | **SCALE #2** → $400k | $0 | $0 | $30,000 | Doubled again |
| **15** | $400,000 | Funded @ $400k | $12,880 | $0 | $42,880 | |
| **16** | $412,880 | Funded | $13,295 | $0 | $56,175 | |
| **17** | $426,175 | Funded | $13,724 | $0 | $69,899 | |
| **18** | $440,000 | **SCALE #3** → $800k | $0 | $0 | $70,000 | Tripled vs funded start |
| **19** | $800,000 | Funded @ $800k | $25,760 | $0 | $95,760 | Earning $25k/month |
| **20** | $825,760 | Funded | $26,589 | $0 | $122,349 | |
| **21** | $852,349 | Funded | $27,446 | $0 | $149,795 | |
| **22** | $880,000 | **SCALE #4** → $1.6M | $0 | $0 | $150,000 | |
| **23** | $1,600,000 | Funded @ $1.6M | $51,520 | $0 | $201,520 | Earning $50k+/month |
| **24** | $1,651,520 | Funded | $53,179 | $0 | $254,699 | |

**Strategy A result at month 24:**
- Account size: $1.65M (in the middle of scaling #5 which triggers at $1.76M ~month 25-26)
- Cumulative equity profit: **~$254,700**
- Cash in your bank: **$0** (you reinvested everything)
- If you stopped trading now and took the 50 % profit split: **$127,350 one-off withdrawal**

---

## Month-by-month table — Strategy B (50/50 SPLIT)

You take out half the profit each month as cash, leaving half on the account. Scaling is ~2× slower because only half of each month's profit compounds.

Effective compound rate for account growth = 3.22 % × 0.5 = **1.61 %/month**
Time to hit +10 % scaling = log(1.10)/log(1.0161) = **~6 months**

| Month | Account | Profit | Withdraw (50 %) | Cum cash | Notes |
|:---:|---:|---:|---:|---:|:---|
| 1-6 | $100k | – | – | $0 | Challenge phase (same 6 months) |
| **7** | $100,000 | $3,220 | **$1,610** | $1,610 | First cash! |
| **8** | $101,610 | $3,272 | $1,636 | $3,246 | |
| **9** | $103,246 | $3,325 | $1,662 | $4,908 | |
| **10** | $104,908 | $3,378 | $1,689 | $6,598 | |
| **11** | $106,597 | $3,432 | $1,716 | $8,314 | |
| **12** | $108,313 | $3,488 | $1,744 | $10,058 | approaching +10 % |
| **13** | $110,057 | **SCALE #1** → $200k | $0 | $10,058 | doubled @ $110,057 |
| **14** | $201,000* | $6,472 | $3,236 | $13,294 | *account reset to $200k + your $1k surplus |
| **15** | $204,236 | $6,576 | $3,288 | $16,582 | |
| **16** | $207,524 | $6,682 | $3,341 | $19,923 | |
| **17** | $210,865 | $6,790 | $3,395 | $23,318 | |
| **18** | $214,260 | $6,899 | $3,450 | $26,768 | |
| **19** | $217,710 | $7,010 | $3,505 | $30,273 | |
| **20** | $221,215 | **SCALE #2** → $400k | $0 | $30,273 | doubled again |
| **21** | $401,215* | $12,919 | $6,459 | $36,732 | |
| **22** | $407,674 | $13,127 | $6,564 | $43,296 | |
| **23** | $414,238 | $13,339 | $6,669 | $49,965 | |
| **24** | $420,907 | $13,553 | $6,777 | $56,742 | |

**Strategy B result at month 24:**
- Account size: **$427,684**
- Cumulative cash in your bank: **$56,742**
- Plus equity on account (your share, ~half of account value above $100k funded baseline): **~$163,842**
- Total realisable if you closed: **$56,742 cash + $163,842 equity share = ~$220,584**

---

## Month-by-month table — Strategy C (WITHDRAW ALL, no scaling)

Take every cent of profit as cash monthly at the 50 % firm split. Account stays flat at $100k forever. No scaling, no capital growth, just steady income.

Effective compound rate for account = 0 % (you withdraw everything, account doesn't grow)
Monthly profit = $100,000 × 3.22 % = **$3,220**
Your 50 % split = **$1,610/month**

| Month | Account | Cash this month | Cum cash |
|:---:|---:|---:|---:|
| 1-6 | $100k | $0 (challenge) | $0 |
| **7** | $100k | $1,610 | $1,610 |
| **8** | $100k | $1,610 | $3,220 |
| ... | ... | ... | ... |
| **24** | $100k | $1,610 | **$28,980** |

**Strategy C result at month 24:**
- Account size: $100k (unchanged)
- Cumulative cash: **$28,980**
- You're locked into ~$1,610/month forever. No upside.

---

## Side-by-side comparison at month 24

| Metric | A (reinvest) | B (50/50) | C (cash-only) |
|---|---:|---:|---:|
| Account size | **$1.65M** | $427k | $100k |
| Cash withdrawn (24 months) | $0 | $56,742 | $28,980 |
| Equity profit on account | $254,700 | ~$163,842 | $0 |
| **Total realisable** (cash + 50 % equity share) | **$127,350** | **$220,584** | **$28,980** |
| Monthly cash flow going forward | ~$25,760 (once you flip) | ~$6,800 | ~$1,610 |

**The winner depends on your goals:**
- **Need cash now** (rent, bills, family): Strategy B gives you steady income AND still scales
- **Want maximum wealth** at month 36: Strategy A — keep reinvesting, flip to cash at month 30+
- **Just want a supplemental salary**: Strategy C is safe and predictable but caps hard

---

## What if the live haircut is harsher?

Three sensitivity cases. Same starting conditions, different monthly rates.

### Pessimistic (40 % of backtest = 2.15 %/mo compound)
| Metric | Value |
|---|---|
| Time to pass challenge | ~5.5 months |
| Time for first scaling ($100k → $200k) | ~4.5 months from funded |
| Month 24 account (Strategy A) | ~$500k (2-3 scalings) |
| Month 24 cumulative cash (Strategy B) | ~$28,000 |

### Realistic (60 % = 3.22 %/mo — used in main tables above)
| Metric | Value |
|---|---|
| Time to pass challenge | 4 months |
| Time for first scaling | ~3 months from funded |
| Month 24 account (Strategy A) | $1.65M |
| Month 24 cumulative cash (Strategy B) | $56,742 |

### Optimistic (backtest holds at 100 % = 5.37 %/mo)
| Metric | Value |
|---|---|
| Time to pass challenge | 2.5 months |
| Time for first scaling | ~2 months from funded |
| Month 24 account (Strategy A) | **$3.2M+** (hit 5%ers cap, then stuck at cap) |
| Month 24 cumulative cash (Strategy B) | ~$120,000+ |

---

## Honest summary — likely earnings

| Scenario | Probability I'd assign | Cash at month 24 (Strategy B) |
|---|---:|---:|
| Hero case (backtest replicates) | **15 %** | ~$120k cash + $800k+ equity share |
| **Realistic case (60 % haircut)** | **50 %** | **~$57k cash + $164k equity share** |
| Dog case (40 % haircut) | **25 %** | ~$28k cash + $100k equity share |
| Challenge fail (bot regime-breaks) | **10 %** | $500 challenge fee lost, $0 earned |

**Expected value (probability-weighted):**
- Cash at 24 months: ~$52,000
- Equity share at 24 months: ~$210,000
- **Total EV: ~$262,000 realisable over 24 months** (Strategy B, 50/50 split)

That's ~$11k/month average over 24 months, front-loaded heavily (near zero months 1-6, escalating to $25k+/month by month 22-24).

---

## The #1 rule: treat the first 60 days of live as tuition

Nothing below matters until the LIVE bot has shown it can at least replicate the backtest at 40 %+ efficiency over 4-6 weeks. Before that, any number in this doc is vapourware.

**Success criteria for month 1-2 of paper-trading:**
- [ ] Bot fires ≥15 trades (to get statistical signal)
- [ ] Win rate ≥55 % (backtest was 65.4 %)
- [ ] Monthly PnL positive
- [ ] Max DD < 4 %
- [ ] Zero 5%ers rule violations

If those hit, **go live on Challenge Step 1 with real money**. If any fail, stop the bot, re-audit, don't burn a challenge fee on a broken system.

---

*Note on assumptions:* This model uses a geometric haircut (60 % of backtest rate) rather than a PnL haircut. That's conservative for two reasons:
1. Slippage and edge decay are likely to compound, not stack linearly
2. Win rate may degrade slightly under live conditions (backtest 65.4 % → live maybe 60 %)

The 60 % figure is a deliberately pessimistic single point. Real live performance is a distribution, not a number. Budget for worst case (Strategy C minimum), plan for realistic (Strategy B 50/50), dream about hero case.
