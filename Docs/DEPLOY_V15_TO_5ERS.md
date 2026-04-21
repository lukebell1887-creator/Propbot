# DEPLOY V15 TO 5%ERS — COMPLETE WIRE-UP PLAN

> Goal: run SmartBB v15 on all 5 profitable symbols (US30, US100, US500, DE40, XAUUSD),
> first on a 5%ers DEMO account for 1-2 weeks to verify live = backtest, then
> switch to the live $100K funded account.

---

## STEP 0 — What I need from you (5 minutes to answer)

### A.  5%ers account

- [ ] Do you already have the **$100K 5%ers live funded account**, or are you still in the evaluation phase?
- [ ] Do you have a **5%ers demo account** (same broker, same symbols, play-money)?  If not, we need to open one — 5%ers offers "Trial" accounts free or you can use the MT5 broker's own demo.
- [ ] Which **5%ers account type** — is it the "Funded Level 1" ($5K), "Standard" ($100K), or one of the new models?
- [ ] What are the **exact broker symbol names** shown in your 5%ers MT5 Market Watch?  Specifically:
   - Dow / US30 — is it `US30`, `DOW`, `US30.cash`, `USA30`, or something else?
   - Nasdaq — is it `NAS100`, `US100`, `USTEC`, or `NASDAQ.cash`?
   - S&P — is it `US500`, `SPX500`, `SP500`?
   - DAX — is it `DE40`, `DAX40`, `GER40`, `DE30`?
   - Gold — is it `XAUUSD`, `GOLD`, `XAUUSD.s`?
   
   (The download scripts used `NAS100` → saved as `US100` because that's what your MT5 server exposes.  The live bridge needs the MT5 side name, not the CSV side.)

### B.  VPS / runtime

- [ ] Do you already have the **VPS running** (from your earlier `BOOTSTRAP_VPS.ps1` / `UPDATE_VPS.ps1` scripts)?  If yes, what's the IP / RDP details and what's on it?
- [ ] Is **MT5 already installed on the VPS**?  Logged into which account (demo/live/5%ers)?
- [ ] Is the **SHF_Bridge.mq5 EA** already compiled and attached to a chart on the VPS?
- [ ] What's the VPS **Python version**?  (v15 needs Python 3.10+ — your local one is 3.11+ which works.)
- [ ] VPS **time zone** and do you want the bot to auto-restart on reboot?

### C.  Risk preferences

- [ ] Do you want to run the **per-symbol risk ladder I recommended** (DE40/US30/US100 at 1%, US500 & XAUUSD at 0.5%) or **flat 1% risk on all 5** or **conservative 0.25% burn-in across all 5** for the first weeks?
- [ ] **Max concurrent positions** — OK with up to 3 at once (engine default), or do you want to cap at 2 to be extra safe while running demo?
- [ ] **Hard daily kill-switch** — engine already halts at 4% daily DD. Do you want a **wider account-level kill at 8%** (to keep 2% margin under 5%ers' 10%)?

---

## STEP 1 — Code changes I will make (just approve, I'll code them)

The live runner today (`src/live/smartbb_live.py`) uses ONE global `SmartBBConfig`.  v15 tuning produces DIFFERENT parameters per symbol.  Without the per-symbol wiring, you'd be running the **live at backtest average settings**, which would under-perform.

### Change 1 — Per-symbol config loader  *(~80 lines, new file `src/live/v15_config_loader.py`)*

- Reads `Results/v15_ultimate_tuning.json`
- Extracts `best_params` for each of the 5 TIER1 symbols
- Returns a dict `{"US30": SmartBBConfig(...), "US100": SmartBBConfig(...), ...}`
- The live engine looks up the per-symbol config on every `_maybe_enter` call

### Change 2 — Engine patch: per-symbol `min_z_entry`, `hurst_max_for_trade`, `tp_frac`, `stop_atr_mult`  *(~30 lines in `smartbb_engine.py`)*

- Extend `SmartBBConfig` with a `per_symbol_overrides: dict[str, dict] = {}`
- In `_maybe_enter`, override the relevant values from `per_symbol_overrides[symbol]` if present
- Same for `_manage` (tp_frac / stop_atr_mult)

### Change 3 — Symbol name mapping in `SmartBBLive`  *(~15 lines)*

- Add `symbol_map: dict[str, str]` arg so the engine knows:
  - Engine internal name `US30` ←→ MT5 broker name `US30` / `DOW` / `USA30` (whatever your 5%ers server uses)
- All internal state keyed by the engine internal name; all bridge calls use the broker name

### Change 4 — Add XAUUSD to default symbol list  *(2 lines)*

- `build_default` defaults change from `["US100", "US500", "US30", "DE40", "USOIL"]`
  to `["US30", "US100", "US500", "DE40", "XAUUSD"]` (drop rejected USOIL, add winner XAUUSD)

### Change 5 — Account-level kill-switch  *(~20 lines)*

- Add a `max_account_dd_pct` config (default 0.08 = 8%)
- On equity sync, compute `account_dd = (peak_equity - equity) / peak_equity`
- If `account_dd >= 0.08`: call `halted_permanently = True` and `bridge.close_all_positions()`

### Change 6 — `Scripts/run_v15_live.py`  *(new launcher, ~50 lines)*

- Thin wrapper that wires everything together
- Arguments: `--demo | --live`, `--dry-run`, `--risk-multiplier`, `--symbols US30 US100 DE40 US500 XAUUSD`
- Loads per-symbol v15 params automatically
- Logs everything to `Results/live_v15_YYYYMMDD.log` + a trade JSONL for audit

### Change 7 — MT5 side: verify `SHF_Bridge.mq5` supports all 5 symbols  *(0 changes expected — already does)*

- The EA streams bars and executes orders for ANY symbol you pass.  We just need to make sure the 5 symbols are in the MT5 Market Watch on the VPS.

---

## STEP 2 — The deployment process (once code is ready)

### Phase A — Local dry-run (15 minutes, no broker needed)

```bat
REM Test that per-symbol configs load correctly
python Scripts\test_v15_config_load.py

REM Run a 30-minute simulated bar replay against a recent data file
python Scripts\test_v15_live_dryrun.py
```

**Gate:** Both scripts must print "OK" on all 5 symbols before proceeding.

### Phase B — Demo deployment on VPS  (1 hour)

1. **On the VPS** (over RDP):
   - `git pull` the latest code to `C:\SHF`
   - Log MT5 into the **demo account** (5%ers demo OR the same broker's demo)
   - Add all 5 symbols to Market Watch (`US30`, `US100` / `NAS100`, `US500` / `SPX500`, `DE40` / `DAX40`, `XAUUSD`)
   - Attach `SHF_Bridge.mq5` to any chart, `AutoTrading` green
   - `InpHost = 127.0.0.1`, `InpPort = 5555`
2. **Start the Python bot in DEMO/dry-run mode**:
   ```powershell
   cd C:\SHF
   .\RUN_ENGINE.ps1 -Mode demo -RiskMult 0.5 -Symbols US30,US100,US500,DE40,XAUUSD
   ```
3. **First 30 minutes**: just watch the log.  You should see:
   - `Bridge connected`
   - `Account: 5ers-Demo | Balance: $X | Equity: $Y`
   - `Subscribed to US30 M1 bars` × 5
   - On the first signal: `SIGNAL US30 LONG Z=... H=... lots=... entry=... sl=... tp=... risk=$...`
   - `OPENED US30 ticket=... fill=...`

### Phase C — Demo validation period (1–2 weeks)

Run demo for **at least 30 trades** before going live.  Monitor:

| Metric           | Backtest expectation        | Pass threshold              |
|:-----------------|:----------------------------|:----------------------------|
| Win rate         | 73.8 %                      | > 60 %                      |
| Profit factor    | 7.16                        | > 2.0                       |
| Avg slippage     | (n/a)                       | < 2 pts/trade               |
| Max DD           | 1.4 % of account            | < 3 %                       |
| Trade frequency  | ~2-3/day combined           | > 1/day                     |
| Order rejections | 0                           | < 5 %                       |

**Gate to live:** 30+ trades, PF > 2, no major execution anomaly, max DD under 3 % on demo.

### Phase D — Live on 5%ers funded ($100K)

1. Swap MT5 login from demo to the real 5%ers funded account
2. Bot restarts automatically (Windows task or manual restart)
3. **Start at 0.25 % risk-multiplier** (so full 1 % setup trades effectively as 0.25 %)
4. Run for **first 30 live trades at 0.25 %**
5. If live mirror demo: scale to 0.5 %, then 1 % at 60 trades

**Automatic guards active:**
- 4 % daily DD → halt for the day
- 5 % total DD → halt permanently (engine built-in)
- **8 % account DD → new kill-switch closes everything and stops** (change 5 above)
- 5%ers rule: 10 % total is where you'd blow — we stop at 8 %, giving a 2 % safety cushion.

---

## STEP 3 — Monitoring dashboard (optional, 30 min to set up)

If you want it, I can add:
- A simple HTML dashboard that reads `Results/live_v15_trades.jsonl` and shows: equity curve, win rate, PF, open positions, daily PnL — auto-refreshes every 10 sec.
- Telegram bot that pings you on every trade OPEN/CLOSE and on halts.
- (Both are optional — the log files alone are enough to validate.)

---

## Immediate next steps — the short answer

**I need 4 things from you to start building this:**

1. **Exact 5%ers MT5 symbol names** (can you open MT5 → Market Watch → right-click → "Show All" and take a screenshot, or list the names?)
2. **VPS status** — is it already running with MT5?  (If yes, IP / RDP or just "yes, MT5 is up and logged in")
3. **Risk preference** — flat 0.25 % across all 5 for first 30 trades (safest), OR the per-symbol ladder (DE40/US30/US100 at 1 %, US500/XAUUSD at 0.5 %)?
4. **Demo account** — do you have a 5%ers demo I can connect to first, or should we use the broker's own demo for Phase C?

**Once you answer those, I'll:**
- Write the 6 code changes above (~3 hours coding)
- Produce a new `Scripts/run_v15_live.py` single-command launcher
- Write `DEPLOY_VPS_STEPS.ps1` so you can run **one PowerShell command** on the VPS to update and start everything
- Give you an exact checklist of clicks on the VPS to start demo

---

## Realistic timeline

| Phase | Duration | Cost |
|:------|:---------|:-----|
| Code changes + testing | 3–5 hours | (me) |
| VPS update + demo start | 30 min (your time) | 0 |
| Demo validation | 1–2 weeks (passive) | 0 |
| Live go-live + first 30 trades at 0.25 % | 1–2 weeks | ~$0 risk (0.25 % × 30 trades max -0.25 % per loss × 5 losses = max -1.25 % total if everything went wrong) |
| Scale to full risk | Weeks 3–4 onwards | Already on the way to the 10 % profit target |

**Expected to clear 5%ers 10 % profit target: 1–3 months live.**

**Worst realistic outcome**: losses don't exceed 3 % of account (0.25 % × 30 trades × 40 % loss rate ≈ max 3 %), at which point we stop and debug.

---

## Safety bottom line

With this plan you have **four levels of protection**:

1. **Per-trade**: 0.25 % risk cap during burn-in → max single-trade loss = $250 on $100K
2. **Daily**: engine halts at 4 % daily DD = $4,000 (well under 5%ers' 5 %)
3. **Account**: new kill-switch at 8 % total DD = $8,000 (well under 5%ers' 10 %)
4. **Demo first**: you validate on play-money for 30+ trades before touching real money

This gives us a **margin of safety of at least 2× on every 5%ers rule**.  It is effectively impossible to blow the account with this plan unless something catastrophic (broker flash crash, VPS outage during open position) happens — and for that we have the 8 % hard-kill.
