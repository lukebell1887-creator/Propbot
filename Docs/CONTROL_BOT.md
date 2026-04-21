# PropBot v15 — Control Cheat Sheet

4 scripts in the repo root — that's all you need. Run them from PowerShell
with `.\SCRIPT.ps1`.

---

## 🟢 `GO_LIVE.ps1` — START live trading

```powershell
.\GO_LIVE.ps1                 # Phase B  → 0.5 × risk (default, 0.25%/trade)
.\GO_LIVE.ps1 -Risk 1.0       # Phase C  → full risk (0.5%/trade)
.\GO_LIVE.ps1 -Risk 0.25      # Ultra-safe first day (0.125%/trade)
```

Does everything automatically:
1. Kills any previous Python / MT5
2. `git pull` for latest code
3. Launches MT5 (auto-login, auto-attach EA)
4. Waits 20 s for MT5 login
5. Runs the **full pre-flight check** (✅/❌ for every safety layer)
6. Starts the engine in LIVE mode

If any pre-flight check fails → aborts before any order can be sent.

---

## 🟡 `GO_DRYRUN.ps1` — START safety-only test run

```powershell
.\GO_DRYRUN.ps1
```

Identical to `GO_LIVE` but sends **no** real orders — decisions logged only.
Use this to sanity-check plumbing after any code change before switching to real money.

---

## 🔴 `STOP_BOT.ps1` — STOP the engine

```powershell
.\STOP_BOT.ps1                # graceful stop, leave MT5 running
.\STOP_BOT.ps1 -AlsoMT5       # also close MT5 (rarely needed)
```

Why leaving MT5 running is safer: open positions keep their
**broker-held SL and TP**. Even if Python dies or VPS loses power,
5%ers' server closes the position at your SL. Closing MT5 cancels
pending orders but keeps filled positions with their stops in place.

---

## 🔍 `STATUS.ps1` — CHECK health & tail logs

```powershell
.\STATUS.ps1                  # one-shot snapshot (process state + last 50 log lines)
.\STATUS.ps1 -Tail            # one-shot snapshot + live-tail the log (Ctrl-C to exit)
```

Outputs:
- ✅/❌ for python engine + MT5 running
- Log file age (should be <120 s if engine is healthy)
- Total trade count from `v15_live_trades.jsonl`
- Most recent `HEARTBEAT` line (shows current equity + running stats)
- Colour-coded last 50 log lines (red=ERROR, yellow=WARN, green=OK, grey=info)

---

## Daily workflow

### Morning check
```powershell
.\STATUS.ps1
```
If log age >120 s or engine missing → restart with `.\GO_LIVE.ps1`.

### Weekend (5%ers markets closed)
```powershell
.\STOP_BOT.ps1                # pause
# Monday 01:00 UTC:
.\GO_LIVE.ps1                 # resume
```

### Upgrade code
```powershell
.\STOP_BOT.ps1
git pull                      # GO_LIVE also does this but manual is safer
.\GO_LIVE.ps1                 # pre-flight will catch anything broken
```

### Monitor during live session (read-only, non-intrusive)
Open a **second** PowerShell window and run:
```powershell
.\STATUS.ps1 -Tail
```
The engine terminal keeps running, this second one streams the same log.

---

## Phase ladder

| Phase | risk-scale | command |
|---|---:|---|
| A — Dry-run  | 0.50 (ignored in dry-run) | `.\GO_DRYRUN.ps1` |
| B — Half size | 0.50 | `.\GO_LIVE.ps1` |
| C — Full size | 1.00 | `.\GO_LIVE.ps1 -Risk 1.0` |

Graduation from B→C: **30 live trades** with measured $/lot slippage **≤ $1.50** and session **PF > 3**. Check via:
```powershell
python Scripts\audit_v15_costs.py         # analyses live trade log
```

---

## Safety layers (shown in pre-flight at every boot)

1. ✅ Broker-held hard SL sent with every order (survives Python/VPS death)
2. ✅ TP sent with every order
3. ✅ Dynamic SL trailing (Python → broker `modify_position`)
4. ✅ Per-symbol ATR-sized stop (from v15 optimiser)
5. ✅ Account-level 8 % kill switch (≈ $8 000 ≤ 5%ers $10 000 limit)
6. ✅ Engine-level 4 % daily halt (5%ers daily-DD rule)
7. ✅ EA dead-Python failsafe — closes all magic-15000 positions after 30 s disconnect
8. ✅ EA magic-number filter — never touches manual / other-EA trades

If ANY of these is missing, pre-flight prints ❌ next to it and the engine refuses to start.
