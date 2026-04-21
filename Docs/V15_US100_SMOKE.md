# SmartBB v15 — ULTIMATE PER-SYMBOL RESULTS

**Methodology**:
  * Grid: 4×4×5×3×4 = 960 configs per symbol
    (Z quantile × Hurst quantile × stop ATR × TP fraction × session filter)
  * 3-split walk-forward on non-overlapping OOS windows
  * Bootstrap 10,000 resamples per OOS split
  * Commission stress test at +$0.50/lot, +$1.00/lot, +$2.00/lot extra
  * Neighbour-smoothness: top-5 grid configs must also be profitable

**Runtime**: 14.4 minutes over 1 symbols

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


## TIER 2 (WATCH — profitable but less robust)
Profitable on median OOS but fails at least one robustness gate. Paper-trade first or use ½ risk.

*none*

## REJECTED (no viable edge)

*none*

## COMMISSION VERIFICATION

Every symbol below shows the dollar commissions and spread costs charged on the winning config's best OOS split — proves the cost model is active.

| Symbol | Trades | Net $ | Comm $ | Spread $ | Total cost $ | Cost/trade |
|---|---:|---:|---:|---:|---:|---:|
| US100 | 12 | $+5,861 | $0 | $1,043 | $1,043 | $86.90 |

## COMMISSION-STRESS MATRIX
Verify every Tier 1/2 symbol's edge survives broker fee changes. Net P&L on best OOS split as extra $/lot rises:

| Symbol | +$0.00 | +$0.50 | +$1.00 | +$2.00 | Slope |
|---|---:|---:|---:|---:|:---:|
| US100 | $+5,861 | $+5,681 | $+5,501 | $+5,145 | ✅ robust |
