# SHF v7.1 ORB Scalper — 5%ers MTB 3-Month Backtest

**Strategy:** Opening-Range Breakout (Crabel 1990; Zarattini 2023)
**NR7 filter:** off
**Kalman-agree gate:** on
**TP ladder:** 1.0R / 2.0R / EVT-GARCH trail
**Max hold:** 180 min
**Account:** MTB Level 1
**Balance:** $5,000
**Backtest window:** 2025-11-15T21:22:00 → 2026-02-13T21:22:00
**Bars processed:** 250,620

## Outcome

- ❌ **Failed** — total DD hit on 2026-01-01T23:00:00

## Headline metrics (net of all fees & slippage)

| Metric | Value |
|---|---:|
| Start equity | $5,000.00 |
| Final equity | $4,695.07 |
| Net P&L | $-304.93 |
| Return | -6.10% |
| Trades | 63 |
| Win rate | 31.7% |
| Profit factor | 0.36 |
| Expectancy (R) | -0.271 |
| Avg winner (R) | 0.79 |
| Avg loser (R) | -0.76 |
| Max draw-down | 6.12% |
| Gross costs | $15.14 |

## Trades by symbol

| Symbol | Trades |
|---|---:|
| US100 | 23 |
| DE40 | 15 |
| XAUUSD | 25 |

## v7.0 vs v7.1 acceptance gate

- ❌ Net P&L > 0
- ❌ PF >= 1.3
- ❌ Max DD < 5%
- ❌ Trades >= 100
- ❌ No total DD blow

## Verdict

**FAILED.**  Tune ORB parameters, re-test.  If still negative, pivot to Proposal B (VWAP-reversion) per PIVOT_TO_PROFITABLE_v71.md.