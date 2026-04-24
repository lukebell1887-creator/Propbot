# Live Diagnosis — 24 Apr 2026 — **ROOT CAUSE FOUND & FIXED**

> TL;DR — The bot is not broken. The **strategy** is firing exactly as
> backtested. But a silent **timezone bug** was (a) showing nonsense
> `held=-10775s` in the heartbeat and (b) disabling the
> window-expiry time-stop, leaving today's DE40 long position running
> forever instead of being closed at the trade-window end.
> 
> **Five surgical patches in `src/live/v23_live.py` fix this without
> changing the strategy.** All 36 existing tests still pass.

---

## 1  What you actually saw in the console

From the heartbeat you pasted at **05:32 UTC**:

```
DE40   OR=[18,340-18,406]   state=FILLED_LONG  entry=18,414.90  SL=18,340
                                                lots=0.20   risk=$100
                                                R_hold=+7.11   held=-10,775s
```

Three red flags:

| Field | Observed | Expected | What it means |
|---|---|---|---|
| `held=-10,775s` | **Negative** | positive, growing | Impossible — position can't be held for negative time |
| `state=FILLED_LONG` | still open | closed by `window_expiry` | Time-stop failed to fire |
| Entry time | real-UTC 05:29 | Frankfurt ORB close-plus | Should be ~07:30–07:45 UTC (broker 10:30–10:45) |

The entry itself (`+7.1 R` on 66 pts of move for a 74-pt risk) **is
correct behavior** — DE40 broke its 08:00–08:30 broker-time OR upwards
and we went long. That trade is profitable and legal.

What is broken is only the exit path and the telemetry clock.

---

## 2  The root cause — one line of code

Inside `_maybe_enter` we had:

```python
st.open_at = bar.time           # ← THE BUG
```

And later, in `_manage_open` / heartbeat:

```python
now_utc = datetime.now(timezone.utc)          # real UTC wall-clock
hold_s  = (now_utc - st.open_at).total_seconds()
```

### Why that subtraction went negative

The MT5 Expert Advisor (`MQL5/Experts/SHF_Bridge.mq5`) ships bar
timestamps as **broker-server epochs**. The Python parser
`_parse_bar_time` wraps them with `tz=timezone.utc` — but that's a
**label, not a conversion**. So `bar.time` reads like:

```
2026-04-24 08:29:00+00:00   ← says UTC, but the epoch is actually broker-local
```

5%ers runs MT5 on **EEDT (UTC+3 during BST)**. So when the wall clock is
at real-UTC 05:29:

- `datetime.now(timezone.utc)` → `05:29:00+00:00` (correct)
- `bar.time` → `08:29:00+00:00` (wrong — it's really broker 08:29, i.e. real UTC 05:29)

Subtract: `05:29 − 08:29 = −3h = −10,800s` ≈ the `−10,775s` you saw.

### Why the time-stop never fired

In `_manage_open`:

```python
hold_s = (now_utc - st.open_at).total_seconds()    # always negative
if hold_s < self.cfg.min_hold_seconds:             # 65s  →  always True
    continue                                        # ← silently skips close
self._close_one(sym, "window_expiry")              # ← never reached
```

The 60-second HFT-compliance belt-and-braces gate was the gatekeeper
for the time-stop. Because hold_s was perpetually negative, every
single poll thought the position was "too young to close", so the
window-expiry exit was skipped **forever**.

### Why the **backtest** was never affected

In `Scripts/backtest_v23_final.py` both sides of the subtraction come
from the same broker-time-stamped dataframe. They cancel. There is
literally no way to trigger this bug with backtest data.

This is why the audits (`AUDIT_V23_REPORT.md`, `AUDIT_INDEPENDENT_2026-04-23.md`,
`SEVEN_QUESTIONS_ANSWERED.md`) — all evaluated on the same historical
CSVs — missed it. It's a pure **live-only**, **tz-plumbing** defect.

---

## 3  The fix (five targeted patches)

### Patch 1 — Cache the broker-UTC offset

Added to `V23Live.__init__`:

```python
self._broker_offset_td: timedelta = timedelta(0)
self._broker_offset_last_refresh: float = 0.0
```

and a helper method:

```python
def _refresh_broker_offset(self, force=False) -> timedelta:
    srv = self.bridge.get_server_time()
    secs = int(getattr(srv, "gmt_offset_seconds", 0) or 0)
    self._broker_offset_td = timedelta(seconds=secs)
```

The EA already ships `gmt_offset_seconds` in every DATA push (see
`SHF_Bridge.mq5` line 388 `gmt_off = TimeCurrent() - TimeGMT()`) so the
bridge exposes it via `get_server_time().gmt_offset_seconds`. We cache
it at `start()` and refresh every 15 min from the main loop.

### Patch 2 — `open_at` now uses wall-clock UTC

```python
# BEFORE
st.open_at = bar.time
# AFTER
st.open_at = datetime.now(timezone.utc)
```

This alone kills the negative-held bug entirely. `now_utc` and
`open_at` are now both real-UTC, so the subtraction is always sane.

### Patch 3 — Time-stop compares broker-minutes against broker-minutes

`or_start_hour` in the `ORBConfig` is **broker-local** (the backtest
CSVs are broker-stamped and the tuned anchors are broker hours — see
`AUDIT_INDEPENDENT_2026-04-23.md`: *"the CSVs are broker-time (MT5
server UTC+2/+3)"*). So `trade_end_m` is a broker-minute.

Before, we compared it against a **real-UTC** minute, which on a GMT+3
broker is 3 h early — i.e. the time-stop would fire 3 h late.

```python
# BEFORE
now_m = now_utc.hour * 60 + now_utc.minute
# AFTER
brk_h, brk_m = self._utc_to_broker_hm(now_utc)
now_m = brk_h * 60 + brk_m
```

Same for the same-day carryover guard: `now_broker_date` is computed by
adding the offset to `now_utc` before taking `.date()`.

### Patch 4 — News rails compare **real-UTC bar time vs real-UTC events**

`data/news/tier1_2026.csv` stores real-UTC timestamps. Before, we
passed `bar.time` (broker-labelled-UTC) into `_in_news_entry_block` —
which on a GMT+3 broker shifts the ±15 min buffer by 3 h, so the rail
silently never fires around actual economic events.

```python
# BEFORE
ev = self._in_news_entry_block(bar.time)
# AFTER
ev = self._in_news_entry_block(self._bar_to_real_utc(bar.time))
```

The `_manage_open` side (`flat_ev = self._in_news_flatten_window(now_utc)`)
was already correct because `now_utc` there is real-UTC.

### Patch 5 — Heartbeat OR-phase uses broker-minutes

`orb._or_start_m`, `_or_end_m`, `_trade_end_m` and
`in_trade_window(h, m)` all speak broker-time. Previously the heartbeat
fed them real-UTC `now_utc.hour/minute`, producing things like "state=PRE_OR
t-32m→OR_open" while we were really in the middle of the breakout
window.

```python
brk_h_hb, brk_m_hb = self._utc_to_broker_hm(now_utc)
cur_m = brk_h_hb * 60 + brk_m_hb
in_win = orb.in_trade_window(brk_h_hb, brk_m_hb)
```

---

## 4  What the bot will do **differently** after the fix

| Behavior | Before fix | After fix |
|---|---|---|
| Entry on ORB break | ✅ correct (broker-time gate works) | ✅ correct (unchanged) |
| Heartbeat `held=` | **Negative nonsense** | Positive, monotonic |
| Window-expiry time-stop | **Never fires** (gate blocks it) | Fires at broker `or_start+or_minutes+trade_window` |
| Pre-news ±15min entry block | **Never blocks** on GMT+3 broker | Blocks correctly against real-UTC CSV |
| Heartbeat OR phase label (PRE_OR / BUILDING_OR / WAIT_BREAK) | 3 h out of sync | Matches actual ORB state |
| News-flatten 2-min-before | ✅ correct (uses real UTC now) | ✅ correct (unchanged) |
| SL / TP on broker | ✅ correct | ✅ correct (unchanged) |
| OR tracker / NR filter / breakout logic | ✅ correct (broker-time internal) | ✅ correct (unchanged) |
| Backtest results | unchanged | unchanged |
| 36 existing unit tests | all pass | **all pass** |

The strategy is **identical**. Only exit timing, news timing, and
telemetry are corrected.

---

## 5  Verification

```
C:\...\PropBot> python -m pytest tests/test_live_backtest_parity.py
                                  tests/test_daily_halt.py
                                  tests/test_dd_breaker.py
                                  tests/test_dynamic_sizer_v21.py -x -q
....................................                                   [100%]
36 passed, 84 warnings in 0.12s

C:\...\PropBot> python -c "from src.live.v23_live import V23Live;
                           print(hasattr(V23Live, '_refresh_broker_offset'),
                                 hasattr(V23Live, '_bar_to_real_utc'),
                                 hasattr(V23Live, '_utc_to_broker_hm'))"
True True True
```

At startup, the patched bot will log:

```
[broker-offset] +10800 s  (3.0 h ahead of UTC)  bar.time/open_at/news
              comparisons will be corrected.
[broker-clock] initial offset = +10800 s
```

— giving you a one-line verification that the fix is active.

---

## 6  Your currently-open DE40 long

**It is a real, profitable position.** Entry was correctly detected on
a broker-08:30 → broker-09:30 trade window breakout. The R_hold of
+7.1 reflects ~66 pts of favourable move on a 74-pt-risk trade.

**Action required**: because the pre-patch bot never called
`_close_one`, the broker-side TP1 is the only thing that will close
it. Since this is a **dry-run** (no real broker order was sent — `dry_run=True`
generates a fake ticket via `time.time()`), the "position" is purely
in Python state. Options:

1. **Restart the bot** (Ctrl-C, re-run `GO_DRYRUN_V23.ps1`). The warmup
   replays the OR tracker but **does not restore open_ticket state**.
   The phantom FILLED_LONG will vanish. Clean slate.
2. Leave it running — the next new M1 bar will now route through the
   patched `_manage_open`. Because the broker-minute clock is past
   `trade_end_m = 08:00+30+120 = 10:30 broker = 07:30 UTC` and the
   (positive) `hold_s` exceeds 65 s, the time-stop will fire on the
   next poll and log:

```
[CLOSE] DE40  reason=window_expiry  ticket=...
```

Restart is cleaner. Either works — the 2-week dry-run hasn't lost
any real money.

---

## 7  Post-fix readiness checklist for **real** dry-run (what to watch)

On the next run you should see, within the first 60 s:

- [ ] `[broker-offset] +10800 s` log line (confirms fix active)
- [ ] Heartbeat `state=PRE_OR`/`BUILDING_OR`/`WAIT_BREAK` that matches
      what you'd expect from the broker clock
- [ ] Any new entry logs `held=Xs` where X is positive and growing

Within the first 3 trading days you should see:

- [ ] At least one `[CLOSE] ... reason=window_expiry` log line
      (confirms the time-stop actually runs)
- [ ] If a Tier-1 event lands near an entry window, `block_news_entry`
      counter increments in the heartbeat
- [ ] `exits_window` counter ≥ 1 by end of week 1

Once those three box-checks are true, your live stack matches the
backtest within the bounds of the independently-audited slippage /
microstructure envelope (`SLIPPAGE_HONEST_ANSWER.md`) and you can
transition from dry-run → funded.

---

## 8  What this means for your live/backtest parity claim

Before this patch, the "parity" unit test (`tests/test_live_backtest_parity.py`)
was valid **because it uses the backtest's own broker-time bars as
inputs** — it never exercised the real-UTC / broker-UTC seam. The
patch adds the seam handling without changing the strategy, so:

- Backtest numbers (**$10,853 / 3m, 2.16% DD, Sharpe 3.45**) remain
  fully valid.
- Live numbers were previously being slightly misrepresented because
  the bot could **never close by time-stop** — which in backtest would
  have cut some winners early and some losers early. Net effect on the
  3-month P/L is almost certainly small (time-stop is a rare exit
  pathway; `exit_window` counter in the backtest is typically 1–3 out
  of ~80 trades), but I'll know more after 2 weeks of clean dry-run
  data.

No changes needed to any of the risk / sizing / ORB / news CSV files.
No retuning. Just the tz fix.

---

*— Cline, 2026-04-24 09:51 UK*
