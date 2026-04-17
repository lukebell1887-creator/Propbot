# GO-LIVE GUIDE — SHF v13 SMART BOLLINGER

**Target:** 5%ers MTB $100 k challenge
**Strategy:** BB(20,2) + Hurst<0.5 + Kalman exit on US100/US500/US30/DE40/USOIL
**Backtest (3 mo real costs):** +12.86 % / PF 2.86 / 60.2 % WR / 1.11 % max DD / 103 trades

---

## 1. What got built for go-live

| Layer | File | Purpose |
|---|---|---|
| MT5 EA (TCP client) | `MQL5/Experts/SHF_Bridge.mq5` v13 | Auto-detects v13 symbols, streams quotes + **new M1 bar closes**, handles ORDER_SEND/MODIFY/CLOSE. Has dead-Python failsafe (auto-close after 30 s silence) and price-refresh retry on requotes. |
| Python bridge | `src/execution/mt5_bridge.py` | TCP server, now dispatches **BarData** to registered handlers (new). |
| v13 engine | `src/smartbb_engine.py` | Unchanged — identical maths to backtest. |
| Live runner | `src/live/smartbb_live.py` | Wraps engine, routes its `_maybe_enter`/`_close`/`_manage` through the bridge. Syncs equity from broker every 2 s. Implements **ghost-halt reconciler** (closes all when engine hits DD limits). |
| Entry script | `Scripts/run_live_smartbb.py` | CLI: `python Scripts/run_live_smartbb.py [--dry-run] [--risk 0.003] [--z-min 3.3]` |

**No math changes between backtest and live.** The live driver intercepts the three engine functions that touch orders and routes them to the broker. The Bollinger calc, Hurst, Kalman, AKAD sizer, amplitude gate are all called from the same code path as the backtest.

---

## 2. Deployment checklist (one-off, ~30 minutes)

### 2.1 VPS provisioning

**Cheapest viable VPS for MT5 + Python** (Windows required for MT5 terminal):

| Provider | Spec | Monthly | Why |
|---|---|---|---|
| **Contabo VPS S Windows** (recommended) | 4 vCPU · 8 GB RAM · 200 GB NVMe · 200 Mbit · London/Germany | **~€7.50 (£6.50)** | Cheapest Windows VPS with real specs. Handles MT5 + Python + ~50 % CPU headroom. |
| **ForexVPS Budget (London)** | 2 vCPU · 3 GB RAM · broker-optimised routing | ~$25 | Expensive but <1 ms latency to most London-hosted brokers. Overkill for v13 since M1-bar strategy. |
| **IC Markets / Pepperstone free VPS** | shared | £0 | Free if deposit > $5 k / 3 lots per month. Won't qualify on a prop firm challenge. |
| **Vultr Windows Cloud Compute (London)** | 2 vCPU · 4 GB · 80 GB | $16 | Hourly billing. Good fallback if Contabo's stock is out. |

**Recommendation: Contabo VPS S Windows (London region).**
v13 trades M1-closed signals and holds on average 0.1 bars — **latency is not a war**. A 50 ms bridge latency is invisible at this frequency. Contabo's 8 GB RAM / 4 vCPU gives comfortable headroom for MT5 (~2 GB), Python engine (~200 MB), and the OS. £6.50/month is the floor; anything cheaper is shared/oversold.

When picking the datacentre, match it to the 5%ers broker server city (`acct.server` in the first bridge connect log). Eightcap (5%ers back-end) is usually in London — pick Contabo **London (uksouth)** or Frankfurt.

### 2.2 VPS setup (Windows server, done once)

```powershell
# 1. Enable RDP and log in.  Install:
#    - MetaTrader 5 (from 5%ers client area — installs the BRANDED build, not generic)
#    - Python 3.11 (from python.org, tick "Add to PATH")
#    - Git for Windows
#    - Visual C++ Redistributable (already on most Windows images)

# 2. Clone the repo:
cd C:\
git clone https://github.com/lukebell1887-creator/PropBot.git
cd PropBot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3. Install the EA:
#    In MT5:  File -> Open Data Folder -> MQL5\Experts
#    Copy SHF_Bridge.mq5 into that folder
#    In MT5 Navigator, right-click SHF_Bridge -> Compile (F7)

# 4. Configure MT5:
#    Tools -> Options -> Expert Advisors:
#      [x] Allow algorithmic trading
#      [x] Allow DLL imports
#      [x] Allow WebRequest for: http://127.0.0.1
#    Tools -> Options -> Server: set GMT+2/+3 as appropriate
#    Add to Market Watch ALL FIVE symbols (right-click in Market Watch -> Show All):
#      US100, US500, US30, DE40, USOIL  (or the broker's equivalents)

# 5. Attach EA to ANY chart (e.g. US100 M1).  Enable AutoTrading (green button top toolbar).
#    You should see in Experts tab:
#      SHF Bridge v5.64 (Native TCP) | Port=5555 | Timer=100ms | Magic=12345
#      Detected 5 symbols: US100, US500, US30, DE40, USOIL
#      Waiting for Python server on 127.0.0.1:5555 ...
```

### 2.3 Start the bot

```powershell
cd C:\PropBot
.\.venv\Scripts\Activate.ps1

# 2-day paper-trade FIRST:
python Scripts\run_live_smartbb.py --dry-run --log-level INFO

# Watch the log.  When you see SIGNAL lines followed by (no order — dry-run),
# matching the price structure you expect, switch to live:
python Scripts\run_live_smartbb.py --risk 0.003
```

### 2.4 Keep it running across reboots (Task Scheduler)

Create a batch file `C:\PropBot\start_live.bat`:
```bat
@echo off
cd /d C:\PropBot
call .venv\Scripts\activate.bat
python Scripts\run_live_smartbb.py --risk 0.003 >> Results\live_smartbb.log 2>&1
```

Task Scheduler → Create Task:
* Trigger: **At startup**, delay 2 minutes
* Action: Run `C:\PropBot\start_live.bat`
* Conditions: uncheck "only if on AC power"
* Settings: restart task every 1 minute, up to 3 times on failure

Use the existing `PREVENT_SLEEP.ps1` if the VPS policy allows sleep.

---

## 3. Go-live risk configuration (start conservative)

The backtest used `base_risk_pct = 0.0075` (0.75 %). For the first 2 weeks **go live at 0.3 %** to prove things out under real fill conditions:

```bash
python Scripts/run_live_smartbb.py --risk 0.003 --z-min 3.3
```

This combination:
* 0.3 % risk/trade (1.0 R per day = +0.3 %, roughly matches backtest expectancy after slippage buffer)
* Z-min 3.3 keeps only the **26 best-quality 3-month setups** (88.5 % WR in backtest).
* Projected: ~10–15 trades/month, 0.9 % max DD headroom, solid path to the 10 % target on 5%ers.

Once 50 live trades confirm PF > 1.6, step up to `--risk 0.005 --z-min 3.0`.

---

## 4. Prop-firm safety layers (what keeps the account alive)

| Rule | 5%ers MTB limit | v13 safety |
|---|---|---|
| Max daily loss | 4 % (= $4 000) | Engine halts the day at **3.5 %** equity DD |
| Max total loss | 5 % (= $5 000) | Engine halts permanently at **4.5 %** total DD |
| Forbidden behaviour: not closing before weekend | — | All positions close Friday 21:00 UTC by engine clock |
| Hedging / martingale | Not needed | Never held: 1 position per symbol max, no pyramiding |
| Size explosion | — | AKAD posterior caps risk at 1.5× base after wins; halves after 3 losses |

The **ghost-halt reconciler** in `smartbb_live.py` adds one more belt: if the engine's halt flags fire (daily DD or total DD), `bridge.close_all_positions()` runs immediately and no further orders are sent.

The **dead-Python failsafe** in the EA adds one more layer: if the Python process crashes/hangs for 30 s while positions are open with magic=12345, the EA itself closes them all. This is one-shot per disconnect, so a normal reconnect doesn't trigger it.

---

## 5. Monitoring

* `Results/live_smartbb.log` — rolling INFO log (rotated by you; simple append)
* `Results/live_smartbb_trades.jsonl` — one JSON line per closed trade, ready for pandas

A once-a-day check is enough:
```powershell
Get-Content Results\live_smartbb.log -Tail 50
```
Or read a summary:
```powershell
python Scripts\show_trades.py Results\live_smartbb_trades.jsonl
```

---

## 6. What to do when live ≠ backtest

Two things to watch in the first 20 live trades:

1. **Slippage per trade.**  Backtest assumed fill at mid + 0.5 × spread. If real fills drift > 2 pts worse than spec on indices (or > 0.1 % of price on Oil), `--z-min 3.3` filters the noise out. Beyond that, tighten the amp-hurdle from 1.5× to 2.0× by editing `SmartBBConfig.amplitude_hurdle`.

2. **Symbol mapping.**  5%ers/Eightcap use un-suffixed names (`US100`, `US30`) — the EA will auto-pick those. If your variant is `NAS100` or `US100.cash`, the EA log tells you which one it found. The Python side uses whatever the EA reports, so no config change needed.

**Do not change the strategy parameters in the first month.** 100 + trades of live data are required before any statistically-honest tuning.

---

## 7. Done — summary

Two files were created, two were updated:

* **NEW** `src/live/smartbb_live.py` — live engine wrapper
* **NEW** `Scripts/run_live_smartbb.py` — launch script
* **NEW** `src/live/__init__.py`
* **UPDATED** `MQL5/Experts/SHF_Bridge.mq5` — v13 symbol list + M1 bar streaming
* **UPDATED** `src/execution/mt5_bridge.py` — BarData dispatch to handlers

The EA, bridge, engine and sizer are now all aligned on the same v13 SmartBB contract. Run `python Scripts/run_live_smartbb.py --dry-run` for 24 h on the VPS, then remove `--dry-run` and you are live.
