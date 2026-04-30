"""
v31_proof_pipeline.py — ONE SCRIPT to prove the 3-layer slippage defense.

Pure local backtest. No MT5. No live data needed. Runs entirely on:
    Results/v30_fresh_trades.json     (264 historical trades)
    data/historical/{SYM}_M1.csv      (3 months of M1 bars per symbol)

What it does
============
For each combination of (slippage scenario × per-trade risk × defense ON/OFF):
    1. Replays all 264 trades from v30 backtest
    2. For each stop-out, looks up the exit bar in the M1 CSV
    3. Computes slip as `bar_excess × adversity_factor`
    4. If defense ON: caps slip at 5pt, shrinks position 15%, blocks toxic windows
    5. Recomputes PnL, equity curve, max DD
    6. Records: total_pnl, max_dd_pct, breach_5pct, days_hit_halt, sharpe

Then: Monte Carlo — bootstrap 1000 random slip realizations, compute
probability statistics for each (risk × defense) combo.

Output
======
    Results/v31_proof_results.json     — all numbers, every scenario
    Results/v31_proof_table.txt        — pretty-printed decision table
    Docs/V31_DEFENSE_PROOF_RESULTS.md  — final write-up

Usage
=====
    python Scripts/v31_proof_pipeline.py

Decision logic afterwards
=========================
The output table tells you, for each plausible future slippage regime,
what defense+risk combo gives you best PnL/DD ratio. You pick.
"""
from __future__ import annotations

import json
import math
import random
import sys
from collections import defaultdict
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from statistics import mean, median, stdev

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


# ============================================================================
# CONFIGURATION — these are the levers
# ============================================================================

# Per-symbol adversity factor — how aggressively the broker fills stops within
# the M1 bar.  0 = ideal (fill at SL), 1 = catastrophic (fill at bar extreme).
# Default scenario set sweeps these to model different broker quality regimes.
ADVERSITY_SCENARIOS = {
    "optimistic":   {"DE40": 0.20, "US30": 0.30, "US500": 0.20, "XAUUSD": 0.05},
    "realistic":    {"DE40": 0.40, "US30": 0.55, "US500": 0.30, "XAUUSD": 0.10},
    "pessimistic":  {"DE40": 0.60, "US30": 0.75, "US500": 0.50, "XAUUSD": 0.20},
    "catastrophic": {"DE40": 0.80, "US30": 0.90, "US500": 0.70, "XAUUSD": 0.40},
}

# Per-trade risk levels to sweep (% of equity per trade — Merton-sized base R).
# Original v30 used 0.170% (= 0.00170).  We sweep around that.
RISK_LEVELS = [0.00100, 0.00125, 0.00150, 0.00170, 0.00200]
# Default v30 = 0.00170 (this is the comparison anchor)

# Defense variants:
#   "none"    = current v30 behaviour (no slip cap, no size shrink, no toxic filter)
#   "layer1"  = stop-limit cap ONLY (Option D) — caps slip, no size penalty
#   "3layer"  = stop-limit cap + slip-aware sizing + toxic windows (Option B)
DEFENSE_VARIANTS = ["none", "layer1", "3layer"]


# Daily halt levels (cuts trading for the rest of the day if cumulative DD
# breaches this from start-of-day equity)
DAILY_HALT = 0.04   # 4% — keep at v30 ship config

# 5%ers hard limit — exceeding this is "breach"
FIVERS_LIMIT = 0.05

START_BALANCE = 100_000.0

# Layer 1: stop-limit cap on slippage (in price points per symbol)
LAYER1_CAPS = {
    "DE40":   5.0,
    "US30":   5.0,
    "US500":  3.0,
    "XAUUSD": 1.0,
}
# If realised slip > cap, the model assumes the stop-limit didn't fill on the
# triggering bar. We model the time-fallback as filling at `cap × 1.5`
# (50% extra drag for the time-fallback market order).
LAYER1_FALLBACK_MULT = 1.5

# Layer 2: shrink position by `(1 + slip_p95 / sl_distance)` factor.
# We approximate slip_p95 as 1.5x median slip per scenario.
# Concretely we just multiply position size by LAYER2_SIZE_FACTOR.
LAYER2_SIZE_FACTOR = 0.85   # 15% shrink

# Layer 3: toxic-window filter.
# Tuples are (start_HHMM, end_HHMM) in UTC.  Trades whose ENTRY falls in
# any window for that symbol are dropped.
TOXIC_WINDOWS = {
    "US30":   [("13:15", "13:45")],   # 15min before NYSE cash open
    "US500":  [("13:15", "13:45")],
    "DE40":   [("06:55", "07:05")],   # XETRA open chaos
    "XAUUSD": [],                     # no known toxic window for XAU
}

# Number of Monte Carlo runs (bootstrap resamples)
MC_RUNS = 1000

# Random seed for reproducibility
RNG_SEED = 20260430


# ============================================================================
# DATA LOADING
# ============================================================================

def load_trades() -> list[dict]:
    path = ROOT / "Results" / "v30_fresh_trades.json"
    return json.loads(path.read_text())


def load_bars(symbol: str) -> pd.DataFrame:
    """Return DataFrame indexed by UTC datetime, with open/high/low/close."""
    path = ROOT / "data" / "historical" / f"{symbol}_M1.csv"
    if not path.exists():
        raise FileNotFoundError(f"Missing bar data: {path}")
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time")
    return df[["open", "high", "low", "close"]]


# ============================================================================
# SLIP MODEL — bar-microstructure derived
# ============================================================================

def precompute_trade_metadata(
    trades: list[dict], bars_by_sym: dict[str, pd.DataFrame]
) -> list[dict]:
    """Add precomputed fields to each trade so the per-replay loop is fast.

    Adds:  entry_dt (datetime), day, sl_distance, bar_excess, is_stopout,
           toxic_per_sym (bool whether this entry falls in toxic window).
    """
    enriched = []
    for t in trades:
        entry_dt = pd.to_datetime(t["entry_time"], utc=True).to_pydatetime()
        exit_dt  = pd.to_datetime(t["exit_time"], utc=True)
        is_stopout = abs(t["realised_R"] - (-1.0)) <= 0.05
        sl_distance = abs(t["entry_price"] - t["exit_price"]) or 1.0

        # Compute bar_excess ONCE — this is the max possible slip for this stop
        bar_excess = 0.0
        if is_stopout:
            sym = t["symbol"]
            bars = bars_by_sym[sym]
            bar_minute = exit_dt.floor("min")
            if bar_minute not in bars.index:
                bar_minute = (exit_dt - pd.Timedelta(seconds=30)).floor("min")
            if bar_minute in bars.index:
                bar = bars.loc[bar_minute]
                sl_price = t["exit_price"]
                if t["side"] == 1:  # LONG
                    bar_excess = max(0.0, sl_price - float(bar["low"]))
                else:               # SHORT
                    bar_excess = max(0.0, float(bar["high"]) - sl_price)

        toxic = in_toxic_window(t["symbol"], entry_dt)

        enriched.append({
            **t,
            "_entry_dt":    entry_dt,
            "_day":         entry_dt.date(),
            "_sl_distance": sl_distance,
            "_bar_excess":  bar_excess,
            "_is_stopout":  is_stopout,
            "_toxic":       toxic,
        })
    # Sort by entry time once
    enriched.sort(key=lambda x: x["_entry_dt"])
    return enriched



# ============================================================================
# DEFENSE LAYERS
# ============================================================================

def apply_layer1(symbol: str, slip_pts: float) -> float:
    """Stop-limit cap.  Returns the realised slip after the defense."""
    cap = LAYER1_CAPS.get(symbol, 5.0)
    if slip_pts <= cap:
        return slip_pts
    # Stop-limit didn't fill on the violent bar; time-fallback applied next
    return cap * LAYER1_FALLBACK_MULT


def in_toxic_window(symbol: str, entry_time: datetime) -> bool:
    """Layer 3 — does this trade's entry fall in a toxic window?"""
    windows = TOXIC_WINDOWS.get(symbol, [])
    if not windows:
        return False
    t = entry_time.time()
    for (start_str, end_str) in windows:
        sh, sm = map(int, start_str.split(":"))
        eh, em = map(int, end_str.split(":"))
        start = dtime(sh, sm)
        end   = dtime(eh, em)
        if start <= t <= end:
            return True
    return False


# ============================================================================
# CORE REPLAY — apply slippage + defense + risk-rescaling to one trade list
# ============================================================================

def replay_trades(
    enriched: list[dict],
    adversity_per_sym: dict[str, float],
    risk_pct: float,
    defense: str,
    base_risk: float = 0.00170,
) -> dict:
    """Run a single replay using PRECOMPUTED enriched trade metadata.

    Args:
        enriched: list of trade dicts already enriched by precompute_trade_metadata()
        adversity_per_sym: dict symbol → adversity factor (the only varying input)
        risk_pct: per-trade risk (0.00170 = original v30)
        defense: "none" or "3layer"
        base_risk: original risk used by the backtest, for PnL scaling
    """
    risk_scaler = risk_pct / base_risk

    equity      = START_BALANCE
    peak        = equity
    max_dd_pct  = 0.0
    breach      = False

    cur_day        = None
    day_start_eq   = equity
    halted_today   = False
    halt_days      = 0
    skipped_toxic  = 0
    n_trades_taken = 0
    pnl_sum_sq     = 0.0
    pnl_sum        = 0.0
    n_pnl          = 0

    for t in enriched:   # already sorted in precompute
        day = t["_day"]

        if cur_day != day:
            cur_day      = day
            day_start_eq = equity
            halted_today = False

        if halted_today:
            continue

        # Layer 3: toxic window filter (only in full 3layer mode)
        if defense == "3layer" and t["_toxic"]:
            skipped_toxic += 1
            continue

        sym  = t["symbol"]
        adv  = adversity_per_sym[sym]

        # Compute slip — bar_excess is precomputed, just multiply
        slip = t["_bar_excess"] * adv if t["_is_stopout"] else 0.0

        # Layer 1 defense: stop-limit cap (active in BOTH "layer1" and "3layer")
        if defense in ("layer1", "3layer") and slip > 0:
            cap = LAYER1_CAPS.get(sym, 5.0)
            if slip > cap:
                slip = cap * LAYER1_FALLBACK_MULT

        # Compute slip-adjusted P&L
        if t["_is_stopout"]:
            extra_R_loss = slip / t["_sl_distance"]
            new_R        = t["realised_R"] - extra_R_loss
            new_pnl      = t["net_pnl"] * (new_R / t["realised_R"])
        else:
            new_pnl = t["net_pnl"]

        # Layer 2: shrink position size (only in full 3layer mode)
        if defense == "3layer":
            new_pnl *= LAYER2_SIZE_FACTOR


        # Risk rescaling
        new_pnl *= risk_scaler

        equity += new_pnl
        n_trades_taken += 1
        pnl_sum    += new_pnl
        pnl_sum_sq += new_pnl * new_pnl
        n_pnl      += 1

        if equity > peak:
            peak = equity
        dd_from_peak = (peak - equity) / peak
        if dd_from_peak > max_dd_pct:
            max_dd_pct = dd_from_peak

        day_dd_pct = (day_start_eq - equity) / day_start_eq
        if day_dd_pct >= DAILY_HALT:
            halted_today = True
            halt_days   += 1
        if day_dd_pct >= FIVERS_LIMIT:
            breach = True

    # Sharpe (lightweight Welford-style)
    if n_pnl >= 2:
        mean_pnl = pnl_sum / n_pnl
        var_pnl  = max(0.0, (pnl_sum_sq / n_pnl) - mean_pnl * mean_pnl)
        std_pnl  = math.sqrt(var_pnl)
        sharpe   = (mean_pnl / std_pnl) * math.sqrt(252 * 4) if std_pnl > 0 else 0.0
    else:
        sharpe   = 0.0
        mean_pnl = 0.0

    return {
        "total_pnl":      equity - START_BALANCE,
        "final_equity":   equity,
        "max_dd_pct":     max_dd_pct * 100.0,
        "breach_5pct":    breach,
        "halt_days":      halt_days,
        "n_trades":       n_trades_taken,
        "n_skipped_toxic": skipped_toxic,
        "sharpe":         sharpe,
        "avg_pnl":        mean_pnl,
    }



# ============================================================================
# MONTE CARLO — bootstrap resample over adversity scenarios
# ============================================================================

def monte_carlo(
    enriched: list[dict],
    risk_pct: float,
    defense: str,
    n_runs: int = MC_RUNS,
) -> dict:
    """Each run draws a fresh adversity factor per symbol from a distribution
    spanning all 4 scenarios.  Returns probability statistics."""
    rng = random.Random(RNG_SEED)
    pnls       = []
    dds        = []
    breaches   = 0
    halt_days_total = 0

    # Pool all scenario adversity values per symbol — sample uniformly
    pool_per_sym: dict[str, list[float]] = defaultdict(list)
    for sc in ADVERSITY_SCENARIOS.values():
        for sym, adv in sc.items():
            pool_per_sym[sym].append(adv)

    for _ in range(n_runs):
        # Draw a random adversity per symbol from the pool (with noise)
        adv_run = {}
        for sym, pool in pool_per_sym.items():
            base = rng.choice(pool)
            # Add ±25% noise to spread the distribution
            noise = rng.uniform(-0.25, 0.25)
            adv_run[sym] = max(0.0, min(1.0, base * (1 + noise)))

        result = replay_trades(enriched, adv_run, risk_pct, defense)
        pnls.append(result["total_pnl"])
        dds.append(result["max_dd_pct"])
        if result["breach_5pct"]:
            breaches += 1
        halt_days_total += result["halt_days"]


    pnls_sorted = sorted(pnls)
    dds_sorted  = sorted(dds, reverse=True)
    return {
        "n_runs":           n_runs,
        "median_pnl":       pnls_sorted[n_runs // 2],
        "p5_pnl":           pnls_sorted[n_runs // 20],     # 5th percentile
        "p95_pnl":          pnls_sorted[n_runs - n_runs // 20],
        "worst_dd":         dds_sorted[0],
        "p95_dd":           dds_sorted[n_runs // 20],
        "median_dd":        dds_sorted[n_runs // 2],
        "p_breach":         breaches / n_runs,
        "avg_halt_days":    halt_days_total / n_runs,
    }


# ============================================================================
# MAIN
# ============================================================================

def main() -> int:
    print("=" * 78)
    print("  v31 PROOF PIPELINE  —  3-layer defense Monte Carlo")
    print(f"  Run seed: {RNG_SEED}    Monte Carlo runs: {MC_RUNS}")
    print("=" * 78)

    print("\n[1/4] Loading trades + bar data...")
    trades = load_trades()
    print(f"      Loaded {len(trades)} trades from v30_fresh_trades.json")

    bars_by_sym: dict[str, pd.DataFrame] = {}
    for sym in ("DE40", "US30", "US500", "XAUUSD"):
        bars_by_sym[sym] = load_bars(sym)
        print(f"      {sym}: {len(bars_by_sym[sym])} bars  "
              f"({bars_by_sym[sym].index[0]} -> {bars_by_sym[sym].index[-1]})")

    print("\n[1b]  Precomputing per-trade bar microstructure ...")
    enriched = precompute_trade_metadata(trades, bars_by_sym)
    n_stops = sum(1 for t in enriched if t["_is_stopout"])
    n_toxic = sum(1 for t in enriched if t["_toxic"])
    avg_excess = (sum(t["_bar_excess"] for t in enriched if t["_is_stopout"])
                  / max(1, n_stops))
    print(f"      Enriched {len(enriched)} trades  ({n_stops} stop-outs, "
          f"{n_toxic} in toxic windows, avg bar_excess on stops = {avg_excess:.2f}pt)")

    # ------------------------------------------------------------------
    # STAGE A - deterministic scenario sweep
    # ------------------------------------------------------------------
    print("\n[2/4] Running deterministic scenario sweep ...")
    scenario_rows = []
    for scenario_name, adv_per_sym in ADVERSITY_SCENARIOS.items():
        for risk in RISK_LEVELS:
            for defense in DEFENSE_VARIANTS:
                result = replay_trades(enriched, adv_per_sym,
                                       risk, defense)
                scenario_rows.append({
                    "scenario":      scenario_name,
                    "risk_pct":      risk * 100,
                    "defense":       defense,
                    **result,
                })
    print(f"      {len(scenario_rows)} scenarios tested.")

    # ------------------------------------------------------------------
    # STAGE B - Monte Carlo at each (risk x defense) combo
    # ------------------------------------------------------------------
    print(f"\n[3/4] Running Monte Carlo ({MC_RUNS} runs per combo) ...")
    mc_rows = []
    for risk in RISK_LEVELS:
        for defense in DEFENSE_VARIANTS:
            print(f"      risk={risk*100:.3f}%  defense={defense:6s} ... ", end="", flush=True)
            mc = monte_carlo(enriched, risk, defense)

            mc_rows.append({
                "risk_pct": risk * 100,
                "defense":  defense,
                **mc,
            })
            print(f"median ${mc['median_pnl']:>10,.0f}   p5 ${mc['p5_pnl']:>10,.0f}   "
                  f"P(breach)={mc['p_breach']*100:>5.2f}%")

    # ------------------------------------------------------------------
    # WRITE RESULTS
    # ------------------------------------------------------------------
    print("\n[4/4] Saving results ...")
    out_path = ROOT / "Results" / "v31_proof_results.json"
    out_path.write_text(json.dumps({
        "config": {
            "ADVERSITY_SCENARIOS": ADVERSITY_SCENARIOS,
            "RISK_LEVELS":         RISK_LEVELS,
            "DEFENSE_VARIANTS":    DEFENSE_VARIANTS,
            "DAILY_HALT":          DAILY_HALT,
            "FIVERS_LIMIT":        FIVERS_LIMIT,
            "LAYER1_CAPS":         LAYER1_CAPS,
            "LAYER2_SIZE_FACTOR":  LAYER2_SIZE_FACTOR,
            "TOXIC_WINDOWS":       TOXIC_WINDOWS,
            "MC_RUNS":             MC_RUNS,
            "RNG_SEED":            RNG_SEED,
        },
        "scenario_sweep": scenario_rows,
        "monte_carlo":    mc_rows,
    }, indent=2, default=str))
    print(f"      → {out_path.relative_to(ROOT)}")

    # ------------------------------------------------------------------
    # PRETTY-PRINT THE DECISION TABLES
    # ------------------------------------------------------------------
    table_lines: list[str] = []
    def emit(s: str = ""):
        print(s)
        table_lines.append(s)

    emit()
    emit("=" * 78)
    emit("  DETERMINISTIC SCENARIO SWEEP")
    emit("=" * 78)
    emit(f"  {'Scenario':<14} {'Risk%':>6} {'Defense':<8} {'PnL ($)':>11} "
         f"{'MaxDD%':>7} {'Breach':>7} {'Halts':>5} {'Trades':>6}")
    emit("  " + "-" * 70)
    for r in scenario_rows:
        emit(f"  {r['scenario']:<14} {r['risk_pct']:>5.3f}% {r['defense']:<8} "
             f"${r['total_pnl']:>10,.0f} {r['max_dd_pct']:>6.2f}% "
             f"{('YES' if r['breach_5pct'] else 'no'):>7} "
             f"{r['halt_days']:>5}  {r['n_trades']:>5}")

    emit()
    emit("=" * 78)
    emit("  MONTE CARLO  (1000 runs across mixed adversity scenarios)")
    emit("=" * 78)
    emit(f"  {'Risk%':>6} {'Defense':<8} {'Median PnL':>12} {'P5 PnL':>11} "
         f"{'P95 PnL':>11} {'WorstDD%':>9} {'P(breach)':>10} {'AvgHalts':>9}")
    emit("  " + "-" * 70)
    for r in mc_rows:
        emit(f"  {r['risk_pct']:>5.3f}% {r['defense']:<8} "
             f"${r['median_pnl']:>11,.0f} ${r['p5_pnl']:>10,.0f} "
             f"${r['p95_pnl']:>10,.0f} {r['worst_dd']:>8.2f}% "
             f"{r['p_breach']*100:>9.2f}% {r['avg_halt_days']:>9.2f}")

    emit()
    emit("=" * 78)
    emit("  KEY COMPARISONS")
    emit("=" * 78)
    # Pair up no-defense vs 3layer at each risk level
    by_risk = defaultdict(dict)
    for r in mc_rows:
        by_risk[r["risk_pct"]][r["defense"]] = r
    for risk_pct, pair in sorted(by_risk.items()):
        if "none" in pair and "3layer" in pair:
            no, df = pair["none"], pair["3layer"]
            pnl_delta = df["median_pnl"] - no["median_pnl"]
            dd_delta  = no["worst_dd"]  - df["worst_dd"]
            breach_delta = (no["p_breach"] - df["p_breach"]) * 100
            emit(f"  Risk {risk_pct:.3f}%: 3-layer vs none -> "
                 f"PnL delta ${pnl_delta:+,.0f}   "
                 f"WorstDD delta {dd_delta:+.2f}pp   "
                 f"P(breach) delta {breach_delta:+.2f}pp")

    emit()

    table_path = ROOT / "Results" / "v31_proof_table.txt"
    table_path.write_text("\n".join(table_lines), encoding="utf-8")
    print(f"      -> {table_path.relative_to(ROOT)}")

    print("\n  Pipeline complete.  Open Results/v31_proof_table.txt for the")
    print("  full decision matrix.")
    return 0



if __name__ == "__main__":
    sys.exit(main())
