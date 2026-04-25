# V30 RELEASE NOTES — v25.1 ship config + slippage tracker

**Date:** 2026-04-25
**Status:** READY FOR DRY-RUN (Monday 2026-04-27)
**Supersedes:** v23 live (currently running in dry-run on the VPS)
**Origin doc:** [`V25_1_SHIP_RECOMMENDATION.md`](V25_1_SHIP_RECOMMENDATION.md)

---

## 1. Why a re-version?

The series `v22 / v23 / v24 / v24b / v24c / v24d / v25 / v25.1` was getting
very confusing on the VPS. **From now on the live bot is just `v30`.** Older
files (`run_v23_live.py`, `GO_DRYRUN_V23.ps1`, etc.) are kept for forensic
reference but **no longer the source of truth**.

| Item | v23 (current live) | **v30 (new)** |
|------|---------------------|---------------|
| Source file | `src/live/v23_live.py` | **`src/live/v30_live.py`** |
| Launcher    | `Scripts/run_v23_live.py` | **`Scripts/run_v30_live.py`** |
| Dry-run PS  | `GO_DRYRUN_V23.ps1` | **`GO_DRYRUN_V30.ps1`** |
| Live PS     | `GO_LIVE_V23.ps1`  | **`GO_LIVE_V30.ps1`** |
| Magic       | 23000 | **30000** |
| Comment tag | `SHF_v23` | **`SHF_v30`** |

The MagicNumber bump means v23 and v30 tickets at the broker can never be
mistaken for each other — important if for any reason the VPS ever ends up
running both (it should not, but the ticket distinction protects you).

---

## 2. Two locked-in config flips (v25.1 ship recommendation)

These are the only two parameter changes vs `V23LiveConfig`. Everything
else (4 ORB symbols, anchors, TP1/TP2, news rails, 4 % daily halt, 4 %
DD breaker, calendar, 65 s min hold, 2-position concurrency cap) is byte-
for-byte identical.

### 2.1 `base_risk_pct: 0.00110 → 0.00170`

* +55 % per-trade risk
* per-trade dollar cap rises 0.55 % → 0.85 % of equity
* projected (3-month real 5ers data, full v25.1 cost model):
  * **Net P&L: $16,977 → $27,668** (+$10,691, +62.9 %)
  * Max DD: 3.35 % → 3.16 % (improved)
  * Worst day: -1.57 % → -2.02 % (still well inside the 4 % halt)
  * PF: 1.94 → 1.94 (unchanged)
  * Sharpe: 5.66 → 5.66 (unchanged)
* Evidence: 64-cell loosened-rails grid in §3.2 of `V25_1_SHIP_RECOMMENDATION.md`

### 2.2 `nochase_cooldown_s: 0.0 → 300.0`

* New rail: any breakout entry on `Symbol X` is dropped if **any other**
  symbol closed a position within 300 seconds before now
* Same-symbol back-to-backs are unaffected (ORB only fires once per
  (symbol, day) anyway)
* Implementation matches the offline filter in
  `Scripts/backtest_v23_nochase.py` exactly:
    * `_last_close_ts_by_symbol[sym]` is stamped on every close (self-close,
      window expiry, broker-detected close, news-flatten, DD-breaker flatten,
      account-kill flatten)
    * `_nochase_block(sym, now_ts)` walks all OTHER symbols and returns
      `(blocker, gap_s)` if any is within `cooldown_s`
    * Heartbeat shows the blocker, the gap, and the unblock time-left for
      every BLOCKED symbol
* Why 300 s: §3.5 cooldown shootout in `V25_1_SHIP_RECOMMENDATION.md`
  shows 300 s is the sweet-spot; 60 s is too short, 600 s starts losing
  good back-to-backs

---

## 3. NEW operational feature — per-trade slippage tracker

Slippage was identified in §3.4 of the ship doc as the dominant residual
cost driver. v23 live did not log it explicitly, so we had no per-trade
ground truth. **v30 fixes that.**

### 3.1 What is captured

For every entry the bot now records:

| field | meaning |
|-------|---------|
| `intended_px` | the bid/ask we observed at submission (`quote.ask` for LONG, `quote.bid` for SHORT) |
| `fill_px`     | the actual price the broker filled at (from `bridge.send_order().price`) |
| `slip_ticks`  | signed; positive = WORSE fill (paid more on long, received less on short) |
| `slip_dollars`| `slip_ticks × pip_value × lots` — the real $ cost of the slip |

Slippage is captured at **entry only**. Exit fills (broker-side SL/TP) are
not echoed back through the current `SHF_Bridge.mq5` reconciliation path,
and the V25_1 sensitivity analysis shows entry slip is >90 % of total slip
cost anyway.

### 3.2 Where you see it

Three places, all designed for the VPS terminal:

**A. One-line per trade in the log:**
```
[ENTRY] DE40 LONG  lots=0.300  intended=17500.00  fill=17500.50
       slip=+0.50t($+0.15)  SL=17480.00  TP1=17522.50  TP2=17545.00  risk=$170
[SLIP]  DE40 LONG  intended=17500.00000  fill=17500.50000  slip=+0.50t  $+0.15
```

**B. Heartbeat block (every 60 s):**
```
SLIPPAGE (entry fills, ticks; +ve = worse fill):
  PORTFOLIO  trades=12   avg=+0.42t  avg_abs=0.85t  sum$=+8.40
            avg$=+0.70  worst=+3.00t(US30)  best=-1.50t(DE40)
  DE40       trades=4    avg=+0.13t  min=-1.50t  max=+1.50t  sum$=+0.50
  US30       trades=3    avg=+1.50t  min=+0.50t  max=+3.00t  sum$=+4.50
  XAUUSD     trades=4    avg=+0.40t  min=-0.20t  max=+1.00t  sum$=+1.60
  US500      trades=1    avg=+0.00t  min=+0.00t  max=+0.00t  sum$=+0.00
```

**C. Append-only JSONL log:** `Results/v30_live_slippage.jsonl`
One JSON record per trade with full intended/fill/lots/etc. fields. Easy
to `tail -f` on the VPS, easy to load into pandas for post-trade audits.

The same numbers are also embedded in `Results/v30_live_telemetry.json`
under `slippage.portfolio` / `slippage.per_symbol` so the existing
heartbeat infrastructure can ingest them.

### 3.3 Note on dry-run mode

In dry-run, no real order is sent — `fill_px = intended_px` by definition,
so every trade logs `slip=+0.00t  $+0.00`. This is expected and the per-
symbol counts still increment so you can verify the wiring works end-to-
end before going live. Real numbers only appear once you flip `--live`.

---

## 4. Compatibility with the rest of the stack

* **Sizer:** unchanged class (`MertonGZSizer` from `src.dynamic_sizer_v21`),
  only `base_risk_pct` is bumped 0.00110 → 0.00170. `cap_mult=5.0` and
  `dd_cap_pct=0.04` are identical to v23.
* **ORB anchors / TP / SL:** identical to v23. The 12-month tuning is
  preserved.
* **News CSV:** same `data/news/tier1_2026.csv`. Same ±15 min entry block,
  same -2 min flatten.
* **Calendar:** same `TradingCalendar` rollover / weekend / holiday rails.
* **DD rails:** `DailyHalt(halt_pct=0.04)` and `DDBreaker(halt_pct=0.04)`
  identical instances; both are active.
* **Bridge:** same `MT5Bridge` ZMQ contract; **no MQL5 EA change required**.
  Just point the EA at the same chart and run `GO_DRYRUN_V30.ps1`.

---

## 5. Test status

```
$ python Scripts\smoke_v30_live.py
[OK] SMOKE OK — imports, state, sizer, no-chase cooldown, slippage all green.

$ python -m pytest tests/ -q
75 passed, 84 warnings in 1.87s
```

Specifically the smoke test verifies:

1. `V30LiveConfig` defaults: `risk=0.170 %`, `nochase=300 s`, `magic=30000`
2. All 4 symbols build (DE40, US30, XAUUSD, US500) with correct broker /
   tick / pip-value / lot specs
3. News CSV loads (31 events for 2026)
4. Sizer warmup → base risk; post-warmup binds at the cap; GZ barrier
   collapses size to 0 at DD = `dd_cap_pct`
5. Cross-symbol no-chase cooldown:
   * blocks DE40 when US30 closed 5 s ago (within 300 s)
   * does NOT block US30's own self-close (cross-symbol only)
   * does NOT block when 305 s have passed (>300 s window)
6. Slippage tracker:
   * captures LONG slip = `(fill - intended) / tick`
   * captures SHORT slip = `(intended - fill) / tick`
   * rolls up per-symbol AND portfolio totals
   * tracks portfolio `worst` / `best` with the symbol that owns it
   * `to_dict()` shape matches the telemetry JSON contract

---

## 6. VPS deployment plan (Monday 2026-04-27)

1. SSH / RDP to VPS
2. **Stop** the v23 dry-run launcher (`STOP_BOT.ps1` or close the window)
3. `git pull` the v30 code
4. `pip install -r requirements.txt` (no new deps; only runs in case)
5. Start `.\GO_DRYRUN_V30.ps1` — leave it running 1+ trading day
6. **Verify in heartbeat** within 5 minutes of start:
    * banner says `risk=0.170%   no-chase cd: 300 s   ★ v25.1 ship`
    * 4 symbols all show `OR=...` after the relevant 08:00 / 14:30 OR window
    * `slippage tracker idle` (no entries yet) is fine and expected
7. Once the first entry fires, confirm the `[ENTRY]` log line shows BOTH
   `intended=` and `fill=` (in dry-run they'll be equal)
8. Once you're satisfied with a clean dry-run trading day, flip:
    * stop dry-run
    * start `.\GO_LIVE_V30.ps1`
    * watch the FIRST live entry's `[SLIP]` line carefully — the magnitude
      tells you whether the broker is filling within the 3-tick "cliff"
      identified in §3.4 of the ship doc

---

## 7. What did NOT change

So you can sanity-check this is genuinely a small-surface delta:

* `src/momentum/orb.py` — untouched
* `src/dynamic_sizer_v21.py` — untouched (just instantiated with new base risk)
* `src/daily_halt.py` — untouched
* `src/dd_breaker.py` — untouched
* `src/trading_calendar.py` — untouched
* `src/execution/mt5_bridge.py` — untouched
* `MQL5/Experts/SHF_Bridge.mq5` — **untouched** — no EA recompile/install needed
* `data/news/tier1_2026.csv` — untouched
* All 75 existing unit tests still pass

The only NEW files are:

* `src/live/v30_live.py`
* `Scripts/run_v30_live.py`
* `Scripts/smoke_v30_live.py`
* `GO_DRYRUN_V30.ps1`
* `GO_LIVE_V30.ps1`
* `Docs/V30_RELEASE_NOTES.md` (this file)
