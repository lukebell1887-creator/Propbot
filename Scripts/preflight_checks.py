#!/usr/bin/env python3
"""Pre-flight risk-tier audit.

For the 4 candidate configs (4-pair @ 0.075 / 0.10 / 0.125 / 0.15 %),
re-run the backtest with trade capture and compute:

  1. Worst single-day PnL (calendar UTC-date grouping)
  2. Worst single-day DD as % of starting balance    (5% daily rule)
  3. Max cumulative drawdown                          (5% static rule)
  4. Ruin probabilities at 3 / 4 / 5 % caps via 5000-path
     stationary-block bootstrap (tighter CI than prior 500 paths)
  5. Trade-hold duration distribution                 (HFT compliance)
  6. Concurrency breakdown                            (bulk-trading compliance)

Output: console table + Results/preflight_v3.json + Results/preflight_v3.txt
"""
from __future__ import annotations
import sys, json, math
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter, defaultdict

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

import numpy as np

from Scripts.backtest_v22_phase_b import (
    MertonGZSizerConfig, run_portfolio, apply_full_safety_rails,
)
from Scripts.backtest_v22_lean_uk5 import stats

BALANCE = 100_000.0
SYMS    = ["DE40", "US30", "XAUUSD", "US500"]  # 4-pair winner
RISKS   = [0.00075, 0.0010, 0.00125, 0.0015]

N_BOOT  = 5000          # 10x the old sweep (tighter tails)
BLOCK   = 5             # avg block length (preserves ~1-week serial correlation)
SEED    = 42


# ------------------------------------------------------------------
#  Helpers
# ------------------------------------------------------------------
def _ts_to_date(ts) -> datetime.date:
    """unix timestamp or datetime -> UTC date"""
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    if isinstance(ts, datetime):
        return ts.astimezone(timezone.utc).date() if ts.tzinfo else ts.date()
    # pandas Timestamp / numpy datetime64 fallback
    try: return ts.date()
    except Exception: return datetime.utcfromtimestamp(float(ts)).date()


def worst_single_day(trades):
    """Return (worst_day_pnl, worst_day_pnl_pct, worst_daily_dd_pct, n_days)."""
    by_day = defaultdict(float)
    for t in trades:
        d = _ts_to_date(t.exit_time)
        by_day[d] += float(t.net_pnl)
    if not by_day:
        return 0.0, 0.0, 0.0, 0
    days = sorted(by_day.keys())
    daily_pnl = np.array([by_day[d] for d in days])
    worst_pnl = float(daily_pnl.min())

    # Daily DD from running EOD equity (the prop-firm definition)
    equity = BALANCE + np.cumsum(daily_pnl)
    prior_eod = np.concatenate(([BALANCE], equity[:-1]))
    daily_dd = (prior_eod - equity) / prior_eod * 100.0  # % of prior EOD balance
    worst_dd = float(max(daily_dd.max(), 0.0))

    return worst_pnl, worst_pnl / BALANCE * 100.0, worst_dd, len(days)


def hold_duration_stats(trades):
    if not trades:
        return {"median_min": 0.0, "p10_min": 0.0, "p90_min": 0.0, "sub60s": 0}
    secs = []
    for t in trades:
        if hasattr(t.entry_time, "timestamp"):
            e = t.entry_time.timestamp(); x = t.exit_time.timestamp()
        else:
            e = float(t.entry_time); x = float(t.exit_time)
        secs.append(max(0.0, x - e))
    a = np.array(secs)
    return {
        "median_min": float(np.median(a) / 60),
        "p10_min":    float(np.quantile(a, 0.10) / 60),
        "p90_min":    float(np.quantile(a, 0.90) / 60),
        "sub60s":     int((a < 60).sum()),
    }


def concurrency_stats(trades):
    """How often are N trades open simultaneously?"""
    events = []
    for i, t in enumerate(trades):
        e = t.entry_time.timestamp() if hasattr(t.entry_time, "timestamp") else float(t.entry_time)
        x = t.exit_time.timestamp() if hasattr(t.exit_time, "timestamp") else float(t.exit_time)
        events.append((e, +1)); events.append((x, -1))
    events.sort()
    level = 0; dist = Counter()
    prev = None
    for ts, delta in events:
        if prev is not None and ts > prev:
            dist[level] += (ts - prev)
        level += delta
        prev = ts
    total = sum(dist.values()) or 1.0
    return {k: round(v / total * 100, 2) for k, v in sorted(dist.items())}


def ruin_probs(pnls, caps=(3.0, 4.0, 5.0),
               n_paths=N_BOOT, avg_block=BLOCK, seed=SEED):
    n = len(pnls)
    if n < 10:
        return {f"ruin{c:g}": float('nan') for c in caps}
    rng = np.random.default_rng(seed)
    p = 1.0 / avg_block
    hits = {c: 0 for c in caps}
    for _ in range(n_paths):
        seq = np.empty(n, dtype=float)
        i = 0
        while i < n:
            start = rng.integers(0, n)
            while i < n and (i == 0 or rng.random() >= p):
                seq[i] = pnls[(start + i) % n]
                i += 1
                if rng.random() < p: break
        equity = BALANCE + np.cumsum(seq)
        peak = np.maximum.accumulate(equity)
        max_dd_pct = ((peak - equity) / peak).max() * 100.0
        for c in caps:
            if max_dd_pct > c: hits[c] += 1
    return {f"ruin{c:g}": hits[c] / n_paths * 100.0 for c in caps}


# ------------------------------------------------------------------
#  Main
# ------------------------------------------------------------------
def run(risk):
    cfg = MertonGZSizerConfig(
        base_risk_pct=risk, cap_mult=3.0, gamma=2.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )
    raw, *_ = run_portfolio(SYMS, cfg)
    tr = apply_full_safety_rails(raw, slippage_ticks=1.0)
    s = stats(tr)

    pnls = np.array([t.net_pnl for t in tr], dtype=float)
    worst_pnl, worst_pct, worst_dd, n_days = worst_single_day(tr)
    ruins = ruin_probs(pnls)
    dur = hold_duration_stats(tr)
    conc = concurrency_stats(tr)

    return dict(
        risk=risk, n=s["n"], net=s["net"], ret_pct=s["ret_pct"],
        dd_pct=s["dd_pct"], pf=s["pf"], sharpe=s["sharpe"], wr=s["wr"],
        n_days=n_days,
        worst_day_pnl=worst_pnl, worst_day_pct=worst_pct,
        worst_daily_dd_pct=worst_dd,
        **ruins, dur=dur, conc=conc,
    )


def main():
    print("=" * 110)
    print("  PRE-FLIGHT RISK-TIER AUDIT — 4-pair @ 4 risk levels, 5000-path bootstrap, "
          "daily-DD & HFT & bulk-trading compliance")
    print("=" * 110)

    rows = []
    for r in RISKS:
        print(f"  [running]  risk = {r*100:.3f} %  ...", flush=True)
        row = run(r)
        rows.append(row)

    # -------- table --------
    hdr = ("RISK    N   PnL          DD%  WrstDay$  WrstDay%  WrstDailyDD%  "
           "Ruin3%  Ruin4%  Ruin5%   HoldMedMin  Sub60s  2-conc%")
    print("\n  " + hdr)
    print("  " + "-" * len(hdr))
    for r in rows:
        two_conc = r["conc"].get(2, 0.0)
        print(f"  {r['risk']*100:>5.3f}%  {r['n']:>3}  "
              f"${r['net']:>+8,.0f}  {r['dd_pct']:>4.2f}  "
              f"${r['worst_day_pnl']:>+7,.0f}  {r['worst_day_pct']:>+6.2f}  "
              f"{r['worst_daily_dd_pct']:>10.2f}  "
              f"{r['ruin3']:>5.1f}  {r['ruin4']:>5.1f}  {r['ruin5']:>5.1f}  "
              f"{r['dur']['median_min']:>9.1f}  "
              f"{r['dur']['sub60s']:>5}  "
              f"{two_conc:>5.1f}")

    # -------- verdict --------
    print("\n" + "=" * 110)
    print("  VERDICT (5ers real rules: 5% static DD + 5% daily DD)")
    print("=" * 110)
    for r in rows:
        pass_static  = r["dd_pct"]              < 5.0
        pass_daily   = r["worst_daily_dd_pct"]  < 5.0
        pass_ruin5   = r["ruin5"]               < 5.0
        pass_hft     = r["dur"]["sub60s"]       == 0
        pass_bulk    = r["conc"].get(3, 0) + r["conc"].get(4, 0) < 1.0  # <1% of time 3+ open

        flags = []
        if not pass_static: flags.append("STATIC DD")
        if not pass_daily:  flags.append("DAILY DD")
        if not pass_ruin5:  flags.append("RUIN@5%>5%")
        if not pass_hft:    flags.append("HFT VIOLATION (sub-60s trades)")
        if not pass_bulk:   flags.append("BULK-TRADING RISK (3+ concurrent)")

        verdict = "[OK]  prop-firm safe" if not flags else \
                  "[FAIL] " + " + ".join(flags)
        print(f"    risk={r['risk']*100:5.3f}%  PnL=${r['net']:+,.0f}  "
              f"daily-DD={r['worst_daily_dd_pct']:.2f}%  ruin(5%)={r['ruin5']:.1f}%   {verdict}")

    # -------- save --------
    Path("Results").mkdir(exist_ok=True)
    out_json = ROOT / "Results" / "preflight_v3.json"
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({
            "syms": SYMS, "balance": BALANCE, "n_boot": N_BOOT,
            "block_avg": BLOCK, "rows": rows,
        }, f, indent=2, default=str)
    print(f"\n  Saved: {out_json}")
    print("=" * 110)


if __name__ == "__main__":
    main()
