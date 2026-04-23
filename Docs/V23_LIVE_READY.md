# V23 LIVE — Deployment & Ops Runbook

**Locked 2026-04-23.**  Author: Cline + Luke.
The file you are reading is the _single source of truth_ for how to deploy,
monitor, and kill the v23 ORB bot on a 5ers live account.

---

## 1. WHY v23 (and why v18 is retired)

| bot | strategy                     | status  | evidence                                                 |
|-----|------------------------------|---------|----------------------------------------------------------|
| v18 | SmartBB breakout/reversal    | RETIRED | PHD honest test: edge SPURIOUS — same-bar & broker-invalid SL exits inflated PnL by $5k+; live NSB PnL = **-$5,075** |
| v23 | ORB 4-pair + Merton-GZ sizer | **LIVE-READY** | Locked backtest: **$10,853 / 3m, 2.16% DD, Sharpe 3.45, ruin@5% = 0.1%, sub60s = 0**, news rails active (31 events) |

v18 is left untouched in repo for archival audit but **must never be
re-deployed**. All GO_LIVE.ps1 / GO_DRYRUN.ps1 scripts now point at v23.

---

## 2. What v23 actually does

**Strategy.**  Single edge: Opening-Range Breakout on 4 symbols.
- `DE40` — 08:00 UTC OR, 30 min range, TP1/TP2 = 1.5x/3.0x, SL buf 0.3x
- `US30` — 14:30 UTC OR, 30 min, TP1/TP2 = 2.0x/4.0x, SL buf 0.0x
- `XAUUSD` — 14:30 UTC OR, 30 min, TP1/TP2 = 2.0x/4.0x, SL buf 0.6x
- `US500` — 14:30 UTC OR, 15 min, TP1/TP2 = 0.5x/1.0x, SL buf 0.6x

Configs are copied _verbatim_ from `Scripts/backtest_v22_lean_uk5.py`, which
is the backtest that produced the $10,853 / 2.16% DD result.

**Sizing.**  Merton-GZ regime-adjusted Kelly.
- base risk = 0.110 % equity
- hard cap  = 3× = 0.330 % equity
- γ = 2.0, EWMA α = 0.20
- 15-trade warmup at base risk
- Grossman-Zhou DD barrier at 4 %

**Lot-value math** uses `SMARTBB_UNIVERSE[sym].pip_value` — the _exact same_
table the backtest uses. Live $-PnL per tick matches backtest $-PnL per tick.

---

## 3. Nine-layer safety ladder (5ers-compliant)

1. **ORB session windows** — entries only inside 60 min post-OR
2. **TradingCalendar** — weekend / rollover / holiday / news buffer
3. **News entry-block** — ±15 min buffer around 31 Tier-1 events
4. **News flatten** — close ALL positions 2 min before each event
5. **Portfolio cap** — max 2 open positions across all symbols
6. **Daily-DD breaker** — halt new entries if today's DD ≥ 2 %
7. **Account kill** — flatten + halt if rolling DD ≥ 8 %
8. **Broker SL/TP** — every ORDER_SEND carries SL and TP at submission
9. **Window time-stop** — close any open position at ORB window expiry

**5ers-prohibited-practices guarantees:**
- NO HFT (min hold 65 s — backtest had 0 sub-60 s trades)
- NO BULK (cap = 2 concurrent, never more)
- NO BRACKETING (we flatten 2 min before news, never bracket)
- NO ROLLOVER SCALP (rollover window blocked by calendar)
- NO TICK SCALP (min TP = 1.0× OR range; typically 15–40 points)
- NO ARBITRAGE (single broker, single feed, one instance)
- NO ONE-SIDED (ORB is direction-agnostic)
- NO 3RD-PARTY EA (all code in your own repo)
- HARD SL (broker-side on every order)

---

## 4. Files that matter

| file                              | purpose                                              |
|-----------------------------------|------------------------------------------------------|
| `src/live/v23_live.py`            | the runner (V23Live class, 9-layer safety ladder)   |
| `Scripts/run_v23_live.py`         | CLI launcher (`--dry-run` / `--live`)                |
| `Scripts/smoke_v23_live.py`       | offline smoke test (runs every deploy)              |
| `GO_DRYRUN_V23.ps1`               | dry-run from local workstation                       |
| `GO_LIVE_V23.ps1`                 | LIVE — requires typing "GO LIVE" to confirm          |
| `data/news/tier1_2026.csv`        | 31 Tier-1 macro events (USD/EUR/GBP/JPY, 2026)      |
| `Scripts/backtest_v23_final.py`   | the backtest whose numbers live-matches              |
| `Results/risk_sweep_fine.json`    | risk-sweep evidence ($10,853 / 2.16 % DD / 0.110 %)  |
| `Results/v23_live_telemetry.json` | live heartbeat snapshot (updated every 60 s)         |
| `Results/v23_live_events.log`     | JSONL event log (FLATTEN, KILL, CLOSE, etc.)         |
| `Results/v23_live_trades.jsonl`   | JSONL trade log (every ENTRY, full sizing audit)     |

---

## 5. Pre-deploy gate — ALWAYS run this first

```cmd
python Scripts\smoke_v23_live.py
```

Expected output:
```
  News events    : 31
  Sizer smoke  :  warmup risk = 0.1100%   ($ per trade @ $100k eq = $110.00)
  ✅ SMOKE OK — imports, state, sizer, rails all green.
```

If `News events : 0` — **STOP**. The news rails are the difference between
2.16 % DD and 5.76 % DD. Do not deploy without them.

If `warmup risk ≠ 0.1100%` — **STOP**. The sizer config has drifted from the
backtest.

---

## 6. Deploy flow

### 6.1  Local dry-run (paper, never sends orders)
```powershell
.\GO_DRYRUN_V23.ps1
```
Runs 5–10 min, you should see heartbeat lines every 60 s with live OR
windows updating. Check `Results/v23_live_telemetry.json` — it tails cleanly.

### 6.2  VPS deploy (LIVE)
On the VPS PowerShell (as Administrator), paste the one-liner from
`Docs/VPS_GO_LIVE_NOW.md` or the block below:

```powershell
cd C:\PropBot
git pull origin main
python -m pip install -r requirements.txt --quiet
python Scripts\smoke_v23_live.py
if ($LASTEXITCODE -eq 0) { .\GO_LIVE_V23.ps1 }
```

The LIVE script asks you to type "GO LIVE" to confirm. It does not proceed
without that string.

### 6.3  Checking that it's alive
```powershell
Get-Content C:\PropBot\Results\v23_live_telemetry.json | ConvertFrom-Json | Format-List
```
Fields you want to see:
- `equity` — current live equity
- `dd_pct_total` — rolling DD from peak (must stay < 8 %)
- `dd_pct_today`  — intra-day DD from day-start (halts entries at 2 %)
- `open_count`    — ≤ 2 always
- `counters.entries` — increments only on real ORB breaks
- `counters.block_news_entry` — shows rails are firing
- `symbols.*.in_window` — true during the 60-min trade window

---

## 7. Emergency stop

### 7.1  Soft stop (finish the day, no new entries)
Edit the running process's log level to WARNING and send SIGTERM — or simply:
```powershell
.\STOP_BOT.ps1
```
Broker-side SL/TP stays on. Open positions are safe.

### 7.2  Hard kill (flatten everything, now)
Use the broker-side "Close All" in MT5 terminal. The bot will reconcile and
log POS_CLOSED_BY_BROKER events. Do not kill the Python process _without_
flattening on the broker — orphaned SL/TPs stay, but orphaned bot-state
means the next start will see positions it doesn't know about (the reconcile
step handles this, but a clean break is cleaner).

### 7.3  Account kill automatic trigger
Rolling DD ≥ 8 % → bot auto-flattens and halts. You will see:
```
  kill=True
```
in the heartbeat. The bot will not re-enter until restarted.

---

## 8. Per-day expectations (from backtest, not guarantees)

- typical open count: **0 → 1 peak, rarely 2**  (most days have 1–2 entries)
- typical trades/week: **8 – 14**
- typical best-case month: **+$3,500 to +$5,000**
- typical worst-case month: **≈ flat** (the backtest had no losing months)
- **3-month DD: 2.16 %** — we designed for ≤ 4 %, operating envelope 5ers
  allows is 5 %

If a single day shows DD > 1.5 %, investigate before next open.
If 5 consecutive days show no entries, investigate data feed / OR tracking.

---

## 9. Retuning policy

Do NOT retune parameters between now and **2026-07-15**.

On 2026-07-15 (6 months from data cutoff), re-run:
```
python Scripts\backtest_v22_phase_b.py
python Scripts\risk_sweep_fine.py
```
If PF drops below 1.3 on any symbol OR rolling DD exceeds 3.5 %, RETIRE
that symbol. Do not add new symbols without a full PHD-grade validation
(PBO < 50 %, deflated Sharpe p > 0.95, IS+OOS+FULL all positive).

---

## 10. Accountability

Every trade the bot takes is logged to `Results/v23_live_trades.jsonl` with
a full sizing audit (OR range, equity at entry, sizer risk%, $-at-risk,
lots, SL, TP1, TP2, ticket). Every rail event is logged to
`Results/v23_live_events.log`. The VPS's GitHub Actions workflow uploads
both files nightly — so there is a permanent paper-trail of every decision
the bot made. No black boxes. No "lost trades". Every dollar accounted for.
