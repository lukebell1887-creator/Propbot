# VPS UPDATE TO v30 — copy-paste cheat-sheet

**Date:** 2026-04-25
**Goal:** Stop the v23 dry-run, pull v30 from GitHub, smoke-test it, start
the v30 dry-run with risk=0.170%, no-chase=300 s, and the per-trade
slippage tracker.
**Time on VPS:** ~3 minutes total.

---

## Step 0 — RDP to the VPS

Connect as you normally do. Open a fresh **PowerShell** window
(not cmd.exe — the launchers are `.ps1`).

---

## Step 1 — One-shot copy-paste

Paste this entire block into the VPS PowerShell prompt and hit Enter.
It will:

1. `cd` to the bot folder
2. Stop any v23 / v18 / v15 dry-run that may still be alive
3. Pull v30 from GitHub
4. Confirm Python deps are still good
5. Run the v30 smoke-test (offline; no broker contact)

```powershell
# ============================================================
# v30 VPS UPDATE  -- safe to re-run
# ============================================================
Set-Location "C:\PropBot"               # <-- adjust ONLY if your bot lives elsewhere

# 1. kill any older bot process (v23/v18/v15) so it can't fight v30
Get-Process python -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'run_v\d+_live\.py' } |
    Stop-Process -Force -ErrorAction SilentlyContinue
Write-Host ">>> old bot processes stopped (if any)" -ForegroundColor Cyan

# 2. pull v30
git fetch --all --prune
git reset --hard origin/main
git --no-pager log -1 --oneline

# 3. (re)install deps - cheap and idempotent
python -m pip install -r requirements.txt --quiet

# 4. offline smoke test - MUST end with "[OK] SMOKE OK"
python Scripts\smoke_v30_live.py
```

You should see the smoke-test finish with the green line:

```
[OK] SMOKE OK — imports, state, sizer, no-chase cooldown, slippage all green.
```

If you do **not** see that line, **STOP**. Don't start the live process.
Send me whatever the smoke test printed and I'll fix it.

---

## Step 2 — Start v30 in DRY-RUN

Make sure your MT5 terminal is open and `SHF_Bridge.mq5` is attached to a
chart and showing **Input: AutoTrading ON** (smiley face). Then in the
same PowerShell window:

```powershell
.\GO_DRYRUN_V30.ps1
```

Expected banner:

```
================================================================
  v30 DRY-RUN  (4-pair ORB + Merton-GZ + news + 4pct DD rails)
  v25.1 ship config: risk=0.170%  nochase=300s  +slippage tracker
  Strategy has ZERO real-order side effects in this mode.
================================================================
```

then within ~5 seconds you'll see:

```
[v30] CONFIG  risk=0.170%  cap_mult=5.0x  max_per_trade=0.850%
[v30] CONFIG  nochase_cd=300s  magic=30000  comment=SHF_v30
[v30] CONFIG  daily_halt=4.0%  dd_breaker=4.0%  account_kill=8.0%
[v30] WARMUP  pulling 2880 M1 bars/symbol from broker ...
```

then a heartbeat every 60 s. Leave it running.

---

## Step 3 — Sanity-check the heartbeat

Within the first heartbeat you should see, near the bottom:

```
SLIPPAGE (entry fills, ticks; +ve = worse fill):
  slippage tracker idle  (no entries yet)
```

That `idle` line means the tracker is wired and waiting for the first
trade. Once the first ORB entry fires (08:00 UK for DE40, 14:30 UK for
US30/XAUUSD/US500), you'll see:

```
[ENTRY] DE40 LONG  lots=0.300  intended=17500.00  fill=17500.00
       slip=+0.00t($+0.00)  SL=...  TP1=...  TP2=...  risk=$170
[SLIP]  DE40 LONG  intended=17500.00000  fill=17500.00000  slip=+0.00t  $+0.00
```

In dry-run `intended == fill` by definition (no real broker fill), so
slip will always read `+0.00t`. **That is correct.** The point of
dry-run is to verify the wiring; real numbers come once you flip to
`GO_LIVE_V30.ps1`.

---

## Step 4 — On Monday, after a clean dry-run trading day

If by Monday EOD the dry-run has run cleanly through both ORB sessions
without errors, you can flip to live:

```powershell
# stop the dry-run window (Ctrl+C in that PowerShell)
.\GO_LIVE_V30.ps1
```

The first **live** entry's `[SLIP]` line will tell you the actual
broker slippage. Watch closely. Anything <= ~3 ticks is within the
v25.1 cost model. >5 ticks = take a screenshot and ping me.

---

## Rollback

If anything goes wrong with v30, falling back to v23 is one command:

```powershell
# stop v30
Get-Process python | Where-Object { $_.CommandLine -match 'run_v30_live\.py' } |
    Stop-Process -Force
# restart v23
.\GO_DRYRUN_V23.ps1
```

v23 is still in the repo and untouched. v30 is purely additive.

---

## What's different in v30 vs v23 (one-glance)

| | v23 (old) | **v30 (new)** |
|---|---|---|
| Risk per trade | 0.110 % | **0.170 %** |
| Per-trade $ cap | 0.55 % | **0.85 %** |
| No-chase cross-symbol cooldown | OFF | **300 s** |
| Per-trade slippage logging | none | **JSONL + heartbeat + per-symbol stats** |
| Magic | 23000 | 30000 |
| Comment tag | SHF_v23 | SHF_v30 |
| Sizer / DD rails / news / OR anchors | unchanged | unchanged |
| MQL5 EA | no change | no change |

Backtest-projected impact (3-month real 5ers data):

* Net P&L: $16,977 → **$27,668** (+62.9 %)
* Max DD: 3.35 % → **3.16 %** (improved)
* PF: 1.94 (unchanged), Sharpe: 5.66 (unchanged)
