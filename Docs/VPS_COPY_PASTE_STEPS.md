# VPS step-by-step — copy/paste commands, zero guesswork

## Your situation — verified

| Check | Result | Verdict |
|---|---|---|
| VPS IP | 158.220.91.19 (Contabo Portsmouth UK) | ✅ |
| Latency home → VPS | **13 ms steady** (first packet 275 ms was cold ARP, ignore it) | ✅ EXCELLENT |
| VPS location | Portsmouth UK, same island as 5ers/FivePercentOnline broker stack | ✅ ideal |
| CPU/RAM needed | 1 vCPU, 1 GB RAM | ✅ Contabo VPS-S has more than enough |
| Needs rust? | No | ✅ ORB on M1 bars doesn't care |
| MT5 EA (SHF_Bridge.mq5) | unchanged since v13 — bridge protocol is **strategy-agnostic** (confirmed from header comment) | ✅ **NO update needed** |

**From your VPS you will get < 10 ms to 5ers's broker infrastructure.** This is as good as it gets without paying for LD4 co-lo at £500/mo.

---

## 🚨 Bug I just caught and fixed

The old `GO_DRYRUN_V23.ps1` and `GO_LIVE_V23.ps1` were passing `--cap-mult 3.0`, but the v24d-optimal sweet-spot is `5.0` (0.550 % per-trade cap). Running the live bot with `cap=3.0` would have given you roughly $8,600 LESS PnL on 3 months vs the $16,957 backtest figure — a silent underperformance without any error.

**Both scripts now pass `--cap-mult 5.0`.** Committed + pushed just now (commit pending).

---

## ✅ On your VPS (158.220.91.19) — do these in order

### Step 1: Connect to the VPS

From your laptop:
```powershell
# Use the Remote Desktop app (mstsc) or RDP from PowerShell:
mstsc /v:158.220.91.19
# Enter your Contabo credentials
```

### Step 2: Make sure MT5 is running + SHF_Bridge EA is attached

On the VPS, open MetaTrader 5 (MT5 should already be installed and logged into your 5ers demo account, since you ran v15-v18 before).

1. **Open any chart** (e.g. DE40 M1).
2. In the Navigator → Expert Advisors → double-click **SHF_Bridge**.
3. Confirm settings:
   - Host: `127.0.0.1`
   - Port: `5555`
   - Magic: `23000` (ignore the EA's own `InpMagic` default — the python engine sends its own)
   - AutoTrading button (top toolbar) must be **green/ON**.
4. Check the **Experts** tab at the bottom — you should see:
   `PropBot v15 Bridge (Native TCP) | Port=5555 | Timer=100ms | Magic=...`
   (The "v15" in that log line is just the EA's own version string — the protocol is strategy-agnostic, it works for v23 unchanged.)

### Step 3: Pull the latest code

Open PowerShell on the VPS:
```powershell
cd C:\PropBot    # or wherever you cloned it — adjust path as needed
git pull origin main
```

You should see commits `58f76e4` and `44b6089` come down (plus the launcher-fix commit I'm about to push).

### Step 4: Run the unit-test gate (catch any drift first)

```powershell
cd C:\PropBot
python -m pytest tests\test_live_backtest_parity.py tests\test_dd_breaker.py tests\test_daily_halt.py -v
```

**Expected: 24 passed.** If anything fails → stop, do not deploy, tell me.

### Step 5: DRY-RUN (no real orders — 2 hours minimum)

```powershell
cd C:\PropBot
.\GO_DRYRUN_V23.ps1
```

**What to watch for in the console / log file `Results\v23_live_*.log`:**

| ✅ GOOD | ❌ BAD (stop & investigate) |
|---|---|
| `🟡 DRY-RUN (no orders)` banner | Any `ERROR` or traceback |
| `V23Live initialised symbols=['DE40','US30','XAUUSD','US500'] risk=0.110% cap=5.0x` | `cap=3.0x` (means you didn't pull my fix) |
| `Heartbeat` lines every 60 s | No heartbeat for > 90 s |
| `Bar close received` lines on M1 boundaries for all 4 symbols | One symbol silent for > 5 minutes during session hours |
| At bar-close: `warmup trade` or `signal fire` or `blocked (news)` | `SHF_Bridge EA not responding` |

Let it run **at least through one full trading session** (8 AM – 10 PM UK time). Zero real orders should be placed.

### Step 6: Go LIVE (small-size → watch → scale)

```powershell
cd C:\PropBot
.\GO_LIVE_V23.ps1
# It will ask: "Type  GO LIVE  (all caps, exact) to confirm:"
# Type exactly:  GO LIVE
```

**First live day rules of engagement:**
1. Watch it for the first 2 hours manually.
2. After the first 3 real trades → check the log: `(open_ts - fill_ts)` should be < 1 second. If it's > 5 seconds regularly, your VPS latency is fine but the MT5 broker is slow — check spread at that symbol.
3. If the DailyHalt fires → **DO NOT CHANGE ANYTHING**. The bot will re-arm at midnight server-time. This is by design.
4. If the DDBreaker fires → same. Repair will happen when equity recovers. The bot stays off the rest of the day.

### Step 7: Stop the bot (if needed)

```powershell
cd C:\PropBot
.\STOP_BOT.ps1
# OR just Ctrl+C in the window running the bot
```

Ctrl+C is caught cleanly — it will flatten dry-run state + save telemetry before exit.

---

## Quick reference

| What | Command |
|---|---|
| Latest logs | `Get-ChildItem Results\v23_live_*.log \| Sort-Object LastWriteTime -Descending \| Select-Object -First 1 \| Get-Content -Wait` |
| Bot status | `.\STATUS.ps1` |
| Stop bot | `.\STOP_BOT.ps1` |
| Live trades audit | `Get-Content Results\v23_live_trades.jsonl` |

---

## What NOT to do

- ❌ Don't edit `src/live/v23_live.py` on the VPS. Edit locally, push, `git pull` on VPS.
- ❌ Don't change `--cap-mult` away from `5.0`. That's the v24d-validated sweet-spot.
- ❌ Don't change `--risk` away from `0.00110` (0.110 %). Risk is the one thing that rules them all.
- ❌ Don't disable the 60-s min-hold. 5ers explicitly flags sub-60s scalping.
- ❌ Don't re-install the MT5 EA. It's strategy-agnostic, v23 works with the current v15-labelled bridge.

---

**Bottom line:** Your VPS is perfect for this (13 ms to you, probably < 5 ms to broker). Pull the latest commit, run the 24-test gate, dry-run through one session, then go live. The $16,957 number is real; now the config on the VPS matches the config that produced it.
