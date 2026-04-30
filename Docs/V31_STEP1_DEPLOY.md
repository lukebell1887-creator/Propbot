# V31 — STEP 1 DEPLOY (risk bump 0.175% → 0.185%, state preserved)

**Status:** ✅ ready  **Code change:** `src/live/v30_live.py` (one line) +
`Scripts/v31_migrate_sizer_state.py` (new helper)

---

## Why we're doing this

Two findings from the deep-dive of the live engine vs backtest:

1. **0.175% → 0.185% risk bump** (per V31 final proof matrix): improves
   net P&L expectancy by ~5.7% with no measurable change in worst-day or
   max-DD.

2. **Merton sizer state safety:** changing `base_risk_pct` triggers
   `MertonGZSizer.from_state(strict_config=True)` to reject the saved
   JSON, forcing a 15-trade cold-start warm-up — i.e. **all your live μ̂/σ̂²
   and R-history would be discarded**. This script prevents that.

R-units (μ̂, σ̂², trade history) are dimensionless, so the sizer's
*statistics* transfer cleanly across base-risk changes. Only the metadata
field (`cfg.base_risk_pct`) needs updating in the persisted state.

---

## Files changed

| File | Change |
|---|---|
| `src/live/v30_live.py` | `base_risk_pct: float = 0.00170 → 0.00185` (line 446 area). Comment updated to reflect v31. Cap value comment fixed (per-trade cap = 0.925%). |
| `Scripts/v31_migrate_sizer_state.py` | **NEW** — atomic migration helper with dry-run, age check, backup, verify. |

---

## VPS deployment (copy-paste)

> **Before you start:** the bot must be **stopped** while migrating.
> Atomic writes are crash-safe but you don't want concurrent reads
> during the swap.

### 1. Stop the bot
```powershell
cd C:\PropBot
.\STOP_BOT.ps1
```

### 2. Pull the latest code
```powershell
cd C:\PropBot
git fetch origin
git pull origin main
```

### 3. **DRY-RUN** the migration (mandatory first)
```powershell
cd C:\PropBot
python Scripts\v31_migrate_sizer_state.py --dry-run
```

**Expected output (key lines):**
```
  File           : C:\PropBot\Results\v30_state\sizer_mertongz.json
  Last modified  : ~0.x days ago
  Trades seen    : ~50
  Symbols w/ μ̂   : 1   (pooled mode → "_GLOBAL_")
  Current base   : 0.001700  (0.170%)
  Target base    : 0.001850  (0.185%)
========================================================================
[DRY-RUN] would write backup -> sizer_mertongz.json.bak.<timestamp>
[DRY-RUN] would update sizer_mertongz.json cfg.base_risk_pct = 0.00185
```

**Hard-stop the deploy if any of these are wrong:**
- Trades seen = 0 (means cold-start anyway — just delete the state file)
- Current base ≠ 0.001700 (someone changed it; investigate before proceeding)
- Last modified > 14 days ago (state is stale; cold-start is safer)

### 4. **APPLY** the migration
```powershell
python Scripts\v31_migrate_sizer_state.py
```

**Expected output (key lines):**
```
[OK] backup saved -> sizer_mertongz.json.bak.<timestamp>
[OK] migration complete: cfg.base_risk_pct = 0.001850  (0.185%)
     n_seen preserved   = ~50  (μ̂/σ̂²/R-history intact)
     backup retained    = C:\PropBot\Results\v30_state\sizer_mertongz.json.bak.<timestamp>
```

### 5. **VERIFY** (eyeball the JSON)
```powershell
type Results\v30_state\sizer_mertongz.json | findstr base_risk_pct
```
Should print: `"base_risk_pct": 0.00185,`

### 6. Restart the bot
```powershell
.\GO_LIVE_V30.ps1
```

**On startup look for these banner lines** (proves state was loaded, not cold-started):
```
  STATE RESTORE       source = LIVE_STATE
                      detail = sizer: loaded ... trades_seen=N
  SIZER (Merton-GZ)   trades_seen = N   μ̂=+0.xxx  σ̂²=x.xxx  (Merton ACTIVE)
  base_risk_pct       0.185%
```

---

## Rollback (if anything looks wrong)

The migration script keeps a **timestamped backup** alongside the
original. To revert:

```powershell
cd C:\PropBot\Results\v30_state
# pick the most recent .bak file (largest timestamp suffix)
copy sizer_mertongz.json.bak.<timestamp> sizer_mertongz.json
```

Then revert the code change:
```powershell
cd C:\PropBot
git revert HEAD
```

…and restart the bot.

---

## What is NOT changed in Step 1

- ✗ Layer 1 (broker stop-limit + 60s timeout) — **Step 2, separate ship**
- ✗ Backtest exit-slip model — **Step 2, separate ship**
- ✗ EA file (`MQL5/Experts/SHF_Bridge.mq5`) — **Step 2, separate ship**
- ✗ `cap_mult` / `gamma` / `dd_cap_pct` / news rails / DD breakers
- ✗ Symbol set / ORB anchors / TP / SL math
- ✗ Sizer math itself (`src/dynamic_sizer_v21.py` is untouched)

The strict-config behaviour in the sizer is **intentionally left as-is**
so it continues to catch accidental config drift. The migration script is
the explicit, audited path for risk changes.

---

## Expected effect on next session

- All trades sized at **0.185% base** (was 0.170%) — about 8.8% larger
- Max per-trade risk now **0.925%** of equity (was 0.85%, cap = 5×)
- Same Merton μ̂/σ̂² → same edge scaling — only the **unit** changes
- DAX bug already fixed; combined with this bump, expected ≈$93/day
  median (vs $85 at v25.1) over the 3-month sample

---

## Step 2 preview (next session)

Layer 1 build (multi-file, careful work):

1. `src/execution/mt5_bridge.py` — add stop-limit construction with 5pt
   max-deviation envelope + 60s timer that converts to market close.
2. `MQL5/Experts/SHF_Bridge.mq5` — handle the new
   `ORDER_TYPE_*_STOP_LIMIT` field in the close-position request +
   server-side timer fallback in OnTimer().
3. `src/momentum/orb.py` (backtest engine) — update exit-slip model to
   `min(spread × 1.0, 5pt)` to mirror Layer 1 caps.
4. New backtest run on 3-month data with Layer-1-aware costs to verify
   the +$3.7k slip-savings projection.
5. `tests/test_layer1_envelope.py` — unit tests for the cap math, the
   60s fallback, and the price-improvement edge case.

Estimated work: 1 focused session. Will be delivered in a separate PR
so this risk-bump can ship cleanly tonight if desired.
