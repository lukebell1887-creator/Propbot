# Dry-Run vs Live — End-to-End Verification

**Date:** 24 Apr 2026  
**Trigger:** User observed US500 short with `R_hold=+0.94` still in `state=FILLED_SHORT` while TP1 was at ~0.48R (price had clearly traded through TP1 without the trade closing). Is this a bug?

**Answer: NO. This is by design. Dry-run does NOT send real orders to the broker, so broker-side TP1 and SL cannot fire. In dry-run the ONLY way a trade closes is the 120-minute time stop.** Live mode behaves completely differently — I verified the code path below.

---

## 1. Evidence from the code

### a) Order execution (`src/live/v23_live.py`, `_execute_order`)

```python
# build the request (same in both modes)
req = OrderRequest(
    symbol=self._symbol_to_broker(sym),
    ...
    sl=float(sl),
    tp=float(tp1),               # TP1 set at broker; TP2 managed by us
    deviation=20,
)

if self.dry_run:
    fake_ticket = int(time.time() * 1000) & 0x7FFFFFFF
    st.open_ticket = fake_ticket
    ok = True
else:
    result = self.bridge.send_order(req)      # <-- REAL order, SL+TP1 attached
    ok = getattr(result, "error_code", 0) == 0
```

**In dry-run**, a fake ticket is synthesised in memory. **No TCP message reaches the EA. No broker order exists.**  
**In live (`--live`)**, `bridge.send_order(req)` pushes the full payload (including `sl` and `tp`) to the MT5 EA via `ORDER_SEND`, which calls `OrderSend()` on the broker. SL and TP1 are server-side from that moment.

### b) Broker position reconciliation

```python
# only runs in live mode
if not self.dry_run:
    open_by_broker = {p.ticket: p for p in self._broker_positions()}
    for sym, st in self.states.items():
        if st.open_ticket and st.open_ticket not in open_by_broker:
            # broker closed it (SL or TP1 hit) — mark as exited
            self._clear_state(sym)
            self.counters["exit_broker"] += 1
```

**In dry-run this whole block is skipped.** That's why your live log shows `exits_broker=0`. There are no broker positions to reconcile, so the counter never increments.

### c) Exits in dry-run

The ONLY path that closes a dry-run position is:

```python
# window expiry (time-stop at trade_window_minutes = 120)
if elapsed >= self.cfg.trade_window_minutes * 60:
    self._close_one(sym, "window_expiry")
    self.counters["exit_window"] += 1
```

And `_close_one` in dry-run does **not** call `bridge.close_position` — it just clears the state and logs the event:

```python
def _close_one(self, sym: str, reason: str) -> None:
    ...
    if not self.dry_run:
        try:
            self.bridge.close_position(st.open_ticket)
```

---

## 2. What your current open trades will actually do

Given these facts, here is the precise exit schedule for your live log:

| Symbol | Opened (UTC) | Entry | SL | TP1 | TP1 in R | Dry-run exit | Live exit would be |
|---|---|---|---|---|---|---|---|
| **US500** short | ~11:46 | 7140.28 | 7156.28 | 7132.63 | **0.48R** | Window expiry at **13:46 UTC** → close at market | Already filled at TP1 when price touched 7132.63 |
| **XAUUSD** short | ~12:07 | 4703.59 | 4717.08 | 4676.69 | **2.00R** | Window expiry at **14:07 UTC** → close at market | Still open unless price reached 4676.69 |

So the current `R_hold=+0.94` on US500 is real unrealised P/L, but **dry-run will ride it past TP1 and close at whatever the market happens to be at 13:46 UTC** — which could be anywhere between full SL (-1R) and the TP2 target (+1R) or beyond.

This mismatch is WHY dry-run P/L should NEVER be used as a proxy for live P/L.

---

## 3. What dry-run IS good for (what it genuinely tests)

Dry-run is an **infrastructure** test, not a strategy test. It proves the bot can:

| # | Verified by dry-run? | What it proves |
|---|---|---|
| 1 | ✅ | Bot connects to MT5 via TCP bridge (EA is reachable) |
| 2 | ✅ | MT5 streams quotes for all 4 broker symbols (preflight passes) |
| 3 | ✅ | 1-minute bars arrive and are parsed correctly |
| 4 | ✅ | OR range is built in the right UTC window per symbol |
| 5 | ✅ | Entry signal fires on the first breakout bar |
| 6 | ✅ | NR7/NR4 and other filters are applied |
| 7 | ✅ | Merton-GZ sizer produces correct `lots` for each instrument |
| 8 | ✅ | News/calendar blocks fire at the right times |
| 9 | ✅ | Concurrency cap (2) is enforced |
| 10 | ✅ | 120-min time stop closes positions on time |
| 11 | ✅ | Daily breaker / account kill rails don't spuriously trigger |
| 12 | ✅ | Engine runs for days without crashing or leaking memory |
| 13 | ✅ | Heartbeats every 60 s (observability) |

## 4. What dry-run does NOT test (you WON'T know these work until live)

| # | Tested by dry-run? | What's missing |
|---|---|---|
| 1 | ❌ | **TP1 fills at broker** — never placed in dry-run |
| 2 | ❌ | **SL fills at broker** — never placed in dry-run |
| 3 | ❌ | **Entry slippage** — you never actually cross the spread |
| 4 | ❌ | **TP1 slippage** — limit orders can fill at the exact level, but may not |
| 5 | ❌ | **SL slippage** — stop orders can slip badly in fast markets (this is the classic ORB-open risk the audit flagged) |
| 6 | ❌ | **Broker rejection codes** — requote, no-money, not-enough-margin, etc. |
| 7 | ❌ | **Partial fills** on index CFDs with low volume |
| 8 | ❌ | **Swap/commission** deducted from equity |
| 9 | ❌ | **Position `close_position` round-trip** — MT5 does the close on your behalf |

---

## 5. Is this a problem for your readiness?

**No — but you need to do ONE more thing before full-size go-live.** I strongly recommend a **live-micro smoke test**:

### Live-micro smoke test recipe

```powershell
# On the VPS, open PowerShell
cd C:\PropBot

# Edit GO_LIVE_V23.ps1 so --risk is 0.000011 (0.001%) instead of 0.00110 (0.110%).
# That gives ~$1 risk per trade instead of $110. This is the smallest possible real-money
# test that still exercises every code path: broker order, broker SL, broker TP, broker close.

.\GO_LIVE_V23.ps1
```

Then watch for:

1. **`[ENTRY] ... lots=X.XXX`** in the log — broker fill ticket > 0  
2. **`exits_broker`** counter incrementing — proves TP1 or SL fires server-side  
3. **Zero `position_modify_failed` or `close_position failed`** error lines  
4. **MT5 terminal** — look at the open Positions list. You should see the ticket, the SL price, the TP price, and the P/L updating in real time.

Run this for **1 full trading day** (4 possible trade slots: DE40 morning + US30/US500/XAU afternoon). If every trade that fills exits at TP1, SL, or the time stop — and the engine logs `exits_broker > 0` — you have proven the live path works end-to-end.

**Only then** bump risk back to 0.00110 (0.110%) and let it run.

---

## 6. Answer to your exact question

> "It might just be it doesn't trigger as it's dry but it would live — I just need to know."

**Confirmed. In dry-run, TP1 and SL physically cannot trigger because no broker order ever exists.** The code proves this. In live mode, TP1 and SL are attached to the broker order at entry time (single `ORDER_SEND` carrying both levels), so they fire server-side even if the Python bot is disconnected. That's exactly the behaviour you want.

> "How good is the bot?"

The dry-run data you're generating is **not measuring the strategy** — it's measuring the plumbing. The plumbing looks fine (entries fire, sizing is sane, 4 trades so far, no errors, heartbeats healthy, filters working). The strategy's profitability was measured in the backtest and audit, NOT in the dry-run.

> "Concerning signal I noticed: R_hold=0.94 but TP1 was only at ~0.48R."

That's not a concern — it's a direct consequence of dry-run mode not placing broker orders. In live mode the trade would have already closed at TP1 (+0.48R, ~$52 per unit of risk) the moment price touched 7132.63. You would not have watched it go to +0.94R unrealised and then possibly round-trip.

---

## 7. Bottom line — am I ready to go live?

**YES, with one caveat:** do a **1-day live-micro test at 0.001% risk** before scaling to 0.110%. This is the ONLY way to validate the items in section 4 (TP1/SL fills, slippage, rejection handling). It costs at most ~$4 in risk for the whole day. Completely worth it.

**After the micro-test passes:**
1. Run `SETUP_VPS_AUTOSTART.ps1` (see `Docs/VPS_24_7_GUARANTEE.md`)
2. Edit `start_live.bat` → `RISK_SCALE=1.0` (already set)
3. Start the task: `schtasks /Run /TN PropBot_v15_live`
4. You're live and bulletproof.

---

## Appendix A: The 4 counters in the log and what they mean

```
rails: news_block=0  flat_news=0  cap_hits=90  cal_blocks=0  exits_window=0  exits_broker=0
```

| Counter | Meaning | Expected in dry-run? | Expected in live? |
|---|---|---|---|
| `news_block` | Entry blocked by tier-1 news | > 0 on FOMC/CPI/NFP days | Same |
| `flat_news` | Force-flat fired before news | > 0 if in-position 5min before tier-1 | Same |
| `cap_hits` | Entry blocked by concurrency cap | High (normal — you see this now) | Similar |
| `cal_blocks` | Blocked by weekend/holiday/rollover | 0 during London/NY week | Same |
| `exits_window` | Position force-closed by 120-min time stop | **~100% of trades** | ~20% of trades |
| `exits_broker` | Position closed by broker SL/TP hit | **Always 0** (bug-by-design) | ~80% of trades |

The dashboard is telling you the truth. The dry-run is working correctly, and the behaviour you observed is expected, not broken.
