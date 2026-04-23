# V24b — CAP SWEEP: The Pareto Frontier

Generated: 2026-04-23T11:22:02

**Sizer family:** MertonGZ γ=3.0  (base_f=0.110 %)  |  **Trades:** 283  |  **Bootstrap paths:** 5,000  |  **Start equity:** $100,000

## 5ers rules (hard lines)

| Rule | Hard line | My safety target (60 % margin) |
|---|---|---|
| Max DD (static) | **10 %** from initial $100k | observed DD ≤ **6 %** |
| Daily loss | **5 %** (resets at EOD on highest of balance/equity) | worst day ≤ **3 %** |
| Ruin@10 % (static max) | — | < **1 %** |
| Ruin@5 % (daily) | — | < **5 %** |

## Full sweep

| cap_mult | cap/trade | PnL | MaxDD | Worst Day | PF | Sharpe | Calmar | Ruin@4% | Ruin@5% | Ruin@6% | Ruin@8% | Ruin@10% |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 2.0 | 0.22% | $+9,529 ✅ | 1.52% | 0.62% | 1.84 | 3.91 | 6.28 | 0.0% | 0.0% | 0.0% | 0.0% | 0.00% |
| 3.0 | 0.33% | $+14,686 ✅ | 1.72% | 0.90% | 1.94 | 4.05 | 8.53 | 0.3% | 0.0% | 0.0% | 0.0% | 0.00% |
| 4.0 | 0.44% | $+20,182 ✅ | 1.94% | 1.20% | 2.01 | 4.12 | 10.41 | 1.6% | 0.3% | 0.1% | 0.0% | 0.00% |
| 5.0 | 0.55% | $+23,311 ✅ | 2.06% | 1.38% | 2.03 | 4.15 | 11.30 | 3.4% | 0.8% | 0.2% | 0.0% | 0.00% |
| 6.0 | 0.66% | $+23,311 ✅ | 2.06% | 1.38% | 2.03 | 4.15 | 11.30 | 3.4% | 0.8% | 0.2% | 0.0% | 0.00% |
| 7.0 | 0.77% | $+23,311 ✅ | 2.06% | 1.38% | 2.03 | 4.15 | 11.30 | 3.4% | 0.8% | 0.2% | 0.0% | 0.00% |
| 8.0 | 0.88% | $+23,311 ✅ | 2.06% | 1.38% | 2.03 | 4.15 | 11.30 | 3.4% | 0.8% | 0.2% | 0.0% | 0.00% |
| 9.0 | 0.99% | $+23,311 ✅ | 2.06% | 1.38% | 2.03 | 4.15 | 11.30 | 3.4% | 0.8% | 0.2% | 0.0% | 0.00% |
| 10.0 | 1.10% | $+23,311 ✅ | 2.06% | 1.38% | 2.03 | 4.15 | 11.30 | 3.4% | 0.8% | 0.2% | 0.0% | 0.00% |

## 🏆 Sweet spot

**SWEET-SPOT (safe under REAL 5ers rules with 40 % margin)** — `cap_mult=5.0` (= **0.55 %** per trade)

- PnL (3 months, backtest): **$+23,311**  (annualised ≈ $+93,245)
- Max DD: **2.06 %**  (safety margin vs 10 % cap: **4.85×**)
- Worst single day: **1.38 %**  (safety margin vs 5 % cap: **3.62×**)
- Profit factor: **2.03**
- Sharpe (per-trade ann.): **4.15**
- Calmar: **11.30**
- **Ruin@5 %**  (daily line):  **0.8 %**
- **Ruin@10 %** (static max):  **0.00 %**

## vs. current live (Flat 0.110 %)

- Current live: **$+14,686** @ DD 1.72 %
- Sweet spot: **$+23,311** @ DD 2.06 %
- Lift: **$+8,625** (+58.7 %) — DD +0.34 pp

## Reading the table

- Rows marked ✅ satisfy ALL safety constraints (DD<6 %, worst_day<3 %, Ruin@10 %<1 %, Ruin@5 %<5 %).
- Sweet spot = highest-PnL row that's ✅.
- Ruin@10 % < 0.1 % means realistic-live probability of blowing the prop firm static line is essentially zero.

