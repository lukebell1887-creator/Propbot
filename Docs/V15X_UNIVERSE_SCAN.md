# v15-X Universe-Wide Mean-Reversion Scan — PhD-Grade Commission-Aware Results

_Generated 2026-04-17T19:45:51.681810Z — SmartBB v14 engine, coarse grid (72 configs) × 70/25 walk-forward with embargo._

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
| JP225 | **TIER3** | index | 100,000 | 84 | 13 | 4.63 | $+136 | 0.00 | $-514 | $0 | fails +$1/lot stress net=$-514 PF=0.00 |
| XBRUSD | **TIER3** | oil | 99,000 | 11 | 4 | 4.05 | $+167 | 0.78 | $-33 | $54 | fails +$1/lot stress net=$-33 PF=0.78 |
| UK100 | **REJECT** | index | 100,000 | 20 | 6 | 0.74 | $-117 | 0.41 | $-417 | $0 | unprofitable OOS net=$-117 PF=0.74 |
| XAGUSD | **REJECT** | metal | 534,711 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| XTIUSD | **REJECT** | oil | 99,000 | 5 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| EURUSD | **REJECT** | forex | 99,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| GBPUSD | **REJECT** | forex | 99,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| USDJPY | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| USDCHF | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| USDCAD | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| AUDUSD | **REJECT** | forex | 99,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| NZDUSD | **REJECT** | forex | 99,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| EURGBP | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| EURJPY | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| EURCHF | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| EURCAD | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| EURAUD | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| EURNZD | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| GBPJPY | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| GBPCAD | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| AUDCAD | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| AUDNZD | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| NZDCAD | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| CADJPY | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |
| CHFJPY | **REJECT** | forex | 100,000 | 0 | 0 | 0.00 | $+0 | 0.00 | $+0 | $0 | only 0 OOS trades |

## 3. Commission models per pair (baked into backtest)

| Sym | Asset | Spread (pts) | Commission model | Round-trip cost @1 lot (typical) |
|-----|-------|-------------:|------------------|---------------------------------|
| AUDCAD | forex | 2.2 | fixed: 2.0 | $4.00 fixed + 2.2 pts spread |
| AUDNZD | forex | 2.5 | fixed: 2.0 | $4.00 fixed + 2.5 pts spread |
| AUDUSD | forex | 1.5 | fixed: 2.0 | $4.00 fixed + 1.5 pts spread |
| CADJPY | forex | 2.0 | fixed: 2.0 | $4.00 fixed + 2.0 pts spread |
| CHFJPY | forex | 2.5 | fixed: 2.0 | $4.00 fixed + 2.5 pts spread |
| EURAUD | forex | 2.8 | fixed: 2.0 | $4.00 fixed + 2.8 pts spread |
| EURCAD | forex | 2.5 | fixed: 2.0 | $4.00 fixed + 2.5 pts spread |
| EURCHF | forex | 2.2 | fixed: 2.0 | $4.00 fixed + 2.2 pts spread |
| EURGBP | forex | 1.5 | fixed: 2.0 | $4.00 fixed + 1.5 pts spread |
| EURJPY | forex | 1.8 | fixed: 2.0 | $4.00 fixed + 1.8 pts spread |
| EURNZD | forex | 3.2 | fixed: 2.0 | $4.00 fixed + 3.2 pts spread |
| EURUSD | forex | 1.0 | fixed: 2.0 | $4.00 fixed + 1.0 pts spread |
| GBPCAD | forex | 3.0 | fixed: 2.0 | $4.00 fixed + 3.0 pts spread |
| GBPJPY | forex | 2.5 | fixed: 2.0 | $4.00 fixed + 2.5 pts spread |
| GBPUSD | forex | 1.5 | fixed: 2.0 | $4.00 fixed + 1.5 pts spread |
| NZDCAD | forex | 2.8 | fixed: 2.0 | $4.00 fixed + 2.8 pts spread |
| NZDUSD | forex | 2.0 | fixed: 2.0 | $4.00 fixed + 2.0 pts spread |
| USDCAD | forex | 1.6 | fixed: 2.0 | $4.00 fixed + 1.6 pts spread |
| USDCHF | forex | 1.8 | fixed: 2.0 | $4.00 fixed + 1.8 pts spread |
| USDJPY | forex | 1.2 | fixed: 2.0 | $4.00 fixed + 1.2 pts spread |
| JP225 | index | 8.0 | zero: 0.0 | $0 (spread-only: 8.0 pts × $0.0091/pt) |
| UK100 | index | 1.5 | zero: 0.0 | $0 (spread-only: 1.5 pts × $1.0/pt) |
| XAGUSD | metal | 2.0 | percent: 0.001 | $2.50 (0.001% ×2 deals on ~$125,000 notional) |
| XBRUSD | oil | 0.03 | percent: 0.002 | $0.34 (0.002% ×2 deals on ~$8,500 notional) |
| XTIUSD | oil | 0.04 | percent: 0.002 | $0.32 (0.002% ×2 deals on ~$8,000 notional) |

## 4. Per-asset-class verdict

### INDEX (2 pairs tested)

- **TIER 1 (deploy)**: none
- **TIER 2 (watch)**: none
- **TIER 3 (stress-fail)**: JP225
- **REJECT**: UK100

### METAL (1 pairs tested)

- **TIER 1 (deploy)**: none
- **TIER 2 (watch)**: none
- **TIER 3 (stress-fail)**: none
- **REJECT**: XAGUSD

### OIL (2 pairs tested)

- **TIER 1 (deploy)**: none
- **TIER 2 (watch)**: none
- **TIER 3 (stress-fail)**: XBRUSD
- **REJECT**: XTIUSD

### FOREX (20 pairs tested)

- **TIER 1 (deploy)**: none
- **TIER 2 (watch)**: none
- **TIER 3 (stress-fail)**: none
- **REJECT**: EURUSD, GBPUSD, USDJPY, USDCHF, USDCAD, AUDUSD, NZDUSD, EURGBP, EURJPY, EURCHF, EURCAD, EURAUD, EURNZD, GBPJPY, GBPCAD, AUDCAD, AUDNZD, NZDCAD, CADJPY, CHFJPY

## 5. Best-config cheat-sheet for TIER 1 pairs

| Sym | Z-quantile | Hurst-quantile | Stop×ATR | TP-frac | Session |
|-----|-----------:|---------------:|---------:|--------:|:-------:|

## 6. Next step

Pipe the TIER 1 and TIER 2 symbols from this coarse scan into `v15_ultimate_optimizer.py` (960-config grid × 3-split WF × 10k bootstrap × commission stress @ +$0.50/+$1/+$2 per lot) to lock in live configs.

Raw JSON: `Results/v15x_universe_scan.json`
