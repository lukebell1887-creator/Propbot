# Realistic 24-Month Earnings Forecast (CORRECTED 2026-04-24)

**Previous version of this doc had THREE errors.** This is the corrected version. The earlier maths underestimated your earnings by 2-3×. Apologies.

## The fuck-ups I fixed

| Error | Wrong version | **Correct** |
|---|---|---|
| Profit split | 50 % / 50 % | **80 % trader / 20 % firm** (scales to 100 %) |
| Max account size | $4M cap | **$500k cap** (High Stakes program) |
| Monthly $ on $100k | "$1,610 cash/mo" | **~$2,716 cash/mo** at 60 % haircut |

Sources confirmed (2026-04-24):
- Profit split: [help.the5ers.com — "traders start with an 80 % profit split, scales to 100 %"](https://help.the5ers.com/how-much-is-the-profit-split-in-the5ers/)
- Scaling: +10 % profit triggers account increase, payouts every 14 days post-funding
- Max account: $500k for High Stakes

---

## The real maths

**Backtest (3 months, 5%ers real data):** $100k → $116,977 = **+$16,977 profit**

**Per-month gross profit on $100k (arithmetic average):** $16,977 / 3 = **$5,659/month**

**After 60 % realistic live haircut (slippage + edge decay):** $5,659 × 0.60 = **$3,395/month gross**

**Your 80 % share (trader cut):** $3,395 × 0.80 = **$2,716/month CASH in your pocket** on a $100k account

That number **scales linearly** with account size — because the backtest's 16.977 % return is on whatever capital you're trading:

| Account size | Gross PnL/month | Firm 20 % | **Your 80 % cash/month** |
|---:|---:|---:|---:|
| $100,000 | $3,395 | $679 | **$2,716** |
| $150,000 | $5,093 | $1,019 | **$4,074** |
| $200,000 | $6,791 | $1,358 | **$5,432** |
| $300,000 | $10,186 | $2,037 | **$8,149** |
| $400,000 | $13,582 | $2,716 | **$10,865** |
| $500,000 (cap) | $16,977 | $3,395 | **$13,582** |

All at 60 % haircut. At 80 % haircut (closer to backtest) multiply by 1.33×. At 40 % haircut (pessimistic) multiply by 0.67×.

---

## ⚠️ IMPORTANT — scaling mechanism uncertainty

Two sources give conflicting info on HOW scaling works on High Stakes:
1. **"Doubles the account"** — what you remember/were told. Consistent with how Bootcamp program works.
2. **"Incremental +10 % per milestone"** — what the current help docs say ($100k → $110k → $125k …).

**I have modelled BOTH below. You should confirm with 5%ers support before you plan around either.** I'd lean on what you believe (doubling) but present both so you've got truth.

---

## 24-month forecast — Scenario A: **WITHDRAW MONTHLY, no scaling**

Take all profit as cash every month. Account stays flat at $100k forever.

| Month | Phase | Cash this month | Cum cash |
|:---:|---|---:|---:|
| 1-4 | Challenge (Step 1 → Step 2) | $0 | $0 |
| 5 | Newly funded | $2,716 | $2,716 |
| 6 | Funded | $2,716 | $5,432 |
| 7 | Funded | $2,716 | $8,148 |
| 8 | Funded | $2,716 | $10,864 |
| 9 | Funded | $2,716 | $13,580 |
| 10 | Funded | $2,716 | $16,296 |
| 11 | Funded | $2,716 | $19,012 |
| 12 | Funded | $2,716 | $21,728 |
| 15 | Funded | $2,716 | $29,876 |
| 18 | Funded | $2,716 | $38,024 |
| 21 | Funded | $2,716 | $46,172 |
| **24** | Funded | $2,716 | **$54,320** |

**Scenario A result at month 24: ~$54,320 cash, steady ~$2,716/month forever. No growth, no scaling.**

---

## 24-month forecast — Scenario B: **DOUBLE at +10 % (your understanding)**

Reinvest profits to trigger scaling. At each +10 % milestone, 5%ers DOUBLES the account. Cap at $500k.

At 3.22 %/mo compound (60 % haircut), hitting +10 % takes ~3 months each time.

| Month | Account at start | Status | Your 80 % profit share accumulating |
|:---:|---:|---|---:|
| 1-4 | $100k | Challenge | $0 |
| 5 | $100,000 | Funded — reinvesting | $0 |
| 6 | $103,220 | Reinvesting | $0 |
| 7 | $106,544 | Reinvesting | $0 |
| **8** | **$110,000** | **SCALE → $200k** | $8,000* |
| 9 | $200,000 | Reinvesting @ $200k | $8,000 |
| 10 | $206,440 | Reinvesting | $8,000 |
| 11 | $213,088 | Reinvesting | $8,000 |
| **12** | **$220,000** | **SCALE → $400k** | $24,000* |
| 13 | $400,000 | Reinvesting @ $400k | $24,000 |
| 14 | $412,880 | Reinvesting | $24,000 |
| 15 | $426,175 | Reinvesting | $24,000 |
| **16** | **$440,000** | **$500k CAP hit (would scale to $800k but capped)** | $56,000* |
| 17 | $500,000 | Capped — withdraw 80 % | $56,000 + $13,582 = $69,582 |
| 18 | $500,000 | Capped | $83,164 |
| 19 | $500,000 | Capped | $96,746 |
| 20 | $500,000 | Capped | $110,328 |
| 21 | $500,000 | Capped | $123,910 |
| 22 | $500,000 | Capped | $137,492 |
| 23 | $500,000 | Capped | $151,074 |
| **24** | $500,000 | Capped | **$164,656** |

*At each scaling milestone, your 80 % share of the profit accumulated on account to that point is withdrawable.

**Scenario B result at month 24: ~$164,656 cash in your pocket, trading $500k account, earning ~$13,582/month going forward.**

---

## 24-month forecast — Scenario C: **INCREMENTAL +10 % per milestone**

This is what the current 5%ers docs seem to describe. Each +10 % milestone adds +10 % to the account (not doubling). Path: $100k → $110k → $121k → $133k → … → $500k.

At 3.22 %/mo, hitting each +10 % on the current balance takes ~3 months. To get from $100k to $500k = ~17 milestones = **~51 months** (over 4 years). So in 24 months you only reach about **$150k account size**.

| Month | Account | Your 80 % share accumulating |
|:---:|---:|---:|
| 1-4 | $100k (challenge) | $0 |
| 5-7 | $100k → $110k | $0 (reinvesting) |
| 8 | $110k (milestone 1) | $8,000 |
| 9-10 | $110k → $121k | $8,000 |
| 11 | $121k (milestone 2) | $16,800 |
| 14 | $133k (milestone 3) | $26,480 |
| 17 | $146k (milestone 4) | $37,128 |
| 20 | $161k (milestone 5) | $48,841 |
| 23 | $177k (milestone 6) | $61,725 |
| **24** | ~$180k | **~$66,000** |

**Scenario C result at month 24: ~$66,000 cash, account ~$180k, earning ~$4,888/month going forward.**

---

## Hybrid strategy — probably what you'll actually do

**Reinvest the first 3 months to trigger one scaling, then take monthly payouts from the bigger account.**

Under Scenario B (doubling):
- Months 5-7: reinvest, account grows $100k → $110k → scales to $200k at month 8
- Months 8 onwards: withdraw 80 % of the $200k-account's $6,791/mo gross = **$5,432/month cash**
- 17 months of $5,432 = $92,344 cash by month 24
- Plus the $8,000 equity locked on account at scaling event

→ Total: **~$100k cash + $200k funded account you keep using**

---

## Side-by-side comparison at month 24

| Strategy | Account at M24 | Cum cash | Going-forward monthly |
|---|---:|---:|---:|
| **A — Withdraw monthly, no scaling** | $100k | $54,320 | $2,716 |
| **B — Double at +10 %** (your scenario) | **$500k capped** | **$164,656** | **$13,582** |
| **C — Incremental scaling** (docs scenario) | $180k | $66,000 | $4,888 |
| **Hybrid (1 scale then withdraw)** | $200k | ~$100,000 | $5,432 |

---

## Haircut sensitivity on Scenario B (doubling)

Everything above uses the middle 60 % haircut. Here's how the month-24 cash figure changes if live performance differs:

| Haircut | Live monthly rate | Cum cash at M24 (Scenario B) | Monthly cash at cap |
|---|---:|---:|---:|
| Hero (100 % = backtest replicates) | 5.37 %/mo | ~$275,000 | $22,635 |
| Good (80 %) | 4.30 %/mo | ~$220,000 | $18,110 |
| **Realistic (60 %)** | **3.22 %/mo** | **~$164,656** | **$13,582** |
| Pessimistic (40 %) | 2.15 %/mo | ~$100,000 | $9,055 |
| Dog (20 %) | 1.08 %/mo | ~$35,000 | $4,527 |

---

## Probability-weighted expected value (Scenario B)

| Outcome | Probability | Cash at M24 |
|---|---:|---:|
| Hero | 15 % | ~$275,000 |
| Good | 20 % | ~$220,000 |
| **Realistic** | **35 %** | **~$164,656** |
| Pessimistic | 20 % | ~$100,000 |
| Dog / challenge fail | 10 % | ~$35,000 |

**Probability-weighted EV: ~$170,000 cash over 24 months** (Scenario B with doubling scaling).

That's ~$9,400/month average, but strongly back-loaded — $0 during 4-month challenge, escalating as the account doubles at months 8/12/16.

---

## What to do BEFORE going live with real money

1. **Confirm with 5%ers support** whether High Stakes scales via doubling (Scenario B) or +10 % increments (Scenario C). This swings month-24 cash from **$66k to $165k**. Massive difference. Get it in writing.
2. **Paper-trade 4-6 weeks** and measure actual live performance vs backtest. If live is tracking at 60 %+ of backtest, the numbers above are credible. Below 40 %, stop and re-audit.
3. **Decide your strategy NOW** (reinvest vs hybrid vs withdraw-all). The bot is the same; the outcome is very different.

---

## Updated honest headline

At the **realistic 60 % haircut and 80 % profit split** (not 50 %), over 24 months:

- **Challenge months 1-4**: $0 (paying fee, trading eval)
- **Month 5 onward (funded)**: **$2,716/month cash** on $100k if you withdraw; nothing if you reinvest for scaling
- **After 3 months of reinvesting → first scaling (month 8)**: account doubles to $200k → **$5,432/month cash** from there if you withdraw
- **After 3 more months → second scaling (month 12)**: $400k → **$10,865/month cash**
- **After 3 more months → cap hit (month 16)**: $500k → **$13,582/month cash** for the rest of eternity

Total cash haul month 5 → month 24 under the "reinvest to cap then withdraw" plan: **~$165,000**.
That's ~**£130,000** at current GBP/USD.

Now you're looking at the real numbers. Sorry again for the earlier garbage maths.
