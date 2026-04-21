# SmartBB v15 — ULTIMATE OPTIMIZER PLAN

## Why this exists

The user pushed back on v14 for two legitimate reasons:

1. **"Include commissions in the testing"** — commissions **ARE** in the v14/v15
   cost model (per-symbol, via `SymbolSpec.round_trip_commission`), but v14's
   optimizer only reported $0 because it dropped every non-index symbol. The
   commission line-item was hidden rather than absent. v15 fixes this by:
   - printing `gross_commissions` + `gross_spread_cost` on every run
   - running an explicit **commission-stress matrix** (+$0.50, +$1.00, +$2.00
     per lot round-trip extra) to prove the edge is robust to fee hikes or
     slippage the cost model didn't anticipate.

2. **"Find optimal entry and exit points for EACH symbol — don't auto-drop"**
   — v14 used a 3×3×3×3 = 81-config grid and dropped everything that failed
   a strict bootstrap gate. v15 triples the grid resolution AND adds a
   session-of-day dimension AND reports the best config for every symbol
   regardless of tier.

## v15 design

### Grid (960 configs / symbol)

| Dim | Values | # |
|---|---|---|
| `z_quantile` | 0.95, 0.97, 0.98, 0.99 | 4 |
| `hurst_quantile` | 0.15, 0.25, 0.35, 0.45 | 4 |
| `stop_atr_mult` | 0.50, 0.75, 1.00, 1.25, 1.50 | 5 |
| `tp_frac` | 0.50, 0.75, 1.00 | 3 |
| `session` | all / US (13-21 UTC) / EU (7-12 UTC) / Overlap (13-17 UTC) | 4 |

### Anti-overfitting machinery (PhD-grade)

1. **3-split walk-forward** — non-overlapping IS/OOS with embargo:
   - Split A: IS 0-55% / OOS 58-75%
   - Split B: IS 0-70% / OOS 73-88%
   - Split C: IS 15-75% / OOS 78-95%

   A config must be net-positive on **≥ 2 of 3** OOS splits to be live.

2. **Bootstrap CIs** — 10,000 resamples of the OOS trade list on each
   split. Require `p05 net ≥ 0` and `p05 PF ≥ 0.9` on at least 2 splits.

3. **Commission stress** — best config is re-run at +$0, +$0.50, +$1, +$2 per
   lot round-trip. Must stay profitable at +$1/lot to be Tier 1.

4. **Neighbour smoothness** — top-5 grid configs (not just #1) must ALL be
   profitable on median OOS. If only #1 works and the 5 nearest neighbours
   collapse, that's knife-edge overfit → demote to Tier 2.

### Tier classification (NO auto-drop)

| Tier | Requirements |
|---|---|
| **TIER 1** (LIVE-ready) | 3-split median PF ≥ 1.0, net > 0 on ≥ 2 splits, bootstrap p05 > 0 on ≥ 2 splits, +$1/lot stress PF ≥ 1.2, neighbour smoothness ≥ 3/5 |
| **TIER 2** (WATCH) | 3-split median net > 0 and PF ≥ 1.0 but fails ≥ 1 robustness gate. Paper-trade or use ½ risk. |
| **REJECT** | Median OOS unprofitable OR trades < 3 across all splits |

### Commission model — already honest

Per `src/smartbb_engine.py::SMARTBB_UNIVERSE`:

| Symbol | Commission model | Spread |
|---|---|---|
| US100, US500, US30, DE40 | **zero** (index — MTB is spread-only) | 0.8-3.0 pts |
| USOIL | **0.002% of notional per deal** (percent × 2 for round-trip) | 0.04 pts |
| XAUUSD | **0.001% of notional per deal** | 0.40 pts |

v15 adds:
- `extra_cost_per_lot` field on `SmartBBV14Config` — stress-test knob
- commission + spread cost reported per OOS split on every winning config

## How to run

```bash
# Single-symbol smoke test (~10 min)
python Scripts/v15_ultimate_optimizer.py --symbols US100 \
    --out Results/v15_us100_smoke.json \
    --report Docs/V15_US100_SMOKE.md

# Full multi-symbol optimization (~60-90 min)
python Scripts/v15_ultimate_optimizer.py \
    --symbols US100 US500 US30 DE40 USOIL XAUUSD \
    --out Results/v15_ultimate_tuning.json \
    --report Docs/V15_ULTIMATE_RESULTS.md
```

## Files

- `src/smartbb_engine_v14.py` — patched with `extra_cost_per_lot`
- `Scripts/v15_ultimate_optimizer.py` — the ultimate optimizer
- `Results/v15_ultimate_tuning.json` — per-symbol tier + best config
- `Docs/V15_ULTIMATE_RESULTS.md` — human-readable report with
  commission tables, bootstrap CIs, stress matrix
