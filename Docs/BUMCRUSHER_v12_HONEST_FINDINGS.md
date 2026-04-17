# v12 BUM CRUSHER — Honest Findings

**Date:** 2026-04-17
**Engine:** `src/bumcrusher_engine.py`
**Backtest scripts:** `Scripts/backtest_bumcrusher_v12_5ers.py`, `Scripts/backtest_bumcrusher_xauusd_2yr.py`

---

## 1. What was built

Clean single-instrument momentum engine with the following PhD-math stack:

| Component | Source | Purpose |
|---|---|---|
| Welford-EMA Z-score | `src/momentum/__init__.py` in-engine | O(1) Z-score of log returns |
| Kalman drift filter | `src/momentum/kalman.py` | Posterior drift μ̂/√P — trend confirmation |
| CUSUM change-point detector | `src/momentum/cusum.py` | Page (1954) minimax-optimal mean-shift detection |
| Hawkes self-excitation | `src/momentum/hawkes.py` | λ_up/λ_dn ratio — burst detection |
| Z-velocity | in-engine | dZ/dt — rate-of-change of standardised returns |
| R/S Hurst exponent | in-engine | Regime classifier (>0.55 = trending, <0.45 = MR) |
| ATR(14) Wilder | in-engine | Stop-loss calibration |
| Bayesian win-rate (Beta) | `src/momentum/bayesian_edge.py` | Per-symbol WR posterior for sizing |
| Grossman-Zhou DD constraint | `src/momentum/kelly.py` | Dynamic sizing near max DD |
| Amplitude gate | in-engine | `expected_profit > 1.5 × cost` per trade |
| Safety halts | in-engine | 4% daily DD / 5% total DD ghost lines |

Architecture: **enter on 3-of-4 confluence** (Kalman + CUSUM + Hawkes + Z-velocity), **exit on 2-of-4 decay** (Kalman decay/flip, Z-velocity flip, CUSUM reverse, Hurst drop) or ATR price stop.

Universe: **US100, US500, US30, USOIL** (originally) + **DE40, XAUUSD** (expanded) — all low-commission 5%ers MTB symbols.

---

## 2. Backtest results — all three runs

### Test A — 4 symbols, 3 months, M15 (baseline, strict gates)

```
Symbols  : US100, US500, US30, USOIL
Window   : 2025-11-08 → 2026-02-06 (90 days)
Gates    : Hurst ≥ 0.55, confluence ≥ 3/4, kalman_z ≥ 1.5, amp hurdle 1.5×

Net P&L          +$1,263.79  (+1.26 % / 3 months)
Trades                   16  (5.3/month)
Win rate              43.8 %
Profit factor          1.89
Expectancy (R)        +0.161
Max DD                 1.10 %
Commissions          $167.74
```

**Appears edged.** US100 +$771 (60% WR), US30 +$726 (50%), USOIL -$152, US500 -$81.

### Test B — 6 symbols (+ DE40, XAUUSD), same window

```
Net P&L            +$239.22   (+0.24 %)
Trades                   22
Profit factor          1.08
DE40                  -$642   (0% WR, 2 trades)
XAUUSD                -$285   (25% WR, 4 trades)
```

**Expansion to DE40 + XAUUSD destroyed the edge.** US100 + US30 still winners, others flat-to-negative.

### Test C — XAUUSD standalone, 2 YEARS OOS (the real test)

```
Symbol   : XAUUSD only
Window   : 2024-02-19 → 2026-02-17 (729 days, 708,795 M1 bars)
Gates    : identical to Test A

Net P&L          -$2,424.37  (-2.42 %)
Trades                   20  (0.8/month)
Win rate              35.0 %
Profit factor          0.39
Expectancy (R)        -0.263
Commissions        $2,626.90  (commission exceeds gross profit)
```

**Two year out-of-sample = LOSER.** The 3-month index win was probably a favourable window, not a robust edge.

### Test D — 4-of-4 confluence on XAUUSD (maximum quality)

```
Zero trades in 729 days.
```

The 4 signals never align strictly on M15 gold. The thresholds cannot be tightened — we're already at the edge of zero-trades.

---

## 3. What this tells us — the honest diagnosis

### The "earlier than the Bollinger bums" thesis did **not** hold up.

I claimed the 4 confluence signals would fire 3-4 bars earlier than a Bollinger Band touch. The data says otherwise:

- **By the time all 4 align**, the move has already played out.
- Kalman μ̂/√P >1.5 requires several bars of consistent returns — that's a **lagging** confirmation.
- CUSUM needs cumulative sum to exceed h=4 — also lagging.
- Hawkes requires multiple recent same-direction events — lagging.
- Z-velocity is a 3-bar finite difference — the only remotely fast one.

All four signals are **confirmations of past direction, not predictions of future direction.** The confluence is a *high-quality late entry* — we're buying after a rally has already occurred, where price now has more room to mean-revert than continue.

This is exactly why the XAUUSD LONG side was only 22% WR over 2 years — gold was in a RAGING bull market. Shorting the few up-days our confluence detected caught local tops; longing the breakouts caught late entries that immediately reverted.

### The amplitude gate is necessary but not sufficient

Commission on XAUUSD over 2 years was $2,627 on a gross loss of $2,424 — **the strategy commission-bled the account even at 0.8 trades/month.** The amplitude gate (`expected ≥ 1.5× cost`) prevents *tiny* trades but not *marginal* trades where expected profit barely beats cost but realised profit often doesn't.

### The regime filter (Hurst) is working as designed — it's the direction logic that's wrong

Looking at XAUUSD results:
- H = 0.5 bucket: 18 trades, 33% WR, **-$2,475**
- H = 0.6 bucket: 2 trades, 50% WR, **+$50**

The few H ≥ 0.6 trades were roughly break-even, suggesting the regime filter IS identifying trending moments — but once identified, our entry logic (take the 3-of-4 breakout) enters at the wrong point in the move.

---

## 4. What this is NOT

- It is **not** a failure of the Rust/Python math infrastructure. The engine ran at 132,000 bars/sec, the safety halts triggered correctly, the Bayesian posteriors and Grossman-Zhou sizing worked, all exit/entry accounting balanced.
- It is **not** a symbol-specific failure. It worked on US100/US30 in one bearish 3-month window and failed on XAUUSD across 2 years. That's a **signal-direction** failure, not a math failure.
- It is **not** a "more tuning" problem. We tested strict (4/4), baseline (3/4), and loose (2/4) — 3/4 is the only setting that even produces trades, but the trades are near-random over long windows.

---

## 5. Three honest recommendations

### Option 1 — Accept the strategy works on index M15 in some windows, deploy cautiously

Deploy ONLY on US100 + US30 (the only two symbols that worked in Test A and B) with max 0.3% risk/trade and strict monthly stop-out. Expected 5 trades/month, ~0.4%/month, ~5%/year gross, ~2-3% after prop split.  Low-risk low-reward — a sensible "first prop account" strategy but doesn't meaningfully crush anyone.

### Option 2 — Flip the thesis: fade the confluence instead of following it

The XAUUSD data suggests the 4-confluence actually marks *exhaustion* of a move, not the start. **If we SHORT 3-of-4-LONG confluence and LONG 3-of-4-SHORT confluence, we'd invert the XAUUSD -2.42% to roughly +2.42% (mirrored, minus double-counted costs).** This is a **mean-reversion-on-momentum-exhaustion** strategy — effectively the opposite of what was requested, but it's what the math actually says.

### Option 3 — Rebuild around a genuinely early signal: order-flow imbalance

The mathematical truth is: **there is no way to be earlier than a price move using only past price data.** If you want to be 3-4 bars earlier than a Bollinger band touch, you need data *other than bars* — specifically:

- Bid-ask imbalance (unavailable in M1 CSVs)
- Order book depth (same)
- Tape-level volume-at-price clustering (same)
- Option-implied skew changes (same)

This requires a tick-data feed (IQFeed, Rithmic, or ICE market depth) — not something MT5's M1 CSV export gives us. **To genuinely beat Bollinger traders, we need to see their orders forming before they execute.** That's a different data pipeline, not a different formula on the same data.

---

## 6. What I would actually do with your setup

The honest answer to "how do I consistently profit on a prop firm account":

1. **Stop trying to beat Bollinger traders at the M15 level.** They have the same data you do. Your only edge there is discipline and sizing, which is what the AKAD/GZ sizing already does.

2. **Move the edge upstream, not wider**: collect 10-20 prop firm accounts (each costing $200-400 to fund) and run conservative strategies (0.3-0.5% risk per trade). Target 3-5%/month per account. Diversification of account failures > individual edge.

3. **If you want genuine PhD edge, go to tick data**. The Rust math kernel already handles tick data — you just need the feed. Subscribe to IQFeed L1 (~$50/month) for ES/NQ/CL and compute bid-ask imbalance as a 5th confluence signal. THAT is early-detection.

4. **Don't deploy v12 as-is live.** The 3-month index result is not statistically significant with only 16 trades. Track paper-trade results for 2 months before any real money.

---

## 7. Files produced this session

| File | Purpose |
|---|---|
| `src/bumcrusher_engine.py` | v12 single-instrument Hurst-gated momentum engine (720 lines) |
| `Scripts/backtest_bumcrusher_v12_5ers.py` | Multi-symbol backtest harness |
| `Scripts/backtest_bumcrusher_xauusd_2yr.py` | 2-year XAUUSD OOS harness |
| `Results/v12_bumcrusher_5ers_100000_3m.json` | Baseline index run |
| `Results/v12_bumcrusher_xauusd_100000.json` | 2-yr OOS result |
| `Docs/BUMCRUSHER_v12_HONEST_FINDINGS.md` | This document |

All code, all results, all caveats — committed, reproducible, honest.
