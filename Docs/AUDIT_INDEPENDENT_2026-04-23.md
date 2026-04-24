# INDEPENDENT AUDIT — v23 live bot on real 5ers MT5 data
**Date:** 2026-04-23   **Auditor:** independent pass, zero prior exposure to repo
**Data:** `data/historical/{DE40,US30,US500,XAUUSD}_M1.csv` — 88k M1 bars each, 2026-01-20 → 2026-04-21 (3 months). Pulled from live 5ers MT5 account per `_provenance.json`.
**Backtest run:** `python Scripts/backtest_v23_final.py` — the exact live-parity test.

---

## A) NUMBERS (real 5ers data, 4-symbol portfolio, $100k start, risk=0.110%, cap=5×)

|                   | Value                   |
|-------------------|-------------------------|
| Total trades      | **283**                 |
| Net PnL           | **+$16,977**            |
| Return            | **+16.98%**             |
| Max drawdown      | **3.35%**               |
| Worst day         | **−1.26%**              |
| Worst daily DD    | **1.15%** (vs 5% rule)  |
| Profit factor     | 1.74                    |
| Win rate          | 65.4%                   |
| Sharpe            | 3.26                    |

### Per-symbol

| Sym    |  N  | PnL $    | Ret %  | WR %  | sub-curve DD | Worst day |
|--------|-----|----------|--------|-------|--------------|-----------|
| DE40   | 115 | +$4,663  | +4.66% | 67.8% | 2.84%        | −0.66%    |
| US30   |  94 | +$6,906  | +6.91% | 55.3% | 2.26%        | −0.63%    |
| US500  |  48 | +$1,672  | +1.67% | 75.0% | 0.61%        | −0.59%    |
| XAUUSD |  26 | +$3,735  | +3.74% | 73.1% | 0.14%        | −0.14%    |

### Compliance flags

| Check                                         | Answer                |
|-----------------------------------------------|-----------------------|
| 4 % internal DD breaker triggered?            | **NO** (peak 3.35%)   |
| Any trade held < 60 seconds?                  | **NO** (min hold 60s) |
| Trades opened+closed on the SAME M1 bar       | **NO** (0/283 = 0.00%) |
| Max DD vs firm's 10% static cap               | 3.35% / 10% (under)   |
| Worst daily DD vs firm's 5% rule              | 1.15% / 5% (under)    |
| Hold duration: min / median / p90 / max (sec) | 60 / 4 500 / 9 096 / 10 800 |

---

## B) TOP 3 RISKS — worst first

### Risk #1 — CRITICAL: the edge is pinned to a single hour bucket (hour-bucket overfitting)

**Evidence** (new shootout I ran: `Scripts/_tz_shootout.py`, same pipeline, only the OR-window anchor shifts):

| OR anchor (broker-time)            | N   | Net        | Return  | DD    | PF   |
|------------------------------------|-----|------------|---------|-------|------|
| **14:30 / 08:00 (current live)**   | 283 | +$16,977   | +16.98% | 3.35% | 1.74 |
| 16:30 / 10:00 (+2 h shift)         | 240 | **−$1,742**| −1.74%  | 4.13% | 0.88 |
| 17:30 / 11:00 (+3 h shift)         | 143 | **−$4,117**| −4.12%  | 4.55% | 0.62 |

A 2-hour shift — nothing else changed — flips the strategy from +17% to a loss. US30 goes +$6,906 → −$1,830. XAU goes +$3,735 → −$1,562. The result is sitting on a knife-edge of exactly one hour bucket.

Compounding this: the CSVs are **broker-time (MT5 server UTC+2/+3, confirmed by volume profile: every symbol peaks at CSV hour 16-17 = real UTC 13-14 = NYSE cash open)**, but `src/momentum/orb.py` docstrings describe `or_start_hour` as "UTC". The live bot consumes the same broker-time bars, so **live/backtest parity is intact** — however the anchor the backtest "found" (broker 14:30) corresponds to **NY pre-market**, not NYSE cash open as the code comments state. The author's stated intent and the actual firing hour disagree by ~2 hours.

Either this edge is a real pre-market phenomenon (possible — London lunch/NY warmup), or it's a 1-in-N artifact. We have no evidence either way, because the bot has only ever been backtested at this one anchor.

### Risk #2 — HIGH: uniform 1-tick slippage is optimistic for the pre-market window

The backtest applies `slippage_ticks=1.0` uniformly. That's reasonable during NYSE cash hours, but the winning anchor fires at broker 14:30 = real UTC 11:30 = ~2 hours **before** NYSE opens. From the same volume audit: US30 averages ~106 ticks/min at that anchor vs ~326 at the real NYSE open — thin book, wider spreads, worse fills. A realistic cost model (2-3 tick slippage pre-open) could take ~$3-6k of the 17% away. The bot stays green but not at the headline number.

### Risk #3 — MEDIUM: zero out-of-sample validation, 3-month window only

The live bot has only ever been backtested on this one 2026-01-20 → 2026-04-21 slice. PF 1.74 + Sharpe 3.26 on 283 trades over 60 trading days is well inside "lucky window" territory for a 4-symbol breakout. No walk-forward. No 2024-2025 holdout. No robustness test across the sizer hyperparameters that were dialled in on THIS data (`cap_mult=5`, `gamma=3`, `base_risk=0.11%`). In combination with Risk #1, this is a classic selection-bias stack.

---

## C) VERDICT

**(ii) Fix these specific things first:**

1. **Prove the pre-market edge is real, not a 1-bucket artifact.** Pull a separate 3-month window (e.g. 2025-09-01 → 2025-12-01) of the same 4 symbols from 5ers and re-run `backtest_v23_final.py` with **no re-tuning**. If it prints positive PnL with DD < 4%, the edge survives. If it doesn't, the current numbers are a mirage.
2. **Re-run with a realistic pre-market cost model.** Raise `slippage_ticks` to 2.5 on US30/US500, 2 on XAU, keep 1.0 on DE40 (whose window straddles European open). Expect the 17% to drop to ~8-12% and the DD to drift toward 4%. If the 4% breaker starts tripping, shrink `base_risk_pct` from 0.110% to ~0.075% and re-tune.
3. **Fix the docstring lie in `src/momentum/orb.py`** — the `or_start_hour` comment says "UTC hour" but the code reads the bar's raw hour, which on this data feed is broker-time (~UTC+3 in DST). Rename the field `broker_hour` or add a `tz_offset` field so the next person isn't misled.
4. **Walk-forward check.** 6-month rolling train / 1-month test across 2024-2025. If Sharpe collapses below 1.0 OOS, the 3.26 in-sample is overfit.

Until items 1-2 are re-run with fresh data (not re-optimised), **do not deploy to 5ers live capital**. The backtest numbers are real in the sense that they were not faked, but they are fragile in the sense that a single hour-shift destroys them. Demo trading the current config will tell you only whether the execution path works — not whether the edge is real, because demo for 2 weeks is in-sample by construction (same anchor, same data regime).
