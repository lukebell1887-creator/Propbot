# v15-X Universe-Wide Mean-Reversion Scan — PhD-Grade Commission-Aware Results

_Generated 2026-04-17T19:04:08.764096Z — SmartBB v14 engine, coarse grid (72 configs) × 70/25 walk-forward with embargo._

## 1. Methodology (2-page summary)

1. For every M1 CSV in `data/historical/` we load the full series and cut    a **walk-forward split**: 70 % in-sample, 5 % embargo, 20 % out-of-sample.
2. A **coarse 72-point grid** is run on IS (3 z-quantiles × 3 hurst-quantiles    × 2 stop-ATR-mults × 2 TP-fractions × 2 sessions).
3. The IS-best config is promoted to OOS, with the **real per-symbol    commission model baked in** (see §3).
4. A **commission-stress** run adds +$1/lot round-trip to test robustness    against broker slippage / fee hikes.
5. Classification:
   * **TIER 1** — OOS PF ≥ 1.5 AND +$1/lot stress still PF ≥ 1.1
   * **TIER 2** — OOS PF 1.0–1.5 (edge exists but marginal)
   * **TIER 3** — OOS profitable but fails commission stress
   * **REJECT** — unprofitable OOS or <3 trades

## 2. Summary — ranked by OOS profit factor

| Sym | Tier | Asset | Bars | IS n | OOS n | OOS PF | OOS Net | Stress PF | Stress Net | Comm $ | Reason |
|-----|------|-------|-----:|-----:|------:|-------:|--------:|----------:|-----------:|-------:|--------|
| EURUSD | **REJECT** | forex | 99,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |

## 3. Commission models per pair (baked into backtest)

| Sym | Asset | Spread (pts) | Commission model | Round-trip cost @1 lot (typical) |
|-----|-------|-------------:|------------------|---------------------------------|
| EURUSD | forex | 1.0 | fixed: 2.0 | $4.00 fixed + 1.0 pts spread |

## 4. Per-asset-class verdict

### FOREX (1 pairs tested)

- **TIER 1 (deploy)**: none
- **TIER 2 (watch)**: none
- **TIER 3 (stress-fail)**: none
- **REJECT**: EURUSD

## 5. Best-config cheat-sheet for TIER 1 pairs

| Sym | Z-quantile | Hurst-quantile | Stop×ATR | TP-frac | Session |
|-----|-----------:|---------------:|---------:|--------:|:-------:|

## 6. Next step

Pipe the TIER 1 and TIER 2 symbols from this coarse scan into `v15_ultimate_optimizer.py` (960-config grid × 3-split WF × 10k bootstrap × commission stress @ +$0.50/+$1/+$2 per lot) to lock in live configs.

Raw JSON: `Results/v15x_universe_scan.json`
