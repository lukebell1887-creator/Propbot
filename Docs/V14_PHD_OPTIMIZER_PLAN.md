# SHF v14 — PhD-Adaptive SmartBB — Optimizer Plan

**Date:** 2026-04-17
**Predecessor:** v13 SmartBB (+12.86 % / 3 mo, PF 2.86, DD 1.11 % — LIVE, UNTOUCHED)
**Goal:** Replace every hand-picked scalar in v13 with either (a) a dynamic self-calibrating threshold, or (b) a per-symbol override tuned by walk-forward with embargo and bootstrap CIs.

---

## 0. Guard-rails (the v9-Apex trauma never happens again)

- **v13 is NOT touched.** `src/smartbb_engine.py`, `src/live/smartbb_live.py`, `Scripts/run_live_smartbb.py`, `MQL5/Experts/SHF_Bridge.mq5` — **all frozen.** Live bot continues to run v13 throughout.
- **All v14 code lives in NEW files.** Zero `replace_in_file` edits in v13 source.
- **No param tuning on full window.** Walk-forward with 60 % IS / 15 % OOS1 / 15 % OOS2 and 5 % embargo between every split.
- **Bootstrap rejection.** Any symbol whose OOS2 5th-percentile bootstrap PF < 1.0 is DROPPED from the live universe.
- **Honest reporting.** If v14 OOS < v13 OOS, we ship v13 and document the failure. Written in stone.

---

## 1. Files to be created (all new, nothing overwritten)

```
src/momentum/
  rolling_quantile.py       # P²-style rolling-quantile estimator (O(log n))
  ou_halflife.py             # Ornstein-Uhlenbeck half-life fitter + gate
  optimal_stop_v14.py        # Gaussian-drift completion-probability exit rule

src/
  smartbb_engine_v14.py      # v14 engine (per-symbol params + dynamic gates)

Scripts/
  optimize_v14_per_symbol.py # Walk-forward grid sweeper + bootstrap CI
  backtest_smartbb_v14.py    # Portfolio backtest with tuned params

Docs/
  V14_PHD_OPTIMIZER_PLAN.md  # THIS FILE
  V14_HONEST_RESULTS.md      # Written AFTER runs — verdict & CIs

Results/
  v14_per_symbol_tuning.json # Per-symbol walk-forward winners + CIs
  v14_smartbb_100000_3m.json # Portfolio backtest (tuned)
  v14_smartbb_100000_3m_trades.json
```

---

## 2. The seven upgrades, in maths

### U1 — Adaptive Z threshold (rolling quantile)

**v13:** `if abs(z) >= 3.0: enter`  (fixed)

**v14:** For each symbol maintain a rolling window of the last N |Z| observations. The entry threshold is the q-quantile of that window.

```
T_z(t, sym) = Q_{|z|, sym, window=W}(q)         # e.g. W=500, q=0.99
enter  ⇔  |Z(t)| >= T_z(t, sym)  AND  Z_min_abs <= |Z(t)| <= Z_max_abs
```

`Z_min_abs = 2.5`, `Z_max_abs = 5.0` are safety rails (never trade noise, never trade falling knives) — these are NOT tuned; they are boundaries.

**Why it's better:** The threshold scales naturally with per-symbol volatility regime. In a calm week |Z|=3 is genuinely rare → take it. In a wild week |Z|=3 is routine → skip it.

### U2 — Adaptive Hurst threshold (rolling quantile)

**v13:** `if hurst < 0.50: allow_trade`  (fixed)

**v14:**
```
T_h(t, sym) = Q_{hurst, sym, window=W_h}(q_h)   # e.g. W_h=200, q_h=0.30
allow_trade  ⇔  hurst(t) <= T_h(t, sym)  AND  hurst(t) <= 0.55
```

Same logic — a deep-MR regime for US100 is a mid-regime for USOIL. Each symbol owns its own definition of "reverting."

### U3 — OU half-life gate and dynamic time-stop

Fit the standard continuous-time mean-reversion SDE to the last W_ou closes:

```
dX_t = -θ (X_t - μ) dt + σ dW_t
```

Discretise as AR(1): `ΔX_t = α + β·X_{t-1} + ε_t`, β<0 ⇒ θ = -β, μ = -α/β, **half-life τ = ln(2)/θ bars**.

**Gate:** If `τ > τ_max` (default 20 bars) → skip the trade. Reversion would take longer than the cost/risk justifies.

**Time-stop:** `T_stop = min(2 * τ, 96)` instead of fixed 96. Shorter-half-life symbols get tighter time stops, matching the physics.

**Why it's better:** "Reversion speed" stops being a guess; it is measured.

### U4 — Optimal-stopping exit via completion probability

**v13's rule:** exit if `bars_held >= 4 AND running_pnl < 0 AND |μ̂/√P| > 1.0 AND drift is against position` (discrete hand-crafted rule).

**v14's rule:** on every bar after `min_bars`, compute

```
P_win(t) = P( hit TP before SL in remaining bars | posterior μ̂, P )
```

Approximate via drifted Brownian motion with barrier-hitting inputs:

```
drift = μ̂ * T_rem                (posterior drift extrapolated)
spread = √(P + σ_obs²) * √T_rem   (posterior std extrapolated)
p_tp  = 1 - Φ((TP_dist - drift) / spread)
p_sl  =     Φ((-SL_dist - drift) / spread)
P_win = p_tp / (p_tp + p_sl)      (competitive barrier approximation)
```

**Exit if `P_win < threshold`** (default 0.40). Also exit if `T_rem <= 0` (time stop).

This is the first-order Bellman-value approximation to the real barrier-hitting optimal-stopping problem. Φ = standard-normal CDF.

**Why it's better:** Replaces four hand-picked constants (4 bars, 1.0 σ, `running_pnl < 0`, "drift against side") with one coherent probability and one tuneable cut-off.

### U5 — Per-symbol parameters, walk-forward tuned

For each symbol independently, the v14 engine accepts a `SymbolParams` dataclass:

```python
@dataclass
class SymbolParams:
    z_quantile: float = 0.99
    z_quantile_window: int = 500
    z_min_abs: float = 2.5
    z_max_abs: float = 5.0

    hurst_quantile: float = 0.30
    hurst_quantile_window: int = 200
    hurst_max_abs: float = 0.55

    use_ou_gate: bool = True
    ou_window: int = 200
    ou_max_halflife: float = 20.0

    stop_atr_mult: float = 1.0
    tp_frac: float = 1.0                # 1.0 = middle band, 0.5 = halfway
    breakeven_trigger_frac: float = 0.5
    breakeven_atr_offset: float = 0.2

    use_optimal_stop: bool = True
    optimal_stop_threshold: float = 0.40
    optimal_stop_min_bars: int = 3
    time_stop_max: int = 96

    allowed_hours: Optional[frozenset[int]] = None    # None = use spec window

    risk_multiplier: float = 1.0
```

The **walk-forward grid** (reduced for tractability — 81 configs per symbol):

| Dimension | Values |
|---|---|
| `z_quantile` | {0.97, 0.98, 0.99} |
| `hurst_quantile` | {0.20, 0.30, 0.40} |
| `stop_atr_mult` | {0.75, 1.00, 1.25} |
| `tp_frac` | {0.50, 0.75, 1.00} |

For each symbol:
1. Full M1 series → [IS 60 %][embargo 5 %][OOS1 15 %][embargo 5 %][OOS2 15 %]
2. Grid-run v14 on IS → rank by in-sample PF
3. Top 20 configs → run on OOS1 → rank by OOS1 net P&L
4. Top 5 from OOS1 → run on OOS2
5. **Survivor filter on OOS2:** `PF > 1.3 AND n_trades >= 5 AND max_dd_pct < 2.5`
6. If survivors exist → pick the one with best OOS2 net P&L; run **10 000× trade-sequence bootstrap** on its OOS2 trade list; require **5th-percentile PF > 1.0**.
7. If the bootstrap-robust survivor exists → symbol is **KEPT** with those params.
8. Else → symbol is **DROPPED** from the v14 live universe.

### U6 — Hour-of-day enablement

On the IS fold, for every (symbol, hour-of-day) pair compute WR and n. Enable only hours with WR > 55 % AND n >= 5. Persist to `allowed_hours` on `SymbolParams`. OOS folds must respect the IS-derived hour mask (no tuning on OOS).

### U7 — Bootstrap confidence intervals

Final portfolio backtest using the per-symbol tuned params produces a trade list. We resample that list (with replacement) 10 000 times, and report:

- Net return: median, 5 %-ile, 95 %-ile
- PF: median, 5 %-ile, 95 %-ile
- Max DD: median, 5 %-ile, **95 %-ile** (the tail matters)

**Decision rule:**
- Ship v14 if: median net > v13 net AND 5 %-ile net > 0 AND 95 %-ile DD < 3 %.
- Else: keep v13.

---

## 3. Universe

Candidate list (all zero-commission indices + USOIL + metals):

```
US100, US500, US30, DE40,      # v13 universe (kept)
UK100, JP225,                   # NEW — zero-commission EU/Asia indices
USOIL,                          # marginal in v13, retested
XAUUSD                          # NEW — 0.001% commission, 2-year data available
```

After walk-forward survival, the live v14 universe will likely be a subset of the above — probably 4–6 symbols.

---

## 4. Engine differences v13 → v14 (high-level)

| Concern | v13 | v14 |
|---|---|---|
| Z gate | `abs(z) >= 3.0` | `abs(z) >= rolling_quantile(|z|, 0.99) AND abs(z) in [2.5, 5.0]` |
| Hurst gate | `h < 0.50` | `h <= rolling_quantile(h, 0.30) AND h < 0.55` |
| Reversion-speed gate | none | `ou_halflife <= 20 bars` |
| Stop | `band ± 1.0 × ATR` | `band ± params.stop_atr_mult × ATR` |
| TP | middle band (fixed) | `entry + params.tp_frac × (middle - entry) × sign` |
| Early exit | fixed 4 bars + 1σ Kalman | `P_win < threshold` after `min_bars` |
| Time stop | fixed 96 bars | `min(2 × ou_halflife, time_stop_max)` |
| Hours | hard-coded | per-symbol `allowed_hours` |
| Risk scalar | same across symbols | `base_risk_pct × params.risk_multiplier` |

**Everything else is identical to v13:** aggregation, AKAD sizing, Beta posterior, Grossman-Zhou DD cap, 4 % daily / 5 % total halts, concurrency caps, per-symbol cost model, break-even trail mechanic, intrabar SL/TP honouring.

---

## 5. Acceptance criteria for "ship v14"

1. OOS2 portfolio PF (median bootstrap) > 1.5.
2. OOS2 portfolio 5th-percentile bootstrap net return > 0.
3. OOS2 portfolio 95th-percentile bootstrap max DD < 3 %.
4. At least 3 symbols survive the walk-forward filter.
5. Full-window backtest with tuned params: net return **>= v13's net return** (tied counts as v13 wins — status quo bias).

All five must pass. Otherwise v13 ships.

---

## 6. Execution order

1. ✅ Plan doc (this file)
2. ⬜ `src/momentum/rolling_quantile.py`
3. ⬜ `src/momentum/ou_halflife.py`
4. ⬜ `src/momentum/optimal_stop_v14.py`
5. ⬜ `src/smartbb_engine_v14.py`
6. ⬜ `Scripts/optimize_v14_per_symbol.py`
7. ⬜ Run per-symbol optimization (all 8 candidates)
8. ⬜ `Scripts/backtest_smartbb_v14.py` — portfolio backtest with tuned params
9. ⬜ Bootstrap CI generation
10. ⬜ `Docs/V14_HONEST_RESULTS.md` — verdict

No step 8+ executes until the optimizer gives clean per-symbol survivors.
