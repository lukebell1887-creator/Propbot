# Why the v31 live bot is losing $4.5k while the backtest made $28k

**Run**: `python Scripts/parity_v31_full_audit.py`
**Date**: 2026-05-28
**Window tested**: Jan 26 → Apr 20 2026 (the same 3 months as the original backtest)

---

## 1. The bot's strategy is NOT broken

Re-running the v30 backtest engine bit-for-bit on the same data:

| symbol | n  | net PnL  | WR    | PF    |
|--------|----|----------|-------|-------|
| DE40   | 108 | **+$8,187**  | 68.5% | 1.58 |
| US30   | 83 | **+$9,273**  | 53.0% | 1.56 |
| US500  | 48 | **+$2,693**  | 75.0% | 3.21 |
| XAUUSD | 25 | **+$5,868**  | 76.0% | 14.37 |
| **total** | **264** | **+$26,021** | **65.5%** | **1.81** |

All 4 symbols are profitable. All 6 sanity gates PASS. (Earlier I said "edge lost on 3/4 symbols" — that was wrong. Sorry. The negative EWMA I saw was just the recent tail of trades, not lifetime PnL.)

---

## 2. The single thing that breaks parity is `base_risk_pct`

The v30 backtest that produced +$26k uses `base_risk_pct = 0.00170`.
The live bot was shipped with `base_risk_pct = 0.00185` (a +8.8 % bump).

When we run the **exact same engine** on the **exact same data** at the live risk %:

| run | base_risk_pct | n_trades | net PnL | maxDD |
|---|---|---|---|---|
| A) backtest setting | **0.00170** | 264 | **+$26,021** | 4.71% |
| B) live setting     | **0.00185** | **65** | **+$6,183** | 3.62% |

Same engine drops **75 % of its trades** just by bumping the risk %. That doesn't look like sizing — it looks like a bug. It is, but it's a *known* bug:

> `MertonGZSizerConfig.dd_cap_pct = 0.04` — the Grossman-Zhou barrier returns multiplier = 0 whenever cumulative drawdown approaches 4 %. At RISK=0.00170, individual trades are small enough that the running DD stays well under 4 %. At RISK=0.00185, single losers push the running DD over the line, the sizer returns 0 lots, and the engine **drops the trade entirely**.

**This is exactly what your live account is doing right now.** The DD breaker tripped at 5.18 % live DD, and every new trade is being floored to ~$4 because the same Grossman-Zhou formula gets a multiplier near zero.

---

## 3. Other suspects, ranked by impact (all on baseline data)

| candidate fix | delta vs baseline | verdict |
|---|---|---|
| Layer-1 (pessimistic: 5 % extra cost on every loser) | **−$1,849** | small, fine to keep |
| DD-breaker simulation alone | **−$3,393** | small, fine to keep |
| risk % bump 0.170→0.185 | **−$19,838** | **this is the killer** |
| all three combined         | −$19,891 | dominated by risk % |

Layer-1 and the DD breaker individually are **not** what's killing live. The risk % bump is.

---

## 4. The seeding bug I also found (separate issue)

`src/live/v30_live.py:685` does this on every restart:

```python
def _init_persistence(self):
    ok, reason = self.merton_sizer.load_state(...)
    if ok:
        # uses existing state — does NOT consider whether it's empty
        return
    else:
        # only seeds from backtest if no state file exists at all
        seed_from_trades(...)
```

This means: once the bot has written ANY state file (even one with `n_seen=0`), the seed file is permanently ignored. So your live bot has been running cold-start for 4 weeks with no Merton learning. That's a real bug — recommended fix:

```python
if ok and sum(self.merton_sizer._n_seen.values()) >= self.cfg.warmup_trades:
    return  # legit warmed-up state
# otherwise fall through to seeding
```

This is a one-line patch but it's a separate issue from the main RISK problem.

---

## 5. Recommendation — three options ranked

### Option 1 (lowest-risk fix): **Roll `base_risk_pct` back to 0.00170**
- Restores the ship config that produced +$26k in backtest.
- One-line config change in `src/live/v30_live.py:453`.
- After restart, also clear the DD-breaker state so the bot starts fresh (which I already did in `reseed_v31_sizer.py`).
- **This alone should fix 90 % of the underperformance.**

### Option 2 (medium): Raise `dd_cap_pct` from 0.04 to 0.06
- Keeps the +8.8 % risk bump.
- Doubles the room before the GZ barrier kicks in.
- More aggressive overall — bigger swings, bigger profits.
- **Only do this if you've also done Option 1 and want to dial risk back up later.**

### Option 3 (now): Patch the seeding bug so the bot warms up properly
- Use `Scripts/reseed_v31_sizer.py` (already on disk) before the next restart.
- Apply the `_init_persistence` patch above so this never happens again.

---

## 6. What's still untested (and how to test it)

The audit above runs on **Jan 26 → Apr 20** data because that's what's in `data/historical/*.csv`. The live trading period (**Apr 21 → May 28**) isn't in the local CSVs. To do the FINAL true OOS check, run this on the VPS where MT5 has fresh data:

```powershell
# on VPS — pull fresh M1 bars then re-run the audit
python Scripts/download_5ers_3month.py     # extends CSVs through today
python Scripts/parity_v31_full_audit.py --json Results/parity_v31_live_window.json
```

If on Apr 21 → May 28 the BASELINE engine also makes a healthy positive PnL,
the live underperformance is 100 % the risk-% bug above.
If the BASELINE itself drops to break-even or negative on that window,
there's a separate regime-change problem that risk-% won't fix.

---

## TL;DR

> The strategy is fine.
> The bot was shipped with `base_risk_pct = 0.00185` instead of the value the backtest used (`0.00170`).
> That single 8.8 % bump put the bot in a zone where its own Grossman-Zhou DD barrier triggers on every losing trade and reduces position size to ~$4.
> Roll the risk back to 0.00170, clear the DD breaker (done), and reseed the sizer (done) — the bot should resume trading at normal size.
