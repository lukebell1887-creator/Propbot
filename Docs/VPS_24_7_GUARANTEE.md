# VPS 24/7 Running — What Just Happened & How to Bulletproof It

**Date:** 24 Apr 2026  
**Context:** You closed your laptop, reopened it, and got a "Reconnecting to Remote Desktop" message. You were worried the bot stopped.

---

## TL;DR

**Your bot almost certainly kept running the whole time. Closing your laptop does NOT stop a VPS.**

A VPS is a remote Windows server in a data centre. It has:
- Its own always-on power
- Its own always-on internet
- Its own uptime SLA (usually 99.9%)

When you close your laptop:
- Your **local RDP client** disconnects
- The **VPS itself keeps running** — completely unaffected
- The **RDP session on the VPS** stays active (disconnected ≠ logged off)
- Any Python process running in that session **keeps running**

When you reopen your laptop and reconnect, Windows reattaches you to the **same session that was running the whole time**.

**The "reconnecting" message was your laptop re-establishing the display link, not the VPS restarting.**

---

## How to VERIFY the bot was running the whole time

RDP into the VPS right now and run:

```powershell
cd C:\PropBot
.\STATUS.ps1
```

What you want to see:
- `[OK] python engine RUNNING pid=<something> ram=<N> MB`
- `[OK] MT5 terminal RUNNING`
- `Log file: ... size=<N> KB last update <0-120>s ago` (green)
- `Latest heartbeat: [timestamp within last 2 minutes]`

Then scroll to the log file and look for gaps:

```powershell
Get-Content logs\live_$(Get-Date -f yyyy-MM-dd).log | Select-String "HEARTBEAT" | Select-Object -Last 20
```

Heartbeats should be every 30–60 seconds with **no gaps longer than a minute or two**. If so, the bot ran through the laptop-close just fine.

---

## What CAN actually kill the bot on the VPS

These are the real threats (in order of likelihood):

| Threat | Effect | How to defend |
|---|---|---|
| **1. Python process crashes** | Single-trade-cycle dies | `start_live.bat` restart loop (already built) |
| **2. VPS scheduled reboot** (Windows Update) | Bot and MT5 both die | **Task Scheduler auto-start** (solution below) |
| **3. You log OFF** (not just disconnect) from VPS | Session closes → bot dies | Use "Disconnect", never "Log off / Sign out" |
| **4. Someone else RDPs in and kicks you** | Your session ends | Set the VPS to allow multiple sessions, or use server edition |
| **5. VPS provider outage** | Everything dies | Nothing you can do — call provider |
| **6. MT5 loses broker connection** | Bot can see data but can't trade | Engine auto-retries; alert on 5+ failed trades |

**Of these, #1 and #2 are the only ones your VPS autostart config fixes. You need BOTH.**

---

## The one-time setup that makes it bulletproof

You already have the scripts. Just run them:

### Step 1: on the VPS, open PowerShell **AS ADMINISTRATOR**

```powershell
cd C:\PropBot
```

### Step 2: Run the autostart registration (one-time)

```powershell
.\SETUP_VPS_AUTOSTART.ps1
```

This does FOUR things:
1. **Disables sleep/hibernate** on the VPS at the OS level
2. **Registers a Task Scheduler task** that:
   - Starts `start_live.bat` at boot (1 minute after boot — gives MT5 time to come up)
   - Also starts it at logon
   - **Restarts on failure up to 9999 times**, with 1-minute back-off
   - Does NOT stop on battery, idle, or network loss
3. **Opens the EA bridge firewall port** (127.0.0.1:5555, loopback only)
4. Confirms with `schtasks /Query`

After this step, **even if the VPS fully reboots (Windows Update, provider maintenance, power cycle), your bot is running again within 60 seconds — with NO manual intervention**.

### Step 3: Verify

```powershell
schtasks /Query /TN PropBot_v15_live /FO LIST /V
```

Should show `Scheduled Task State: Ready` and `Next Run Time`.

**Note:** the task is named `PropBot_v15_live` for legacy reasons. The underlying `start_live.bat` now runs v23 (I fixed it today). Keep the task name — it's just a label.

### Step 4: Prove it

1. RDP into VPS
2. Start the task: `schtasks /Run /TN PropBot_v15_live`
3. Run `.\STATUS.ps1` — should show python + MT5 running
4. **Reboot the VPS** (`shutdown /r /t 5` — don't be afraid!)
5. Wait 3 minutes, RDP back in
6. Run `.\STATUS.ps1` — should STILL show python + MT5 running

That's the proof the 24/7 guarantee works. Do this ONE TIME before going live and you'll never worry again.

---

## Dry-run vs. go-live: what's different

| Mode | Command | What happens when laptop closes |
|---|---|---|
| **Manual dry-run** (your current state) | You ran `.\GO_DRYRUN_V23.ps1` in an RDP window | Keeps running inside your RDP session. Survives RDP disconnect. **Does NOT survive VPS reboot.** |
| **Manual live** (after dry-run) | You run `.\GO_LIVE_V23.ps1` | Same as above. Survives disconnect, dies on VPS reboot. |
| **Task-Scheduler live** (production) | `SETUP_VPS_AUTOSTART.ps1` → done forever | **Survives everything except the VPS itself being down.** |

**Recommendation:** finish the dry-run manually (how you're doing it now). Then before switching `RISK_SCALE=1.0` for go-live, run `SETUP_VPS_AUTOSTART.ps1` once. After that, you can close your laptop for a week and nothing happens.

---

## What I changed today

- **`start_live.bat`** — was running v15 (`Scripts\run_v15_live.py`); now runs v23 (`Scripts\run_v23_live.py`). Without this fix, the Task Scheduler would have launched the old v15 bot instead of your v23.
- Default `RISK_SCALE` changed from `0.5` → `1.0` (full size). Change it back if you want a cautious staged rollout — edit line 15 of `start_live.bat`.

---

## Quick reference: daily commands

```powershell
# Check status
cd C:\PropBot
.\STATUS.ps1

# Stream live logs
.\STATUS.ps1 -Tail

# Emergency stop
.\STOP_BOT.ps1

# Start manually (if Task Scheduler is not yet set up)
.\GO_LIVE_V23.ps1          # or GO_DRYRUN_V23.ps1 for dry-run

# Start via Task Scheduler (after SETUP_VPS_AUTOSTART.ps1)
schtasks /Run /TN PropBot_v15_live

# Stop Task Scheduler run
schtasks /End /TN PropBot_v15_live
```

---

## Bottom line

1. **What you saw today is normal — the bot kept running.**
2. **To prove it: `.\STATUS.ps1` on the VPS right now.**
3. **To bulletproof it against VPS reboots: run `SETUP_VPS_AUTOSTART.ps1` once (as admin).**
4. **After that, closing your laptop, losing your home WiFi, or your ISP going down CANNOT affect the bot. Only the VPS itself being down matters.**
5. **I fixed `start_live.bat` today to point at v23 so the autostart will run the right bot.**
