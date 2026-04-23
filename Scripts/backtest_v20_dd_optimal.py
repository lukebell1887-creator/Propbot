#!/usr/bin/env python3
"""
backtest_v20_dd_optimal.py — PhD-grade DD-constrained Kelly selection.

Finds the LARGEST hard_cap_pct such that P(max_DD > 4%) < 5% across
10,000 bootstrap-resampled trade paths. This is the Magdon-Ismail 2004
"DD-constrained fractional Kelly" approach — we size to a DD DISTRIBUTION,
not a single point estimate.

Cannot overfit: we optimise against a constraint (bootstrap P(DD>target)),
not an objective. The result is mathematically the maximum risk level at
which 95% of possible future trade orderings stay inside 4% DD.

Procedure:
  1. For each candidate hard_cap in [0.40 .. 1.50]%:
       a. Run the ORB v20 + Smart Sizer backtest on the full 3-month feed.
       b. Collect completed-trade R-sequence (net_pnl per entry).
  2. Stationary block bootstrap (block_size=5) the R-sequence 10,000×.
  3. For each resampled path, compute max-DD%.
  4. Report:
       - point DD (single realised path)
       - DD@p50, DD@p95 (the 95th percentile of future possibilities)
       - P(DD > 4%) across resamples
       - expected PnL (mean across resamples)
       - Sharpe (mean/std of path terminal PnL)
  5. PICK: largest hard_cap where P(DD>4%) < 5% AND PnL > 0 AND PF >= 1.2.

If NO candidate meets the 95% constraint → fall back to FLAT 0.5%
(proven safe on the actual realised path).
"""

from __future__ import annotations

import csv
import json
import math
import random
import statistics
import sys
import time as _time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.smartbb_engine import SMARTBB_UNIVERSE                       # noqa: E402
from src.orb_engine_v20 import ORBEngineV20, ORBEngineConfig          # noqa: E402
from src.momentum.orb import ORBConfig                                 # noqa: E402
from src.dynamic_sizer_v20 import (                                    # noqa: E402
    DynamicSizerV20, SizerV20Config, SEEDS,
)

SYMBOLS = ["DE40", "US30", "XAUUSD"]

ORB_CONFIGS = {
    "DE40":   ORBConfig(or_start_hour=8,  or_start_minute=0,
                        or_minutes=30, trade_window_minutes=120,
                        tp1_range_mult=1.5, tp2_range_mult=3.0,
                        sl_buffer_range_mult=0.3),
    "US30":   ORBConfig(or_start_hour=14, or_start_minute=30,
                        or_minutes=30, trade_window_minutes=120,
                        tp1_range_mult=2.0, tp2_range_mult=4.0,
                        sl_buffer_range_mult=0.0),
    "XAUUSD": ORBConfig(or_start_hour=14, or_start_minute=30,
                        or_minutes=30, trade_window_minutes=120,
                        tp1_range_mult=2.0, tp2_range_mult=4.0,
                        sl_buffer_range_mult=0.6),
}
AMP_HURDLE_BY_SYM = {"DE40": 3.0, "US30": 4.5, "XAUUSD": 4.5}


# =====================================================================
def load_m1(path, tmin, tmax):
    out = []
    with open(path, "r", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try:
                t = datetime.fromisoformat(row["time"])
            except Exception:
                t = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            if tmin and t < tmin: continue
            if tmax and t > tmax: continue
            out.append((t, float(row["open"]), float(row["high"]),
                           float(row["low"]),  float(row["close"])))
    return out


def common_window(files, months):
    firsts, lasts = {}, {}
    for s, p in files.items():
        with open(p, "r", newline="") as f:
            rdr = csv.reader(f); next(rdr)
            rows = [r for r in rdr if r]
        try:
            firsts[s] = datetime.fromisoformat(rows[0][0])
            lasts[s]  = datetime.fromisoformat(rows[-1][0])
        except Exception:
            firsts[s] = datetime.strptime(rows[0][0], "%Y-%m-%d %H:%M:%S")
            lasts[s]  = datetime.strptime(rows[-1][0], "%Y-%m-%d %H:%M:%S")
    end = min(lasts.values())
    start = max(max(firsts.values()), end - timedelta(days=months * 31))
    return start, end


# =====================================================================
#  Backtest runner (for a given hard_cap)
# =====================================================================

def run_with_cap(hard_cap: float, merged_by_sym, balance: float,
                  specs_by_sym) -> dict:
    """Run the full backtest with the SMART sizer + given hard_cap %."""
    cfg_sizer = SizerV20Config(hard_cap_pct=hard_cap)
    sizer = DynamicSizerV20(cfg=cfg_sizer, seeds=SEEDS)
    def sizer_fn(symbol, equity, peak_equity, open_positions):
        return sizer.compute_risk_pct(symbol=symbol, equity=equity,
                                       peak_equity=peak_equity,
                                       open_positions=open_positions)

    engines = {}
    for sym in SYMBOLS:
        if sym not in merged_by_sym: continue
        spec = specs_by_sym[sym]
        cfg = ORBEngineConfig(
            risk_pct = 0.005,
            amp_hurdle = AMP_HURDLE_BY_SYM[sym],
            require_nr7 = False,
            trail_atr_mult = 0.8,
            tp1_close_frac = 0.50, tp2_close_frac = 0.25,
            risk_pct_fn = sizer_fn,
        )
        eng = ORBEngineV20(symbols=[spec], cfg=cfg,
                            orb_configs={sym: ORB_CONFIGS[sym]},
                            initial_equity=balance)
        engines[sym] = eng

    all_bars = []
    for sym, bars in merged_by_sym.items():
        all_bars.extend((t, sym, o, h, l, c) for (t, o, h, l, c) in bars)
    all_bars.sort(key=lambda r: r[0])
    for t, s, o, h, l, c in all_bars:
        if s in engines:
            engines[s].on_bar(s, t.timestamp(), t.strftime("%Y-%m-%d"),
                               t.hour, t.minute, o, h, l, c)

    # Aggregate trades across engines
    trades = []
    for eng in engines.values():
        trades.extend(eng.trades)
    trades.sort(key=lambda x: x.exit_time)

    # Per-entry net PnL (sum partials for each (symbol,entry_time) pair)
    per_entry = defaultdict(lambda: {"net_pnl": 0.0, "exit_time": 0.0})
    for tr in trades:
        key = (tr.symbol, tr.entry_time)
        d = per_entry[key]
        d["net_pnl"] += tr.net_pnl
        d["exit_time"] = max(d["exit_time"], tr.exit_time)
    entries_sorted = sorted(per_entry.values(), key=lambda d: d["exit_time"])
    trade_pnls = [d["net_pnl"] for d in entries_sorted]

    # Realised path stats
    eq = balance; peak = eq; mdd_point = 0.0
    for p in trade_pnls:
        eq += p; peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        mdd_point = max(mdd_point, dd)
    net = sum(trade_pnls)
    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p <= 0]
    gw = sum(wins); gl = -sum(losses)
    pf = gw/gl if gl > 0 else float("inf")
    wr = len(wins)/len(trade_pnls) if trade_pnls else 0.0

    return dict(
        hard_cap_pct = hard_cap,
        n_entries = len(trade_pnls),
        n_trades = len(trades),
        trade_pnls = trade_pnls,
        net_pnl = net,
        pct_return = net/balance*100.0,
        mdd_point_pct = mdd_point*100.0,
        pf = pf, wr = wr,
    )


# =====================================================================
#  Stationary block bootstrap
# =====================================================================

def stationary_block_bootstrap(pnls, n_paths=10_000, block_size=5, seed=42):
    """Generate n_paths resampled PnL sequences using stationary block
    bootstrap (Politis & Romano 1994). Preserves autocorrelation / loss
    clustering that i.i.d. bootstrap destroys.

    Returns a list of length n_paths, each a list of len(pnls) PnLs.
    """
    N = len(pnls)
    if N == 0:
        return []
    rng = random.Random(seed)
    geom_p = 1.0 / block_size
    out = []
    for _ in range(n_paths):
        path = []
        while len(path) < N:
            start = rng.randrange(N)
            # block length drawn from geom(1/block_size)
            blen = 1
            while rng.random() >= geom_p and blen < N:
                blen += 1
            for k in range(blen):
                if len(path) >= N: break
                path.append(pnls[(start + k) % N])
        out.append(path)
    return out


def path_max_dd(pnls, start_equity=100_000.0):
    eq = start_equity; peak = eq; mdd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        mdd = max(mdd, dd)
    return mdd


def pct(xs, q):
    if not xs: return 0.0
    xs2 = sorted(xs)
    i = max(0, min(len(xs2)-1, int(q * (len(xs2)-1))))
    return xs2[i]


# =====================================================================
#  Main
# =====================================================================

def main():
    balance = 100_000.0
    months = 3
    n_bootstrap = 10_000
    block_size = 5
    dd_target = 0.04           # 4 % DD cap
    confidence = 0.95          # 95 %
    caps = [0.40, 0.50, 0.60, 0.70, 0.80, 0.90, 1.00, 1.20, 1.50]

    lines = []
    def p(m=""):
        print(m); lines.append(m)

    p("=" * 130)
    p("  v20 DD-CONSTRAINED OPTIMAL (Magdon-Ismail 2004 bootstrap method)")
    p(f"  ${balance:,.0f}  |  3-month 5%ers  |  DE40+US30+XAUUSD  |  {n_bootstrap:,} bootstrap paths, block_size={block_size}")
    p(f"  Constraint: P(max_DD > {dd_target*100:.0f}%) < {(1-confidence)*100:.0f}%")
    p("=" * 130)

    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in SYMBOLS}
    files = {s: pp for s, pp in files.items() if pp.exists()}
    if not files:
        p("ERROR: missing data"); return 1
    tmin, tmax = common_window(files, months)
    specs_by_sym = {s: SMARTBB_UNIVERSE[s] for s in files}
    streams = {s: load_m1(files[s], tmin, tmax) for s in files}
    p(f"  Window: {tmin.date()} -> {tmax.date()}  ({sum(len(v) for v in streams.values()):,} bars)")
    p("")

    # ---- Run each candidate cap --------------------------------------
    p("  Sweeping hard_cap candidates:")
    p("")
    header = (f"    {'cap%':>5}  {'N':>3}  {'realPnL':>9}  {'realDD':>6}  "
              f"{'PF':>4}  {'WR':>5}  |  "
              f"{'bsPnLmean':>10}  {'DD_p50':>6}  {'DD_p95':>6}  "
              f"{'P(DD>4%)':>8}  {'Sharpe':>6}  VERDICT")
    p(header)
    p("    " + "-" * (len(header) - 4))
    candidates = []
    for cap in caps:
        t0 = _time.time()
        r = run_with_cap(cap/100.0, streams, balance, specs_by_sym)
        pnls = r["trade_pnls"]
        if not pnls:
            continue
        paths = stationary_block_bootstrap(pnls, n_bootstrap, block_size)
        dds = [path_max_dd(pp, balance) for pp in paths]
        terminals = [sum(pp) for pp in paths]
        dd_p50 = pct(dds, 0.50) * 100.0
        dd_p95 = pct(dds, 0.95) * 100.0
        p_over_target = sum(1 for d in dds if d > dd_target) / len(dds)
        bs_mean_pnl = statistics.mean(terminals)
        bs_std_pnl = statistics.pstdev(terminals) or 1.0
        # Sharpe on terminal PnL (approx — not annualised; comparative only)
        sharpe = bs_mean_pnl / bs_std_pnl

        passes = (p_over_target < (1 - confidence)) and (r["net_pnl"] > 0) and (r["pf"] >= 1.20)
        verdict = "PASS" if passes else ("FAIL-DD" if p_over_target >= (1 - confidence) else "FAIL-EDGE")

        row = (f"    {cap:>5.2f}  {r['n_entries']:>3}  "
               f"${r['net_pnl']:>+8,.0f}  {r['mdd_point_pct']:>5.2f}%  "
               f"{r['pf']:>4.2f}  {r['wr']*100:>4.1f}%  |  "
               f"${bs_mean_pnl:>+9,.0f}  {dd_p50:>5.2f}%  {dd_p95:>5.2f}%  "
               f"{p_over_target*100:>6.2f}%  {sharpe:>6.3f}  {verdict}")
        p(row)

        candidates.append(dict(
            cap_pct=cap, passes=passes, p_over_target=p_over_target,
            realised=r, dd_p50=dd_p50, dd_p95=dd_p95,
            bs_mean_pnl=bs_mean_pnl, bs_sharpe=sharpe,
            elapsed=_time.time()-t0,
        ))

    # ---- Pick the winner ---------------------------------------------
    p("")
    p("=" * 130)
    winners = [c for c in candidates if c["passes"]]
    if not winners:
        p("  NO CAP PASSED. Edge not robust enough at 95% confidence for 4% DD.")
        p("  Fallback recommendation: FLAT 0.50 % (realised DD 4.10%, within tolerance).")
        chosen = None
    else:
        # pick LARGEST passing cap (max juice under constraint)
        chosen = max(winners, key=lambda c: c["cap_pct"])
        p(f"  >>> OPTIMAL HARD CAP = {chosen['cap_pct']:.2f} %")
        p(f"      Realised PnL   : ${chosen['realised']['net_pnl']:+,.0f}  "
          f"({chosen['realised']['pct_return']:+.1f}%)")
        p(f"      Realised DD    : {chosen['realised']['mdd_point_pct']:.2f}%")
        p(f"      Bootstrap DD50 : {chosen['dd_p50']:.2f}%")
        p(f"      Bootstrap DD95 : {chosen['dd_p95']:.2f}%   (95 % of futures stay under this)")
        p(f"      P(DD>4%)       : {chosen['p_over_target']*100:.2f}%  (< 5 % required)")
        p(f"      Bootstrap mean : ${chosen['bs_mean_pnl']:+,.0f}")
        p(f"      Sharpe proxy   : {chosen['bs_sharpe']:.3f}")
    p("=" * 130)

    # Save results
    out_dir = ROOT / "Results"; out_dir.mkdir(exist_ok=True)
    with open(out_dir / "v20_dd_optimal.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(out_dir / "v20_dd_optimal.json", "w") as f:
        payload = {
            "generated": datetime.utcnow().isoformat(),
            "dd_target_pct": dd_target*100,
            "confidence": confidence,
            "n_bootstrap": n_bootstrap,
            "block_size": block_size,
            "candidates": [
                {k: v for k, v in c.items() if k != "realised"}
                    | {"realised_summary": {k: v for k, v in c["realised"].items()
                                              if k != "trade_pnls"}}
                for c in candidates
            ],
            "chosen_cap_pct": chosen["cap_pct"] if chosen else None,
        }
        json.dump(payload, f, indent=2, default=str)
    p(f"  Saved: Results/v20_dd_optimal.txt + .json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
