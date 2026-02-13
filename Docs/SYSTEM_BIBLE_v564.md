# SHF Trading System — Complete System Bible (v5.6.4)

**Last Updated**: 13 February 2026
**Version**: 5.6.4 — Oil + Index Duo | Hidden-Window Freeze-Proof Launch | Coma Recovery | Timed Halt
**Status**: LIVE ON VPS — 2/2 pairs active (NAS100/DAX40 + XTIUSD/XBRUSD)
**Broker**: FivePercentOnline-Real (Fintokei prop firm) | **VPS**: 78.141.192.253 | **Path**: `C:\SHF`
**Account**: $4,889.35 (started $5,000) | Challenge target: pass evaluation, scale to 400K

---

## Table of Contents

1. [What This System Does](#1-what-this-system-does)
2. [Architecture Overview](#2-architecture-overview)
3. [Signal Flow](#3-signal-flow)
4. [Core Components (Detailed)](#4-core-components)
5. [Risk Management (17 Layers)](#5-risk-management)
6. [Pair Selection — The Full Journey](#6-pair-selection)
7. [Version History — Everything We Tried](#7-version-history)
8. [Live Deployment & Infrastructure](#8-live-deployment)
9. [Known Issues & Fixes Applied](#9-known-issues--fixes)
10. [Backtest Results & Projections](#10-backtest-results)
11. [File Structure](#11-file-structure)
12. [Quick Reference Commands](#12-quick-reference)

---

## 1. What This System Does

The SHF Trading System is a **cointegration-based pairs trading engine** that profits from mean-reverting spreads between correlated assets. When two correlated instruments temporarily diverge, the system enters a hedged position (long one, short the other) and profits when they converge back.

It runs a **hybrid Rust/Python stack** where all latency-critical math (spread calculation, Z-scores, Hurst exponent, Kalman filtering, risk sizing) lives in compiled Rust exposed via PyO3, while orchestration, execution, and risk management run in Python's asyncio event loop.

### Current Production Config (v5.6.4 — Live Feb 13, 2026)

| Pair | Assets | Broker Symbols | HMM Hold | Dwell Base | Dwell at H=0.5 | Max Spread |
|------|--------|----------------|----------|------------|-----------------|------------|
| **Index Spread** | US100 vs DE40 | `NAS100` / `DAX40` | 20 | 60s | ~100s (2 bars) | 200/200 pts |
| **Oil Spread** | XTIUSD vs XBRUSD | `XTIUSD` / `XBRUSD` | 5 | 1800s | ~3000s (50 bars) | 150/150 pts |

### Key Numbers

| Metric | Value |
|--------|-------|
| Signal cadence | 1 per M1 bar close (not every tick) |
| Pre-warm | 768 M1 bars from broker (~12.8 hours of history) |
| Tick loop | 100ms |
| Rust compute per pair | ~25us |
| End-to-end latency | ~9ms tick-to-decision |
| VPS to broker ping | 7ms |
| Daily DD limit | 4% (ghost stop) |
| Max DD limit | 9% (kill switch) |
| Position sizing | Dynamic AKAD (adaptive from DD headroom + rolling WR) |
| Execution | TCP socket bridge (native MQL5, zero DLL dependencies) |
| Process isolation | Hidden CMD window (immune to console freezes) |

---

## 2. Architecture Overview

```
+===========================================================================+
|                        MT5 Terminal (Broker Server)                        |
|  +---------------------------------------------------------------------+  |
|  |           SERVER-SIDE STOP LOSS  (Huber 4.815 sigma)                 |  |
|  |   Stored on broker server -- survives crash / disconnect / GC pause  |  |
|  +---------------------------------------------------------------------+  |
+===========================================================================+
                                    |
                            TCP localhost:5555
                            10 msg/s (100ms timer)
                                    |
+===========================================================================+
|                  SHF_Bridge.mq5 (EA on MT5 Chart)                         |
|                                                                           |
|  OnTimer() every 100ms:                                                   |
|    1. Collect quotes (bid/ask/spread) for NAS100, DAX40, XTIUSD, XBRUSD  |
|    2. Collect account info (balance/equity/margin)                        |
|    3. Collect open positions (ticket/symbol/volume/PnL)                   |
|    4. JSON-encode + 4-byte length header + SocketSend()                   |
|    5. Every 5th tick: check for Python commands (ORDER_SEND etc.)         |
+===========================================================================+
                                    |
                            TCP localhost:5555
                                    |
+===========================================================================+
|              Python Engine (src/engine.py) — 100ms async loop             |
|                                                                           |
|  +------------------------------------------------------------------+    |
|  | RUST CointegrationEngine (per pair)                                |    |
|  |   Spread = ln(A) - beta x ln(B)     beta = 1.0 (static)          |    |
|  |   Z = Welford_Normalize(Spread)      O(1) per bar                 |    |
|  |   H = R/S Hurst (window=512)         computed in Rust              |    |
|  |   Z_crit = 2.0 x (1 + 6.0 x max(0, H - 0.5))                     |    |
|  |   Z_exit = 0.5 x (1 + 2.0 x (H - 0.5))  clamped [0.1, 1.0]      |    |
|  +------------------------------------------------------------------+    |
|                                                                           |
|  +----------------+ +----------------+ +------------------------------+   |
|  | Kalman Sentinel| | Dynamic AKAD   | | Correlation Risk Monitor     |   |
|  | beta drift>15% | | base from DD   | | cross-pair corr -> reduce    |   |
|  | -> KILL SWITCH | | headroom + WR  | | sizing (0.4x to 1.0x)       |   |
|  +----------------+ +----------------+ +------------------------------+   |
|                                                                           |
|  +----------------+ +----------------+ +------------------------------+   |
|  | HMM 3-Regime   | | Dynamic Dwell  | | Risk Supervisor              |   |
|  | Volatility     | | per-pair       | | 4% daily / 9% max DD         |   |
|  | Filter (Numba) | | Oil: 1800s base| | 5 losses -> 60min halt       |   |
|  +----------------+ +----------------+ +------------------------------+   |
|                                                                           |
|  Coma Detector: If loop frozen >60s, re-warm all engines from broker     |
|  Process: Runs in hidden CMD window (immune to QuickEdit/console freeze) |
+===========================================================================+
```

---

## 3. Signal Flow

```
M1 Bar Closes (from EA via TCP, every 60 seconds)
    |
    v
+----------------------------------------------------+
| M1 BAR AGGREGATION                                  |
| Ticks arrive every 100ms but signals ONLY compute   |
| on M1 bar close (new minute detected).              |
| This matches backtest cadence exactly.               |
+----------------------------------------------------+
    |
    v
+----------------------------------------------------+
| RUST: Spread + Welford + Hurst + Dynamic Z           |
|   Spread = ln(A) - 1.0 x ln(B)                      |
|   Z = Welford_Normalize(Spread)    O(1)              |
|   H = Hurst_RS(spread_buffer, window=512)            |
|   Z_crit = 2.0 x (1 + 6.0 x max(0, H - 0.5))       |
|   Z_exit = 0.5 x (1 + 2.0 x (H - 0.5))             |
+----------------------------------------------------+
    |
    v
+----------------------------------------------------+
| RUST: Kalman Sentinel Check (~50ns)                  |
|   |Kalman_beta - Static_beta| > 0.15 -> ABORT       |
+----------------------------------------------------+
    |
    v
+----------------------------------------------------+
| HMM VOLATILITY FILTER (Python + Numba JIT)          |
|   3-regime: MR(OK) / Trending(CAUTION) / Vol(BLOCK) |
|   Per-pair hold: Index=20, Oil=5                     |
+----------------------------------------------------+
    |
    v
+----------------------------------------------------+
| SIGNAL DECISION                                      |
|   |Z| > Z_crit -> ENTRY   |Z| < Z_exit -> EXIT      |
+----------------------------------------------------+
    |
    v
+----------------------------------------------------+
| DYNAMIC DWELL GATE                                   |
|   Exit blocked if hold_time < dwell                  |
|   dwell = DWELL_BASE x (H / DWELL_ANCHOR)           |
|   Index: base=60s, range=[30s, 300s]                 |
|   Oil:   base=1800s, range=[900s, 9000s]             |
|   Emergency (|Z|>2.5x entry): ALWAYS bypasses        |
+----------------------------------------------------+
    |
    v
+----------------------------------------------------+
| DYNAMIC AKAD RISK SIZING                             |
|   base = f(daily_DD_headroom, rolling_WR)            |
|   final = base x exp(-40 x total_DD)                 |
|   Correlation risk multiplier applied (0.4-1.0x)     |
+----------------------------------------------------+
    |
    v
+----------------------------------------------------+
| EXECUTION via TCP Bridge                             |
|   ORDER_SEND to EA -> MT5 server                     |
|   SL = Huber 4.815 sigma hard stop (server-side)     |
+----------------------------------------------------+
```

---

## 4. Core Components

### 4.1 Rust CointegrationEngine (`math_kernel.rs`)

The entire signal pipeline for one pair. Parameters:

```python
engine = CointegrationEngine(
    span=100,           # Welford EMA span
    beta=1.0,           # Static cointegration beta
    z_base=2.0,         # Dynamic entry base
    gamma=6.0,          # Entry Hurst sensitivity
    hurst_window=512,   # R/S analysis window
    dynamic_z=True,     # Hurst-adaptive entry
    exit_z_base=0.5,    # Dynamic exit base
    exit_gamma=2.0,     # Exit Hurst sensitivity
    dynamic_exit=True,  # Hurst-adaptive exit
)
```

**Welford Online Update (O(1), EMA variant):**
```
alpha = 2 / (span + 1)
delta = x - mu
mu <- mu + alpha x delta
M2 <- (1-alpha) x M2 + alpha x delta x (x - mu)
Z = (Spread - mu) / sqrt(M2)
```

**Dynamic Entry Z:**
```
Z_crit = z_base x (1 + gamma x max(0, H - 0.5))

H < 0.50 -> Z_crit = 2.0    (mean-reverting: standard threshold)
H = 0.58 -> Z_crit ~ 3.0    (trending: sniper mode)
H = 0.70 -> Z_crit = 4.4    (strongly trending: ultra-rare entries only)
```

**Dynamic Exit Z:**
```
Z_exit = exit_z_base x (1 + exit_gamma x (H - 0.5))
Clamped to [0.1, 1.0]

H = 0.30 -> Z_exit = 0.30   (hold longer -- squeeze more reversion)
H = 0.50 -> Z_exit = 0.50   (standard)
H = 0.70 -> Z_exit = 0.70   (exit sooner -- don't fight the trend)
```

### 4.2 Rust KalmanSentinel

Full 2x2 Kalman predict-gain-update cycle. Returns `(beta, should_abort)` in ~50ns.

```python
sentinel = KalmanSentinel(static_beta=1.0, beta_tolerance=0.15)
beta, should_abort = sentinel.update(log_a, log_b)
# If |beta_kalman - 1.0| > 0.15 -> should_abort = True -> KILL position
```

### 4.3 Dynamic AKAD Risk (`src/risk/akad_risk.py`)

**The PRIMARY risk calculator.** Replaces fixed 0.75% base with adaptive base from DD headroom + rolling win rate.

```
dd_remaining = max(0.001, 4% - daily_dd)
wr = rolling_win_rate(last 50 trades), clamped [0.50, 0.85]
n_survive = log(P_RUIN) / log(1 - wr)
base = (exp(40 x dd_remaining) - 1) / (40 x n_survive)
base = clamp(base, 0.3%, 3.0%)
final = max(0.05%, base x exp(-40 x total_dd))
```

**Key innovation:** Base risk dynamically adapts to:
1. How much daily DD headroom remains (more room = larger base)
2. Recent win rate (higher WR = can risk more per trade)
3. The 4% daily DD ceiling is mathematically guaranteed never breached

**Stress test (12 scenarios x 500K bars): +144.6% more P&L than fixed base, worst DD = 3.09%**

### 4.4 Rust CorrelationRiskMonitor

Tracks rolling pairwise Pearson correlation of spread returns. Reduces portfolio risk when pairs become correlated.

| Max |corr| | Risk Multiplier | Meaning |
|-----------|-----------------|---------|
| < 0.3 | 1.0 | Uncorrelated -- full risk |
| 0.3 - 0.5 | 0.8 | Mild -- reduce 20% |
| 0.5 - 0.7 | 0.6 | Moderate -- reduce 40% |
| > 0.7 | 0.4 | High -- reduce 60% |

### 4.5 HMM Volatility Filter (`src/strategies/hmm_regime.py`)

Three-regime volatility classifier (Numba JIT compiled):
- Regime 0: Low volatility (TRADEABLE)
- Regime 1: Medium volatility (CAUTION)
- Regime 2: High volatility (BLOCKED)

**Per-pair HMM hold** prevents regime flapping:
- Index: hold=20 (trending pair, vol regimes change faster)
- Oil: hold=5 (fast mean-reversion, respond quickly to vol changes)

### 4.6 Dynamic Dwell (Per-Pair)

Prevents signal flickering and prop firm spam. **Hurst-adaptive minimum hold time** per pair:

```
dwell = DWELL_BASE x (H / DWELL_ANCHOR)
Clamped to [DWELL_MIN, DWELL_MAX]
```

| Pair | Dwell Base | Anchor | Min | Max | At H=0.5 |
|------|-----------|--------|-----|-----|-----------|
| Index | 60s | 0.3 | 30s | 300s | ~100s (2 bars) |
| Oil | 1800s | 0.3 | 900s | 9000s | ~3000s (50 bars) |

**Oil's raised dwell is critical** -- it filters out bid-ask bounce trades (see Section 6.5).

**Bypass rules:**
- Emergency exits (|Z| > 2.5x entry_Z): ALWAYS bypass dwell
- Sentinel aborts (Kalman drift > 15%): ALWAYS close immediately
- Normal exits: Must wait for dwell to expire

### 4.7 Risk Supervisor (`src/risk/supervisor.py`)

Portfolio-level safety net:
- **Daily DD 4%**: Ghost stop -- close all positions, halt trading
- **Max DD 9%**: Kill switch -- close all, halt permanently
- **5 consecutive losses**: 60-minute trading halt (auto-resumes)
- **Timed halt** (v5.6.4): Halt auto-expires after 60 minutes, triggers full re-warm

### 4.8 Coma Detector (v5.6.4)

Detects when the Python process was frozen (QuickEdit, Windows Update, antivirus scan, etc.):
- If the main loop detects a gap > 60 seconds between ticks, it logs `COMA DETECTED`
- If no open positions: logs warning, forces full 768-bar re-warm from broker
- If open positions exist: emergency closes all positions first, then re-warms

### 4.9 TCP Socket Bridge (`SHF_Bridge.mq5` + `mt5_bridge.py`)

**Native MQL5 TCP sockets -- zero external DLL dependencies.**

Wire protocol: `[4-byte big-endian length][JSON payload]`

EA sends every 100ms:
```json
{
  "mt": "DATA",
  "q": {"NAS100": {"b": 21450.5, "a": 21451.2, "s": 0.7}, ...},
  "a": {"balance": 5000.0, "equity": 5012.3, "margin": 45.2},
  "p": [{"ticket": 12345, "symbol": "NAS100", "type": 0, "volume": 0.01}],
  "t": {"h": 19, "m": 58, "s": 44}
}
```

Python can send commands:
```json
{"type": "ORDER_SEND", "symbol": "NAS100", "action": "BUY", "volume": 0.01, "sl": 24000.0}
{"type": "ORDER_CLOSE", "ticket": 12345}
{"type": "GET_HISTORY", "symbol": "NAS100", "count": 768}
```

### 4.10 Server-Side Hard Stops (Huber 4.815 sigma)

```
eq_std = sigma / sqrt(2 x theta)
LONG stop  = exp(mu - 4.815 x eq_std)
SHORT stop = exp(mu + 4.815 x eq_std)
```

Uses Huber-robust OU sigma (IRLS with MAD scale). Stored on broker server -- survives Python crash, VPS reboot, network disconnect.

---

## 5. Risk Management (17 Layers)

| # | Layer | Protection | Location | Speed |
|---|-------|------------|----------|-------|
| 1 | Server-Side Stop | Huber 4.815 sigma hard stop | Broker server | Instant |
| 2 | Kalman Sentinel | Beta drift > 15% kill-switch | Rust | ~50ns |
| 3 | HMM Filter | High volatility blocking | Python+Numba | ~1ms |
| 4 | Ghost Stop Daily | 4% daily DD | Python engine | <=100ms |
| 5 | Ghost Stop Max | 9% max DD | Python engine | <=100ms |
| 6 | Dynamic AKAD | Adaptive risk from DD headroom | Python | O(1) |
| 7 | Correlation Risk | Cross-pair corr reduction | Rust | ~50ns |
| 8 | Dynamic Z Entry | Hurst-adaptive entry threshold | Rust | ~200us |
| 9 | Dynamic Z Exit | Hurst-adaptive exit threshold | Rust | ~200us |
| 10 | Dynamic Dwell | Per-pair min hold (30s-9000s) | Python | O(1) |
| 11 | Re-entry Cooldown | Block re-entry after close | Python | O(1) |
| 12 | Spread Blowout | Per-pair max spread filter | Python | O(1) |
| 13 | Delta Staleness | 5s stale feed detection | Python | O(1) |
| 14 | Rollover Lockout | +/-5min around midnight | Python | O(1) |
| 15 | Consecutive Loss | 5 losses -> 60min halt (auto) | Python | O(1) |
| 16 | Coma Detector | >60s freeze -> re-warm all | Python | O(1) |
| 17 | Execution Reconciliation | 3-state Widowmaker audit | Python | ~3s |

---

## 6. Pair Selection — The Full Journey

This section documents every pair we tested, why we kept or dropped it, and the evidence.

### 6.1 The Original Holy Trio (v5.1-v5.5)

**US100/DE40 (Index Spread)** -- KEPT through all versions
- Economic link: Tech-heavy US index vs European index. Correlated through global risk appetite.
- Hurst: 0.585 (slightly trending). Z_crit ~3.0 (strict entries).
- Strengths: Zero commission on Fintokei, wide natural spread absorbed by large moves, low trade count (~24/month) = safe for prop firms.
- Weaknesses: Lowest WR of the trio (70%), trending character means many bars in "blocked" HMM state.

**AUDUSD/NZDUSD (Forex Anchor)** -- DROPPED in v5.6.3
- Economic link: Commodity-linked Antipodean currencies, same China/commodity exposure.
- Hurst: 0.512 (near random walk boundary = classic mean-reversion).
- Backtest PF: 3.82 (best of all pairs). WR: 81.9%. Strongest statistical edge.
- **Why dropped**: Real-world execution costs ($5 + $7 spread + $4/lot commission per round trip) exceeded average gross win ($20.19). Net P&L was NEGATIVE after costs. The edge is real but too small for the execution costs on Fintokei.

**EURUSD/GBPUSD (EUR/GBP Spread)** -- DROPPED in v5.6.2
- Economic link: Two major European currencies against each other.
- Hurst: 0.539. Only 36 trades in 3.5 months.
- PF=0.66 (unprofitable). Only traded in November. Zero trades Dec-Feb.
- **Why dropped**: Not enough mean-reversion signal. The GBP had its own macro drivers (UK fiscal policy) that broke the pair's cointegration for months at a time.

### 6.2 EURJPY/CHFJPY — Added v5.6.2, Dropped v5.6.3

- Economic link: Both European currencies against JPY (carry trade target).
- Hurst: 0.528. PF=4.85. WR=83.4%. 344 trades across all months.
- **Why added (v5.6.2)**: Replaced EUR/GBP. Strongest new pair found, consistent monthly P&L, same statistical signature as AUDUSD/NZDUSD.
- **Why dropped (v5.6.3)**: Same cost problem as AUDUSD/NZDUSD. Average gross win $10.29 vs average cost per trade $16.86. Costs destroy the edge.
- **Lesson learned**: Forex pairs on Fintokei have a real gross edge but their average wins are too small (< $25) to survive the spread + commission overhead.

### 6.3 XTIUSD/XBRUSD (Oil Spread) — Added v5.6.3

- Economic link: WTI crude vs Brent crude. Same commodity, different delivery points.
- Hurst: 0.397 (strongly mean-reverting). AC(1) = -0.427 (strongest negative autocorrelation).
- Half-life: 358 bars (6 hours) -- fastest of all pairs.

**The Oil Bid-Ask Bounce Problem (Critical Discovery)**:
- Initial backtest showed $437K profit, 97.2% WR, PF=34.47. This was TOO GOOD.
- The -0.427 AC(1) means the WTI-Brent spread mechanically bounces between bid and ask.
- At standard dwell (60s), most "profits" came from trading this bounce -- not real price movement.
- With 9-minute average hold, you'd be trading the bid-ask spread itself.

**The Solution -- Raised Dwell (1800s base)**:
- By forcing oil to hold at least 40-60 M1 bars (40-60 minutes), we filter out ALL bid-ask bounce trades.
- The edge PERSISTS at every hold time up to 4+ hours -- this proves genuine mean-reversion beyond microstructure.
- Result with raised dwell: 659 trades, 79.7% WR, PF=4.70, $28,389 net P&L (after costs).
- Oil's commission is 0.03% (~$7.80/lot) but average win is $178 -- costs are only 49% of edge, which is survivable.

### 6.4 Candidate Pairs Tested and Rejected

**XAUUSD/XAGUSD (Gold/Silver)** -- REJECTED
- 85,593 Kalman sentinel aborts in 100K bars = beta constantly breaking
- Gold-silver ratio is mean-reverting at macro timescales but too volatile at M1
- Only 1 month of tradeable data, wildly unstable PF (1.08 to 11.92 depending on HMM)
- Average loss ($583) nearly 2x average win ($299) -- relying entirely on high WR

**US30/US500 (Dow/S&P500)** -- REJECTED
- PF=0.88 = net LOSER. 49.9% WR.
- 99,721 sentinel aborts -- Kalman can't find a stable beta.
- The indices are TOO correlated -- the spread is random noise, not mean-reverting structure.

**UK100/DE40 (FTSE/DAX)** -- REJECTED
- PF=1.06 -- razor-thin edge destroyed by any execution cost.
- Different trading hours create artificial overnight spread jumps.

### 6.5 Why the Duo Works: Cost Analysis

| Pair | Avg Gross Win | Avg Cost/Trade | Cost as % of Win | Verdict |
|------|-------------|---------------|-------------------|---------|
| **Index (US100/DE40)** | $148.62 | $5.37 | 3.6% | Edge survives easily |
| **Oil (XTIUSD/XBRUSD)** | $177.65 | $87.37 | 49.2% | Edge survives (barely) |
| Forex Anchor (AUD/NZD) | $20.19 | $20.76 | 102.8% | COSTS > WINS |
| EURJPY/CHFJPY | $10.29 | $16.86 | 163.8% | COSTS >> WINS |

**Key insight**: Only pairs with average wins > $100 survive Fintokei's cost structure. Index and Oil qualify. All forex pairs failed.

---

## 7. Version History — Everything We Tried

### v5.1 — Quantum Trinity (Static Beta)
**Date**: Early Feb 2026
**What**: 3 pairs (US100/DE40, USOIL/USDCAD, AUDUSD/NZDUSD), fixed beta=1.0, fixed Z=2.0, no regime protection.
**Result**: +$393, 77.6% WR, PF=1.89, MaxDD <2%
**Problem**: Blind to regime changes. No kill-switch if cointegration breaks.
**Moved on because**: USOIL/USDCAD had unstable beta. Needed dynamic regime detection.

### v5.2 — Kalman-Welford Adaptive
**What**: Dynamic beta via Kalman filter for BOTH execution and signals.
**Result**: 88.7% WR, PF=1.24, +$153
**Discovery**: US100/DE40 beta ~1.0 confirmed. USOIL/USDCAD beta wildly unstable (dropped). Kalman over-trades.
**Moved on because**: Dynamic beta for execution was worse -- it chases beta noise. Better to use static beta=1.0 for execution and monitor beta drift as a kill-switch.

### v5.3 — Sentinel Architecture (Holy Trio)
**What**: Static beta=1.0 execution + Kalman monitoring as kill-switch only. Added EURUSD/GBPUSD as third pair.
**Result**: 74.5% WR, +$142, MaxDD <1%, 4 sentinel aborts caught
**Key innovation**: "Sentinel" pattern -- Kalman doesn't trade, just watches. If beta drifts >15%, KILL the position. Best of both worlds.
**Moved on because**: Needed adaptive risk sizing (fixed lots were too conservative).

### v5.4 — AKAD Adaptive Risk
**What**: Added Adaptive Kelly-ATR-Drawdown risk sizing on top of v5.3. Same signals, dynamic position sizes.
**Result**: Same edge quality, ruin probability dropped from 6.3% to <0.1%.
**Moved on because**: Fixed Z=2.0 entry threshold doesn't adapt to market character.

### v5.5 — Dynamic Z-Score Entry (Hurst-Adaptive)
**What**: Entry threshold Z_crit scales with Hurst exponent. Higher Hurst = stricter entries.
**Result**: PF +30% (1.76 -> 2.29), MaxDD -45%, trades -47% (eliminated low-quality entries).
**Key insight**: H=0.58 (US100/DE40) should require Z=3.0+ to enter, not Z=2.0. Dynamic Z massively improved signal quality by only taking extreme deviations in trending markets.
**Moved on because**: Exit threshold was still fixed Z=0.5. Should also adapt to Hurst.

### v5.6.0 — Dynamic Exit Z + Cross-Pair Correlation Risk
**What**: Exit threshold now Hurst-adaptive too. Added cross-pair correlation monitor to reduce risk when pairs become correlated.
**Result**: PF 2.29 -> 2.30. Marginal improvement but completes "everything dynamic" philosophy.
**Key insight**: Cross-pair correlation is almost always <0.3 for the Holy Trio -- monitor runs at 1.0x almost always but provides safety net during rare correlation spikes.

### v5.6.1 — M1 Bar Aggregation + Historical Pre-Warm (CRITICAL FIX)
**Date**: Feb 11, 2026
**What**: Fixed fundamental frequency mismatch between backtest and live.
**The bug**: Live engine was feeding EVERY TICK (~10/sec) to CointegrationEngine.update(). Welford window of 768 updates covered only 77 seconds instead of 12.8 hours. Hurst was wrong, Z_crit was floored at 2.0, HMM was flapping every 10 seconds. Bot was placing 30+ trades/hour instead of 8-12/day.
**The fix**: M1 bar aggregation -- signals only compute on M1 bar close (new minute). Plus 768-bar pre-warm from broker history at startup (ready in ~2 seconds instead of 3.3 hours).
**Impact**: Trade frequency matched backtest. Hurst values correct. HMM stable.

### v5.6.2 — EURJPY/CHFJPY + Per-Pair HMM Holds
**Date**: Feb 11, 2026
**What**: Replaced EURUSD/GBPUSD (PF=0.66) with EURJPY/CHFJPY (PF=4.85). Calibrated HMM hold per-pair based on Hurst (Index=10, Forex=100).
**Result**: 561 trades, $25,573, 25.57% return. Every month profitable.
**Investigation**: Tested 7 HMM methods across 4 hold values. Per-pair physics-based calibration (trending pairs need lower hold for faster regime re-entry).

### v5.6.3 — Oil + Index Duo (Real-Cost Validation)
**Date**: Feb 12, 2026
**What**: Comprehensive real-cost analysis killed all forex pairs. Only Index and Oil survived costs. Designed per-pair dwell with raised oil base (1800s) to eliminate bid-ask bounce.
**Result**: $32,766 combined (Oil $28,389 + Index $4,377). Oil dwell blocked 630 bounce entries.
**Key innovations**:
- Per-pair dwell config (PairConfig has dwell_base, dwell_anchor, dwell_min, dwell_max)
- Oil bid-ask bounce discovery and mitigation
- Realistic broker cost modelling (spread, fill costs, commission, session multipliers)

### v5.6.4 — Freeze-Proof Operations (CRITICAL STABILITY FIX)
**Date**: Feb 13, 2026
**What**: Fixed production freezes caused by Windows QuickEdit mode and console interaction pausing the Python process. Multiple operational hardening improvements.

**Fixes applied**:
1. **Hidden window launch**: Engine runs in `Start-Process cmd -WindowStyle Hidden`, completely detached from any console. PowerShell only tails the log file. Even if RDP disconnects, engine keeps running.
2. **QuickEdit disable**: `ctypes` Win32 API call at Python import time disables QuickEdit for the process.
3. **Coma detector**: If main loop frozen >60s, detects and forces full 768-bar re-warm from broker.
4. **Timed halt auto-resume**: RiskSupervisor halt (5 consecutive losses) now auto-expires after 60 minutes and triggers re-warm. Previously halted permanently until manual restart.
5. **Windows sleep prevention**: `powercfg` disables standby/hibernate on VPS.

**Root cause of production freezes**: Windows QuickEdit mode -- clicking in a PowerShell window enters "selection mode" which pauses the ENTIRE Python process until Enter is pressed. On a VPS with RDP, any accidental mouse click in the console window could freeze the bot for hours. The hidden window approach makes this impossible.

---

## 8. Live Deployment & Infrastructure

### VPS Details

| Setting | Value |
|---------|-------|
| IP | 78.141.192.253 |
| User | Administrator |
| Path | `C:\SHF` |
| Broker | FivePercentOnline-Real (Fintokei prop firm) |
| MT5 Account | Started $5,000, current $4,889.35 |
| Python | 3.11 |
| Rust DLL | `shf_core.pyd` (434,176 bytes) |

### How the Engine Launches

1. `RUN_ENGINE.ps1` runs pre-flight checks (file integrity, Rust validation, HMM, AKAD, pair config, TCP port, log dirs)
2. Disables Windows sleep via `powercfg`
3. Launches `python -u -m src.engine` in a **hidden CMD window** via `Start-Process cmd -WindowStyle Hidden`
4. All Python stdout/stderr goes to `logs/console.log`
5. PowerShell tails `logs/console.log` so you can watch -- Ctrl+C stops watching only, engine keeps running
6. Engine opens TCP server on port 5555, waits for EA connection
7. EA connects, starts streaming data at 10 msg/s
8. Engine fetches 768 M1 bars per symbol from broker (pre-warm)
9. Replays bars through all engines (Welford, Hurst, HMM, Kalman, Correlation)
10. Trading loop starts at 100ms tick

### Symbol Auto-Detection

The EA defines canonical names with aliases:
```
US100: ["US100", "NAS100", "USTEC", "USTECH100", "NDX100"]
DE40:  ["DE40", "DAX40", "GER40", "DE30", "DAX30"]
XTIUSD: ["XTIUSD", "USOIL", "WTI", "USOUSD", "CrudeOIL"]
XBRUSD: ["XBRUSD", "UKOIL", "BRENT", "UKOUSD"]
```
First valid symbol (bid > 0 on broker) wins. Fintokei resolves to: NAS100, DAX40, XTIUSD, XBRUSD.

### TCP Bridge Performance

| Metric | Value |
|--------|-------|
| Data push rate | 10.0 msg/s |
| Push interval | 100.1ms avg (15.1ms jitter) |
| Message size | ~1094 bytes |
| Command round-trip | ~449ms (500ms EA poll) |
| VPS-to-broker ICMP | 7ms, 0% loss, 0.3ms jitter |

### End-to-End Latency

| Component | Time | % of 100ms budget |
|-----------|------|--------------------|
| Rust compute (2 pairs) | ~0.05ms | 0.05% |
| HMM (2 pairs) | ~0.36ms | 0.36% |
| Python overhead | ~1.0ms | 1.0% |
| **Total per tick** | **~1.4ms** | **1.4%** |
| **Headroom** | **98.6ms** | **98.6%** |

---

## 9. Known Issues & Fixes Applied

### Production Incidents

| Date | Issue | Root Cause | Fix | Severity |
|------|-------|------------|-----|----------|
| Feb 10 | 30+ trades/hour | Tick-level signal processing instead of M1 bar | M1 bar aggregation (v5.6.1) | CRITICAL |
| Feb 11 | Engine needs 3.3h warmup | No historical data at startup | 768-bar pre-warm from broker | HIGH |
| Feb 13 | 46-minute process freeze | Windows QuickEdit mode | Hidden window launch (v5.6.4) | CRITICAL |
| Feb 13 | 104-second process freeze | Console interaction | Hidden window + QuickEdit disable | CRITICAL |
| Feb 13 | RiskSupervisor halt permanent | No auto-resume mechanism | Timed halt (60min auto-expire) | MEDIUM |
| Feb 13 | Coma re-warm incomplete | Subtracted 200 from bar count instead of full re-warm | Full 768-bar re-warm from broker | MEDIUM |

### Historical Fixes (Pre-Live)

| Date | Issue | Fix |
|------|-------|-----|
| Feb 8 | OneDrive corrupted multiple files | Restored from Bot.zip + reconstructed math_kernel.rs |
| Feb 8 | engine.py called wrong API names | Fixed 6 API mismatches (dynamic_exit_z -> dynamic_exit, etc.) |
| Feb 9 | Hard stops calculated in log-space | Delta method conversion: dx = price * d(ln(x)) |
| Feb 10 | Unicode characters crash VPS console | Replaced all Unicode with ASCII in log messages |
| Feb 10 | ZMQ DLL dependency issues on VPS | Replaced ZMQ with native TCP sockets (SHF_Bridge.mq5) |

### Design Decisions & Known Constraints

**"Why MT5? Why not FIX protocol?"** -- MT5 is the ONLY execution platform for prop firm challenges. No FIX access, no exchange API, no co-location. The TCP bridge is already the fastest possible approach.

**"Why Rust for math when you have a 60-second signal cadence?"** -- Rust at 25us means the signal is computed BEFORE the market moves during the execution window. A 200ms Python math kernel would introduce stale-signal risk. Rust makes computation essentially "free."

**"Why beta=1.0 instead of dynamic beta?"** -- v5.2 tested dynamic beta and it was WORSE. The Kalman chases beta noise and over-trades. Static beta=1.0 for execution + Kalman monitoring as kill-switch is the optimal pattern.

---

## 10. Backtest Results & Projections

### Current Production Config (Oil + Index, Real Costs, Per-Pair Dwell)

**3.5 months real M1 data, $100K starting balance:**

| Pair | HMM | Trades | Tr/Mo | WR | PF | Net P&L | Return | MaxDD | Avg Hold |
|------|-----|--------|-------|-----|-----|---------|--------|-------|----------|
| **Oil** | 5 | 659 | 185 | 79.7% | 4.70 | $28,389 | 28.39% | 0.81% | 41 bars |
| **Index** | 20 | 98 | 28 | 70.4% | 1.75 | $4,377 | 4.38% | 1.61% | 31 bars |
| **COMBINED** | -- | **757** | **213** | -- | -- | **$32,766** | **32.77%** | -- | -- |

### 2-Year Stress Test (12 Scenarios, Fixed AKAD)

| Scenario | Return | WR | PF | MaxDD | Ghost Stop? |
|----------|--------|-----|-----|-------|-------------|
| Normal | +12.49% | 75.2% | 2.70 | 0.27% | No |
| Bull Market | +14.41% | 76.9% | 3.53 | 0.23% | No |
| Bear Market | +4.21% | 66.8% | 1.48 | 0.55% | No |
| Flash Crash | +20.36% | 77.1% | 3.71 | 0.21% | No |
| Correlation Breakdown | +6.25% | 68.5% | 1.73 | 0.90% | No |
| High Volatility | +17.26% | 71.3% | 1.91 | 0.62% | No |
| Pandemic Shock | +14.28% | 73.6% | 2.11 | 1.28% | No |
| **Combined Worst-Case** | **+7.84%** | **68.7%** | **1.50** | **1.40%** | **No** |

**All 12 scenarios profitable. Zero ghost stops triggered. Worst DD = 1.40%.**

### Dynamic AKAD vs Fixed AKAD (12 Scenarios)

| Metric | Fixed AKAD | Dynamic AKAD | Delta |
|--------|-----------|-------------|-------|
| Total P&L (12 scenarios) | $145,168 | $355,094 | **+144.6%** |
| Avg Return per scenario | 12.10% | 29.59% | +17.49% |
| Worst Max DD | 1.40% | 3.09% | +1.69% (still safe) |
| Profitable scenarios | 12/12 | 12/12 | Both perfect |

### Realistic Live Projections

| Scenario | Oil Net | Index Net | Combined | Monthly Rate |
|----------|---------|-----------|----------|-------------|
| **Backtest (best case)** | $28,389 | $4,377 | $32,766 | ~9.2%/mo |
| **Realistic (good start)** | ~$18-22K | ~$3-4K | ~$21-26K | ~6-7%/mo |
| **Conservative (bad start)** | ~$12-16K | ~$2-3K | ~$14-19K | ~4-5%/mo |

**Factors that reduce live vs backtest:**
- AKAD compounding path dependency (-15 to -25%)
- Random slippage on ~2,600 oil fills (-3 to -5%)
- Weekly EIA inventory report causing oil spread blowouts (1-2 bad trades/month)
- Regime change risk (unknown -- oil market character could shift)

---

## 11. File Structure

```
C:\SHF\ (VPS) / PropBot\ (local)
|
|-- shf_core.pyd              # Compiled Rust library (Win x64, Py 3.11)
|-- RUN_ENGINE.ps1             # One-command launcher (pre-flight + hidden window)
|
|-- rust_core/                 # Rust source (PyO3)
|   |-- Cargo.toml
|   |-- src/
|       |-- math_kernel.rs     # CointegrationEngine, KalmanSentinel, AKADRiskCalculator,
|       |                      # CorrelationRiskMonitor, Huber OU, Hurst R/S, hard stops
|       |-- lib.rs             # PyO3 module exports
|
|-- src/                       # Python engine
|   |-- engine.py              # Main loop, pair config, signal processing, coma detector
|   |-- execution/
|   |   |-- mt5_bridge.py      # TCP server, async receiver, in-memory cache, order execution
|   |-- risk/
|   |   |-- supervisor.py      # RiskSupervisor (DD limits, halt, auto-resume)
|   |   |-- akad_risk.py       # DynamicAKAD (PRIMARY) + legacy AKADRiskManager
|   |-- strategies/
|       |-- hmm_regime.py      # HMM 3-regime volatility filter (Numba JIT)
|
|-- MQL5/Experts/
|   |-- SHF_Bridge.mq5         # Native TCP socket EA (v5.61, PRODUCTION)
|   |-- SHF_ZMQ_Bridge.mq5     # Legacy ZMQ bridge (deprecated)
|
|-- Scripts/                   # Test & validation scripts (30+ scripts)
|-- Results/                   # Test outputs (JSON + MD reports)
|-- Docs/                      # Architecture documents
|-- logs/                      # Runtime logs (console.log, trading.log)
|-- data/historical/           # M1 CSV data for backtesting
```

---

## 12. Quick Reference Commands

### VPS Operations

```powershell
# Pull latest code (force reset to GitHub)
cd C:\SHF; git fetch origin; git reset --hard origin/main

# Start engine
powershell -ExecutionPolicy Bypass -File C:\SHF\RUN_ENGINE.ps1

# Watch logs (engine keeps running)
Get-Content C:\SHF\logs\console.log -Wait -Tail 50

# Stop engine
Get-Process python | Stop-Process -Force

# Check if engine is running
Get-Process python -ErrorAction SilentlyContinue
```

### Local Development

```powershell
cd C:\Users\lukeb\OneDrive\Desktop\PropBot

# Validate Rust core (120 tests)
python Scripts/validate_rust_core.py

# Run real-cost Oil+Index backtest
python Scripts/test_oil_index_live.py

# Pre-live wiring audit (41 checks)
python Scripts/pre_live_audit.py

# Push to VPS
git add -A; git commit -m "description"; git push origin main
```

### Emergency Procedures

```powershell
# EMERGENCY: Kill everything on VPS
Get-Process python | Stop-Process -Force

# Check for orphan positions in MT5
# Open MT5 Terminal -> Trade tab -> verify no open positions

# Force restart after crash
cd C:\SHF; Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep 2
powershell -ExecutionPolicy Bypass -File C:\SHF\RUN_ENGINE.ps1
```

---

## Appendix A: Rust PyO3 Exports

**Classes**: CointegrationEngine, KalmanSentinel, AKADRiskCalculator, CorrelationRiskMonitor, OnlineNormalizer, SpreadSignal, OUFitResult, DynamicSignalResult, ExecutionCore, MathKernel

**Functions**: fit_robust_ou_process, calculate_rolling_hurst, calculate_prop_kelly, calculate_hard_stop_price, calculate_equilibrium_std, calculate_z_score, calculate_z_score_quantiles, calculate_hurst_quantiles, generate_dynamic_signal, calculate_rolling_z_scores, calculate_rolling_hurst_series, calculate_correlation, calculate_correlation_matrix

## Appendix B: What NOT to Change

1. **Do NOT use dynamic beta for execution** -- v5.2 proved it's worse. Static beta=1.0 + Kalman monitoring is optimal.
2. **Do NOT add forex pairs on Fintokei** -- costs exceed average wins. Only pairs with avg win > $100 survive.
3. **Do NOT lower oil dwell below 1800s base** -- you'll re-introduce bid-ask bounce trades.
4. **Do NOT run the engine in a visible console window** -- Windows QuickEdit WILL freeze it eventually.
5. **Do NOT feed ticks to CointegrationEngine** -- signals must compute on M1 bar close only (v5.6.1 fix).

---

*This document is the single source of truth for the SHF v5.6.4 production system as of 13 February 2026.*
