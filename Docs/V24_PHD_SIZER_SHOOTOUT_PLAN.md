# V24 — PhD SIZER SHOOTOUT (plan)

**User brief (verbatim, 2026-04-23):**
> "put lots of genius phd calculus maths into testing all the 4 symbols using all
> different sizing options so we can get the best results so we can find out the
> optimal amount which is low dd and high profit. flat is stupid. you need to do
> thorough testing with the current set up using the best maths out there"
>
> "LITERALLY TEST EVERYTHING!!!"

---

## Principle

The ORB trade signals are **fixed** (the entries/exits are deterministic given the
data + rails). **Only position SIZE varies between strategies.** So we:

1. Run ORB + rails ONCE with a tiny placeholder sizer → dump the raw trade list
   (sym, entry_time, side, entry, SL, TP1, TP2, exit_time, exit_px, R_multiple).
2. For each sizer in the zoo, replay the trades, letting the sizer decide risk_frac
   bar-by-bar from its own internal state.
3. Compute 15 risk metrics per sizer.
4. Rank by 5 different criteria so the user can see every trade-off.

This is a **controlled experiment**: same trades, same window, same seed, only the
$ amount per trade differs.

---

## Symbols (LITERALLY EVERYTHING)

All symbols in `SMARTBB_UNIVERSE` with M1 data on disk. Expected:
`DE40, US30, US500, US100, XAUUSD, XAGUSD, USOIL` (7 symbols). Also run a
4-sym subset (DE40, US30, XAUUSD, US500 = proven) for the portfolio leaderboard,
and per-symbol leaderboards so different pairs can pick different sizers.

---

## Sizer zoo (13 sizers)

Every sizer implements the same interface:

```python
class Sizer:
    def size(self, equity: float, history: TradeHistory, trade: TradeMeta) -> float:
        """Return risk fraction (0.0 - 0.01 typically). Never return > hard_cap."""
```

| # | Sizer | Math |
|---|---|---|
| S01 | **Flat 0.05 %** | `f = 0.0005` — control, ultra-conservative |
| S02 | **Flat 0.10 %** | `f = 0.0010` — control, mid |
| S03 | **Flat 0.15 %** | `f = 0.0015` — control, higher |
| S04 | **Flat 0.20 %** | `f = 0.0020` — control, aggressive |
| S05 | **½ Kelly (empirical)** | `f = 0.5 × (p·b − q)/b` where p = rolling WR, b = avg_win/avg_loss, q = 1-p |
| S06 | **¼ Kelly (conservative)** | as S05 × 0.5 |
| S07 | **Bayesian Kelly (CI-floor)** | p ~ Beta(α₀+wins, β₀+losses). Use the **10th percentile** of the posterior → shrinks to zero when CI is wide |
| S08 | **Merton-GZ v21 (γ=2.0)** | current live sizer; includes drawdown barrier f_base·(1-DD/cap)² |
| S09 | **Merton-GZ (γ=1.5)** | less risk-averse → larger base f |
| S10 | **Merton-GZ (γ=3.0)** | more risk-averse → smaller f, tighter DD |
| S11 | **GARCH-Merton** | GARCH(1,1) vol forecast σ²_{t+1} = ω + α·ε²_t + β·σ²_t; f = μ̂/(γ·σ²_{t+1}) |
| S12 | **Pure Grossman-Zhou barrier** | f = f_base·(1 − DD_t/DD_cap)^η with η = γ/(γ-1). Pure barrier, no Merton core |
| S13 | **Vince Optimal f (numerical)** | Numerically maximise Σ log(1 + f·R_i) over the empirical R-distribution. Bounded at Kelly for safety |
| S14 | **ATR-inverse (Van Tharp)** | f = base × (σ_target / σ_realised). Targets constant $-vol per trade |
| S15 | **HMM-regime gate** | 2-state Hamilton HMM on daily returns. Risk multiplier ∈ [0.5, 1.5] proportional to P(trend-state|y_{1:t}) |
| S16 | **CPPI cushion** | f = m × max(0, (equity − floor)/equity) with floor = 0.96·start_equity, m = 3 |
| S17 | **ENSEMBLE (top-3 average)** | size = arithmetic mean of top 3 sizers' outputs (pick via composite score on IS, eval on OOS) |

Every sizer is capped at a hard `max_f = 0.005` (0.5% per trade) regardless of its
internal calc, as a prop-firm sanity belt.

---

## Metrics (15 per sizer)

Profit side:
- Net PnL ($), Return %, Monthly compound %, Year-1 projection
- Profit Factor, Win Rate, Avg R, Expectancy

Drawdown / tail side:
- Max static DD %, Worst daily DD %, Ulcer Index, CVaR_95 (tail loss)

Risk-adjusted:
- Sharpe (annualised, trade-level)
- Sortino (downside-deviation)
- Calmar (CAGR/MaxDD)
- MAR ratio (monthly return / max DD)
- Omega(0) ratio

Stability & survival:
- IS/OOS PnL consistency ratio
- 5000-path stationary-block bootstrap: ruin@3%, ruin@4%, ruin@5%
- Sub-60 s trade count (must be 0 for 5ers)

---

## Rankings (5 flavours)

1. **PnL-first:** highest net PnL (profit maximiser)
2. **DD-first:** lowest max DD (defensive)
3. **Calmar-first:** best risk-adjusted growth
4. **Survival-first:** lowest ruin@4% probability (prop-firm paranoid)
5. **Composite:** Calmar × Sortino × Omega / Ulcer (PhD aggregator)

Each ranking produces its own top-5 table. If the same sizer wins 3+ rankings,
that's our production choice.

---

## Per-symbol shootout

Each of the 7 symbols gets its own individual shootout table. A symbol may
choose different sizers (e.g. DE40 may prefer HMM-regime, XAUUSD may prefer
GARCH-Merton). The final "live config" can use different sizers per symbol
if stats warrant it.

---

## Files produced

```
src/sizers/
  __init__.py                 # registry
  base.py                     # Sizer ABC + TradeMeta/History dataclasses
  flat.py                     # S01-S04
  kelly.py                    # S05-S07
  merton_gz.py                # S08-S10 (wraps existing dynamic_sizer_v21)
  garch_merton.py             # S11 (new — uses src/momentum/garch.py)
  grossman_zhou.py            # S12
  vince.py                    # S13
  atr_inverse.py              # S14
  hmm_regime.py               # S15 (uses src/regime/hmm2.py)
  cppi.py                     # S16
  ensemble.py                 # S17

Scripts/
  generate_shootout_trades.py  # one-shot trade emitter
  phd_sizer_shootout_v24.py    # replay harness + metrics + rankings
  sizer_report_v24.py          # generates V24_RESULTS.md from JSON

Results/
  v24_trades.json              # the gold-standard trade list
  v24_shootout_portfolio.json  # all 17 sizers on 4-pair portfolio
  v24_shootout_per_symbol.json # all 17 sizers per symbol
  v24_rankings.json            # 5 ranking tables

Docs/
  V24_PHD_SIZER_SHOOTOUT_PLAN.md      # this file
  V24_SIZER_SHOOTOUT_RESULTS.md       # human-readable leaderboard
```

---

## Acceptance criteria

- [ ] All 17 sizers execute without runtime error on the 283-trade stream
- [ ] Every sizer's max_f ≤ 0.005 verified at runtime (safety belt)
- [ ] Rankings produced on 5 criteria for portfolio
- [ ] Per-symbol rankings produced for each of 7 symbols
- [ ] Winner sizer documented with its exact config
- [ ] Live bot (`src/live/v23_live.py`) wired to the winning sizer
- [ ] Smoke test green
- [ ] Commit + push with full results
