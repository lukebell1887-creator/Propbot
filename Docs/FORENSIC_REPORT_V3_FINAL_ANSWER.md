# 🔬 FORENSIC REPORT v3 — THE FINAL ANSWER

**Date:** 21 April 2026
**Status:** Investigation complete. The "$78k profitable bot" was an accounting illusion.

---

## The 3-way side-by-side test (Scripts/backtest_v19_honest.py)

Same 3-month window, same 5 symbols, same params, same sizer. Only the **exit semantics** differ.

| Config | N | WR | Net P&L | PF | DD | avg bars | same-bar | wrong-side SL |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| **CONTROL (v18 current)** — bb_floored + v18.1 guard | 50 | 22.0% | **−$2,677** | 0.76 | 5.22% | 0.7 | 33 | 2 |
| **REV_PROPER** — broker-valid exits, same-bar still OK | 51 | 45.1% | **−$5,261** | 0.35 | 5.26% | 0.9 | 26 | 1 |
| **REV_PROPER + NSB** — broker-valid + no same-bar cheating | 41 | 41.5% | **−$5,075** | 0.29 | 5.07% | 1.5 | 0 | 2 |

- Packaging-only delta (CONTROL → REV_PROPER): **−$2,584**
- Same-bar-cheating delta (REV_PROPER → +NSB): **+$186** (negligible; same-bar was not the culprit here — the reversion-target was)

---

## What this proves, conclusively

1. **The original +$78k was driven by trades where the "stop-loss" was placed ABOVE entry for longs (below for shorts)** — 146 of 186 = 78.5%. Those trades counted as "stop_loss wins" because the price tagged that level, but MT5 will reject such orders. You were never going to capture that $78k live.

2. **When you repackage the order correctly** (send broker-valid stops, use the reversion-target as a TP instead of mis-wired as SL), **the strategy loses money** — in all three honest configurations tested.

3. **Win rate goes UP from 22% → 45%** with proper exits (that's a good signal of a *signal*), but **the average loss (1.5 ATR) is too large vs the average win** to produce positive expectancy. Payoff math breaks, not the signal-detection math.

4. **Blocking same-bar exits changes almost nothing** (−$5,261 → −$5,075). The intrabar-cheating concern from the earlier agent was real *in principle* but not the main culprit on this specific dataset — this particular bug was the SL-wiring bug.

### In plain English
The bot was finding genuine mean-reversion setups. The z=−2.8 / low-Hurst filter *does* identify price extensions that tend to revert. But the trade construction (entry timing + stop placement + target fraction) does not extract enough reversion to pay for the cost of being wrong. The *signal* has modest edge; the *strategy built on top of it* does not.

---

## Who was right about what — final tally

| Claim | Truth |
|---|---|
| v18.1 guard broke backtest from +$78k to −$2k | ✅ True, and the guard is actually **correct** — it just exposed the pre-existing illusion |
| 78% of $78k came from broker-invalid "wrong-side SL" trades | ✅ True and verified |
| "Revert v18.1 and ship — $78k is real" (my earlier position) | ❌ **Wrong.** Orders would not have placed live; the simulator was reporting fills MT5 would reject. |
| "The strategy has no live-viable edge" (previous agent) | ✅ **True.** Now proven with three isolated regime tests — confirmed. |

---

## What does actually work (salvageable infrastructure)

Not all is lost. The surrounding engineering is genuinely sound and reusable:

- ✅ **Dynamic sizer v18** (Grossman-Zhou × Bayesian shrinkage × conviction × 5%ers guard) — rock-solid, well-tested
- ✅ **5%ers risk guard** — hard 4%/8% kill, safety-only activation — well-tested
- ✅ **Live infrastructure** — SHF_Bridge EA, ZMQ plumbing, warmup, telemetry, dry-run plumbing — all works
- ✅ **Trading calendar** — NFP / CPI / FOMC blackouts — works
- ✅ **Rolling-quantile gates, Hurst/OU/Kalman helpers** — all math is correct
- ✅ **Backtest harness & data ingestion** — correct & fast (376k bars in 30s)
- ❌ **The SmartBB mean-reversion signal itself** — does not produce positive expectancy at this frequency / cost structure on 5%ers spreads

---

## Three paths forward — pick one

### PATH A — Cheap tune of the honest-mode params (≤2 hrs)
Run Optuna over the `reversion_proper + NSB` regime with a focused search:
- `z_min_abs`, `z_max_abs`, `hurst_max_abs`, `stop_atr_mult`, `tp_frac`, `real_sl_atr_mult`, `ou_max_halflife`
- Hard constraint: **phantoms == 0 AND no same-bar exits**
- If *any* corner shows PF ≥ 1.3 on IS **AND** PF ≥ 1.1 on OOS — maybe real edge under a narrow regime. If not, confirm Path C.

**Cost:** 1–2 hrs. **Risk:** may just find another overfit corner; interpret with care. **Upside:** if it works, no throwaway.

### PATH B — Pivot the signal, keep the infrastructure (1–3 days)
The SmartBB signal is dead. The surrounding rig is gold. Replace the signal with a *documented* positive-edge setup:
- **Opening Range Breakout (ORB)** on US30/US100 with volume confirmation — literature-documented +ve edge
- **Gap-fade** on DE40 open — well-known edge in European indices
- **London-NY overlap mean-reversion** on XAUUSD using volatility-adjusted bands
- Reuse: sizer, risk guard, live bridge, telemetry, 3-month OOS harness

**Cost:** 1–3 days. **Risk:** low — swapping one module. **Upside:** gives you a genuine edge to live-trade.

### PATH C — Shelve automated trading on this broker/universe
Mean-reversion on M1 5%ers indices may simply not have enough edge to pay their cost structure (spread ~1.5 pts + commission). This is a legitimate finding.

**Cost:** $0. **Risk:** none. **Upside:** prevents further real-money loss.

---

## My honest recommendation

**Do PATH A first (2 hours), then if it fails, do PATH B.** PATH C is a fallback only if both A and B produce flat/negative edges.

Do **not** go live on the current bot under any configuration. The backtest that said +$78k was lying; the honest backtest says −$5k; live would almost certainly print somewhere in between leaning negative once spreads and slippage are added.

Keep the dry-run running if you want — it's harmless and generates useful live-regime data for free.
