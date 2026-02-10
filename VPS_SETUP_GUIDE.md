# SHF v5.6 — VPS Setup Guide (Step by Step)

**Date:** February 10, 2026  
**VPS:** 78.141.192.253 (Vultr London)  
**Goal:** Nuke old bot, deploy v5.6 fresh, test everything

---

## PHASE 1: Connect to VPS with Drive Sharing

### Step 1: Open Remote Desktop Connection

1. On your PC, press **Windows key**, type **Remote Desktop Connection**, press Enter
2. The RDP window opens

### Step 2: Enable Drive Sharing (CRITICAL — do this BEFORE connecting)

1. In the RDP window, click **"Show Options"** (bottom left)
2. Click the **"Local Resources"** tab at the top
3. In the "Local devices and resources" section, click **"More..."**
4. Tick the checkbox next to **"Drives"** (this shares your C: drive with the VPS)
5. Click **OK**

### Step 3: Connect to the VPS

1. Click the **"General"** tab
2. In **"Computer"** field, type: `78.141.192.253`
3. In **"User name"** field, type: `Administrator`
4. Click **"Connect"**
5. Enter your VPS password when prompted
6. If you get a certificate warning, click **"Yes"** to connect anyway
7. You should now see the VPS desktop

---

## PHASE 2: Nuke Old Bot & Deploy v5.6

### Step 4: Open PowerShell as Administrator

1. On the VPS desktop, right-click the **Start button** (bottom left Windows icon)
2. Click **"Windows PowerShell (Admin)"** or **"Terminal (Admin)"**
3. If prompted "Do you want to allow this app to make changes", click **Yes**
4. You should see a blue PowerShell window

### Step 5: Open the deployment script

1. In the PowerShell window, type this command and press Enter:

```powershell
notepad "\\tsclient\C\Users\lukeb\OneDrive\Desktop\PropBot\DEPLOY_VPS_FRESH.ps1"
```

2. Notepad opens with the deployment script
3. Press **Ctrl+A** to select all text
4. Press **Ctrl+C** to copy

### Step 6: Run the deployment script

1. Click back on the **PowerShell window**
2. **Right-click** anywhere in the PowerShell window (this pastes the script)
3. Press **Enter** to run it
4. Watch the output — it will show progress through 9 steps:

```
[0/9] Checking mapped drive...        ← Verifies your PC drive is accessible
[1/9] NUKING old bot at C:\SHF...     ← Deletes everything old
[2/9] Creating directory structure...  ← Fresh folders
[3/9] Copying SHF v5.6 files...       ← Copies all your code
[4/9] Verifying file integrity...      ← Checks for corruption
[5/9] Checking Python installation...  ← Finds or installs Python
[6/9] Installing Python dependencies...← numpy, numba, pyzmq, etc.
[7/9] Smoke testing Rust core...       ← Tests shf_core.pyd works
[8/9] Smoke testing Python modules...  ← Tests all imports work
[9/9] MT5 Expert Advisor setup...      ← Copies EA to MT5 folder
```

5. **Expected result:** All green "OK" messages and "SHF v5.6 DEPLOYMENT COMPLETE"

### Step 7: Troubleshooting (if something goes wrong)

**If Step 0 fails ("Cannot access mapped drive"):**
- You forgot to enable Drive Sharing in Step 2
- Close RDP, go back to Step 2, enable Drives, reconnect

**If Step 5 fails ("Python 3.10+ not found"):**
- The script will try to install Python automatically via Chocolatey
- If that fails, manually install Python: open a browser on the VPS, go to python.org, download Python 3.12, install it (tick "Add to PATH" during install)

**If Step 7 fails ("Rust core smoke test failed"):**
- The shf_core.pyd was compiled for a different Python version
- Check what Python version is on the VPS: `python --version`
- The .pyd needs Python 3.10 or higher

---

## PHASE 3: Set Up MetaTrader 5

### Step 8: Install MT5 (if not already installed on VPS)

1. If MT5 is already installed on the VPS, skip to Step 9
2. If not, open a browser on the VPS and go to:
   `https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe`
3. Download and run the installer
4. Follow the setup wizard (click Next/Install)

### Step 9: Log in to your broker account

1. Open MetaTrader 5 on the VPS
2. Go to **File → Open an Account** (or **File → Login to Trade Account**)
3. Search for your broker (e.g., "The 5%ers", "FXIFY", etc.)
4. Enter your account credentials (login number + password + server)
5. Click **OK** — you should see your balance in the bottom bar

### Step 10: Copy the EA to MT5

The deployment script tried to do this automatically. Check if it worked:

1. In MT5, press **Ctrl+N** to open the Navigator panel (left side)
2. Expand **Expert Advisors**
3. Look for **SHF_ZMQ_Bridge**

**If it's NOT there, copy manually:**

1. In MT5, press **F4** to open MetaEditor
2. In MetaEditor, click **File → Open**
3. Navigate to: `C:\SHF\MQL5\Experts\SHF_ZMQ_Bridge.mq5`
4. The file opens — now click **File → Save As**
5. Save it to the default Experts folder that MetaEditor shows
6. Close the file

### Step 11: Compile the EA

1. In MetaEditor (press F4 in MT5 if not open), open SHF_ZMQ_Bridge.mq5
2. Press **F7** to compile
3. Look at the bottom panel — it should say **"0 errors"**
4. If there are errors about ZMQ includes missing, you need to install ZMQ libraries for MQL5 (the EA needs `Zmq/` headers in the MQL5/Include folder)

### Step 12: Enable AutoTrading & DLL imports

1. In MT5 (not MetaEditor), go to **Tools → Options**
2. Click the **"Expert Advisors"** tab
3. Tick: **☑ Allow algorithmic trading**
4. Tick: **☑ Allow DLL imports** (needed for ZMQ)
5. Click **OK**
6. In the toolbar at the top, make sure the **"AutoTrading"** button is GREEN (click it to toggle)

### Step 13: Attach the EA to a chart

1. In MT5, open any chart (e.g., right-click a symbol → Chart Window)
2. In the Navigator panel (Ctrl+N), expand **Expert Advisors**
3. **Drag SHF_ZMQ_Bridge** onto the chart
4. A dialog pops up:
   - On the **"Common"** tab: tick **☑ Allow DLL imports** and **☑ Allow algo trading**
   - Click **OK**
5. You should see a smiley face 😊 in the top-right corner of the chart (means EA is running)
6. In the **"Experts"** tab at the bottom of MT5, you should see messages like:
   ```
   SHF_ZMQ_Bridge: ZMQ REP socket listening on port 5555
   SHF_ZMQ_Bridge: ZMQ PUB socket on port 5556
   ```

---

## PHASE 4: Start the Python Engine

### Step 14: Open a new PowerShell window

1. Right-click the Start button → **Windows PowerShell** (doesn't need to be Admin this time)
2. Or if you still have the one from earlier, that's fine too

### Step 15: Navigate to the bot folder

Type and press Enter:
```powershell
cd C:\SHF
```

### Step 16: Start the trading engine

Type and press Enter:
```powershell
python -m src.engine
```

### Step 17: Watch the startup

You should see output like:
```
SHF v5.6 Engine initialized
  Rust available: True
  HMM available: True
  Dynamic Z: base=2.0, gamma=6.0
  Dynamic Exit Z: base=0.5, gamma=2.0
  Dynamic Dwell: base=60.0s, anchor_H=0.3, range=[30.0s, 300.0s]
  AKAD: base=0.75%, lambda=40.0
Dynamic AKAD initialized (PRIMARY risk calculator)
Rust AKADRiskCalculator initialized (legacy fallback)
Rust CorrelationRiskMonitor initialized (window=200)
FFI contract validated — all Rust getters present
  Index Spread: Rust CointegrationEngine (dynamic_z=True, dynamic_exit_z=True)
  Index Spread: Rust KalmanSentinel (tol=0.15)
  Forex Anchor: Rust CointegrationEngine (dynamic_z=True, dynamic_exit_z=True)
  ...
MT5 connected | Balance: $XXXXX USD
Starting v5.6 trading loop (100ms tick)...
```

**If you see "Failed to connect to MT5":**
- The EA is not running or not attached to a chart (go back to Step 13)
- The ZMQ ports are wrong (default: 5555 for REQ, 5556 for SUB)

### Step 18: Monitor the bot

The engine is now running! Here's how to monitor it:

- **Live logs in the terminal** — you'll see entries/exits/signals in real-time
- **Log file:** `C:\SHF\logs\trading.log` — open with Notepad for full history
- **State file:** `C:\SHF\state\engine_state.json` — current positions and stats

### Step 19: Stop the bot (when needed)

- Press **Ctrl+C** in the PowerShell window where it's running
- The engine will gracefully shut down, save state, and disconnect from MT5

---

## PHASE 5: Future Updates (after making changes locally)

When you change code on your PC and want to push updates to the VPS:

1. RDP to the VPS (with Drive Sharing enabled — Step 2)
2. Open PowerShell as Admin
3. Run:
```powershell
notepad "\\tsclient\C\Users\lukeb\OneDrive\Desktop\PropBot\FIX_VPS.ps1"
```
4. Select all (Ctrl+A), copy (Ctrl+C)
5. Paste into PowerShell (right-click), press Enter
6. This re-syncs all files without nuking everything
7. Then restart the engine: `cd C:\SHF` → `python -m src.engine`

---

## Quick Reference

| What | Command / Location |
|------|-------------------|
| VPS IP | `78.141.192.253` |
| VPS User | `Administrator` |
| Bot folder | `C:\SHF` |
| Start engine | `cd C:\SHF && python -m src.engine` |
| Stop engine | `Ctrl+C` |
| View logs | `notepad C:\SHF\logs\trading.log` |
| Fresh deploy | Paste `DEPLOY_VPS_FRESH.ps1` into VPS PowerShell |
| Quick sync | Paste `FIX_VPS.ps1` into VPS PowerShell |
| MT5 EA | SHF_ZMQ_Bridge (attached to any chart) |

---

## What the Bot Does Once Running

The engine runs a 100ms tick loop monitoring 3 pairs:

1. **US100/DE40** (Index Spread) — tech sentiment mean-reversion
2. **AUDUSD/NZDUSD** (Forex Anchor) — commodity currency pair
3. **EURUSD/GBPUSD** (EUR/GBP Spread) — European currency pair

On each tick it:
- Gets prices from MT5 via ZMQ
- Computes spread Z-scores with Hurst-adaptive thresholds (Rust, ~4μs)
- Checks Kalman Sentinel for regime breaks
- Checks HMM for volatile regime blocking
- Applies Dynamic AKAD risk sizing based on drawdown + win rate
- Applies cross-pair correlation risk reduction
- Enforces dwell (minimum hold time) and re-entry cooldown
- Checks spread blowout filter and staleness guard
- Executes spread trades concurrently (~15ms inter-leg gap)
- Monitors ghost stop (4% daily DD / 9% max DD)

All of this is automatic. You just need to make sure MT5 is running with the EA attached.
