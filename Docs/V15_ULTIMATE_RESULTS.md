# SmartBB v15 — ULTIMATE PER-SYMBOL RESULTS

**Methodology**:
  * Grid: 4×4×5×3×4 = 960 configs per symbol
    (Z quantile × Hurst quantile × stop ATR × TP fraction × session filter)
  * 3-split walk-forward on non-overlapping OOS windows
  * Bootstrap 10,000 resamples per OOS split
  * Commission stress test at +$0.50/lot, +$1.00/lot, +$2.00/lot extra
  * Neighbour-smoothness: top-5 grid configs must also be profitable

**Runtime**: 160.4 minutes over 6 symbols

## TIER 1 (LIVE-READY)
Survives 3-split WF, bootstrap p05 > 0 on ≥2 splits, commission-stress +$1/lot still profitable, smoothness ≥3/5.

### US100 — TIER TIER1
Reason: 3-split median PF 10.75, net $2554, +$1/lot stress PF inf

**Best params:**  Z-quantile=0.97, Hurst-q=0.35, stop=0.5×ATR, TP=1.0×band, session=all

**3-split OOS performance:**

| Split | n | Net $ | PF | DD% | WR | Comm $ | Spread $ |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0 | 13 | $+2,554 | 7.28 | 0.23% | 84.6% | $0 | $1,624 |
| 1 | 4 | $+868 | 10.75 | 0.09% | 50.0% | $0 | $366 |
| 2 | 12 | $+5,861 | inf | 0.00% | 100.0% | $0 | $1,043 |

**Median:** net $+2,554, PF 10.75

**Bootstrap CIs (10k resamples on each split):**

| Split | Observed net | p05 net | p50 net | p95 net | p05 PF |
|---|---:|---:|---:|---:|---:|
| 0 | $+2,554 | $+1,175 | $+2,564 | $+3,906 | 2.46 |
| 1 | $+868 | $-94 | $+868 | $+1,912 | 0.00 |
| 2 | $+5,861 | $+4,646 | $+5,864 | $+7,040 | 10.00 |

### US500 — TIER TIER1
Reason: 3-split median PF 5.79, net $535, +$1/lot stress PF 2.20

**Best params:**  Z-quantile=0.98, Hurst-q=0.45, stop=0.5×ATR, TP=0.75×band, session=all

**3-split OOS performance:**

| Split | n | Net $ | PF | DD% | WR | Comm $ | Spread $ |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0 | 5 | $+26 | 1.27 | 0.10% | 40.0% | $0 | $300 |
| 1 | 6 | $+543 | 5.79 | 0.11% | 83.3% | $0 | $340 |
| 2 | 5 | $+535 | 6486.13 | 0.00% | 80.0% | $0 | $291 |

**Median:** net $+535, PF 5.79

**Bootstrap CIs (10k resamples on each split):**

| Split | Observed net | p05 net | p50 net | p95 net | p05 PF |
|---|---:|---:|---:|---:|---:|
| 0 | $+26 | $-154 | $+24 | $+261 | 0.00 |
| 1 | $+543 | $+73 | $+544 | $+929 | 1.32 |
| 2 | $+535 | $+51 | $+527 | $+1,408 | 10.00 |

### US30 — TIER TIER1
Reason: 3-split median PF 18.97, net $5561, +$1/lot stress PF 9.95

**Best params:**  Z-quantile=0.97, Hurst-q=0.45, stop=0.5×ATR, TP=0.75×band, session=all

**3-split OOS performance:**

| Split | n | Net $ | PF | DD% | WR | Comm $ | Spread $ |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0 | 17 | $+5,561 | 13.99 | 0.27% | 82.4% | $0 | $2,933 |
| 1 | 11 | $+3,288 | 22.09 | 0.16% | 90.9% | $0 | $1,751 |
| 2 | 18 | $+6,010 | 18.97 | 0.17% | 72.2% | $0 | $3,346 |

**Median:** net $+5,561, PF 18.97

**Bootstrap CIs (10k resamples on each split):**

| Split | Observed net | p05 net | p50 net | p95 net | p05 PF |
|---|---:|---:|---:|---:|---:|
| 0 | $+5,561 | $+3,669 | $+5,604 | $+7,304 | 5.59 |
| 1 | $+3,288 | $+2,104 | $+3,302 | $+4,369 | 6.20 |
| 2 | $+6,010 | $+3,025 | $+5,833 | $+9,623 | 6.84 |

### DE40 — TIER TIER1
Reason: 3-split median PF 3.02, net $3610, +$1/lot stress PF 2.03

**Best params:**  Z-quantile=0.97, Hurst-q=0.35, stop=0.5×ATR, TP=0.75×band, session=all

**3-split OOS performance:**

| Split | n | Net $ | PF | DD% | WR | Comm $ | Spread $ |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0 | 29 | $+3,610 | 6.14 | 0.22% | 69.0% | $0 | $2,958 |
| 1 | 28 | $+3,664 | 3.02 | 1.38% | 67.9% | $0 | $2,869 |
| 2 | 25 | $+2,620 | 2.61 | 0.63% | 60.0% | $0 | $2,622 |

**Median:** net $+3,610, PF 3.02

**Bootstrap CIs (10k resamples on each split):**

| Split | Observed net | p05 net | p50 net | p95 net | p05 PF |
|---|---:|---:|---:|---:|---:|
| 0 | $+3,610 | $+1,951 | $+3,592 | $+5,330 | 2.82 |
| 1 | $+3,664 | $+1,202 | $+3,674 | $+6,076 | 1.44 |
| 2 | $+2,620 | $+419 | $+2,623 | $+4,850 | 1.18 |

### XAUUSD — TIER TIER1
Reason: 3-split median PF 9.93, net $494, +$1/lot stress PF 2.79

**Best params:**  Z-quantile=0.99, Hurst-q=0.35, stop=0.5×ATR, TP=0.5×band, session=all

**3-split OOS performance:**

| Split | n | Net $ | PF | DD% | WR | Comm $ | Spread $ |
|---|---:|---:|---:|---:|---:|---:|---:|
| 0 | 6 | $+494 | 7.21 | 0.08% | 83.3% | $1,923 | $173 |
| 1 | 7 | $+641 | 11.49 | 0.05% | 71.4% | $2,422 | $187 |
| 2 | 5 | $+490 | 9.93 | 0.05% | 80.0% | $1,738 | $129 |

**Median:** net $+494, PF 9.93

**Bootstrap CIs (10k resamples on each split):**

| Split | Observed net | p05 net | p50 net | p95 net | p05 PF |
|---|---:|---:|---:|---:|---:|
| 0 | $+494 | $+98 | $+500 | $+863 | 1.59 |
| 1 | $+641 | $+203 | $+640 | $+1,081 | 2.56 |
| 2 | $+490 | $+100 | $+490 | $+879 | 1.70 |


## TIER 2 (WATCH — profitable but less robust)
Profitable on median OOS but fails at least one robustness gate. Paper-trade first or use ½ risk.

*none*

## REJECTED (no viable edge)

### USOIL — rejected
Reason: no trades in any OOS split


## COMMISSION VERIFICATION

Every symbol below shows the dollar commissions and spread costs charged on the winning config's best OOS split — proves the cost model is active.

| Symbol | Trades | Net $ | Comm $ | Spread $ | Total cost $ | Cost/trade |
|---|---:|---:|---:|---:|---:|---:|
| US100 | 12 | $+5,861 | $0 | $1,043 | $1,043 | $86.90 |
| US500 | 6 | $+543 | $0 | $340 | $340 | $56.67 |
| US30 | 18 | $+6,010 | $0 | $3,346 | $3,346 | $185.88 |
| DE40 | 28 | $+3,664 | $0 | $2,869 | $2,869 | $102.48 |
| USOIL | 1 | $+26 | $12 | $30 | $42 | $41.58 |
| XAUUSD | 7 | $+641 | $2,422 | $187 | $2,609 | $372.71 |

## COMMISSION-STRESS MATRIX
Verify every Tier 1/2 symbol's edge survives broker fee changes. Net P&L on best OOS split as extra $/lot rises:

| Symbol | +$0.00 | +$0.50 | +$1.00 | +$2.00 | Slope |
|---|---:|---:|---:|---:|:---:|
| US100 | $+5,861 | $+5,681 | $+5,501 | $+5,145 | ✅ robust |
| US500 | $+543 | $+393 | $+243 | $-57 | ⚠️ fragile |
| US30 | $+6,010 | $+5,621 | $+5,232 | $+4,456 | ✅ robust |
| DE40 | $+3,664 | $+3,005 | $+2,341 | $+1,001 | ✅ robust |
| XAUUSD | $+641 | $+485 | $+329 | $+17 | ✅ robust |
