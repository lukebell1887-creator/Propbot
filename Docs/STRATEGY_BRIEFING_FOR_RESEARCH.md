# SHF Strategy Briefing — For Deep Research on New Pair Candidates

## 1. What My Strategy Does (The Core Engine)

I run a **cointegration-based pairs trading** bot on MetaTrader 5. The fundamental idea: find two assets whose price spread (log(A) - beta * log(B)) is mean-reverting. When the spread deviates far enough from its mean, enter expecting reversion. Exit when it returns.

### The Signal Pipeline (per M1 bar):
1. **Welford Online Statistics** (span=100 bars) — computes rolling mean and standard deviation of the log spread
2. **Hurst Exponent** (R/S method, 512-bar window) — measures how mean-reverting the spread actually is. H < 0.5 = mean-reverting, H > 0.5 = trending
3. **Dynamic Z-Score Entry**: `Z_crit = Z_BASE + GAMMA * (H - 0.5)` where Z_BASE=2.0, GAMMA=6.0. When H is low (strong MR), Z_crit drops to ~2.0. When H is high (trending), Z_crit rises to ~3.5+, requiring a bigger dislocation
4. **Dynamic Z-Score Exit**: `Z_exit = EXIT_BASE + EXIT_GAMMA * (H - 0.5)` where EXIT_BASE=0.5, EXIT_GAMMA=2.0. Adapts exit threshold to Hurst
5. **Kalman Sentinel** — tracks the live hedge ratio (beta) via Kalman filter. If beta deviates >15% from the static assumption, the pair is blocked (cointegration breakdown)
6. **HMM 3-Regime Volatility Filter** (Numba JIT) — classifies spread returns into 3 regimes (low/medium/high vol). Blocks new entries during high-volatility regime. Per-pair `min_regime_hold` parameter prevents regime flicker
7. **Correlation Risk Monitor** — tracks cross-pair spread return correlation. Reduces position size when pairs become correlated (diversification collapses)

### Risk Management:
- **Dynamic AKAD**: Adaptive position sizing from DD headroom + rolling win rate. `base_risk = f(daily_dd_remaining, win_rate, lambda=40)`. Uses ACTUAL MT5 profit (including spreads, commissions, swaps) for win/loss determination
- **Ghost Stops**: 4% daily DD kill, 9% max DD kill (prop firm safe)
- **Huber 4.815-sigma hard stops**: Server-side catastrophe net on each leg
- **Dynamic Dwell**: Minimum hold time = `dwell_base * (H / 0.3)`, clamped to per-pair min/max. Prevents exiting inside bid-ask bounce
- **Rollover Lockout**: No new entries within +/-30 minutes of broker midnight
- **Spread Blowout Filter**: Blocks entry when bid-ask spread exceeds threshold
- **3-State Reconciliation**: After execution timeout, audits MT5 positions to detect widowmaker (one leg filled, other not)

### Execution:
- Rust core (`shf_core.pyd`) for Welford, Hurst, Kalman, Z-scores (~50ns per update)
- Python asyncio orchestrator with 100ms tick loop
- MQL5 Expert Advisor (native TCP sockets on localhost:5555, NOT ZMQ)
- M1 bar aggregation (signals on bar close, not every tick — matches backtest cadence)
- 768-bar historical pre-warm on startup (immediately ready to trade)

---

## 2. What I Tested and What Failed

### Pairs Tested (3.5 months of M1 data, Oct 2025 - Feb 2026):

| Pair | Symbols | Hurst | Gross WR | Avg Gross Win | Avg Cost/Trade | Net Result | Verdict |
|------|---------|-------|----------|---------------|----------------|------------|---------|
| **Index Spread** | US100 (NAS100) / DE40 (DAX40) | 0.585-0.646 | 70.4% | $148.62 | $9.59 | **+$3,855** (PF 1.67) | **LIVE** |
| **Oil Spread** | XTIUSD (WTI) / XBRUSD (Brent) | 0.36-0.40 | ~90% | $177.65 | $87.37 | **+$28,388** (PF 4.70) | **LIVE** (with 1800s dwell) |
| Forex Anchor | AUDUSD / NZDUSD | 0.382 | 77.7% | $20.19 | $20.76 | **-$1,224** | **KILLED** |
| JPY Cross | EURJPY / CHFJPY | 0.166 | 77.1% | $10.29 | $16.86 | **-$2,953** | **KILLED** |

### Why Forex Pairs Failed:
- **AUDUSD/NZDUSD**: Beautiful mean-reversion (H=0.382), 77.7% gross win rate. But average gross win was only $20.19 and average cost per trade was $20.76. **Costs literally exceeded the gross wins.** The spread bid-ask ($5+$7 = $12/fill) plus $4/lot commission killed every penny of alpha.
- **EURJPY/CHFJPY**: Even worse. Gross wins averaged $10.29 but costs were $16.86/trade. The wider JPY spreads ($6.2 + $9.4 = $15.6/fill) made it impossible.
- **Key lesson**: Forex pairs with tight cointegration often have spreads that look mean-reverting on mid-prices, but the bid-ask cost of trading the spread exceeds the actual mean-reversion amplitude.

### Why Oil Works (With Caveats):
- WTI/Brent is a natural cointegrated pair (same underlying commodity, different delivery points)
- Large absolute price ($65-75) means percentage spreads are small relative to move size
- **The catch**: Oil H~0.36-0.40 (fast mean-reversion) + tight dwell = trades close too quickly and get caught in bid-ask bounce. The solution was raising dwell to 1800s base, which forces 30+ minute holds. This cut trades from 1,475 to 659 and profit from $119k to $28k — but the remaining $28k represents real trends, not bid-ask noise.

### Why Index Works:
- NAS100/DAX40: Different economies but highly correlated equity indices
- H=0.585-0.646 (slightly trending) means Zcrit rises to 3.5+, requiring genuine dislocations
- Low spreads ($1+$1) and low commission mean costs are only $9.59/trade vs $148 average win
- Fewer trades (98 over 3.5 months, ~28/month) but each trade is high quality

---

## 3. What I Need From Deep Research

### The Ideal Pair Must Have:
1. **Genuine economic cointegration** — not just statistical correlation. The assets must be fundamentally linked (same commodity, same sector, same economy driver)
2. **Hurst exponent 0.30-0.55** — proven mean-reversion. H < 0.30 is too fast (bid-ask bounce trap). H > 0.55 means the spread trends more than it reverts
3. **Spread amplitude >> trading costs** — the average spread mean-reversion swing (in dollars) must be at least 3-5x the round-trip cost (bid-ask + commission + swap)
4. **Available on MetaTrader 5** — must be offered by retail CFD brokers (I use a prop firm)
5. **Sufficient liquidity** — tight bid-ask spreads during London/NY sessions
6. **Not correlated with existing pairs** — if it moves with NAS100/DAX40 or WTI/Brent, it doesn't add diversification

### What I'm Specifically Looking For:
- **Other commodity pairs**: Natural Gas/Heating Oil? Gold/Silver? Different crude oil benchmarks?
- **Cross-market index pairs**: FTSE100/DAX40? Nikkei/ASX200? SP500/NAS100?
- **ETF-like CFD pairs**: If brokers offer them
- **Metal spreads**: Gold/Platinum? Copper/Zinc?
- **Energy spreads**: Nat Gas/Crude? Power/Gas?
- **Agricultural spreads**: If available on MT5

### What Will NOT Work (Based on My Testing):
- **Forex majors** — bid-ask costs too high relative to mean-reversion amplitude
- **JPY crosses** — even worse cost ratio
- **Pairs with H > 0.6** — the spread trends too much; entries require Z>4.0 which rarely happens
- **Pairs with H < 0.25** — too fast; even with raised dwell, you get trapped in noise
- **Any pair where average round-trip cost > 50% of average gross win** — costs will eat the edge over time

### My System Parameters (For Context):
- Entry: Dynamic Z_crit = 2.0 + 6.0*(H - 0.5), minimum 2.0
- Exit: Dynamic Z_exit = 0.5 + 2.0*(H - 0.5)
- Welford window: 100 bars (M1)
- Hurst window: 512 bars (R/S method)
- Beta: Static 1.0 with Kalman monitoring (15% tolerance)
- HMM: 3 regimes, lookback=100, per-pair min_hold (5-20)
- Dwell: Per-pair, 60s base for fast instruments, 1800s for instruments with wide spreads

### Cost Model I Need Per Candidate Pair:
- Typical bid-ask spread for leg A (in points/dollars during London session)
- Typical bid-ask spread for leg B
- Commission per lot (round trip)
- Any percentage commission
- Asian session spread multiplier (usually 1.5-2.0x)
- Overnight swap rates (if holding overnight)

---

## 4. Current Live Portfolio (Running Now)

| Pair | Symbols | HMM Hold | Dwell Base | Status |
|------|---------|----------|------------|--------|
| Index Spread | NAS100 / DAX40 | 20 bars | 60s | LIVE |
| Oil Spread | XTIUSD / XBRUSD | 5 bars | 1800s | LIVE |

**Account**: $4,890 prop firm | **Risk**: Dynamic AKAD (~1.2% at 0% DD) | **Ghost Stops**: 4% daily, 9% max

---

## 5. Summary Request

Please research and suggest **new cointegrated pairs** that:
1. Have strong economic/fundamental reasons for cointegration
2. Are available on MetaTrader 5 (retail CFD brokers)
3. Have Hurst exponents likely in the 0.30-0.55 range
4. Have trading costs that are small relative to the mean-reversion amplitude
5. Are not highly correlated with my existing NAS100/DAX40 or WTI/Brent pairs
6. Provide actual spread/commission data from major brokers where possible

For each candidate, explain: (a) why the pair is cointegrated, (b) expected Hurst range, (c) typical trading costs, (d) whether the cost-to-alpha ratio is favorable, (e) any risks or caveats.
