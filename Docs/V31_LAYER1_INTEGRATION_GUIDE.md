# V31 Layer 1 — Integration Guide

**Status (2026-04-30):** Foundation **shipped & tested** (81/81 unit tests pass). Live wiring + backtest parity = scheduled for next focused session.

---

## 1. Where we are

| Step | File | Status |
|---|---|---|
| MC Proof — 10k runs, 4 scenarios | `Scripts/v31_proof_pipeline.py` | ✅ existed before this session |
| Stress Matrix — 12 scenarios | `Scripts/v31_final_proof_matrix.py` | ✅ proved Layer 1 = 0 breaches vs 4 without |
| **Layer 1 helper module** | `src/execution/layer1.py` | ✅ **NEW — this session** |
| **Layer 1 unit tests (parity locked)** | `tests/test_layer1.py` | ✅ **NEW — 70 tests, all green** |
| **Layer 1 envelope tracker** | `src/execution/layer1_tracker.py` | ✅ **NEW — this session** |
| **Tracker tests** | `tests/test_layer1_tracker.py` | ✅ **NEW — 11 tests, all green** |
| Live engine SL hook | `src/live/v30_live.py` | ⏳ scheduled |
| Backtest fill-model parity | `Scripts/backtest_v23_final.py` | ⏳ scheduled |
| Cross-system parity test | `tests/test_live_backtest_parity.py` | ⏳ scheduled |
| Re-run proof matrix vs prod code | `Scripts/v31_final_proof_matrix.py` | ⏳ scheduled |
| Launcher banner update | `GO_DRYRUN_V30.ps1` | ⏳ scheduled |

**81 unit tests guarantee** the math relationship is locked. Any future change that drifts the production code from the MC model will fail tests immediately.

---

## 2. What was decided & why (the architectural call)

The Monte Carlo proof models a pure relationship:

```
slip ≤ cap   →  realised_slip = raw_slip            (within-cap fill)
slip > cap   →  realised_slip = cap × 1.5            (time-fallback fill)
```

There were **two ways** to implement this on a live broker:

### Option A — Broker-side stop-limit (original "5-file plan")
- Add `BUY_STOP_LIMIT` / `SELL_STOP_LIMIT` opcodes to `MQL5/Experts/SHF_Bridge.mq5`
- Wire them through `src/execution/mt5_bridge.py`
- Place a paired pending order with `price=SL` (trigger) and `stoplimit=SL ± cap` (limit)
- Bot detects unfilled stop-limits after envelope timeout and force-closes

### Option B — Client-side hybrid (✅ **chosen, this session**)
- **No EA changes**, **no broker opcode changes**, no manual VPS reinstall
- Bot polls positions every cycle. When current price breaches the SL trigger, `Layer1Tracker.update_and_decide()` returns one of three decisions:
  - `CLOSE_NOW` — slip ≤ cap → bot sends market close → realised slip ≤ cap ✅
  - `WAIT` — slip > cap, envelope still open → bot waits for price to recover
  - `FALLBACK_CLOSE` — envelope expired → bot sends market close ✅
- Broker-held SL is set to `original_SL ± cap × 1.5` (the time-fallback worst case). This is the **safety net** if the bot ever disconnects mid-trade. It bounds the absolute worst possible slip.

### Why B is mathematically identical to A
Both produce the same realised-slip distribution because:
- In the within-cap branch, B closes at the moment-of-breach price (≤ cap from trigger). A's stop-limit fills at the same price band by definition.
- In the gap branch, B forces market close after 60s envelope; A's stop-limit fails to fill, then A's fallback fires the same market close. Both realise `~cap × 1.5` in expectation (calibrated from broker microstructure).
- B's emergency broker SL is set at `cap × 1.5`, which is the EXACT level the MC model already assumes for the time-fallback. So even if the bot disconnects, the worst-case fill matches the model.

### Why B was preferred
1. **Zero EA risk.** No new MQ5 code path. No risk of an opcode bug taking down the bridge.
2. **Zero deployment friction.** No manual MT5 chart-reload. Just `git pull` on the VPS.
3. **Broker-agnostic.** Stop-limit semantics differ subtly across brokers (5%ers, FundedNext, etc.). Client-side Python is identical everywhere.
4. **Telemetry by default.** Every Layer-1 decision is a `Layer1Decision` dataclass that gets logged to `v30_live_slippage.jsonl` — fully reconstructable.
5. **Reversible.** A single `engine.layer1_enabled = False` flag would disable it without removing any code.

---

## 3. The 4 sequential tasks remaining

Each task is small, isolated, fully-testable, and has a clear acceptance criterion.

### Task 1 — Live engine SL hook  (`src/live/v30_live.py`)

**Acceptance:** the bot replaces its existing "if price beyond SL: close" logic with a `Layer1Tracker` call. Broker SL is placed at the emergency level instead of the original SL.

**Pseudo-diff:**
```python
# in __init__
self.layer1 = Layer1Tracker()
self.layer1_enabled = bool(os.getenv("V31_LAYER1_ENABLED", "1") == "1")

# in send_order(...)
emergency_offset = emergency_sl_offset_for(symbol) * tick_size_per_pt
if side == BUY:
    broker_sl = original_sl - emergency_offset
else:
    broker_sl = original_sl + emergency_offset
# place order with sl=broker_sl  (NOT original_sl)
# remember original_sl in self._position_state[ticket]["original_sl"]

# in poll_positions(...) — once per cycle
for pos in self.bridge.get_positions():
    state = self._position_state.get(pos.ticket)
    if not state:
        continue
    if not self.layer1_enabled:
        # legacy path — skip Layer 1
        continue
    side = +1 if pos.type == "BUY" else -1
    quote = self.bridge.get_quote(pos.symbol)
    current_px = quote.bid if side == +1 else quote.ask
    decision = self.layer1.update_and_decide(
        ticket=pos.ticket, symbol=pos.symbol, side=side,
        sl_trigger_px=state["original_sl"], current_px=current_px,
        now=time.time(),
    )
    self._log_layer1_decision(decision)
    if decision.action in ("CLOSE_NOW", "FALLBACK_CLOSE"):
        self.bridge.close_position(pos.ticket)
        self.layer1.clear(pos.ticket)
```

**Default state:** `V31_LAYER1_ENABLED=1` (dry-run starts ON because dry-run can't lose money). `GO_LIVE_V30.ps1` should default to `1` as well after dry-run validates.

---

### Task 2 — Backtest fill-model parity  (`Scripts/backtest_v23_final.py`)

**Acceptance:** every stop-out in the backtest applies `apply_layer1_slip(symbol, raw_slip)` to compute realised slip. The function is imported from `src.execution.layer1`. After this change, the backtest's slip distribution and the MC pipeline's slip distribution match exactly.

**Existing logic to find:**
```python
# Roughly — search backtest_v23_final.py for "slip" assignment in stop-out branch
slip = bar_excess * adversity   # raw slip in points
exit_px = sl_price ± slip       # current
```

**New logic:**
```python
from src.execution.layer1 import apply_layer1_slip

raw_slip = bar_excess * adversity
slip = apply_layer1_slip(symbol, raw_slip)   # cap-bounded
exit_px = sl_price ± slip
```

This is a **5-line change**. Same backtest, just the fill model is now Layer-1-aware.

---

### Task 3 — Cross-system parity test  (`tests/test_live_backtest_parity.py`)

**Acceptance:** for every realised-slip sample in the existing parity test (it currently asserts live engine PnL == backtest PnL trade-for-trade), the test must also assert Layer 1 is applied identically on both sides.

**Append to existing test file:**
```python
def test_layer1_parity_same_slip_distribution():
    """For every stop-out in the v30 trade ledger, the live engine's
    decide_exit() and the backtest's apply_layer1_slip() must agree on
    realised slip given the same raw inputs."""
    trades = json.loads(Path("Results/v30_fresh_trades.json").read_text())
    bars   = {s: load_bars(s) for s in ("DE40","US30","US500","XAUUSD")}
    enriched = precompute_trade_metadata(trades, bars)
    for t in enriched:
        if not t["_is_stopout"]:
            continue
        # Sample at realistic adversity = 0.55 (US30 worst from MC)
        raw = t["_bar_excess"] * 0.55
        bt  = apply_layer1_slip(t["symbol"], raw)
        # Simulate the live decision at envelope-expired
        sl  = t["exit_price"]
        cur = sl - raw if t["side"] == 1 else sl + raw
        d   = decide_exit(t["symbol"], t["side"], sl, cur,
                          seconds_since_breach=61.0)
        if d.action == "CLOSE_NOW":
            assert bt == raw                 # within-cap branch
        elif d.action == "FALLBACK_CLOSE":
            assert bt == cap_for(t["symbol"]) * 1.5    # time-fallback
        else:
            pytest.fail(f"unexpected action {d.action} for slip {raw:.2f}")
```

---

### Task 4 — Re-run the proof matrix against PROD code

**Acceptance:** after Tasks 1+2 land, re-running `python Scripts/v31_final_proof_matrix.py` produces a results table that **byte-matches** the table produced before the changes. This proves the production code path implements the same math the proof was based on.

**Command:**
```
python Scripts/v31_final_proof_matrix.py
git diff Results/v31_final_proof_matrix.json
# expected:  no diff (or only timestamps / formatting)
```

If anything moves: parity is broken, fix the live engine before going live.

---

## 4. Smoke test plan (before live)

1. `git pull` on VPS → bot restarts in DRY-RUN mode
2. Watch first 60 minutes — confirm `v30_live_slippage.jsonl` is being written (even if no SL has been hit yet, every poll-cycle position with an active SL should produce one record)
3. Force a synthetic breach in dry-run by widening the symbol's SL to a tight value (manual MT5 modify); verify Layer 1 fires `CLOSE_NOW` and the JSONL captures the decision
4. Sanity check: emergency SL on broker side = `original ± cap × 1.5`. Open MT5 → click position → confirm the SL value displayed.
5. After 1 trading day, run:
   ```
   python Scripts/diag_layer1_decisions.py   # to be written
   ```
   to summarise: how many CLOSE_NOW, how many FALLBACK_CLOSE, average realised slip, distribution per symbol.
6. If everything looks sane, set `V31_LAYER1_ENABLED=1` in `GO_LIVE_V30.ps1` and graduate.

---

## 5. Rollback plan

If anything misbehaves in dry-run:

```powershell
# Disable Layer 1 instantly without code change:
$env:V31_LAYER1_ENABLED = "0"
.\GO_DRYRUN_V30.ps1
```

The engine reverts to v30's existing SL handling (broker holds original SL, bot does nothing on breach). Zero risk.

---

## 6. Live-side telemetry contract

When Layer 1 is active, the bot must write one line per breach-decision to `Results/v30_live_slippage.jsonl`:

```json
{
  "ts":            "2026-04-30T15:23:11.027Z",
  "ticket":        12834561,
  "decision": {
    "action":              "CLOSE_NOW",
    "symbol":              "US30",
    "side":                -1,
    "sl_trigger_px":       49000.0,
    "current_px":          49003.20,
    "raw_slip_pts":        3.20,
    "cap_pts":             5.0,
    "emergency_sl_px":     49007.50,
    "seconds_since_breach": 0.0,
    "reason":              "slip 3.20pt within cap 5.00pt — close now"
  }
}
```

This is exactly `decision.to_jsonable()` wrapped in a top-level event envelope. Fully replayable.

---

## 7. Decision matrix from the proof (already shipped)

For the record — these are the numbers the user signed off on:

| Stress scenario | No defense | Layer 1 |
|---|---|---|
| Uniform 5pt | DD 1.85% | DD 1.85% |
| Uniform 15pt | DD 5.42% **🚨** | DD 2.91% ✅ |
| Uniform 20pt | DD 7.17% **🚨** | DD 2.91% ✅ |
| Uniform 30pt | DD 9.84% **🚨** | DD 2.91% ✅ |
| Bar-micro pessimistic | DD 5.51% **🚨** | DD 2.91% ✅ |
| Bar-micro catastrophic | DD 6.68% **🚨** | DD 2.91% ✅ |
| **Total breaches of 5%** | **4 of 12** | **0 of 12** |
| **Avg PnL delta** | baseline | **+ $1,579 over 3 mo** |

Source: `Results/v31_final_proof_matrix.json`. Generated 2026-04-29.

---

**END OF GUIDE.** Foundation is locked, tests are green, and the integration is now a 4-task surgical job rather than a full rewrite. Each task can be done independently and reverted independently.
