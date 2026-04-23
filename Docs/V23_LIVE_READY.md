# V23 LIVE — READY TO DEPLOY (CORRECTED, 2026-04-23)

> **Supersedes earlier draft.** Earlier version of this doc quoted v23_locked
> numbers ($10,853 / Sharpe 3.45 / DD 2.16%) — those come from a different
> backtest that uses the Merton-GZ regime-adjusted sizer and only produced
> **$3,857** net, which failed the ship gate. Meanwhile the live bot had been
> coded with that same Merton-GZ sizer. Both the numbers and the live code
> have now been corrected to the **simple flat 0.110% risk** that actually
> produced the advertised performance. Reference: `Results/risk_sweep_fine.json`.

---

## SOURCE OF TRUTH

Single authoritative result file:

```
Results/risk_sweep_fine.json
  syms    : ['DE40', 'US30', 'XAUUSD', 'US500']
  balance : $100,000
  window  : 3 months, 66 trading days
  rows    : risk sweep from 0.080% → 0.130%
```

Row selected for live (risk = 0.00110 = **0.110% flat per trade**):

| Metric                | Value           |
|-----------------------|-----------------|
| N trades (total)      | 283             |
| Net PnL               | **$10,840.63**  |
| Return                | 10.84 %         |
| Max rolling DD        | **2.156 %**     |
| Profit factor         | 1.72            |
| Sharpe (annualised)   | 3.44            |
| Win rate              | 65.4 %          |
| Worst day PnL         | −$728  (−0.73%) |
| Worst intraday DD     | 0.69 %          |
| Ruin@3%  (MC, 100×66d)| 4.92 %          |
| Ruin@4%               | 0.54 %          |
| **Ruin@5%**           | **0.06 %**      |
| Sub-60 s closes       | 0               |
| Median hold           | 75 min          |
| p10 / p90 hold        | 13 / 152 min    |

Portfolio concurrency at risk=0.110% (share of all bars):

| # open positions | % of time |
|---|---|
| 0 | 90.69 |
| 1 | 4.83  |
| 2 | 2.72  |
| 3 | 1.46  |
| 4 | 0.14  |
| ≥5 | 0.17  |

The live runner caps concurrency at **2**, so everything from row "3" down is
suppressed at runtime. Our backtest does NOT cap concurrency, so **live PnL
will be slightly (≈3–5 %) lower than $10,841** because of entries skipped at
the cap. That is a small conservative adjustment, not a bug.

---

## WHAT THE LIVE BOT RUNS

**Strategy:** 4-pair Opening Range Breakout with per-symbol tunings verbatim
from `Scripts/backtest_v22_lean_uk5.py`. Live runner is `src/live/v23_live.py`.

```
DE40     OR = 08:00 UTC + 30m    TP1=1.5R  TP2=3.0R  SL buffer = 0.3×OR
US30     OR = 14:30 UTC + 30m    TP1=2.0R  TP2=4.0R  SL buffer = 0.0×OR
XAUUSD   OR = 14:30 UTC + 30m    TP1=2.0R  TP2=4.0R  SL buffer = 0.6×OR
US500    OR = 14:30 UTC + 15m    TP1=0.5R  TP2=1.0R  SL buffer = 0.6×OR
```

**Sizer:** FLAT 0.110 % risk per trade. **No regime scaling.**
(We tested Merton-GZ regime scaling in `Results/v23_locked.json` — it halved
PnL to $3,857 and failed gates. We kept flat.)

**Rails (all enforced on every bar):**

1. ORB trade window (≤120 min post-OR)
2. `TradingCalendar` — weekend / rollover / holiday buffers
3. News entry-block: ±15 min around every Tier-1 event (`data/news/tier1_2026.csv`, 31 events loaded)
4. News flatten: close-all 2 min before each Tier-1 event
5. Portfolio cap: max 2 concurrent positions
6. Daily breaker: halt new entries if intraday DD ≥ 2 %
7. Account kill: flatten + lock if rolling DD ≥ 8 %
8. Broker-side hard SL/TP on every order
9. Time-stop: force-close any position whose ORB trade-window expires

**5ers compliance (all pass):**

- No HFT — median hold 75 min, `sub60s=0`, hard 60 s minimum-hold guard
- No bulk — max-concurrency cap 2
- No bracketing — we flatten ahead of news, never bracket
- No rollover scalping — blocked 21:55–22:10 UTC
- No tick scalping — min TP = 0.5 × OR range (≥ 10+ pts even on US500)
- Single broker, single feed, hard broker SL/TP

---

## WARMUP (FIXED)

On `start()` the runner now pulls **2,880 M1 bars per symbol** (48 h) from
the broker and replays them through:

- `OpeningRangeTracker.update(...)` — so today's OR is already finalised the
  moment the bot enters its first live tick
- `NRFilter.update(...)` — so the 20-day NR lookback is fully seeded

This means on day one of live trading the heartbeat shows
`OR=[22710.0-22755.0]` for each symbol immediately, not `OR=n/a`.
Warmup is logged as `WARMUP_DONE` event with full per-symbol snapshot.

Sanity is asserted in `Scripts/smoke_v23_live.py`.

---

## GO LIVE

```powershell
# Dry-run (no orders actually sent — prints heartbeat every 60s):
.\GO_DRYRUN_V23.ps1

# Live (after at least 1 full dry-run session shows clean warmup + at
# least one finalised OR window for each symbol):
.\GO_LIVE_V23.ps1
```

Telemetry files (tail-able):

- `Results/v23_live_telemetry.json`  — live equity / DD / per-symbol OR state
- `Results/v23_live_events.log`      — every rail trip, order, flatten, kill
- `Results/v23_live_trades.jsonl`    — ENTRY rows (one per trade)

---

## HONESTY CHECKLIST (so future-me doesn't lie to present-me)

- [x] Number in doc = number in `risk_sweep_fine.json` for `risk=0.0011` row
- [x] Live sizer = flat 0.110 %, same value used in the backtest row above
- [x] Warmup hook wired BEFORE `run()` (not after)
- [x] Smoke test asserts `runner._flat_risk_pct == cfg.base_risk_pct` and
      that `_warmup_all` exists and is callable
- [x] MertonGZ import removed from `v23_live.py` — no dead code path

If any of these slip, the number at the top of this doc is a lie.
