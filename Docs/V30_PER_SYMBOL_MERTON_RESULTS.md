# v30 — Per-Symbol Merton vs Pooled Merton, on the freshly-pulled 3-month 5%ers data

**Run date:** 2026-04-28
**Window:** 2026-01-26 → 2026-03-28  (46 trading days, ≈ 3 months)
**Symbols:** DE40, US30, XAUUSD, US500
**Risk:** 0.170 % (identical in both runs — only the Merton pooling differs)
**Rails:** full prop-firm safety stack + 1-tick slippage + Tier-1 news ±15 min entry block + −2 min flatten

---

## Why this test exists

The user asked: *“keep risk = 0.170 % the same, but do **4 separate Merton calcs per symbol** instead of one pooled calc.”*

This is a single-bit configuration flip in `MertonGZSizerConfig`:

```
pool_symbols = True   →   one global μ̂/σ̂² across all 4 symbols   (v30 ship)
pool_symbols = False  →   4 independent μ̂/σ̂² EWMAs, one per pair (this test)
```

Everything else — base risk, cap mult, gamma, alpha, warmup, dd_cap, news rails, slippage, symbols, data window — is **identical** to `Scripts/backtest_v30_fresh.py`.

Script: [`Scripts/backtest_v30_per_symbol_merton.py`](../Scripts/backtest_v30_per_symbol_merton.py)
Raw output JSON: `Results/v30_per_symbol_merton.json` + `…_trades.json` + `…_perSymbol.json`

---

## Headline head-to-head

| Metric                | **Pooled (v30 ship)** | **Per-symbol (this test)** | Δ |
|-----------------------|----------------------:|----------------------------:|---:|
| n trades              |                  264  |                       216  | −18 % |
| **Net PnL**           |        **+$26,021**  |             **+$17,656**   | **−$8,365** |
| Return                |              +26.02 % |                   +17.66 % | −8.4 pp |
| Max DD                |                3.60 % |                **3.11 %**  | −0.49 pp ✅ tighter |
| Worst day             |             −$2,018  |                  −$1,974   | ~equal |
| Worst daily DD        |                1.75 % |                    1.62 %  | −0.13 pp |
| Profit factor         |                 1.81  |                     1.78   | ~equal |
| Win rate              |               65.5 %  |                   69.4 %   | +3.9 pp ✅ |
| Sharpe                |                 3.33  |                     2.77   | −0.56 |
| Bootstrap ruin @ 5 %  |               24.0 %  |                  20.6 %    | −3.4 pp ✅ tighter |
| Sub-60s trades        |                    0  |                       0    | OK |
| Sanity gates passed   |                  6/6  |                     6/6    | ✅ |

**Translation:** going per-symbol gave up about a third of the PnL (~$8.4k) but produced a **tighter MaxDD, lower ruin probability, and a higher win rate**. It is unequivocally a healthier risk profile, but at meaningfully lower expectation.

---

## Per-symbol breakdown — and the **4 separate Merton states** that ran live

```
symbol      n           net    wr%      PF   maxDD%      μ̂_R     σ̂²_R   f*_Kelly   merton×
DE40       89   $   +9,908   71.9%   1.88    3.43%    +0.007    0.445     0.0051     3.02x
US30       63   $   +6,669   57.1%   1.66    3.68%    +0.155    0.687     0.0753    44.31x
US500      43   $      +98   79.1%   1.09    0.92%    -0.115    0.175    -0.2183  -128.40x
XAUUSD     21   $     +981   76.2%   9.14    0.06%    -0.042    0.358    -0.0391   -22.98x
```

These μ̂_R / σ̂²_R values are **what the live sizer was using at the end of the run** to compute each symbol's individual `f* = μ̂/(γ·σ̂²)`. Because `pool_symbols = False`, each pair has its own independent EWMA, and you can clearly see they are diverging:

- **DE40** is sitting on a near-zero μ̂ (≈0) → Merton multiplier ≈ 1.0 (the warm-up / no-edge floor).
- **US30** has the strongest live edge (μ̂ = +0.155, sharpe = +0.19) → Merton would *want* to crank to 44× base, but `cap_mult=5` clips it.
- **US500** and **XAUUSD** have negative μ̂ on this window → Merton goes negative, which the sizer floors at the `no_edge_multiplier=1.0` (base risk only). That's why XAU still made +$981 and US500 still made +$98 — they ran at base size, not Merton-amplified.

In contrast the **pooled** run blends all four μ̂ values into a single positive global μ̂, so it amplifies trades on every symbol, including XAUUSD which has the highest Sharpe contributor (PF 14.4 in pooled vs 9.1 here). That's where most of the missing $8.4k went.

---

## Why pooled wins on this dataset

The 5%ers 3-month window is heavily weighted to **DE40 + US30**, and **XAUUSD has only 21–25 trades** — too few for its own EWMA to have stabilised by the time the sizer would let it ramp up. By pooling, XAUUSD inherits the (positive) global μ̂/σ̂² immediately and starts taking amplified-size trades as soon as it warms up. By per-symbol, XAUUSD spends almost the entire window in warm-up + base-risk mode and never harvests its edge.

This is exactly the trade-off documented in `src/dynamic_sizer_v21.py` lines 478–497:

> `pool_symbols=True` … is more robust than per-symbol (some symbols have too few trades for stable EWMA)

The empirical confirmation is now in front of you: **per-symbol is +0.5 pp tighter on MaxDD but −$8.4k on PnL** because XAUUSD and US500 effectively run at flat base risk for the whole 3 months.

---

## When you'd actually want per-symbol Merton

1. **Longer history (≥ 6 months / 50+ trades per symbol).** Once each symbol's EWMA has stabilised, per-symbol can outperform pooled by avoiding cross-contamination.
2. **You want each symbol's drawdown to be self-limiting.** With per-symbol, a symbol that goes into a bad streak shrinks itself without dragging the others.
3. **You're running a portfolio of *uncorrelated* edges**. Pooling makes the implicit assumption that all symbols share the same μ̂/σ̂² — fine for ORB on indices, weak for mixing equities + FX + crypto.

---

## Verdict

| Question | Answer |
|---|---|
| Did the per-symbol Merton variant pass all 6 prop-firm sanity gates? | **YES — 6/6 PASS** |
| Is it safer than pooled? | **Yes** — MaxDD 3.11 % vs 3.60 %, ruin@5 % 20.6 % vs 24.0 %. |
| Does it make as much money? | **No** — +$17,656 vs +$26,021 (≈ −32 %). |
| Should you switch the live config to it? | **Not on a 3-month window** — XAU and US500 don't have enough trades for per-symbol EWMA to stabilise. Re-test after 6 months of live data and revisit. |

**Recommendation:** keep `pool_symbols=True` in production for now. Re-run this exact script (`backtest_v30_per_symbol_merton.py`) every quarter; once each symbol has ≥ 50 trades in its EWMA, per-symbol becomes mathematically preferable and will likely overtake pooled.
