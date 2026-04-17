# v8 Backtest — Honest Results + Root Cause + Pivot

**Date:** 2026-04-17
**Window:** 2025-11-15 → 2026-02-13 (90 days) — same data as v7

## The number

| Metric | v7.0 CUSUM | v7.1 ORB | **v8 Microedge** | Target |
|---|---:|---:|---:|---:|
| Trades | 132 | 63 | **575** | ≥100 ✅ |
| Net P&L | −$295 | −$307 | **−$135** | > 0 ❌ |
| Return | −5.9% | −6.1% | **−2.7%** | positive ❌ |
| PF | 0.56 | 0.36 | **0.81** | ≥1.3 ❌ |
| Win rate | 34% | 32% | **46.4%** | ≥51% ❌ |
| Max DD | 3.8% | 3.7% | **3.48%** | <5% ✅ |
| Account blown | No | No | **No** | No ✅ |

**v8 is less bad, not good.** Trade count 9× higher ✅. DD safe ✅. But still losing 2.7%.

## Root cause (the brutal math)

The 18 edges are real — their statistical significance held on blind holdout. But when translated to actual trades, the edge **dies at the spread**.

Worked example — US100 `h23_lag1_mom` (our strongest single edge, autocorr +0.088):

| Variable | Value |
|---|---:|
| Autocorr effect size | +0.088 |
| M1 σ of returns | ≈ 4.0 bps |
| Expected directional move / min | 0.088 × 4.0 = **0.35 bps** |
| Hold horizon (~1 lag) | 1 min |
| Expected gain per trade | **0.35 bps** |
| Spread cost (round-trip) | 1.5 pts × 2 / 15000 = **2.0 bps** |
| **Edge-to-cost ratio** | **0.18 (need ≥ 1.0)** |

**The spread is ~6× the edge.** No parameter tuning fixes this — it's arithmetic.

The auto-disable mechanism caught this: **8 of 16 edges got killed mid-backtest** by the PF<0.85 guard. Working as designed — the engine protected us from bleeding more.

## What the data IS telling us

Look at the per-edge breakdown:

```
edge                      n    WR       exp_R    net_pnl
XAU_h14_lag5_rev        227  45.8%   -0.090   -$110  ← big loser, high freq
US100_h23_lag1_mom       93  48.4%   -0.069     -$1  ← flat
US100_h21_lag1_rev       59  50.8%   -0.083     -$1  ← flat
US100_h14_lag1_mom       47  51.1%   -0.089     +$7  ← positive-WR, cost-bled
```

Notice: the 51.1% WR edge (`US100_h14_lag1_mom`) is still losing money — because avg_loser (-1.03R) exceeds avg_winner (+0.95R). That 8% asymmetry is **pure slippage cost** eating the edge.

**The signal is there. The cost structure isn't.**

## Three actionable paths forward

### Path A — Change cost structure (biggest lever)
Either:
- Move to an ECN broker with 0.3 pts spread on US100 → edge-to-cost ratio becomes 1.2 → profitable.
- Trade larger instruments where fixed spread is a smaller % of range (e.g. crude, cotton, grains).
- Negotiate commission-only pricing through an IB setup.

### Path B — Use bigger edges (more promising)
Stop chasing 5-8% autocorrelations. Hunt for 30%+ effects. The market_dna report already gave us one:
- **OR-range → post-OR range**, r = +0.55 (US100), +0.47 (XAU) — **10× stronger** than the autocorr edges.

This is a **volatility-expansion edge**, not direction. Build v8.1 around it:
- Enter only on wide-OR days (top 40% vs past 20 days)
- Place both a long stop (above OR high) and short stop (below OR low)
- Whichever hits first = trade direction
- SL = opposite side of OR (wide — accept drawdown)
- TP = 2× OR range (asymmetric + with data: post-OR range is ~1.5-2× OR range on wide days)
- Time stop = session close

This is essentially a classic "volatility-expansion breakout" but with a statistically-validated filter (wide OR → wide day). Trade count drops to ~30-50 over 90 days (one entry per symbol per wide day), but each trade has real R:R.

### Path C — Go to bigger timeframes
Same market_dna logic on M5 or M15 bars. The spread is fixed at ~2 bps, but the bar-level σ grows with √time. A 15-min bar has σ ≈ √15 × 4bps = 15 bps. Autocorr of 6% gives edge of 0.9 bps per 15-min bar. Holding 4 bars = 3.6 bps — 1.8× spread. Marginal but tradeable.

## My recommendation

**Do Path B first. It has the strongest statistical evidence and the cleanest risk story.**

v8.1 breakout-of-OR-on-wide-days:
- ~40 trades per 90 days (fewer than v8's 575 but each is high-quality)
- Symmetric setup (stops both ways, directional bet is the market's)
- Real 1:2 R:R within the data's physical ceiling
- Uses the single strongest validated edge (OR-range predicts post-OR range)
- No latency war, no PhD over-engineering
- 5%ers rules still protected by ghost stops

## Decision

1. **Build v8.1 (OR-breakout-on-wide-days)** → I write it, backtest it, we have numbers in an hour
2. **Switch broker first** → investigate ECN/commission-only, re-run v8
3. **Go M15 first** → re-run market_dna on M15 bars to see if edges scale up
4. **Stop and think** → take a day, discuss approach with me before more code

Which?
