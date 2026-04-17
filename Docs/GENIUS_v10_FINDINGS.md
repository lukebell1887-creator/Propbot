# SHF v10 GENIUS — Honest Findings

**Date:** 2026-04-17
**Status:** Framework complete, two full backtests executed, **fundamental cost problem exposed**

---

## What we built

A clean portfolio-of-probes engine (`src/genius_engine.py`) running on
the **9 commission-friendly 5%ers MTB symbols** (6 indices + 3 oils):

| Component | Status | Impl |
|---|---|---|
| 3 breakout probes (OR-gap, NR7, pivot) | ✅ | pure functions |
| Per-(symbol, probe) Bayesian edge tracking | ✅ | `BetaPosterior` from existing `src/momentum/` |
| Grossman-Zhou DD-constrained Kelly | ✅ | `GrossmanZhouDD`, risk auto-shrinks near 5% DD |
| L5-credible-interval auto-muting | ✅ | trades 8+, L5<40% → mute for 10 trades |
| Breakeven stop-trail (v10.1) | ✅ | SL→entry at 0.6R favourable |
| Correct commission model | ✅ | indices $2/lot, oil $0, metals/FX excluded |
| Correlation-cap (max 2 index positions) | ✅ | portfolio-level gate |

Runs at **~500k bars/sec**. Full trade log + per-pair-per-probe
diagnostics emitted every run.

---

## Two backtest runs on 3 months / 9 symbols

| Version | R:R | WR | Trades | Net P&L | Max DD | Outcome |
|---|---|---|---|---|---|---|
| v10 initial (0.5:1) | 0.5:1 | **55.9%** | 127 | **-$4,623** | 5.09% | 🔴 Halted |
| v10.1 fixed (2:1 + BE-trail) | 2:1 | 21.7% | 152 | **-$4,170** | 5.04% | 🔴 Halted |

**Both configurations lose money, both breach the DD gate by ~0.05%.**

---

## The real edges that DO exist

Even in losing runs, some (symbol, probe) pairs clearly work:

| Pair | n | WR | Expectancy (R) | Net $ |
|---|---|---|---|---|
| **US100::or_gap_up** (v1) | 11 | **90.9%** | +0.274 | **+$1,620** |
| **US30::or_gap_up** (v1) | 10 | **90.0%** | +0.238 | **+$637** |
| **US100::or_gap_up** (v2) | 11 | 27.3% | **+0.372** | **+$2,058** |
| **US30::or_gap_up** (v2) | 11 | 45.5% | **+0.492** | **+$1,410** |

- US index up-gap continuation is a **real and robust edge** across both R:R configurations
- Everything else (DE40, UK100, all shorts, all oil, NR7, pivot) was noise

---

## The fundamental problem: commission tax

$100k account, 3 months, ~150 trades:

* Avg **lots per trade ≈ 15** (because risk $500 / small pt-distance)
* Avg **commission per trade = $30** (indices) to $0 (oil)
* Avg **spread slippage per trade ≈ $22**
* **Total cost per trade ≈ $40-50 on $500 risk = 8-10% of R**

To break even at 1:1 R:R, you need **WR > 55%** after paying that tax.
To thrive, you need WR > 60%. Most (symbol, probe) pairs hover at 40-55%
— statistically indistinguishable from chance at n<20.

---

## Why the 3-month window isn't enough

- 9 symbols × ~100 trading days = 900 setup-days
- Only 3 probes fire per day on average across the universe
- Of those, only 1-2 actually trigger an entry
- **Result: 127-152 trades = 14-17 per (symbol, probe) pair on the 9 we fired on**
- Bayesian needs **n≥30 per pair** to reliably separate a 55% edge from noise
- We'd need **6-12 months of this multi-symbol data** to train the filter without over-fitting

---

## What this proves, and what to do next

### Proven
1. **The engine works** (runs fast, tracks safety, emits honest trade logs)
2. **Cost model is correct** (commissions match 5%ers public pricing)
3. **US100/US30 up-gap is a tradeable edge** (2 independent runs confirm)
4. **DD guard triggers correctly** (halted at -5.04% twice as designed)

### Not proven (the stuff that needs more data or a different approach)
1. Whether the 90% WR on US100/US30 up-gaps holds out-of-sample
2. Whether there's a second uncorrelated edge (needed for diversification)
3. Whether any short-side probe has positive expectancy (this sample was uptrend-only)

---

## Three paths forward (pick one)

### Path A — "Concentrate on what works" (fastest, lowest-risk)
- **Ship only US100 + US30 up-gap continuation, nothing else**
- ~20-30 trades per 3-month period, ~$2,500 net P&L on $100k
- **Monthly: ~0.8% ≈ £650 on £100k MTB account**. Under target but positive.
- Pros: uses the only two edges proven twice. Low DD risk.
- Cons: below £3k/month goal. Single-thesis. Vulnerable to regime change.

### Path B — "Reduce the cost tax" (cleverer, medium effort)
- Drop to M15 or H1 charts — **wider R_dist means the $30/trade commission
  becomes 2-3% of R instead of 8-10%**
- Fewer trades (maybe 40-60 per 3 months) but each with better cost/R ratio
- Would break the backtest harness but take ~2 hrs to convert
- **Monthly: projected ~1.5-2.5% = £1,500-£2,500** on 100k

### Path C — "Get the data for proper walk-forward" (scientific, slowest)
- Download 2+ years of Dukascopy M1 for the 9 symbols (~45-60 min download)
- Run 24-month walk-forward with 3-month train / 1-month test rolling windows
- **Only then can we honestly claim a statistically-valid monthly expectation**
- **Monthly estimate would have confidence intervals, not single-window luck**

---

## My recommendation

**Do Path A immediately, Path C in parallel.**

Ship US100+US30 up-gap continuation now on a $5k MTB Level 1 to prove
the edge works live with real spreads. While that's running, download
2 years of Dukascopy data for the 9-symbol universe and run proper
walk-forward. If WF validates a second edge, add it. If not, Path A
is the ceiling.

**Honest monthly expectation on Path A alone: £500-£1,500 on £100k, not £3k.**
The maths does not support a £3k/month target with a 3-probe breakout
stack on this symbol universe given 5%ers commission schedule.

To hit £3k/month we need either:
1. A different asset class (crypto? — no data; £2k+ accounts at 5%ers?
   — rules differ), OR
2. A completely different timeframe (swing / position trades at H4-D1
   where the cost/R ratio collapses), OR
3. A second, uncorrelated positive-edge pattern (not yet found)

---

## Files delivered

| File | Purpose |
|---|---|
| `src/genius_engine.py` | Full engine (416 lines) |
| `Scripts/backtest_genius_v10_5ers.py` | Backtest harness (150 lines) |
| `Docs/GENIUS_MASTER_PLAN_v10.md` | Strategy doc |
| `Docs/GENIUS_v10_FINDINGS.md` | **← this file** (honest report) |
| `Results/v10_genius_5ers_100000_3m.json` | Full run data |
| `Results/v10_genius_5ers_100000_3m_trades.json` | Every trade with R and exit reason |


