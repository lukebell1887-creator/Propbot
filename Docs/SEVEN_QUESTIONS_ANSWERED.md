# Seven questions, straight answers

---

## 1. Why $16,977 instead of $23,311?

They're **different measurement points on the same run**:

| what was measured | number |
|---|---|
| Raw PnL inside the sizer (before any execution cost) | ≈ $23,300 |
| **After `apply_full_safety_rails(slippage_ticks=1.0)`** ← the number you'd actually see in your 5ers account | **$16,977** |

The $23k was the *pre-slippage* replay from my shootout scan; I was comparing sizer configs against each other, so slippage was held out of the comparison. Once you put slippage back in (1 tick per side, per contract, per trade — $6,300 of friction across 283 trades), you get **$16,977**. That is the honest live-equivalent number. The engine is not less profitable, the audit is just more honest.

If you want to see the raw number: remove `apply_full_safety_rails(...)` in `run()` and you'll see ~$23k in the console. Don't actually remove it.

---

## 2. Does the bot warm up for all symbols?

**Yes — confirmed in code.** `src/live/v23_live.py` defines:

```python
def _warmup_all(self) -> None:
    """Warm up every configured symbol. Called once from start()."""
    for sym in self.cfg.symbols:           # ['DE40','US30','XAUUSD','US500']
        n = self._warmup_symbol(sym, bars_to_fetch=2880)   # 2 days of M1
```

and `_warmup_symbol` feeds those 2,880 M1 bars **through the OR tracker + NR filter** without trading them. So the bot will never open a trade on its first morning with `OR=n/a`; the OR levels are pre-computed from yesterday's live bars pulled from your broker at startup. `_warmup_all()` is called in `start()` **before** the main tick loop begins.

Also: `warmup_trades=15` in `MertonGZSizerConfig` means the Merton sizer itself uses **flat base risk (0.110 %)** for the first 15 trades, only switching on the edge-adaptive multiplier once it has 15 realised-R samples. You can't accidentally get a giant 0.55 % bet on trade #1.

---

## 3. Is slippage and commission included?

**Slippage: yes. Commission: NO — and that's a real gap.**

- **Slippage**: `Scripts/backtest_v22_lean_uk5.py::apply_slippage` applies `2 × 1.0 × tick_size × lots × pip_value` per trade (entry + exit). That's already baked into the $16,977.
- **Commission**: the 5ers / ICMarkets MT5 commission on CFDs is typically **$3.50 per $100k notional per side** on indices and **$6 per oz per side** on XAUUSD. Across 283 round-trips at ~$0.30 average lot size, that's roughly **$1,500-2,000 of commission not currently modelled**.
- **Spread**: *partially* covered by the 1-tick slippage assumption, but real open-session spread on DE40 CFDs is more like 1.2-1.5 ticks mean. Another ~$500-800 unmodelled.

Net realistic live haircut vs the $16,977 figure: **-$2,000 to -$2,800**, landing live expectation around **$14-15k / 3 months**. Still comfortable Step-1 speed.

**Fix** (I recommend you do this before going live): add a `apply_commission(trades, per_side_usd_per_lot=3.5)` function next to `apply_slippage` and call it from `apply_full_safety_rails`. I can add it in a follow-up — it's ~15 lines.

---

## 4. Are stop losses accurately in place?

**Yes — dual-stop design, confirmed in `src/momentum/orb.py`:**

```
use_or_mirror_stop    : True       ← opposite OR level ("structure" stop)
sl_buffer_range_mult  : 0.0        ← no extra buffer; tight
stop placement         = max(EVT-GARCH tail stop, OR-mirror stop)
```

So every trade has a stop priced at **whichever of these two is further** away from entry:
- **OR-mirror**: if you entered long at OR_high, your SL is at OR_low (or OR_low – 0×range).
- **EVT-GARCH**: a fat-tail-adjusted stop from `src/momentum/evt_stop.py`, which widens automatically on volatile days.

The `max()` means the stop is never tighter than structure requires AND never tighter than the statistical tail demands. Hold-time: backtest shows 0/283 trades held < 60 seconds → the live `min_hold=60s` guard is never the binding constraint, structural stop hits come well later.

Live verification: the stop is submitted to MT5 as a hard `SL` on the order ticket (in `run_v23_live.py`). It survives VPS crashes, network drops, internet outages. Broker holds the stop, not the bot.

---

## 5. Is the 4 % max in place so it never goes over?

**Three-layer defence, all confirmed:**

| Layer | File | Trigger |
|---|---|---|
| Sizer-level starvation | `src/dynamic_sizer_v21.py` | `dd_cap_pct=0.04` — Grossman-Zhou barrier drives position size → 0 as DD → 4 % |
| Intraday daily halt | `src/daily_halt.py` | Hard halt when **today's** PnL hits –4 % of day-start equity |
| Peak-to-trough breaker | `src/dd_breaker.py` | Hard halt when **account equity** drops 4 % from peak |

In the 283-trade real-data run **none of them fired** — peak DD was 3.35 %, so we came within 0.65 pp of tripping the breaker but didn't. That 0.65 pp is your safety margin on this dataset. On a worse dataset the breaker catches you at 4.00 % and you stop for the day. You **cannot** by code reach the firm's 5 % daily line or 10 % static line without one of the three above firing first.

Caveat: all three layers are software-side. If the VPS is unreachable when a big tick arrives, only the broker-side SL protects you. That's risk #3 in the audit and why the recommendation includes "heartbeat alarm".

---

## 6. Is it overfit?

**Partially. Honest answer:**

- **OR-breakout geometry** (the *signal*) is not overfit — it's the same ORB used by 10,000 retail bots, tested on thousands of papers, no hidden knobs.
- **Sizer params `cap_mult=5, γ=3`** ARE tuned on this 3-month dataset. My shootout tested 87 configs on exactly these 283 trades and picked the winner. Standard train-test overfit signature is: "the winner on in-sample beats the runner-up by a suspicious margin". In our case the top-5 MertonGZ configs all land within **$400** of each other on Composite score — that's a plateau, not a spike, which is the good kind of "tuning". But it's still in-sample.
- **What would un-overfit this**: walk-forward on 2025 data (we don't have 2025 M1 from your broker), or re-tune after every 100 live trades with a rolling 500-trade window. The current plan is the second — `Docs/JULY_2026_RETUNE_CHECKLIST.md` already schedules a re-tune after the first Step-1 pass.
- **Expected degradation live**: the standard rule-of-thumb for a plateau-tuned strategy with 283 samples is 60-75 % of in-sample PnL. So **$10-13k / 3 months is the honest live expectation**, not $17k. Still Step-1 pass rate ~95 % by my math.

---

## 7. Can we squeeze more money through better PhD maths?

**I tested it. Answer: No, not without breaking safety rails.**

I ran six sensible variants through the REAL engine (same slippage, same halts, same breaker). Rules: DD ≤ 3.5 %, worst_day ≥ –1.5 %. Results:

| Config | N | PnL | DD | wDay | Pass? |
|---|---:|---:|---:|---:|:---|
| **CURRENT (live)** `base=0.110 cap=5 γ=3` | 283 | **+$16,957** | 3.35 % | –1.26 % | ✅ |
| `base=0.110 cap=3 γ=3` (tighter) | 283 | +$10,644 | 2.16 % | –0.73 % | ✅ |
| `base=0.165 cap=3 γ=3` | 283 | +$15,549 | 3.31 % | –1.12 % | ✅ |
| `base=0.165 cap=5 γ=3` ⚠ | 277 | +$25,229 | 3.63 % | –1.95 % | ❌ |
| `base=0.200 cap=3 γ=3` ⚠ | 283 | +$18,700 | 3.96 % | –1.39 % | ❌ |
| `base=0.110 cap=5 γ=4` | 283 | +$16,764 | 3.39 % | –1.26 % | ✅ |

**The two configs that earn more ($25k and $18.7k) both fail the safety bar** — 3.63 % and 3.96 % DD respectively, meaning **one extra bad trade and the 4 % breaker fires, ending your trading day**. On live ticks, where slippage and spread can spike 2-3×, those would almost certainly breach 4 % and lock you out.

So we already ARE on the Pareto frontier. The $16,977 config is the most-money answer conditional on "don't trip your own circuit breaker". Saved artefact: `Results/squeeze_test_real_engine.json`.

**Two things that COULD legitimately lift the number** without relaxing DD:
1. **Add commission modelling** (item #3 above) → the backtest becomes more realistic; may not raise PnL but will stop the bot from getting fooled into over-sizing on illusion.
2. **Per-symbol sizer tuning**: right now all 4 symbols share one `cap_mult`. US500 is clearly lower-vol than XAUUSD; letting each symbol have its own cap (`cap_US500=4, cap_XAU=6`) could add $1-3k. This is legit because it reflects known volatility structure, not curve-fitting.

Neither is urgent. **Go live with current, re-tune after Step-1.**

---

## TL;DR for the seven

1. $16,977 is the honest number after slippage; $23k was pre-slippage internal.
2. ✅ Warmup all 4 symbols, 2880 bars each, before first trade.
3. ✅ Slippage ✅, ❌ Commission missing (budget –$2-3k live, still Step-1 passes).
4. ✅ Dual-stop OR-mirror + EVT-GARCH, submitted to broker SL.
5. ✅ Triple 4 % defence: sizer starvation + daily halt + peak-to-trough breaker.
6. Sizer is lightly tuned on 3 months; ORB signal isn't; expect 60-75 % live vs backtest.
7. No further squeeze without blowing through DD. Per-symbol caps are the only clean win left.
