# V31.3 — Sizer Stability Fix (slow EWMA + risk floor)

**Date:** 2026‑05‑28
**Branch:** main
**Scope:** Two code changes, zero strategy changes. No new entries, no new
exits, no broker behaviour change. Only the **risk-per-trade** dial.

---

## TL;DR

The live bot is correctly wired. The backtest on the same live window
(2026‑05‑14 → 2026‑05‑28) also loses money — the strategy itself just
went through an unfavourable 2-week tape after a profitable Jan-Apr.

The **$10-per-trade live sizing** you noticed is a real bug though, but
it's downstream of the strategy P&L: the Merton EWMA was tuned with
α = 0.20 (half-life ≈ 3 trades). On a 4-symbol portfolio that means
**3 consecutive losers can collapse μ̂ into negative territory and the
risk drops to ~0** — which is what produced the $5–$10 paper-cut trades.

This patch makes two surgical changes that bring live sizing into line
with the backtest assumptions:

| Knob               | Before | After  | Effect                                                                |
|--------------------|-------:|-------:|-----------------------------------------------------------------------|
| `ewma_alpha`       | 0.20   | **0.05** | Half-life lifts from ~3 trades → ~13 trades. A small streak can no longer wipe out months of evidence. |
| `min_risk_pct`     | 0.0    | **0.0005** | Floor at 0.05 % of equity (~$50/trade on $100k). Never $5. |

Total risk *upside* is unchanged — the cap remains `cap_mult × base =
5 × 0.185 % = 0.925 %`. We are only lifting the **floor**, not the
ceiling. The 4 % daily-halt + 8 % total-DD breakers still fire first
(the floor is overridden when GZ hits the DD barrier).

---

## Why this is safe

1. **Backtest parity.** The backtest computes risk identically (same
   `MertonGZSizer.compute_risk_pct`), so the floor + slower EWMA apply
   equally in the backtest. Anyone running `backtest_v23_locked.py`,
   `backtest_v30_3sym_pairings.py`, or
   `Scripts/run_v30_backtest_live_period.py` will see the same change.
   The 3-month $28k backtest result already used the cap-driven sizing
   for most trades, so this floor is well below normal operating size.

2. **5ers hard limits unchanged.**
   - Daily halt 4 % (DailyHalt) — unchanged.
   - Total DD 8 % (DDBreaker)   — unchanged.
   - GZ barrier at 4 % DD       — unchanged (still drives risk → 0 at the
     barrier; `min_risk_pct` is overridden in that branch by design).

3. **State auto-migrates.** The persisted sizer state in
   `Results/v30_state/sizer_mertongz.json` records `ewma_alpha=0.20`,
   so on first boot after this change the `load_state` config-check
   will refuse the file and the bot will fall back to **seed-from-backtest**
   (the 264-trade Jan-Apr ledger) — exactly what was just rebuilt during
   the audit. No manual reseed needed.

---

## Files changed

```text
src/dynamic_sizer_v21.py          # defaults (alpha 0.20 → 0.05, min 0 → 0.0005)
src/live/v30_live.py              # V30LiveConfig matches, passes min_risk_pct through
```

Smoke-test (already run on local Windows box):

```
> python -c "from src.dynamic_sizer_v21 import MertonGZSizerConfig; ..."
alpha= 0.05  min_risk= 0.0005
live alpha= 0.05  live min_risk= 0.0005
```

---

## How to deploy to the VPS

From the VPS:

```powershell
cd C:\PropBot
git pull
# stop the bot if it's running
.\STOP_BOT.ps1
# verify the change loaded:
python -c "from src.live.v30_live import V30LiveConfig; v=V30LiveConfig(); print(v.ewma_alpha, v.min_risk_pct)"
# expected: 0.05 0.0005

# Optional: clear sizer state file so the bot re-seeds cleanly from Jan-Apr.
# (The config-mismatch guard will do this automatically, but if you want a
#  visible audit trail, delete it explicitly:)
Remove-Item Results\v30_state\sizer_mertongz.json -ErrorAction SilentlyContinue

# Start
.\GO_LIVE.ps1
```

Look for these lines in the startup banner:

```
[restore] sizer  ✗ state config mismatch on 'ewma_alpha':
                      file=0.2 runtime=0.05 — falling back to seed
[restore] sizer  ✓ seeded with 264 trades from v30_fresh_trades.json
  SIZER (Merton-GZ)   trades_seen = 264   μ̂=+0.XXX  σ̂²=X.XXX  (Merton ACTIVE)
```

That is the green light. From the next entry onwards risk per trade
should be at the cap (~$880 / 0.925 % of $100k) for normal conditions,
and never below ~$50 even after a bad streak.

---

## What this does NOT fix

- The actual **−$4.5k loss over the last 2 weeks** was a strategy P&L
  loss, not a wiring bug. The backtest on the same window also lost
  money (see `Docs/LIVE_PERIOD_AUDIT_VERDICT_2026-05-28.md`). This
  patch makes the sizing **stable** so a loss-streak doesn't compound
  into a self-inflicted starvation problem, but the underlying
  return distribution is unchanged.

- The strategy edge degradation (if it is one) is a separate question
  — see `Docs/CAN_LIVE_MATCH_THE_28K_BACKTEST.md` for the per-symbol
  walk-forward analysis and the proposed 3-month retune cadence.

---

## Rollback

Pure config rollback, no schema migration needed:

```powershell
cd C:\PropBot
git revert <commit-hash>
.\STOP_BOT.ps1
.\GO_LIVE.ps1
```

The state file written under the new `alpha=0.05` will fail the
config-check on rollback and the bot will reseed from backtest again —
same auto-migration logic, the opposite direction.

---

## Acceptance test (paper-trade smoke)

Before going live, run a 24h dry-run with the new defaults:

```powershell
.\GO_DRYRUN_V30.ps1
```

In the heartbeat (`Results/heartbeat_v30.json`), confirm:

- `merton.alpha == 0.05`
- `merton.min_risk_pct == 0.0005`
- At least one ENTRY event with `risk_usd >= 50.00`
- No `RISK_FLOOR_TRIGGERED` events unless we're actually in a DD streak.

If those pass, ship it live with `.\GO_LIVE.ps1`.
