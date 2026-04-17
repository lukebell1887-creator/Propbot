# MARKET DNA v1 — Evidence-First Characterization Report

**Window:** 2025-11-15 21:22:00 → 2026-02-13 21:22:00
**Train slice:** 2025-11-15 21:22:00 → 2026-01-14 21:22:00
**Holdout slice:** 2026-01-14 21:22:00 → 2026-02-13 21:22:00

## Executive summary

- Total candidate edges tested: **124**
- Edges surviving holdout validation: **18**
- Survival rule: train p<0.05, holdout same-sign, holdout |effect| ≥ 50% of train |effect|, holdout n ≥ 10.

## Surviving edges (ranked by survival score)

### 1. US100  —  or_predicts_post_range
- **Description**: OR range vs next-55-min range correlation: r=+0.555 on 40 days — wide OR = wide day
- Train effect: **+0.5552** (p=0.0001, n=40)
- Holdout effect: **+0.4443** (p=0.0374, n=22)
- Holdout / train magnitude ratio: 0.80
- Survival score: 3.04
- **SURVIVED HOLDOUT**

### 2. XAUUSD  —  or_predicts_post_range
- **Description**: OR range vs next-55-min range correlation: r=+0.467 on 41 days — wide OR = wide day
- Train effect: **+0.4674** (p=0.0018, n=41)
- Holdout effect: **+0.3892** (p=0.0733, n=22)
- Holdout / train magnitude ratio: 0.83
- Survival score: 1.91
- **SURVIVED HOLDOUT**

### 3. US100  —  autocorr_h23_lag1
- **Description**: Lag-1 M1 autocorrelation at hour 23:00 UTC = +0.0922 (momentum)
- Train effect: **+0.0922** (p=0.0001, n=1800)
- Holdout effect: **+0.0879** (p=0.0083, n=900)
- Holdout / train magnitude ratio: 0.95
- Survival score: 0.63
- **SURVIVED HOLDOUT**

### 4. DE40  —  autocorr_h06_lag3
- **Description**: Lag-3 M1 autocorrelation at hour 06:00 UTC = +0.0830 (momentum)
- Train effect: **+0.0830** (p=0.0004, n=1800)
- Holdout effect: **+0.0860** (p=0.0099, n=900)
- Holdout / train magnitude ratio: 1.04
- Survival score: 0.52
- **SURVIVED HOLDOUT**

### 5. DE40  —  autocorr_h20_lag3
- **Description**: Lag-3 M1 autocorrelation at hour 20:00 UTC = +0.0978 (momentum)
- Train effect: **+0.0978** (p=0.0000, n=1800)
- Holdout effect: **+0.0593** (p=0.0755, n=900)
- Holdout / train magnitude ratio: 0.61
- Survival score: 0.51
- **SURVIVED HOLDOUT**

### 6. XAUUSD  —  autocorr_h08_lag5
- **Description**: Lag-5 M1 autocorrelation at hour 08:00 UTC = -0.0610 (reversal)
- Train effect: **-0.0610** (p=0.0097, n=1800)
- Holdout effect: **-0.0922** (p=0.0056, n=900)
- Holdout / train magnitude ratio: 1.51
- Survival score: 0.38
- **SURVIVED HOLDOUT**

### 7. XAUUSD  —  autocorr_h05_lag3
- **Description**: Lag-3 M1 autocorrelation at hour 05:00 UTC = +0.0470 (momentum)
- Train effect: **+0.0470** (p=0.0460, n=1800)
- Holdout effect: **+0.0856** (p=0.0102, n=900)
- Holdout / train magnitude ratio: 1.82
- Survival score: 0.25
- **SURVIVED HOLDOUT**

### 8. XAUUSD  —  autocorr_h14_lag5
- **Description**: Lag-5 M1 autocorrelation at hour 14:00 UTC = -0.0576 (reversal)
- Train effect: **-0.0576** (p=0.0146, n=1800)
- Holdout effect: **-0.0579** (p=0.0823, n=900)
- Holdout / train magnitude ratio: 1.01
- Survival score: 0.19
- **SURVIVED HOLDOUT**

### 9. US100  —  autocorr_h14_lag1
- **Description**: Lag-1 M1 autocorrelation at hour 14:00 UTC = +0.0655 (momentum)
- Train effect: **+0.0655** (p=0.0055, n=1800)
- Holdout effect: **+0.0333** (p=0.3174, n=900)
- Holdout / train magnitude ratio: 0.51
- Survival score: 0.16
- **SURVIVED HOLDOUT**

### 10. XAUUSD  —  autocorr_h07_lag20
- **Description**: Lag-20 M1 autocorrelation at hour 07:00 UTC = +0.0605 (momentum)
- Train effect: **+0.0605** (p=0.0103, n=1800)
- Holdout effect: **+0.0417** (p=0.2108, n=900)
- Holdout / train magnitude ratio: 0.69
- Survival score: 0.16
- **SURVIVED HOLDOUT**

### 11. US100  —  autocorr_h21_lag1
- **Description**: Lag-1 M1 autocorrelation at hour 21:00 UTC = -0.0466 (reversal)
- Train effect: **-0.0466** (p=0.0481, n=1800)
- Holdout effect: **-0.0616** (p=0.0646, n=900)
- Holdout / train magnitude ratio: 1.32
- Survival score: 0.16
- **SURVIVED HOLDOUT**

### 12. US100  —  autocorr_h07_lag5
- **Description**: Lag-5 M1 autocorrelation at hour 07:00 UTC = -0.0535 (reversal)
- Train effect: **-0.0535** (p=0.0233, n=1800)
- Holdout effect: **-0.0446** (p=0.1813, n=900)
- Holdout / train magnitude ratio: 0.83
- Survival score: 0.13
- **SURVIVED HOLDOUT**

### 13. XAUUSD  —  autocorr_h11_lag1
- **Description**: Lag-1 M1 autocorrelation at hour 11:00 UTC = +0.0481 (momentum)
- Train effect: **+0.0481** (p=0.0412, n=1800)
- Holdout effect: **+0.0467** (p=0.1615, n=900)
- Holdout / train magnitude ratio: 0.97
- Survival score: 0.12
- **SURVIVED HOLDOUT**

### 14. US100  —  autocorr_h06_lag5
- **Description**: Lag-5 M1 autocorrelation at hour 06:00 UTC = -0.0494 (reversal)
- Train effect: **-0.0494** (p=0.0362, n=1800)
- Holdout effect: **-0.0388** (p=0.2446, n=900)
- Holdout / train magnitude ratio: 0.79
- Survival score: 0.10
- **SURVIVED HOLDOUT**

### 15. XAUUSD  —  autocorr_h11_lag3
- **Description**: Lag-3 M1 autocorrelation at hour 11:00 UTC = -0.0493 (reversal)
- Train effect: **-0.0493** (p=0.0363, n=1800)
- Holdout effect: **-0.0369** (p=0.2689, n=900)
- Holdout / train magnitude ratio: 0.75
- Survival score: 0.10
- **SURVIVED HOLDOUT**

### 16. US100  —  autocorr_h21_lag10
- **Description**: Lag-10 M1 autocorrelation at hour 21:00 UTC = +0.0509 (momentum)
- Train effect: **+0.0509** (p=0.0307, n=1800)
- Holdout effect: **+0.0302** (p=0.3647, n=900)
- Holdout / train magnitude ratio: 0.59
- Survival score: 0.09
- **SURVIVED HOLDOUT**

### 17. XAUUSD  —  followthrough_h21
- **Description**: After a 1σ 5-min move at hour 21:00 UTC, next-15-min same-direction return = -6.8 bps over 35 samples
- Train effect: **-0.0007** (p=0.0009, n=35)
- Holdout effect: **-0.0014** (p=0.2087, n=39)
- Holdout / train magnitude ratio: 2.00
- Survival score: 0.00
- **SURVIVED HOLDOUT**

### 18. XAUUSD  —  followthrough_h03
- **Description**: After a 1σ 5-min move at hour 03:00 UTC, next-15-min same-direction return = -2.7 bps over 77 samples
- Train effect: **-0.0003** (p=0.0038, n=77)
- Holdout effect: **-0.0004** (p=0.6120, n=39)
- Holdout / train magnitude ratio: 1.59
- Survival score: 0.00
- **SURVIVED HOLDOUT**

## Failed candidates (rejected — audit trail)

| Symbol | Edge | Train effect | Train p | Holdout effect | Verdict |
|---|---|---:|---:|---:|---|
| US100 | autocorr_h23_lag10 | +0.1086 | 0.0000 | -0.0217 | sign flipped on holdout — fake edge |
| US100 | autocorr_h23_lag3 | -0.1059 | 0.0000 | +0.0211 | sign flipped on holdout — fake edge |
| US100 | autocorr_h04_lag20 | +0.1025 | 0.0000 | -0.0385 | sign flipped on holdout — fake edge |
| XAUUSD | autocorr_h23_lag60 | +0.1000 | 0.0000 | -0.0254 | sign flipped on holdout — fake edge |
| DE40 | autocorr_h23_lag3 | -0.0915 | 0.0001 | +0.0783 | sign flipped on holdout — fake edge |
| DE40 | autocorr_h23_lag10 | +0.0859 | 0.0003 | +0.0031 | magnitude collapsed on holdout — weak edge |
| XAUUSD | autocorr_h20_lag1 | -0.0847 | 0.0003 | -0.0222 | magnitude collapsed on holdout — weak edge |
| XAUUSD | autocorr_h07_lag10 | -0.0835 | 0.0004 | +0.0567 | sign flipped on holdout — fake edge |
| XAUUSD | autocorr_h08_lag10 | +0.0831 | 0.0004 | +0.0201 | magnitude collapsed on holdout — weak edge |
| US100 | autocorr_h05_lag5 | +0.0823 | 0.0005 | -0.0077 | sign flipped on holdout — fake edge |
| US100 | autocorr_h07_lag10 | -0.0801 | 0.0007 | -0.0032 | magnitude collapsed on holdout — weak edge |
| XAUUSD | autocorr_h18_lag1 | -0.0781 | 0.0009 | +0.0286 | sign flipped on holdout — fake edge |
| XAUUSD | autocorr_h12_lag3 | -0.0746 | 0.0015 | -0.0176 | magnitude collapsed on holdout — weak edge |
| US100 | autocorr_h18_lag20 | +0.0744 | 0.0016 | +0.0252 | magnitude collapsed on holdout — weak edge |
| DE40 | autocorr_h16_lag10 | -0.0715 | 0.0024 | -0.0161 | magnitude collapsed on holdout — weak edge |
| XAUUSD | autocorr_h23_lag1 | -0.0712 | 0.0025 | -0.0068 | magnitude collapsed on holdout — weak edge |
| DE40 | autocorr_h01_lag3 | -0.0708 | 0.0027 | +0.0278 | sign flipped on holdout — fake edge |
| DE40 | followthrough_h10 | -0.0003 | 0.0028 | +0.0001 | sign flipped on holdout — fake edge |
| US100 | autocorr_h21_lag5 | -0.0702 | 0.0029 | +0.0596 | sign flipped on holdout — fake edge |
| XAUUSD | autocorr_h07_lag1 | -0.0690 | 0.0034 | +0.0403 | sign flipped on holdout — fake edge |
| XAUUSD | autocorr_h01_lag1 | -0.0677 | 0.0041 | -0.0300 | magnitude collapsed on holdout — weak edge |
| DE40 | autocorr_h05_lag5 | +0.0676 | 0.0041 | +0.0016 | magnitude collapsed on holdout — weak edge |
| US100 | autocorr_h23_lag5 | -0.0670 | 0.0045 | +0.0310 | sign flipped on holdout — fake edge |
| DE40 | autocorr_h04_lag3 | -0.0658 | 0.0052 | +0.0239 | sign flipped on holdout — fake edge |
| US100 | autocorr_h16_lag10 | -0.0648 | 0.0060 | +0.0388 | sign flipped on holdout — fake edge |
| XAUUSD | autocorr_h02_lag5 | +0.0645 | 0.0062 | -0.0373 | sign flipped on holdout — fake edge |
| US100 | autocorr_h20_lag3 | +0.0635 | 0.0071 | +0.0252 | magnitude collapsed on holdout — weak edge |
| DE40 | autocorr_h23_lag5 | -0.0608 | 0.0099 | -0.0036 | magnitude collapsed on holdout — weak edge |
| XAUUSD | autocorr_h03_lag1 | -0.0603 | 0.0105 | +0.0017 | sign flipped on holdout — fake edge |
| DE40 | followthrough_h18 | +0.0002 | 0.0106 | -0.0001 | sign flipped on holdout — fake edge |
| US100 | autocorr_h17_lag1 | +0.0595 | 0.0115 | -0.1051 | sign flipped on holdout — fake edge |
| DE40 | followthrough_h17 | +0.0002 | 0.0118 | -0.0000 | sign flipped on holdout — fake edge |
| XAUUSD | autocorr_h04_lag60 | +0.0579 | 0.0141 | -0.0301 | sign flipped on holdout — fake edge |
| US100 | autocorr_h04_lag3 | -0.0578 | 0.0143 | +0.0073 | sign flipped on holdout — fake edge |
| US100 | autocorr_h15_lag1 | +0.0574 | 0.0149 | -0.0537 | sign flipped on holdout — fake edge |
| US100 | autocorr_h13_lag10 | -0.0572 | 0.0153 | -0.0109 | magnitude collapsed on holdout — weak edge |
| XAUUSD | autocorr_h21_lag10 | +0.0571 | 0.0154 | -0.0092 | sign flipped on holdout — fake edge |
| XAUUSD | autocorr_h11_lag10 | -0.0568 | 0.0159 | -0.0039 | magnitude collapsed on holdout — weak edge |
| US100 | autocorr_h06_lag60 | -0.0559 | 0.0178 | +0.0333 | sign flipped on holdout — fake edge |
| US100 | followthrough_h18 | +0.0004 | 0.0207 | -0.0000 | sign flipped on holdout — fake edge |
| DE40 | autocorr_h06_lag60 | -0.0531 | 0.0244 | +0.0022 | sign flipped on holdout — fake edge |
| US100 | autocorr_h23_lag60 | +0.0520 | 0.0273 | +0.0052 | magnitude collapsed on holdout — weak edge |
| US100 | autocorr_h01_lag3 | -0.0504 | 0.0325 | +0.0206 | sign flipped on holdout — fake edge |
| XAUUSD | autocorr_h17_lag5 | -0.0501 | 0.0335 | +0.0018 | sign flipped on holdout — fake edge |
| DE40 | autocorr_h18_lag3 | +0.0501 | 0.0337 | +0.0249 | magnitude collapsed on holdout — weak edge |
| DE40 | autocorr_h03_lag60 | -0.0488 | 0.0383 | +0.0051 | sign flipped on holdout — fake edge |
| DE40 | autocorr_h08_lag5 | -0.0486 | 0.0390 | +0.0039 | sign flipped on holdout — fake edge |
| XAUUSD | followthrough_h10 | -0.0003 | 0.0401 | -0.0000 | magnitude collapsed on holdout — weak edge |
| XAUUSD | autocorr_h14_lag1 | -0.0483 | 0.0404 | +0.0398 | sign flipped on holdout — fake edge |
| DE40 | autocorr_h02_lag5 | +0.0483 | 0.0406 | +0.0162 | magnitude collapsed on holdout — weak edge |
| XAUUSD | autocorr_h08_lag1 | -0.0482 | 0.0408 | +0.0051 | sign flipped on holdout — fake edge |
| XAUUSD | autocorr_h13_lag20 | +0.0476 | 0.0435 | +0.0046 | magnitude collapsed on holdout — weak edge |
| DE40 | autocorr_h02_lag10 | -0.0472 | 0.0451 | +0.0014 | sign flipped on holdout — fake edge |
| DE40 | autocorr_h04_lag5 | -0.0469 | 0.0467 | +0.0254 | sign flipped on holdout — fake edge |
| XAUUSD | autocorr_h18_lag60 | -0.0466 | 0.0480 | -0.0009 | magnitude collapsed on holdout — weak edge |
| DE40 | autocorr_h22_lag10 | +0.0462 | 0.0498 | -0.0551 | sign flipped on holdout — fake edge |
| XAUUSD | dow_Sun | +0.0014 | 0.0541 | — | failed train alpha |
| DE40 | autocorr_h09_lag20 | -0.0453 | 0.0545 | +0.0196 | failed train alpha |
| DE40 | autocorr_h13_lag10 | -0.0451 | 0.0557 | -0.0439 | failed train alpha |
| DE40 | dow_Fri | +0.0038 | 0.0562 | +0.0048 | failed train alpha |
| US100 | followthrough_h13 | -0.0003 | 0.0562 | -0.0003 | failed train alpha |
| DE40 | autocorr_h05_lag1 | +0.0450 | 0.0565 | -0.0328 | failed train alpha |
| US100 | autocorr_h15_lag5 | -0.0449 | 0.0567 | -0.0656 | failed train alpha |
| XAUUSD | autocorr_h01_lag10 | -0.0446 | 0.0585 | -0.0831 | failed train alpha |
| DE40 | autocorr_h05_lag3 | -0.0446 | 0.0586 | +0.0401 | failed train alpha |
| XAUUSD | autocorr_h01_lag5 | +0.0442 | 0.0610 | -0.0655 | failed train alpha |
| US100 | autocorr_h03_lag60 | -0.0441 | 0.0612 | +0.0102 | failed train alpha |
| XAUUSD | autocorr_h21_lag3 | -0.0436 | 0.0642 | -0.0099 | failed train alpha |
| XAUUSD | autocorr_h12_lag60 | -0.0434 | 0.0654 | -0.0073 | failed train alpha |
| DE40 | autocorr_h19_lag10 | -0.0433 | 0.0660 | +0.1132 | failed train alpha |
| DE40 | autocorr_h08_lag3 | +0.0431 | 0.0671 | +0.0239 | failed train alpha |
| DE40 | or_predicts_post_range | +0.2986 | 0.0684 | +0.2775 | failed train alpha |
| DE40 | autocorr_h09_lag5 | +0.0425 | 0.0712 | -0.0428 | failed train alpha |
| US100 | autocorr_h07_lag20 | -0.0422 | 0.0734 | +0.0214 | failed train alpha |
| DE40 | autocorr_h14_lag5 | -0.0419 | 0.0755 | -0.0113 | failed train alpha |
| US100 | autocorr_h16_lag3 | +0.0419 | 0.0757 | -0.0306 | failed train alpha |
| XAUUSD | autocorr_h15_lag60 | +0.0416 | 0.0775 | +0.0245 | failed train alpha |
| US100 | autocorr_h20_lag20 | +0.0415 | 0.0784 | +0.0381 | failed train alpha |
| DE40 | followthrough_h12 | +0.0002 | 0.0793 | -0.0000 | failed train alpha |
| US100 | autocorr_h04_lag5 | +0.0413 | 0.0795 | -0.0092 | failed train alpha |
| US100 | autocorr_h12_lag1 | +0.0412 | 0.0801 | +0.0278 | failed train alpha |
| DE40 | autocorr_h06_lag5 | -0.0409 | 0.0828 | -0.0003 | failed train alpha |
| DE40 | autocorr_h18_lag1 | -0.0406 | 0.0848 | +0.0009 | failed train alpha |
| DE40 | autocorr_h01_lag10 | +0.0403 | 0.0875 | -0.0088 | failed train alpha |
| DE40 | autocorr_h17_lag10 | -0.0402 | 0.0885 | -0.0139 | failed train alpha |
| DE40 | autocorr_h01_lag1 | -0.0400 | 0.0900 | +0.0345 | failed train alpha |
| US100 | autocorr_h13_lag20 | -0.0397 | 0.0918 | +0.0460 | failed train alpha |
| DE40 | autocorr_h10_lag60 | +0.0396 | 0.0931 | -0.0550 | failed train alpha |
| US100 | autocorr_h08_lag10 | -0.0394 | 0.0948 | -0.0213 | failed train alpha |
| XAUUSD | orb_raw_wr | -0.1389 | 0.0956 | +0.0294 | failed train alpha |
| US100 | autocorr_h14_lag3 | -0.0390 | 0.0976 | -0.0106 | failed train alpha |
| US100 | autocorr_h19_lag3 | -0.0390 | 0.0979 | -0.0241 | failed train alpha |
| XAUUSD | autocorr_h10_lag20 | -0.0389 | 0.0990 | -0.0090 | failed train alpha |
| DE40 | followthrough_h03 | -0.0001 | 0.1067 | -0.0000 | failed train alpha |
| DE40 | followthrough_h05 | -0.0002 | 0.1104 | -0.0000 | failed train alpha |
| US100 | followthrough_h03 | -0.0003 | 0.1113 | +0.0001 | failed train alpha |
| US100 | followthrough_h15 | -0.0002 | 0.1122 | -0.0002 | failed train alpha |
| US100 | followthrough_h12 | -0.0004 | 0.1134 | -0.0000 | failed train alpha |
| XAUUSD | dow_Tue | +0.0031 | 0.1134 | — | failed train alpha |
| DE40 | followthrough_h08 | -0.0002 | 0.1188 | -0.0000 | failed train alpha |
| XAUUSD | dow_Fri | +0.0033 | 0.1445 | -0.0054 | failed train alpha |
| DE40 | followthrough_h23 | -0.0002 | 0.1515 | +0.0001 | failed train alpha |
| XAUUSD | followthrough_h11 | -0.0002 | 0.1591 | -0.0005 | failed train alpha |
| DE40 | followthrough_h15 | -0.0001 | 0.1960 | -0.0001 | failed train alpha |
| DE40 | orb_raw_wr | -0.0946 | 0.2498 | +0.1364 | failed train alpha |
| US100 | orb_raw_wr | -0.0897 | 0.2623 | -0.0455 | failed train alpha |

## Raw phase measurements

### US100

- Hurst exponent (train): **0.527** (trending)
- Overall autocorr lag-1: +0.0077
- Overall autocorr lag-5: -0.0048
- Overall 1σ follow-through: +0.30 bps
- ORB raw WR@1R: 41.0% (n=39)
- OR→post-range correlation: +0.555
- Mean overnight gap: +4.97 bps (fill rate 85.0%)
- 30-min horizon: MAE q25 -12.2 bps, MFE q60/q75 +5.8/+11.5 — target R:R≈0.62
- 60-min horizon: MAE q25 -17.8 bps, MFE q60/q75 +8.6/+16.6 — target R:R≈0.62
- 180-min horizon: MAE q25 -33.0 bps, MFE q60/q75 +15.8/+31.9 — target R:R≈0.62

### DE40

- Hurst exponent (train): **0.512** (random)
- Overall autocorr lag-1: -0.0137
- Overall autocorr lag-5: +0.0009
- Overall 1σ follow-through: -0.08 bps
- ORB raw WR@1R: 40.5% (n=37)
- OR→post-range correlation: +0.299
- Mean overnight gap: +2.85 bps (fill rate 100.0%)
- 30-min horizon: MAE q25 -10.5 bps, MFE q60/q75 +5.6/+10.9 — target R:R≈0.69
- 60-min horizon: MAE q25 -14.5 bps, MFE q60/q75 +8.3/+15.8 — target R:R≈0.74
- 180-min horizon: MAE q25 -25.5 bps, MFE q60/q75 +15.4/+29.1 — target R:R≈0.78

### XAUUSD

- Hurst exponent (train): **0.516** (random)
- Overall autocorr lag-1: -0.0296
- Overall autocorr lag-5: -0.0059
- Overall 1σ follow-through: -0.15 bps
- ORB raw WR@1R: 36.1% (n=36)
- OR→post-range correlation: +0.467
- Mean overnight gap: +3.37 bps (fill rate 90.2%)
- 30-min horizon: MAE q25 -17.4 bps, MFE q60/q75 +9.7/+17.6 — target R:R≈0.70
- 60-min horizon: MAE q25 -24.7 bps, MFE q60/q75 +13.9/+25.0 — target R:R≈0.71
- 180-min horizon: MAE q25 -43.1 bps, MFE q60/q75 +25.1/+46.7 — target R:R≈0.75
