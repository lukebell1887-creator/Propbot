"""
compare_tp1_only_vs_full_ladder.py
==================================
Quantify the live/backtest parity gap.

The v30 backtest (Results/v30_fresh_trades.json) was generated with the full
TP1+TP2+trail partial-close ladder (50% / 25% / 25%).  The live engine
(src/live/v30_live.py) only places TP1 server-side and closes 100% there
(or at SL, or on time-stop).

This script:
  1. Loads the partial-trade backtest output.
  2. Aggregates partials per (symbol, entry_time) — that gives the actual
     "as-traded" outcome of the full ladder per entry.
  3. Synthesises a "TP1-only" outcome for each entry by:
        a) finding the first partial that closed (chronologically),
        b) re-scaling its dollar P/L and R-multiple by the inverse of the
           partial's lot fraction.
     For an entry that hit TP1 (50% partial) and then TP2 / trail, this
     captures the realistic case where 100% would have closed at TP1.
     For an entry stopped before TP1 (single partial = 100% at SL), the
     P/L is unchanged.

Reports side-by-side metrics for both scenarios.
"""
from __future__ import annotations
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRADES_PATH = ROOT / "Results" / "v30_fresh_trades.json"
BACKTEST_PATH = ROOT / "Results" / "v30_fresh_backtest.json"
BALANCE = 100_000.0

# ----------------------------------------------------------------------
#  Load data
# ----------------------------------------------------------------------
with open(TRADES_PATH, "r", encoding="utf-8") as f:
    partials = json.load(f)

with open(BACKTEST_PATH, "r", encoding="utf-8") as f:
    backtest_summary = json.load(f)

# group partials by (symbol, entry_time)
groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
for t in partials:
    groups[(t["symbol"], t["entry_time"])].append(t)

# sort each group's partials by exit_time so partials[0] = first to close
for k in groups:
    groups[k].sort(key=lambda x: x["exit_time"])

print(f"=" * 78)
print("V30 PARITY GAP ANALYSIS — TP1-only (live) vs Full ladder (backtest)")
print(f"=" * 78)
print(f"Partial trade records read : {len(partials)}")
print(f"Distinct entry events       : {len(groups)}")

# Distribution of partials per entry
from collections import Counter
n_partials = Counter(len(v) for v in groups.values())
print(f"Partials per entry          : {dict(sorted(n_partials.items()))}")
print()


# ----------------------------------------------------------------------
#  Build "as-traded full ladder" trade list (one row per entry, summed P/L)
# ----------------------------------------------------------------------
ladder_trades: list[dict] = []
for (sym, et), parts in groups.items():
    net = sum(p["net_pnl"] for p in parts)
    R   = sum(p["realised_R"] for p in parts)
    ladder_trades.append({
        "symbol":     sym,
        "entry_time": et,
        "exit_time":  parts[-1]["exit_time"],
        "net_pnl":    net,
        "realised_R": R,
        "n_partials": len(parts),
    })


# ----------------------------------------------------------------------
#  Build "TP1-only synthetic" trade list
#  Heuristic to scale partial → full position:
#    – If only 1 partial in the entry → that's already 100% (stop / time / etc.)
#    – If ≥2 partials → first partial is the TP1 hit (50%); P/L for "100% at TP1"
#                       is partial.net_pnl * (1.0 / 0.5) = 2 * partial.net_pnl
#                       (same scaling for realised_R)
#  This is a faithful synthesis because the engine's TP1 partial fraction is
#  fixed at 0.50 in src/orb_engine_v20.py (tp1_close_frac=0.50).
# ----------------------------------------------------------------------
tp1_trades: list[dict] = []
for (sym, et), parts in groups.items():
    first = parts[0]
    if len(parts) == 1:
        # single partial = full-position exit (SL / time / session)
        net = first["net_pnl"]
        R   = first["realised_R"]
    else:
        # first of multiple = TP1 hit on 50% → scale to 100%
        net = first["net_pnl"] * 2.0
        R   = first["realised_R"] * 2.0
    tp1_trades.append({
        "symbol":     sym,
        "entry_time": et,
        "exit_time":  first["exit_time"],
        "net_pnl":    net,
        "realised_R": R,
    })


# ----------------------------------------------------------------------
#  Metrics helpers
# ----------------------------------------------------------------------
def compute_metrics(trs: list[dict]) -> dict:
    pnls = [t["net_pnl"] for t in trs]
    Rs   = [t["realised_R"] for t in trs]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    n = len(trs)
    net = sum(pnls)
    gross_w = sum(wins)
    gross_l = abs(sum(losses)) if losses else 0.0

    # equity curve & drawdown
    eq = BALANCE
    peak = BALANCE
    max_dd_dollars = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = peak - eq
        max_dd_dollars = max(max_dd_dollars, dd)

    # daily rollup
    daily = defaultdict(float)
    for t in trs:
        d = t["exit_time"][:10]
        daily[d] += t["net_pnl"]
    worst_day = min(daily.values()) if daily else 0.0

    # Sharpe approximation (per-trade)
    if n >= 2:
        mean_p = statistics.mean(pnls)
        sd_p   = statistics.pstdev(pnls)
        sharpe = (mean_p / sd_p * math.sqrt(252.0 / max(1, len(daily)) * n)) if sd_p > 0 else 0.0
    else:
        sharpe = 0.0

    return {
        "n_trades":          n,
        "net_pnl":           round(net, 2),
        "net_pnl_pct":       round(100.0 * net / BALANCE, 3),
        "win_rate_pct":      round(100.0 * len(wins) / max(1, n), 2),
        "profit_factor":     round(gross_w / max(1e-9, gross_l), 3),
        "avg_win":           round(statistics.mean(wins), 2) if wins else 0.0,
        "avg_loss":          round(statistics.mean(losses), 2) if losses else 0.0,
        "max_dd_dollars":    round(max_dd_dollars, 2),
        "max_dd_pct":        round(100.0 * max_dd_dollars / BALANCE, 3),
        "worst_day_dollars": round(worst_day, 2),
        "worst_day_pct":     round(100.0 * worst_day / BALANCE, 3),
        "mean_R":            round(statistics.mean(Rs), 4) if Rs else 0.0,
        "var_R":             round(statistics.pvariance(Rs), 4) if len(Rs) >= 2 else 0.0,
        "sharpe_approx":     round(sharpe, 2),
    }


m_ladder = compute_metrics(ladder_trades)
m_tp1    = compute_metrics(tp1_trades)


# ----------------------------------------------------------------------
#  Side-by-side print
# ----------------------------------------------------------------------
print(f"=" * 78)
print(f"{'METRIC':<28} | {'FULL LADDER (BACKTEST)':>24} | {'TP1-ONLY (LIVE)':>20}")
print(f"-" * 78)
labels = [
    ("# trades / entries",         "n_trades",          "{:>24d}"),
    ("Net P/L ($)",                "net_pnl",           "${:>23,.2f}"),
    ("Net P/L (% of $100k)",       "net_pnl_pct",       "{:>23.3f}%"),
    ("Win rate",                   "win_rate_pct",      "{:>23.2f}%"),
    ("Profit factor",              "profit_factor",     "{:>24.3f}"),
    ("Average winner ($)",         "avg_win",           "${:>23,.2f}"),
    ("Average loser  ($)",         "avg_loss",          "${:>23,.2f}"),
    ("Max drawdown ($)",           "max_dd_dollars",    "${:>23,.2f}"),
    ("Max drawdown (%)",           "max_dd_pct",        "{:>23.3f}%"),
    ("Worst single day ($)",       "worst_day_dollars", "${:>23,.2f}"),
    ("Worst single day (%)",       "worst_day_pct",     "{:>23.3f}%"),
    ("Mean R per trade",           "mean_R",            "{:>24.4f}"),
    ("Variance of R",              "var_R",             "{:>24.4f}"),
    ("Sharpe (approx)",            "sharpe_approx",     "{:>24.2f}"),
]
for label, key, fmt_l in labels:
    fmt_r = fmt_l.replace(":>23", ":>19").replace(":>24", ":>20")
    val_l = m_ladder[key]
    val_r = m_tp1[key]
    print(f"{label:<28} | {fmt_l.format(val_l)} | {fmt_r.format(val_r)}")
print(f"=" * 78)


# ----------------------------------------------------------------------
#  Per-symbol break-down
# ----------------------------------------------------------------------
def per_symbol(trs):
    by = defaultdict(list)
    for t in trs:
        by[t["symbol"]].append(t)
    out = {}
    for sym, lst in by.items():
        pnls = [t["net_pnl"] for t in lst]
        wins = sum(1 for p in pnls if p > 0)
        out[sym] = {
            "n":         len(lst),
            "net_pnl":   round(sum(pnls), 2),
            "win_pct":   round(100.0 * wins / max(1, len(lst)), 2),
        }
    return out

per_lad = per_symbol(ladder_trades)
per_tp1 = per_symbol(tp1_trades)
syms = sorted(set(per_lad.keys()) | set(per_tp1.keys()))

print()
print("PER-SYMBOL BREAKDOWN")
print(f"-" * 78)
print(f"{'SYMBOL':<8} | {'LADDER n':>8} {'LADDER $':>11} {'LAD WR%':>7} | {'TP1 n':>6} {'TP1 $':>11} {'TP1 WR%':>7} | DELTA$")
for s in syms:
    a = per_lad.get(s, {"n": 0, "net_pnl": 0.0, "win_pct": 0.0})
    b = per_tp1.get(s, {"n": 0, "net_pnl": 0.0, "win_pct": 0.0})
    delta = b["net_pnl"] - a["net_pnl"]
    print(f"{s:<8} | {a['n']:>8d} {a['net_pnl']:>11,.2f} {a['win_pct']:>6.2f}% | "
          f"{b['n']:>6d} {b['net_pnl']:>11,.2f} {b['win_pct']:>6.2f}% | {delta:>+,.2f}")
print()


# ----------------------------------------------------------------------
#  Sizer impact estimate (Merton GZ uses mean & variance of R)
# ----------------------------------------------------------------------
print(f"=" * 78)
print("MERTON GZ SIZER IMPACT")
print(f"-" * 78)
print(f"Sizer formula: r* = (mean_R / var_R) / GAMMA, capped at 5x base risk.")
print(f"  Backtest seed (full ladder): mean_R={m_ladder['mean_R']:.4f}  var_R={m_ladder['var_R']:.4f}")
print(f"  Live reality (TP1-only):     mean_R={m_tp1['mean_R']:.4f}  var_R={m_tp1['var_R']:.4f}")
gamma = 3.0
if m_ladder["var_R"] > 0:
    r_lad = (m_ladder["mean_R"] / m_ladder["var_R"]) / gamma
else:
    r_lad = 0.0
if m_tp1["var_R"] > 0:
    r_tp1 = (m_tp1["mean_R"] / m_tp1["var_R"]) / gamma
else:
    r_tp1 = 0.0
print(f"  Implied raw risk fraction (γ=3): ladder={r_lad*100:.4f}%   tp1={r_tp1*100:.4f}%")
print(f"  → Sizer seeded with ladder data will under/over-size live trades")
print(f"     by approximately {abs(r_tp1 - r_lad)/(max(abs(r_lad),1e-9))*100:.1f}% relative.")
print(f"=" * 78)


# ----------------------------------------------------------------------
#  Save JSON for later reference
# ----------------------------------------------------------------------
out = {
    "ladder": m_ladder,
    "tp1_only": m_tp1,
    "per_symbol_ladder": per_lad,
    "per_symbol_tp1": per_tp1,
    "n_entries": len(groups),
    "entries_with_tp1_or_better":   sum(1 for v in groups.values() if len(v) >= 2),
    "entries_stopped_before_tp1":   sum(1 for v in groups.values() if len(v) == 1),
}
out_path = ROOT / "Results" / "tp1_vs_ladder_compare.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(out, f, indent=2)
print(f"\nWrote {out_path}")
