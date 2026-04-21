# v16.1 – Warm-up & Live Telemetry

**Date:** 2026-04-21
**Added to:** SmartBB v16 (engine unchanged; launcher + live runner upgraded)

You asked two very sharp questions:

> 1. Does the bot work better the more info it has?
> 2. Can I see where each symbol is up to on the Z-score so I know
>    when we're close to a trade?

**Yes, both.** This doc is what we shipped to cover them.

---

## 1. Warm-up — answering "more info ⇒ better from tick 1"

### Why it matters
Without warm-up, the v16 engine needs **live** bars before every gate is hot:

| Indicator                    | Window (M5 bars) | Equivalent wall-clock |
|------------------------------|-----------------:|----------------------:|
| Bollinger Z-score            |              200 |        ~16 hrs        |
| Hurst rolling                |              500 |        ~40 hrs        |
| OU half-life                 |              300 |        ~25 hrs        |
| **abs-Z rolling quantile**   |         **1000** |      **~83 hrs**      |
| **Hurst rolling quantile**   |          **500** |      **~40 hrs**      |

Worst case ⇒ ~3.5 **days** of zero-signal "staring" before the bot fires a trade.

### What we built
Two stages, both run automatically by `run_v16_live.py` before `run()`:

**Stage A — Kelly trade history warm-up**
```
WARM-UP  A/B  Kelly trade history
  ✅ loaded 186 historical R-values into sizer from Results/v16_SC_100000_3m_trades.json
  ✅ Kelly is ACTIVE from bar 1 (≥ 10 per side for all symbols)
```
Pre-loads the most-recent realised-R per (symbol, side) from a v16 backtest
trade log into `DynamicSizerV16`. This directly fixes the "Kelly needs
30+ live trades to activate" cold-start we documented in
`V16_RESULTS_HONEST.md §4`. The genius math is working from trade 1.

**Stage B — Engine indicator warm-up from broker M1 history**
```
WARM-UP  B/B  Engine indicators  (5000 M1 bars/symbol)
  ✅ warm-up complete — per-symbol bars streamed:
     US30    5,000 bars  (~ 3.5 days)  BB+Hurst hot, quantiles warming
     US100   5,000 bars  (~ 3.5 days)  BB+Hurst hot, quantiles warming
     US500   5,000 bars  (~ 3.5 days)  BB+Hurst hot, quantiles warming
     DE40    5,000 bars  (~ 3.5 days)  BB+Hurst hot, quantiles warming
     XAUUSD  5,000 bars  (~ 3.5 days)  BB+Hurst hot, quantiles warming
```
Pulls the last N M1 bars per symbol via `MT5Bridge.get_history()` and streams
them through `engine.on_bar(...)` so every rolling indicator is populated.
Any synthetic "trades" materialised during replay are cleared and equity is
reset to the starting equity — the live P&L starts at zero.

### Tuning knobs
```powershell
--warmup-bars 5000              # ~3.5 days, BB+Hurst hot from bar 1 (default)
--warmup-bars 12000             # ~8 days, ALL gates including quantiles hot
--warmup-bars 0                 # disable (old behaviour)
--warmup-sizer-from Results\v16_SC_100000_3m_trades.json  # (default)
--warmup-sizer-from ""          # disable Kelly pre-load
```

Rule of thumb:
- **0–2000 bars**: BB will warm up in a few hours, Hurst / quantiles not
- **2000–5999 bars**: BB + Hurst hot, quantiles still warming (quantile
  gate just lets everything through until 250 samples present)
- **6000+ bars**: every gate is hot from tick 1

### Does it literally improve outcomes?

Yes, in three ways:
1. **Shorter "dead" period**: v16 can open a valid trade the first time a
   qualifying setup appears rather than waiting ~3 days.
2. **Better Kelly from day 1**: instead of sitting at cold-start risk
   (0.50 %), Kelly can already size up on the symbols where the backtest
   shows strong edge and down on weaker ones.
3. **Calendar + blackout counts are already meaningful on day 1** — the
   warm-up itself doesn't trade, but the live telemetry shows which
   gate is active immediately.

---

## 2. Live telemetry — "am I close to a trade?"

### What you see now
Every `--heartbeat-sec` seconds (default 60 s) v16 writes this to the
heartbeat log:

```
── TELEMETRY 2026-04-21T13:47:00+00:00 ─────────────────────────
   eq=$100,243  peak=$100,243  dd=+0.00%  trades=0  blackouts={'weekend': 120}
   SYM     PRICE    Z       |Z|/trig HURST   HL  READY                 BLOCK  POS
   US30    41258.50  +1.82      68%   0.45    14    YES                    -  -
   US100   18224.30  -2.65     102%   0.41    11    YES            z_min_abs  -
   US500    5301.20  +0.44      16%   0.52    22     NO  hurst_abs_max (0.52)  -
   DE40    18902.00  -3.11     118%   0.38     9    YES                    -  SHORT @ 18902.00 x0.04 lots SL=18919.50 TP=18868.40
   XAUUSD   2412.88  +0.92      35%   0.47    17    YES            z_quantile  -
```

Legend:
- **PRICE** — last close the engine saw
- **Z** — signed Bollinger z-score (position relative to BB mean ±σ)
- **|Z|/trig** — distance to the adaptive quantile trigger.
  `100%` means the z-gate is exactly at the firing threshold; `68%` means
  we need the z-score magnitude to grow ~47 % more to fire.
- **HURST / HL** — current Hurst exponent and OU half-life (bars)
- **READY** — all three quantile gates have ≥ min_samples
- **BLOCK** — the first gate that would reject an entry right now
  (`-` if nothing is blocking i.e. a trade is about to fire this bar)
- **POS** — open position if any (side, entry, lots, SL, TP)

### JSON dump for dashboards
Same content written to **`Results/v16_live_telemetry.json`** every
heartbeat so you can stream it into Grafana / a Discord bot / whatever.

```json
{
  "ts_utc": "2026-04-21T13:47:00+00:00",
  "equity": 100243.5,
  "dd_pct": 0.0,
  "trades": 0,
  "blackouts": {"weekend": 120},
  "symbols": {
    "US30":  {"last_price": 41258.5, "z": 1.82,  "abs_z": 1.82,
              "z_trigger": 2.67, "dist_to_trigger_pct": 68.2,
              "hurst": 0.45, "ou_halflife_bars": 14.0,
              "ready": true, "next_gate_blocking": null,
              "position": "-"},
    "DE40":  {"last_price": 18902.0, "z": -3.11, "abs_z": 3.11,
              "z_trigger": 2.64, "dist_to_trigger_pct": 117.8,
              "hurst": 0.38, "ou_halflife_bars": 9.0,
              "ready": true, "next_gate_blocking": null,
              "position": "SHORT @ 18902.00 x0.04 lots SL=18919.50 TP=18868.40"}
  }
}
```

### When should you act on it?
- **`dist_to_trigger_pct > 90 %`** AND `BLOCK == "-"` ⇒ a trade is 1–2
  bars away. Watch it.
- **`BLOCK == "calendar:*"`** ⇒ the signal WOULD fire but the calendar
  blackout is holding it back. Expected on Fri-evening / Sun-afternoon
  and at 20:58-22:02 UTC rollover.
- **`BLOCK == "hurst_abs_max"`** ⇒ market is too trending for mean-
  reversion right now. This is good risk control; don't touch.
- **Chronic `READY == "NO"`** after 12+ hours live ⇒ warm-up didn't
  stream enough bars; run with `--warmup-bars 12000`.

---

## 3. Full start-up sequence (what you actually see when you run it)

```
python Scripts\run_v16_live.py --live --risk-scale 0.5

[banner] SmartBB v16  LIVE LAUNCHER  2026-04-21T12:00:00+00:00
 ...
[banner] PRE-FLIGHT 1/10  ZMQ bridge                          ✅
[banner] PRE-FLIGHT 2/10  MT5 account info                    ✅
[banner] PRE-FLIGHT 3/10  Symbol resolution + quotes          ✅
[banner] PRE-FLIGHT 4/10  5%ers hard-caps not tripped         ✅
[banner] PRE-FLIGHT 5/10  Tuning JSON loads, 5/5 TIER1        ✅
[banner] PRE-FLIGHT 6/10  Dry/live / risk-scale sanity        ✅
[banner] PRE-FLIGHT 7/10  Broker SL / TP / lot step checks    ✅
[banner] PRE-FLIGHT 8/10  Disk + log directory writable       ✅
[banner] PRE-FLIGHT 9/10  Trading calendar                    ✅
[banner] PRE-FLIGHT 10/10 Dynamic sizer config                ✅

[banner] ENGINE STARTING
[banner] WARM-UP  A/B  Kelly trade history
  ✅ loaded 186 historical R-values into sizer from Results/v16_SC_100000_3m_trades.json
  ✅ Kelly is ACTIVE from bar 1 (≥ 10 per side for all symbols)

[banner] WARM-UP  B/B  Engine indicators  (5000 M1 bars/symbol)
  ✅ warm-up complete — per-symbol bars streamed:
     US30    5,000 bars  (~ 3.5 days)  BB+Hurst hot, quantiles warming
     ...

[smartbb.v16.live] SmartBB v16 LIVE | symbols=['US30','US100','US500','DE40','XAUUSD']
[smartbb.v16.live] ── TELEMETRY ... ──────────   ← IMMEDIATE view of every symbol
[smartbb.v16.live] ♥ eq=$100,000 trades=0 pnl=$0  ← every 60 s from here
[smartbb.v16.live] ── TELEMETRY ... ──────────
```

No more "why isn't it trading?" mystery — you can see exactly which gate
is holding each symbol back, and how far each symbol is from a fire.

---

## 4. Updated one-liners

```powershell
# DRY-RUN with full warmup + telemetry (use this first, 24-48h)
.\GO_DRYRUN_V16.ps1

# LIVE calendar-only, 0.5 % fixed risk, with warmup + telemetry
.\GO_LIVE_V16.ps1 -Risk 0.5 -NoSizer

# LIVE with full v16 (Kelly already warm from backtest history)
.\GO_LIVE_V16.ps1 -Risk 0.5

# If your broker doesn't expose enough history, disable warmup
python Scripts\run_v16_live.py --live --warmup-bars 0

# Slower heartbeat if the telemetry is spamming your log
python Scripts\run_v16_live.py --live --heartbeat-sec 300
```

---

## 5. Files delivered in v16.1

```
src/live/warmup.py                   # 2-stage warm-up (engine + sizer)
src/live/v16_live.py                 # adds telemetry_snapshot + _log_telemetry + overrides run()
Scripts/run_v16_live.py              # adds --warmup-bars / --warmup-sizer-from / --heartbeat-sec
Docs/V16_WARMUP_AND_TELEMETRY.md     # this doc
```

**v14 engine, v15 live, v16 engine**: **all unchanged**. The warm-up module
only calls public methods on existing objects and doesn't mutate any
engine code.
