# 🚀 VPS GO-LIVE — DO THIS NOW

One-page checklist for getting v15 live 24/7 on your Contabo VPS
(`158.220.91.19`). You are done with bootstrap; this is the last mile.

---

## What you still need to do (in order)

### 1 — Pull the 3 new files I just created from GitHub

Already on VPS, in PowerShell (you don't need admin for this):

```powershell
cd C:\PropBot
git pull
```

This pulls:

- `start_live.bat`              ← new v15 watchdog launcher (auto-restart on crash)
- `SETUP_VPS_AUTOSTART.ps1`     ← one-shot Task Scheduler + power-config installer
- `Scripts\run_v15_live.py`     ← the live runner that uses `v15_ultimate_tuning.json`
- `Results\v15_ultimate_tuning.json`  ← per-symbol optimised params

---

### 2 — Install MT5 from the 5%ers dashboard

1. Log in: <https://www.the5ers.com/>  →  **Client Area**  →  **Download MT5**.
2. Install MT5 on the VPS (defaults are fine).
3. In MT5 → `File → Login to Trade Account`:

   | Field    | Value                    |
   |----------|--------------------------|
   | Login    | **26059964**             |
   | Password | (from 5%ers email)       |
   | Server   | **FivePercentOnline-Real** |

4. In MT5, `Tools → Options → Expert Advisors`:
   - [x] **Allow algorithmic trading**
   - [x] **Allow WebRequest for listed URL:** `http://127.0.0.1:5555`

5. `Right-click Market Watch → Show All`. Confirm these 5 symbols visible:
   - **US30**, **NAS100**, **SP500**, **DAX40**, **XAUUSD**
   - (5%ers sometimes prefixes them `#US30` or similar — no action needed as long
     as Market Watch shows ticks. The engine auto-detects suffix/prefix.)

---

### 3 — Install and attach the SHF_Bridge EA

1. In MT5: `File → Open Data Folder`  →  you'll see a MetaTrader folder.
2. Go to `MQL5\Experts\` inside that data folder.
3. Copy `C:\PropBot\MQL5\Experts\SHF_Bridge.mq5` into it.
4. In MT5 press `F4` (MetaEditor opens) → open `SHF_Bridge.mq5` → press `F7` to compile.
   - Expect: `0 error(s), 0 warning(s)`.
5. Back in MT5 → double-click `SHF_Bridge` in the *Navigator* pane (Experts) →
   attach to any chart (e.g. US100 M1).
6. In the EA dialog → *Common* tab → [x] Allow DLL imports → OK.
7. Click the big green **AutoTrading** button in the toolbar — it turns GREEN.
8. On the chart you'll see a smiley face ☺ in the top-right corner of the EA
   label → bridge is running.

---

### 4 — Run the smoke test (no real orders)

```powershell
cd C:\PropBot
.\.venv\Scripts\Activate.ps1
python Scripts\smoke_v15_live.py
```

Expect: `🟢 SMOKE TEST PASSED` at the bottom. This verifies:

- ZMQ bridge connects on `127.0.0.1:5555`
- MT5 symbols resolve
- Engine loads `v15_ultimate_tuning.json`

If smoke fails → the error message tells you which of steps 1-3 is off.

---

### 5 — Register the autostart scheduled task (one-time, ADMIN)

Open PowerShell **AS ADMINISTRATOR** (right-click PowerShell → Run as admin):

```powershell
cd C:\PropBot
.\SETUP_VPS_AUTOSTART.ps1
```

This does 3 things:
1. Disables VPS sleep / hibernate / disk-timeout (Windows Server 2022 needs this).
2. Creates Task Scheduler entry `PropBot_v15_live` that:
   - Starts 1 minute after **every Windows boot**
   - Also starts when you log in via RDP
   - Restarts automatically if it ever crashes (x9999)
3. Allows inbound TCP 5555 on loopback (127.0.0.1 only) for the EA bridge.

Verify it's registered:

```powershell
schtasks /Query /TN PropBot_v15_live /V /FO LIST | findstr /C:"Task To Run" /C:"Next Run Time" /C:"Status"
```

---

### 6 — First launch (DRY-RUN for 24 h)

Edit `start_live.bat` one-time change: change the line

```
set RISK_SCALE=0.5
```

to

```
set RISK_SCALE=0
```

then save. `RISK_SCALE=0` = dry-run mode (no live orders, full logging).

Now launch:

```powershell
schtasks /Run /TN PropBot_v15_live
```

Tail the log to watch it:

```powershell
Get-Content logs\live_(Get-Date -Format yyyy-MM-dd).log -Wait -Tail 40
```

Let it run for 24 h. You should see:
- Bar ticks every minute
- Maybe 5-20 "SIGNAL …" lines (one every 1-3 hours on active symbols)
- Each signal followed by `DRY-RUN: would have placed order …`
- **NO actual trades in MT5**.

---

### 7 — Phase B: GO LIVE at half size

After 24 h dry-run with no errors:

```powershell
schtasks /End /TN PropBot_v15_live     # stop current dry-run
notepad start_live.bat                  # change back to: set RISK_SCALE=0.5
schtasks /Run /TN PropBot_v15_live     # restart in live mode
```

`RISK_SCALE=0.5` = 0.25 % risk per trade = ~22 lots avg (half of the backtest's 43).
This halves slippage sensitivity — recommended for the first 30 live trades.

---

### 8 — Phase C: scale up after 30 trades

After ~30 trades (typically 4-5 trading days):

```powershell
python Scripts\analyze_live_trades.py    # shows live PF, avg slippage $/lot
```

If live PF > 3 **AND** avg slippage ≤ $1.50/lot:

```powershell
schtasks /End /TN PropBot_v15_live
notepad start_live.bat                   # set RISK_SCALE=1.0  (full size)
schtasks /Run /TN PropBot_v15_live
```

---

## 🚨 Kill switch

If anything ever looks wrong:

```powershell
schtasks /End /TN PropBot_v15_live      # stops the watchdog
taskkill /F /IM python.exe               # force-kills any running engine
# then in MT5: turn OFF AutoTrading button (it goes RED/grey)
```

Both must happen — the bot won't be able to place orders with either one off.

---

## ✅ Bulletproof checklist (validate once)

Tick each of these after setup — if any is **NO**, the bot will NOT run 24/7:

- [ ] `git pull` succeeded; `start_live.bat`, `SETUP_VPS_AUTOSTART.ps1` and
      `Scripts\run_v15_live.py` all exist under `C:\PropBot\`.
- [ ] MT5 is **logged in** to account 26059964 on **FivePercentOnline-Real**.
- [ ] Market Watch shows live ticks for US30 / NAS100 / SP500 / DAX40 / XAUUSD.
- [ ] `SHF_Bridge` EA attached to at least one chart, AutoTrading = GREEN.
- [ ] `python Scripts\smoke_v15_live.py` → `🟢 SMOKE TEST PASSED`.
- [ ] `schtasks /Query /TN PropBot_v15_live` → shows *Ready* or *Running*.
- [ ] `powercfg /query SCHEME_CURRENT | findstr /i "Sleep Standby"`
      → shows *0* for "AC Power Setting Index" (no sleep).
- [ ] `start_live.bat` has the right `RISK_SCALE` for the phase you want.
- [ ] Reboot VPS once → log back in after 3 min → `tasklist | findstr python.exe`
      shows python running (proves boot-trigger works).

---

## Quick reference — log & status commands

```powershell
# where am I?
cd C:\PropBot

# is engine running?
tasklist | findstr python.exe

# tail today's log
Get-Content logs\live_$(Get-Date -Format yyyy-MM-dd).log -Wait -Tail 40

# list scheduled task
schtasks /Query /TN PropBot_v15_live /V /FO LIST

# manual start / stop
schtasks /Run /TN PropBot_v15_live
schtasks /End /TN PropBot_v15_live

# update code + restart
schtasks /End /TN PropBot_v15_live
git pull
schtasks /Run /TN PropBot_v15_live
```

---

## Troubleshooting

| Symptom                                | Fix                                       |
|----------------------------------------|-------------------------------------------|
| `smoke_v15_live.py` fails "ZMQ connect refused" | EA not attached / AutoTrading OFF |
| Symbols missing in log                 | Market Watch doesn't show all 5 symbols   |
| Engine loop-crashes every 30 s         | `python Scripts\run_v15_live.py --dry-run` manually to see real traceback |
| No trades after 24 h live              | Check log for "SIGNAL" lines — if none, market is in a slow regime; that's NORMAL |
| Bot stops when I close RDP             | You skipped SETUP_VPS_AUTOSTART.ps1 (logon-trigger + boot-trigger ensure it keeps running) |
| Windows updates reboot the VPS         | BootTrigger restarts bot 1 min after reboot — no action needed |

---

**Total time to complete once you RDP in: ~20 minutes.**
**After step 5, the bot survives reboots, RDP disconnects, and Python crashes — zero manual intervention needed.**
