# Deploy v15 live to fresh VPS — step-by-step

**Account**: 5%ers #26059964 (FivePercentOnline-Real) — $100K High Stakes 1st Eval
**VPS**: 158.220.91.19 (Administrator)
**Deadline**: must place ≥1 live trade within 30 days (to avoid inactivity re-closure)

---

## Phase 0 — Smoke test on local PC (5 min)  ✅ DONE

```powershell
cd "c:\Users\lukeb\OneDrive\Desktop\New folder\PropBot"
python Scripts\smoke_v15_live.py
```

Expected: `🟢 SMOKE TEST PASSED` — all 5 symbols load their per-symbol v15 params, engine builds, bars feed clean. (Already verified 2026-04-21.)

---

## Phase 1 — VPS basic setup (20 min)

### 1a. Connect
```powershell
# Windows + R
mstsc
# Computer: 158.220.91.19
# User:     Administrator
# Password: <the one Contabo emailed you on VPS provisioning>
```

### 1b. Install MT5 (5%ers edition)
1. In the RDP session, open Edge/Chrome.
2. Go to the 5%ers member area → the account #26059964 credentials screen.
3. Click **Windows** under "Account Credentials" → downloads the 5%ers-branded MT5 installer.
4. Run it, accept defaults. Terminal auto-opens and logs into FivePercentOnline-Real.
5. If not auto-logged-in: **File → Login → Account → 26059964 / password / server FivePercentOnline-Real**.

### 1c. Add the 5 symbols
1. **View → Market Watch (Ctrl+M)**
2. Right-click → **Symbols…** (Ctrl+U)
3. Expand tree, find each and double-click / "Show":
   - `US30`
   - `NAS100`
   - `SP500`
   - `DAX40`
   - `XAUUSD`
4. Close. All 5 should now show bid/ask in Market Watch.

### 1d. Place the dummy inactivity trade (NOW, before anything else)
Just to reset the 30-day clock while we finish wiring:
1. Right-click `XAUUSD` → **New Order**.
2. Type: **Market execution**, volume: **0.01 lots**, click **Sell**.
3. Wait 2 seconds, close the trade immediately. Max loss on 0.01 lot of XAUUSD over 2s is ~$0.50.

That alone satisfies "resume trading" and buys you 30 more days of breathing room.

---

## Phase 2 — VPS tools (15 min)

### 2a. Install Python 3.11
```powershell
# In an Administrator PowerShell on the VPS:
winget install -e --id Python.Python.3.11 --silent --accept-source-agreements --accept-package-agreements
# Log out of RDP and back in so PATH refreshes (or open a new PowerShell)
python --version     # should print 3.11.x
```

### 2b. Install Git
```powershell
winget install -e --id Git.Git --silent
```

### 2c. Clone PropBot
```powershell
cd C:\
mkdir PropBot
cd PropBot
git clone https://github.com/lukebell1887-creator/PropBot.git .
pip install -r requirements.txt
```

### 2d. Smoke test on VPS
```powershell
python Scripts\smoke_v15_live.py
```
Must end `🟢 SMOKE TEST PASSED`. If not, stop and ping me.

---

## Phase 3 — MT5 bridge EA (10 min)

### 3a. Compile & attach SHF_Bridge.mq5
1. In MT5: **File → Open Data Folder** → `MQL5\Experts\`
2. Copy `C:\PropBot\MQL5\Experts\SHF_Bridge.mq5` into that folder.
3. In MT5: **View → Navigator (Ctrl+N)** → Expert Advisors → right-click `SHF_Bridge` → **Modify** → **F7** to compile. Should say "0 errors".
4. Allow DLL imports:
   - **Tools → Options → Expert Advisors**
   - ✅ Allow automated trading
   - ✅ Allow DLL imports
   - ✅ Allow WebRequest for listed URL (add `http://127.0.0.1`)
5. Drag `SHF_Bridge` onto **any chart** (doesn't matter which symbol; any 1-minute chart is fine).
6. In the dialog: **Common** → ✅ Allow modification of Signal settings / Allow live trading.  **Inputs** → leave defaults (port 9090).
7. Click OK. The chart should show a little smiley face ☺ top-right = EA is running.

### 3b. Test the bridge
In a VPS PowerShell:
```powershell
cd C:\PropBot
python Scripts\test_tcp_bridge.py
```
Should print `✅ Connected. Account #26059964 balance=$100,000.00`.

---

## Phase 4 — Go live in DRY-RUN first (2 min)

**Still on the VPS:**
```powershell
cd C:\PropBot
python Scripts\run_v15_live.py
```

No flags = dry-run by default. The bot:
- Connects to MT5 bridge ✅
- Verifies all 5 symbols available ✅
- Loads per-symbol v15 params ✅
- Subscribes to live M1 bars ✅
- Logs every signal it *would* take, **does NOT send orders** 🟡
- Heartbeats every 60s

Leave it running 24 hours. Check `Results\v15_live.log` for `SIGNAL` lines.

**Expected**: 1-5 SIGNAL lines per day (quiet strategy — highly selective).

---

## Phase 5 — Go live at 0.5× risk (Phase B)

Once you see clean SIGNAL lines in dry-run and trust the plumbing:

```powershell
# From the VPS:
cd C:\PropBot
python Scripts\run_v15_live.py --live --risk-scale 0.5
```

This places real orders at **half normal size** (0.25 % risk per trade instead of 0.5 %). Your worst single trade loses ~$250 on a $100K account (0.25 %). Your worst simultaneous DD across 3 positions ≈ 0.75 %.

**Kill-switches active:**
- Engine: 4 % daily DD halt, 5 % total DD halt
- v15 runner: **8 % account-level hard kill** (closes ALL positions, halts permanently)
- 5%ers firm: 5 % daily blow, 10 % total blow

Our 8 % is 2 % *below* the firm's 10 % — so even if everything else fails, you stay funded.

### Target for Phase B → Phase C
Stay at risk-scale 0.5 until you have **30 live trades** and the live PF > 3 (vs backtest PF ≈ 7). Then bump to `--risk-scale 1.0` for full size.

---

## Phase 6 — Auto-restart on VPS reboot (5 min, optional)

Create a scheduled task so the bot restarts if the VPS reboots (Contabo maintenance etc.):

```powershell
$action  = New-ScheduledTaskAction -Execute "python.exe" -Argument "C:\PropBot\Scripts\run_v15_live.py --live --risk-scale 0.5" -WorkingDirectory "C:\PropBot"
$trigger = New-ScheduledTaskTrigger -AtLogOn
$pr      = New-ScheduledTaskPrincipal -UserId "Administrator" -RunLevel Highest
Register-ScheduledTask -TaskName "SmartBB_v15_Live" -Action $action -Trigger $trigger -Principal $pr
```

Also run `C:\PropBot\PREVENT_SLEEP.ps1` (already in the repo) once per RDP session to keep MT5 alive.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Could not connect to MT5 bridge` | SHF_Bridge EA not running on any chart. Drag it onto one; check the ☺ face. |
| `Broker is missing these symbols` | Not all 5 added to Market Watch. Re-do 1c. |
| `No TIER1 params found` | `Results/v15_ultimate_tuning.json` not present on VPS. Re-clone or `git pull`. |
| SIGNAL lines but order REJECTED | Check "Allow live trading" in EA properties AND Tools → Options → Expert Advisors. |
| Engine halts permanently after small DD | Either `base_risk_pct` is wrong or broker account equity is syncing wrong. Check logs. |

---

## Daily monitoring (2 min/day)

```powershell
# Tail log in real time
Get-Content C:\PropBot\Results\v15_live.log -Wait -Tail 40
```

Look for: heartbeat line every 60s, `SIGNAL`/`OPENED`/`CLOSED` lines, no repeating `ERROR`.

Trade log (machine-readable):
```powershell
Get-Content C:\PropBot\Results\v15_live_trades.jsonl
```
