# V30.4 — Broker Pip-Value Fix (DAX40 EUR→USD conversion)

**Date:** 2026-04-30
**Severity:** Sizing inaccuracy on DAX40 only.  US30 / US500 / XAUUSD unaffected.

---

## What the bug was

`src/live/v30_live.py` hardcoded `pip_value_per_lot = $1.00 / point / lot`
for every symbol it traded.  That assumption is correct for **USD-quoted**
indices (US30, US500, XAUUSD on a USD account) but **wrong** for the
**EUR-quoted** DAX40 / DE40 / GER40 contract.

Mechanism on a USD-denominated 5ers account:

```
DAX40 P/L per point per 1.0 lot:
    €1.00  (raw contract spec)
    × EURUSD  (live FX, ≈1.166 today)
    = $1.166
```

Because the bot used $1.00 instead of $1.166 to compute *planned risk*,
it ended up with two correlated errors:

1. **Planned risk was understated** by ~14-17 %.  When the bot wanted
   to risk e.g. $853, the real risk on a stop-out was $1,019.
2. **Lot sizes were inflated** by the same fraction, because
   `lots = risk_$ / (sl_distance × pip_value)` divides by a number
   that's too small.

Concrete example from 2026-04-30:

| Field | Bot's view | Broker truth |
|---|---|---|
| Symbol | DAX40 | DAX40 |
| Side | LONG | LONG |
| Lots | 11.5 | 11.5 |
| SL distance (points) | 76.04 | 76.04 |
| `pip_value_per_lot` | **$1.000** | **$1.166** |
| Planned risk_$ | ~$853 | ~$1,019 |
| Actual SL hit cost | — | **−$1,019.84** |

The bot logged a clean −1.000R outcome internally, but the equity curve
moved −1.195R.  Multiplied across DAX40 trades that compounds.

The bug was **pre-existing** — it dates to v30 launch.  The recent
parity work (commit 7ccd369 / f963e65) is unaffected: those fixes were
about lot-step / SL-buffer parity between live and backtest, not the
underlying `pip_value` table.

---

## The fix (v30.4)

Three new pieces, no breaking changes.

### 1. `src/live/broker_specs.py` (new)

Self-contained helper that calls `MetaTrader5.symbol_info(broker_sym).trade_tick_value`
for every symbol the bot trades.  `trade_tick_value` is the cash value
in **account currency** of a 1-tick move on 1.0 lot — MT5 already does
the FX conversion internally, so we never have to hard-code an FX rate.

If the `MetaTrader5` python package isn't installed, or the terminal
isn't reachable, or any single symbol returns garbage, we fall back to
the hardcoded $1.00 / pt and log a loud WARNING.  The bot still trades
— it just sizes DAX40 the old (wrong-on-EUR) way until you fix the
environment.

### 2. `src/live/v30_live.py`  (one new method, called from `start()`)

New method: `_apply_broker_pip_value_overrides()`.  Runs once at
startup, after the START banner, before warm-up:

* Calls `fetch_live_pip_values(bot_to_broker)`
* Logs the **BROKER PIP-VALUE TABLE** banner to the PowerShell console
* Mutates `self.specs[sym].pip_value_per_lot` for every symbol whose
  broker-truth tick value differs from the hardcoded fallback
* Records `self._pip_value_source` ("broker_live" / "fallback_*") so
  preflight #14 / heartbeat / WARMUP_RESTORE event can see it

Expected console output on a healthy USD account:

```
==============================================================================
  BROKER PIP-VALUE TABLE   source=broker_live
==============================================================================
    DE40    $/pt/lot =   1.1664   ✓ broker-truth (was $1.00, +16.6% via FX)
    US30    $/pt/lot =   1.0000   (USD-quoted, no FX needed)
    US500   $/pt/lot =   1.0000   (USD-quoted, no FX needed)
    XAUUSD  $/pt/lot =   1.0000   (USD-quoted, no FX needed)
==============================================================================
```

### 3. `Scripts/probe_broker_pip_values.py` (new)

Standalone CLI tool.  Run it on the VPS *before* going live to verify
broker numbers without launching the bot:

```
cd C:\PropBot
python Scripts\probe_broker_pip_values.py
```

Exit-codes are stable: `0` on success, `1` on any fallback, so you can
gate launches in PowerShell with `if ($LASTEXITCODE -ne 0) { exit 1 }`.

The launcher `PULL_AND_GO_LIVE_V30.ps1` now calls this in **STEP 4/6**
between dependency installation and the parity tests, so a broken MT5
environment is caught loudly instead of silently sizing DAX40 wrong.

### 4. `requirements.txt`

Adds:

```
MetaTrader5>=5.0.45 ; sys_platform == "win32"
```

Windows-only.  Linux CI runs (where MT5 isn't available) skip it
automatically thanks to the environment marker.

---

## Effect on the Merton-GZ sizer

The historical 271-trade pool **is not replayed**.  We only change how
*future* R-multiples are recorded.

* **Before fix** — a DAX40 SL hit recorded
  `realised_R = pnl_$ / planned_risk_$ ≈ −$1,019 / $853 ≈ −1.195R`.
  μ̂ was being pulled deeper-negative than the strategy actually was.

* **After fix** — same SL hit records `realised_R = −1.000R` cleanly,
  because both numerator and denominator now use the same broker-truth
  pip value.  μ̂ and σ̂² calibrate honestly going forward.

* **Today's next-trade sizing is unchanged** (~$119 risk).  The fix
  affects DAX40 *lot calculation* only — same risk_$, but spread over
  ~14% fewer lots, which produces an identical $-loss if SL hits.

---

## How to verify on the VPS

```powershell
cd C:\PropBot
git pull
python -m pip install -r requirements.txt        # picks up MetaTrader5
python Scripts\probe_broker_pip_values.py        # prints the table
```

If you see `source=broker_live` and DE40 ≈ $1.16-$1.18, you're good.
If you see `source=fallback_*` then either the MT5 terminal isn't
running, or the python package wasn't picked up — fix that before
launching real-money trading.

The launcher `PULL_AND_GO_LIVE_V30.ps1` already does all of this for
you and aborts before placing a single order if anything's wrong.

---

## What the engine no longer does

* No more silent assumption that every contract is USD-quoted.
* No hard-coded FX rate (we never had one — we just *implicitly*
  assumed EURUSD = 1.000).
* No need to manually update anything when EURUSD moves — every bot
  restart re-pulls live broker truth.

## What the engine still does

* Honours the broker's own tick_value verbatim.  If the broker reports
  something insane, we fall back rather than trade on bad data.
* Logs every override decision to `events.log` as `PIP_VALUE_OVERRIDE`
  with the old / new values per symbol, so post-mortems can see exactly
  which numbers were in play on any given day.
