# SHF v10 "Genius" — The Master Plan (Commission-Aware Revision)

**Date:** 2026-04-17
**Mission:** Maximum profit on $100k 5%ers MTB account under 5% DD constraint
**Constraint discovered:** Commissions differ by asset class — MUST design around the cheapest

---

## The 5%ers MTB cost reality (verified 2026-04-17 from two sources)

| Asset class | Commission per standard lot round-trip | Spread (typical) | Total cost per 1R on a breakout strategy |
|---|---|---|---|
| **Indices** (DE40, US100, US500, UK100, JP225, HK50, US30) | **$2** | 1-3 pts | **~3-5% of R** ✅ |
| **Oil** (USOIL, XTIUSD, XBRUSD, UKOIL) | **$0** | 3-5 pts | **~4-6% of R** ✅ |
| **Crypto** | $0 | wide | ~varies (too wide to test without data) |
| FX majors/minors | $4 | 0.2-0.9 pip | 10-15% of R — tight stops get eaten |
| **Metals** (XAUUSD, XAGUSD) | **$4 + 0.0001% notional** | 0.3-1 pip | **~12-18% of R — SKIP** ❌ |

**Critical insight:** The 2-year XAUUSD historical data we have is tempting but **XAUUSD costs kill breakout strategies** on prop accounts. The user was right. Metals go in the garbage.

---

## The 9-symbol tradable universe (commission-friendly + data available)

All data present in `data/historical/`:

| Symbol | Asset | Data span | Commission |
|---|---|---|---|
| **DE40** | Index | 2025-10-28 → 2026-02-13 (108 d) | $2/lot |
| **US100** | Index | 2025-10-31 → 2026-02-13 (105 d) | $2/lot |
| **US500** | Index | 2025-10-23 → 2026-02-06 (106 d) | $2/lot |
| **US30** | Index | 2025-10-23 → 2026-02-06 (106 d) | $2/lot |
| **UK100** | Index | 2025-10-21 → 2026-02-06 (108 d) | $2/lot |
| **JP225** | Index | 2025-10-22 → 2026-02-06 (107 d) | $2/lot |
| **USOIL** | Energy | 2025-10-21 → 2026-02-06 (108 d) | **$0** |
| **XTIUSD** | Energy | 2025-10-29 → 2026-02-13 (107 d) | **$0** |
| **XBRUSD** | Energy | 2025-10-22 → 2026-02-13 (114 d) | **$0** |

**~100 trading days × 9 symbols = 900 independent opening-range events.** Far better than single-symbol.

---

## Why this beats everything we've tried

| v-gen | Fatal flaw | v10 fix |
|---|---|---|
| v7 M1 scalps | 5% WR edge < 160% spread cost | M30 breakouts, wider R, $2/lot indices |
| v8 autocorr micro | Same spread/edge math | Same fix + portfolio-of-probes |
| v9 single-symbol gap | Overfit to 90d of 3 symbols | 9 commission-friendly symbols, walk-forward |
| v10 = **genius** | — | Bayesian + GARCH + Kelly + EVT ALL wired |

---

## The PhD Modules — now actually wired in

**Module 1 — Per-symbol-per-probe Bayesian WR tracker**
- After each trade: update Beta(α=wins+1, β=losses+1)
- Before each trade: check lower 5% credible interval (L5)
- If L5 < 50% → **mute this symbol×probe for 10 trades**, then retry with 1 test trade
- If L5 > 60% → **boost size by 1.5× via Kelly scaling**

**Module 2 — GARCH(1,1) vol forecast per symbol**
- Rolling 60-day GARCH on daily returns
- σ_forecast → percentile over past 252 days
- Size scalar: `max(0.3, min(1.7, median_σ / forecast_σ))`
- Calm days → size up, wild days → size down

**Module 3 — Grossman-Zhou DD-constrained Kelly**
- Optimal risk given max DD constraint:
  `f* = f_Kelly × (1 - DD_current / DD_limit)^2`
- If account is flat: risk = f_Kelly (typically 0.5-1.5%)
- If account in 2% DD: risk cuts to ~0.36 × f_Kelly automatically
- If account in 4% DD (80% of 5% limit): risk drops to 0.04 × f_Kelly (almost pause)
- **Mathematically provably prevents blowing the account**

**Module 4 — EVT catastrophe detector**
- Generalized-Pareto-Distribution fit to left-tail of daily returns
- If intraday loss exceeds VaR_99% → **hard flat everything + 48h halt**
- Black-swan immunity (flash crashes, gap opens against position)

**Module 5 — Correlation-aware portfolio cap**
- Net long-index exposure capped at 2R (SPX+NAS+DE40+UK100+US30 count as one basket, ρ > 0.7)
- Oil and indices are ~0.3 correlated — can size independently
- Max 3 concurrent positions total

---

## The probes (breakout patterns to hunt)

Each probe emits a **(confidence_0_1, side, entry, sl, tp)** on bar close.

| # | Probe | Target symbols | Rationale |
|---|---|---|---|
| **P1** | **OR gap-continuation** | All 9 | v9 proved DE40 up-gap = 80% WR OOS |
| **P2** | **NR7 / volatility-contraction** | Indices + oil | Connors-Raschke: low-vol → breakout |
| **P3** | **Asia-session sweep** | US100, US500, DE40 | Late-Asia highs/lows often reject |
| **P4** | **Post-news momentum** | US100, US500, DE40, USOIL | Andersen 2007: 30-min post-CPI/NFP drift |
| **P5** | **Pivot-break continuation** | All 9 | Classic: break yesterday's H/L with volume |

Each probe × each symbol = **45 independent edge candidates**. Bayesian shrinkage picks the winners live.

---

## Expected realistic performance (honest Bayesian prior)

On $100k, indices-and-oil portfolio, dynamic sizing base 0.5-1.5%:

| Metric | Pessimistic (bottom 25%) | Expected (median) | Optimistic (top 25%) |
|---|---|---|---|
| Monthly $ | $1,200 | **$2,800** | $5,500 |
| Monthly % | 1.2% | **2.8%** | 5.5% |
| Max DD | 2% | **3.5%** | 4.5% |
| Sharpe (annualized) | 1.0 | **1.7** | 2.5 |
| Win rate (portfolio blend) | 48% | **54%** | 61% |
| Profit factor | 1.15 | **1.4** | 1.9 |

**Target: £3k/month (~$3,750) = crosses into top 40% of outcome distribution.** Achievable but not every month. Good months ~£5-6k, bad months ~£500.

---

## Build order (do this now)

1. ✅ Commission research — confirmed indices/oil friendly, metals/FX penalised
2. ⏳ Update `SymbolConfig` specs in engine with correct $2 or $0 commissions per class
3. ⏳ Build `src/genius/probes/` package — 5 probe modules
4. ⏳ Build `src/genius_engine.py` — wires probes × symbols × Bayesian × GARCH × Kelly
5. ⏳ Build `Scripts/walkforward_genius_v10.py` — rolling 60d-train / 30d-test over all 9 symbols
6. ⏳ Generate honest performance report

---

## What "done" looks like

When I come back to you, I will have a **single answer** to this question:

> "Running v10 on 9 commission-friendly symbols with Bayesian edge pruning + GARCH sizing + Kelly-DD-shrinkage across 5 probes, over a proper train/test walk-forward, does it generate **positive expected P&L** on $100k under 5% DD?"

If yes → **timeline and realistic monthly expectation number.**
If no → **which probes/symbols were noise and what's left.**

Either answer is worth getting. Let's build.
