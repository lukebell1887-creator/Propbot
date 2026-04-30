# V31 Slippage-Defense — Proof Pipeline Plan

**Date:** 2026-04-30
**Status:** Active build
**Goal:** PROVE the proposed 3-layer slippage defense is profitable and safe BEFORE deploying any production code.

---

## The problem in one sentence

The v30 backtest (3 months, $26,020 PnL) assumed 1-tick slippage on every stop-out.  
TODAY we measured 14.82 pts on US30, 1.89 on DAX, 0.89 on US500, 0.02 on XAU.  
**The backtest's slippage assumption is fiction.** We need to see what the bot would have made with REAL slippage, with and without defense, before risking another dollar.

---

## What we have

| Asset | Used for |
|---|---|
| `Results/v30_fresh_trades.json` (264 trades) | Trade list — entry, exit, side, R, PnL |
| `data/historical/{SYM}_M1.csv` (~340k bars) | Microstructure — every minute's OHLC for 3 months |
| Today's 4 forensic SL hits | Real slip measurements — used to calibrate |
| MT5 live API | Source of truth for today's bars + deals |

---

## What we DON'T have (and why it matters)

- **Robust live slip distribution** — bot only ran 3 days, only 4 measured stop-outs.
- **Tick data** — only minute bars. Within a single bar we have to model the fill point.

This is why we use the **adversity factor** approach: we treat each M1 bar as having a known range, and model the broker fill as somewhere between the SL trigger and the bar's adverse extreme.

---

## The 5-stage pipeline

### Stage 1 — Calibrate the adversity factor (TODAY's data)
**Script:** `Scripts/calibrate_adversity_v31.py`

For each of today's 4 trades:
```
adversity = actual_slip_pts / (bar_extreme - SL_price)
          = how aggressively this broker fills inside the bar's range
          ∈ [0, 1]   (0 = ideal, 1 = always fills at worst extreme)
```

Output: `Results/v31_adversity_calibration.json`  
Per-symbol adversity: `{DE40: 0.4, US30: 0.7, US500: 0.3, XAUUSD: 0.05}` (illustrative)

**Why:** This anchors the slip simulation to YOUR specific broker, not assumed values.

---

### Stage 2 — Build slip distribution from 3-month bars
**Script:** `Scripts/build_slip_distribution_v31.py`

For each of the 264 historical trades:
1. If it was a stop-out (`realised_R ≈ -1.0`):
   - Find the exit bar in `{SYM}_M1.csv`
   - Compute `bar_excess = |bar_extreme - SL|`
   - Compute `slip_pts = bar_excess × adversity[symbol]`
2. If it hit TP (limit fill): `slip_pts = 0`
3. If it was time-exit / trail: `slip_pts ≈ 0.5 × adversity` (small market-order slip)

Output: `Results/v31_slip_per_trade.json`  
A list of 264 slip estimates calibrated to YOUR broker's behavior.

**Why:** This is the empirical slip distribution — derived from real microstructure, anchored to today's measurements.

---

### Stage 3 — Replay v30 backtest with REAL slip injected
**Script:** `Scripts/replay_v30_baseline_v31.py`

For each trade:
- `delta_R = slip_pts / sl_distance`
- `new_R   = old_R - delta_R`
- `new_pnl = (new_R / old_R) × old_pnl`  for losers
- For winners (TP hits): unchanged

Aggregate: total PnL, max DD, Sharpe, breach probability.

Output: `Results/v31_baseline_realslip.json`  
**This is the honest baseline.** Compare to v30's $26,020.

**Expected:** PnL drops by 5-15% due to slippage drag. Max DD increases.

---

### Stage 4 — Apply 3-layer defense, replay
**Script:** `Scripts/replay_v30_defense_v31.py`

Same as Stage 3 BUT with 3-layer defense logic:

**Layer 1: Stop-Limit cap.** `slip_pts_after_defense = min(slip_pts, 5.0)` per symbol (configurable).  
Models: stop-LIMIT order with 5pt tolerance. Above tolerance, position would not fill via stop-limit — model as time-fallback fill at `min(slip_pts, 8pt)` (5pt limit + 3pt time-fallback drag).

**Layer 2: Slip-aware sizing.** Reduce position size by `(1 + slip_p95 / sl_distance)` factor up-front. Smaller positions → smaller absolute $ loss for same R-loss.

**Layer 3: Toxic-window filter.** Skip trades whose entry falls in:
- US30 / US500 : 13:15 – 13:45 UTC (15 min before NYSE cash open)
- DE40         : 06:55 – 07:05 UTC (XETRA open chaos)

Aggregate metrics same as Stage 3.

Output: `Results/v31_defense_realslip.json`  
**Direct comparison to Stage 3 = the value of the defense.**

**Expected:** PnL recovers most of Stage 3's loss, max DD drops materially, breach probability collapses.

---

### Stage 5 — Monte Carlo over slip distribution
**Script:** `Scripts/montecarlo_v31.py`

Bootstrap-resample the 264 slip values from Stage 2 → run the backtest 1000 times. For each run:
- Sample slip per stop-out from the empirical distribution (with replacement)
- Compute PnL, max DD, breach? (DD > 5%)

Run this both WITHOUT and WITH defense.

Output: `Results/v31_montecarlo.json`

```
                                NO DEFENSE          3-LAYER DEFENSE
Median PnL                         $X                  $Z
5th percentile PnL                 $Y                  $W
Max realized DD (worst run)        ?%                  ?%
P(breach 5ers in 3mo)              ?%                  ?%
P(hit halt in any given day)       ?%                  ?%
Sharpe (median)                    ?                   ?
```

**Plus stress tests:**
- All-trades-95th-percentile slip
- All-trades-MAX-observed slip
- 2× wider slip distribution (regime shift)

**Why:** Probabilities, not single numbers. Direct decision input.

---

## Risk controls in the pipeline itself

1. **No production code touched.** Pipeline reads JSON + CSV, writes JSON. The live engine is unaffected until we explicitly approve a deployment.
2. **Reproducible.** Random seed is fixed in Stage 5. Same data → same probabilities.
3. **Sensitivity.** Stage 5 reports both base case AND stress tests, so we see how robust each scenario is.
4. **Honest about limitations.** The doc surfaces every assumption (adversity model, no tick data, 4-sample calibration).

---

## Decision tree after pipeline

After Stages 1-5 complete, the user sees a single decision table:

| Plan | Median 3mo PnL | P(breach) | P(halt-day) | Recommendation |
|---|---|---|---|---|
| Today (no defense, 1.0% risk) | $X | Y% | Z% | Baseline |
| 3-layer defense, 1.0% risk | $X' | Y'% | Z'% | Safer + maybe less profit |
| 3-layer defense, 1.25% risk | $X'' | Y''% | Z''% | Best risk-adjusted |
| 3-layer defense, 1.40% risk + 4.5% halt | $X''' | Y'''% | Z'''% | Aggressive |

**The user picks the row with the best (PnL × safety) tradeoff for THEIR risk appetite.**

If no row beats current behavior on a risk-adjusted basis: don't deploy.
If 3-layer defense + same risk shows breach probability dropping >50% and PnL is broadly preserved: ship the defense, hold risk constant.
If 3-layer defense + 1.25% risk shows higher PnL AND lower breach probability: ship that.

---

## Time estimate

| Stage | Code size | Compute time | Owner |
|---|---|---|---|
| 1. Calibrate (today's 4 trades) | 320 lines | 30 sec | USER (needs MT5) |
| 2. Slip distribution (264 trades) | ~250 lines | 5 sec | Cline |
| 3. Replay baseline | ~150 lines | 1 sec | Cline |
| 4. Replay with defense | ~200 lines | 2 sec | Cline |
| 5. Monte Carlo (1000 runs × 264 trades) | ~250 lines | ~30 sec | Cline |
| **Total** | ~1170 lines | ~1 min compute | |

Build time (writing scripts): ~3-4 hours.

---

## Production deployment (post-pipeline only)

If and only if the pipeline produces a winning configuration:

1. Write `src/live/v31_defense.py` (the actual defense logic for live)
2. Modify `src/execution/mt5_bridge.py` to support stop-limit orders
3. Update `src/live/v30_live.py` to:
   - Apply Layer 2 sizing
   - Block toxic windows (Layer 3)
   - Send stop-limit (Layer 1)
4. Add tests: `tests/test_defense_v31.py`
5. Re-run preflight: `python Scripts/preflight_v30.py` → `python Scripts/preflight_v31.py`
6. Deploy with `GO_DRYRUN_V31.ps1` → 1 day dry-run → `GO_LIVE_V31.ps1`

But none of that happens until the pipeline gives a green light.

---

## Status

- [x] Stage 1 script built (`Scripts/calibrate_adversity_v31.py`)
- [ ] Stage 2 script built
- [ ] Stage 3 script built
- [ ] Stage 4 script built
- [ ] Stage 5 script built
- [ ] User runs Stage 1 (needs MT5 access)
- [ ] User runs Stages 2-5 (no MT5 needed)
- [ ] Decision table reviewed
- [ ] If green light: production v31 build begins

---

## File outputs index

```
Results/v31_adversity_calibration.json   ← Stage 1
Results/v31_slip_per_trade.json          ← Stage 2
Results/v31_baseline_realslip.json       ← Stage 3
Results/v31_defense_realslip.json        ← Stage 4
Results/v31_montecarlo.json              ← Stage 5
Docs/V31_DEFENSE_PROOF_PLAN.md           ← This file
Docs/V31_DEFENSE_PROOF_RESULTS.md        ← Final write-up after Stage 5
```
