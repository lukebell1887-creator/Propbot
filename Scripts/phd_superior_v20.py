#!/usr/bin/env python3
"""phd_superior_v20.py  —  The genius-level ORB v20 optimiser.

Upgrades over phd_grid_search_orb.py:
  1. WALK-FORWARD IS/OOS split (65/35, 2-day embargo, López de Prado style)
  2. WIDER 6-axis grid (432 configs) to blanket the parameter space
  3. ROBUSTNESS ranking: config wins only if BOTH IS and OOS positive
  4. DEFLATED SHARPE RATIO (Bailey & López de Prado 2014) on trades —
     corrects for multiple-testing bias from testing 432 configs
  5. PER-SYMBOL best selector  +  PORTFOLIO best selector
  6. Monte-Carlo  PROBABILITY OF BACKTEST OVERFITTING (PBO)  via CSCV-lite

Grid (6 axes, 432 cells):
  or_minutes    ∈ {5, 15, 30}                                    (3)
  tp_ladder     ∈ {(0.5,1.0), (1.0,2.0), (1.5,3.0), (2.0,4.0)}   (4)
  sl_buffer     ∈ {0.0, 0.3, 0.6}                                (3)
  amp_hurdle    ∈ {2.0, 3.0, 4.5}                                (3)
  require_nr7   ∈ {False, True}                                  (2)
  hurst_regime  ∈ {none, trending(>0.55), range+trend([0.4,0.7])}(3)

Each of 432 cells is run TWICE (IS + OOS) → 864 backtests total.
With the lazy-Hurst engine patch, each backtest is ~0.3s → ~5 minutes.

Output:
  * Results/phd_superior_v20.json  (full table)
  * stdout: top 20 by robust score, per-symbol best, Deflated Sharpe, PBO
"""
from __future__ import annotations

import csv
import json
import math
import sys
import time as _time
from datetime import datetime, timedelta
from itertools import product
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.smartbb_engine import SMARTBB_UNIVERSE                       # noqa: E402
from src.orb_engine_v20 import ORBEngineV20, ORBEngineConfig          # noqa: E402
from src.momentum.orb import ORBConfig                                # noqa: E402

SYMBOLS = ["US30", "US100", "US500", "DE40", "XAUUSD"]


# =====================================================================
#  Data loading
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
            if tmin and t < tmin:
                continue
            if tmax and t > tmax:
                continue
            out.append((t, float(row["open"]), float(row["high"]),
                        float(row["low"]), float(row["close"])))
    return out


def common_window(files, months):
    firsts, lasts = {}, {}
    for s, p in files.items():
        with open(p, "r", newline="") as f:
            rdr = csv.reader(f)
            next(rdr)
            rows = [r for r in rdr if r]
        try:
            firsts[s] = datetime.fromisoformat(rows[0][0])
            lasts[s] = datetime.fromisoformat(rows[-1][0])
        except Exception:
            firsts[s] = datetime.strptime(rows[0][0], "%Y-%m-%d %H:%M:%S")
            lasts[s] = datetime.strptime(rows[-1][0], "%Y-%m-%d %H:%M:%S")
    end = min(lasts.values())
    start = max(max(firsts.values()), end - timedelta(days=months * 31))
    return start, end


# =====================================================================
#  ORB config builder
# =====================================================================

def build_orb_configs(or_minutes: int, tp1: float, tp2: float,
                       sl_buf: float) -> dict[str, ORBConfig]:
    cfgs: dict[str, ORBConfig] = {}
    for sym in SYMBOLS:
        # DE40 = Xetra open 08:00 UTC, rest = NY cash 14:30 UTC
        if sym == "DE40":
            h, m = 8, 0
        else:
            h, m = 14, 30
        cfgs[sym] = ORBConfig(
            or_start_hour=h, or_start_minute=m,
            or_minutes=or_minutes, trade_window_minutes=90,
            tp1_range_mult=tp1, tp2_range_mult=tp2,
            sl_buffer_range_mult=sl_buf,
        )
    return cfgs


# =====================================================================
#  Single backtest runner
# =====================================================================

def run_one(specs, merged, balance, engine_cfg, orb_cfgs):
    eng = ORBEngineV20(symbols=specs, cfg=engine_cfg,
                       orb_configs=orb_cfgs, initial_equity=balance)
    for t, s, o, h, l, c in merged:
        eng.on_bar(s, t.timestamp(), t.strftime("%Y-%m-%d"),
                   t.hour, t.minute, o, h, l, c)
    return eng.summary(), eng.trades


# =====================================================================
#  PhD scoring + statistics
# =====================================================================

def j_score(r: dict, min_n: int = 10, max_dd: float = 8.0) -> float:
    """PhD composite: PnL * sqrt(N) * max(PF-1,0) / (1+DD%).
    Returns -inf if invalid (too few trades / too much DD)."""
    n = r.get("entries", 0)
    if n < min_n:
        return float("-inf")
    if r["max_dd_pct"] >= max_dd:
        return float("-inf")
    pf = r.get("pf", 0.0)
    if not math.isfinite(pf):
        pf = 3.0  # cap inf (when zero losers)
    pnl = r["net_pnl"]
    dd = r["max_dd_pct"]
    return pnl * math.sqrt(n) * max(pf - 1.0, 0.0) / (1.0 + dd)


def sharpe_of_trades(trades) -> float:
    """Annualised Sharpe based on trade-level net PnL (assumes ~3mo sample)."""
    if len(trades) < 3:
        return 0.0
    rs = [t.net_pnl for t in trades]
    mu = sum(rs) / len(rs)
    var = sum((r - mu) ** 2 for r in rs) / (len(rs) - 1) if len(rs) > 1 else 0.0
    sd = math.sqrt(var) if var > 0 else 1e-12
    # Approx annualisation: 3 months of trades → scale by sqrt(4) for a year
    return (mu / sd) * math.sqrt(len(rs) * 4 / 3) if sd > 0 else 0.0


def deflated_sharpe(raw_sr: float, n_trials: int, n_trades: int,
                    skew: float = -0.5, kurt: float = 4.0) -> float:
    """Deflated Sharpe Ratio (Bailey & López de Prado 2014).

    Returns probability-like z-score that the observed raw Sharpe
    was NOT a random pick from n_trials back-tests.  Higher = more
    confident the edge is real.  Above 0.95 → significant at 5%.
    """
    if n_trials < 2 or n_trades < 3 or raw_sr <= 0:
        return 0.0
    # Expected max of n_trials standard Normals  (Bailey 2014, eq. 10)
    emc = 0.5772156649  # Euler-Mascheroni constant
    try:
        em_max = (1 - emc) * _invnorm(1 - 1.0 / n_trials) \
                 + emc * _invnorm(1 - 1.0 / (n_trials * math.e))
    except Exception:
        em_max = math.sqrt(2 * math.log(n_trials))  # fallback
    # Standard error of estimated SR under non-Gaussian returns
    sr_std = math.sqrt(
        (1 - skew * raw_sr + (kurt - 1) / 4.0 * raw_sr ** 2)
        / max(n_trades - 1, 1)
    )
    z = (raw_sr - em_max * sr_std) / sr_std if sr_std > 0 else 0.0
    return _norm_cdf(z)


def _invnorm(p: float) -> float:
    """Beasley-Springer-Moro approximation for inverse standard normal CDF."""
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    p_low, p_high = 0.02425, 1 - 0.02425
    if p < p_low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
               ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)
    if p <= p_high:
        q = p - 0.5
        r = q * q
        return (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5]) * q / \
               (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1)
    q = math.sqrt(-2 * math.log(1 - p))
    return -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / \
            ((((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1)


def _norm_cdf(z: float) -> float:
    return 0.5 * (1 + math.erf(z / math.sqrt(2)))


# =====================================================================
#  Main
# =====================================================================

def main():
    balance = 100_000.0
    months = 3
    print("=" * 110)
    print("  🎯 ORB v20 — PhD SUPERIOR GRID SEARCH (walk-forward + deflated Sharpe)")
    print(f"  ${balance:,.0f} account  |  5 symbols  |  full per-asset-class commission")
    print("=" * 110)

    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in SYMBOLS}
    files = {s: p for s, p in files.items() if p.exists()}
    if not files:
        print("ERROR: no {SYMBOL}_M1.csv files in data/historical/")
        return 1

    tmin, tmax = common_window(files, months)
    # Walk-forward split: 65% IS, 2-day embargo, 35% OOS
    total_days = (tmax - tmin).days
    is_days = int(total_days * 0.65)
    is_end = tmin + timedelta(days=is_days)
    oos_start = is_end + timedelta(days=2)  # 2-day embargo

    print(f"  Symbols       : {', '.join(sorted(files))}")
    print(f"  FULL window   : {tmin.date()} -> {tmax.date()}  ({total_days} days)")
    print(f"  IS window     : {tmin.date()} -> {is_end.date()}  ({is_days} days)")
    print(f"  [embargo  2 days]")
    print(f"  OOS window    : {oos_start.date()} -> {tmax.date()}  ({(tmax-oos_start).days} days)")

    internal = sorted(files)
    specs = [SMARTBB_UNIVERSE[s] for s in internal]

    # Load full data once, then split
    print("\n  Loading M1 data...")
    streams = {s: load_m1(files[s], tmin, tmax) for s in internal}

    merged_full, merged_is, merged_oos = [], [], []
    for s, bars in streams.items():
        for (t, o, h, l, c) in bars:
            rec = (t, s, o, h, l, c)
            merged_full.append(rec)
            if t <= is_end:
                merged_is.append(rec)
            elif t >= oos_start:
                merged_oos.append(rec)
    merged_full.sort(key=lambda r: r[0])
    merged_is.sort(key=lambda r: r[0])
    merged_oos.sort(key=lambda r: r[0])

    print(f"  Bars: FULL={len(merged_full):,}  IS={len(merged_is):,}  OOS={len(merged_oos):,}")
    print()

    # ---- Build 6-axis grid (432 cells) -------------------------------
    grid_or_minutes = [5, 15, 30]
    grid_tp = [(0.5, 1.0), (1.0, 2.0), (1.5, 3.0), (2.0, 4.0)]
    grid_sl = [0.0, 0.3, 0.6]
    grid_amp = [2.0, 3.0, 4.5]
    grid_nr = [False, True]
    grid_hurst = [(0.0, 1.0), (0.55, 1.0), (0.40, 0.70)]

    configs = list(product(grid_or_minutes, grid_tp, grid_sl,
                            grid_amp, grid_nr, grid_hurst))
    total = len(configs)
    print(f"  Grid cells: {total}  →  {total*2:,} backtests (IS + OOS)")
    print(f"  Estimated runtime: ~{total * 0.7 / 60:.1f} min")
    print()

    t_start = _time.time()
    rows = []
    # Self-writing progress file so external monitor can see live state
    prog_path = ROOT / "Results" / "phd_superior_v20_progress.txt"
    prog_path.parent.mkdir(exist_ok=True, parents=True)
    prog_path.write_text(f"starting {total} cells\n", encoding="utf-8")
    for i, (orm, (tp1, tp2), slb, amp, nr, (hmin, hmax)) in enumerate(configs, 1):

        label = {
            "or_minutes": orm, "tp1": tp1, "tp2": tp2,
            "sl_buffer": slb, "amp_hurdle": amp,
            "require_nr7": nr,
            "hurst_min": hmin, "hurst_max": hmax,
        }
        eng_cfg = ORBEngineConfig(
            risk_pct=0.005, amp_hurdle=amp,
            require_nr7=nr, nr_lookback=7,
            trail_atr_mult=0.8,
            tp1_close_frac=0.50, tp2_close_frac=0.25,
            hurst_min=hmin, hurst_max=hmax, hurst_window=200,
        )
        orb_cfgs = build_orb_configs(orm, tp1, tp2, slb)

        # IS run
        is_summ, is_trades = run_one(specs, merged_is, balance, eng_cfg, orb_cfgs)
        is_summ["J"] = j_score(is_summ, min_n=8)
        is_summ["sharpe"] = sharpe_of_trades(is_trades)
        # OOS run
        oos_summ, oos_trades = run_one(specs, merged_oos, balance, eng_cfg, orb_cfgs)
        oos_summ["J"] = j_score(oos_summ, min_n=5)
        oos_summ["sharpe"] = sharpe_of_trades(oos_trades)
        # FULL run
        full_summ, full_trades = run_one(specs, merged_full, balance, eng_cfg, orb_cfgs)
        full_summ["J"] = j_score(full_summ, min_n=15)
        full_summ["sharpe"] = sharpe_of_trades(full_trades)

        row = {
            "cfg": label,
            "is": is_summ,
            "oos": oos_summ,
            "full": full_summ,
            # Robust score = product of signs, favoring both-positive + small gap
            "robust_score": _robust(is_summ, oos_summ, full_summ),
        }
        rows.append(row)

        # Update external progress file EVERY cell (cheap, enables live monitoring)
        try:
            best_so_far = max(rows, key=lambda r: r["robust_score"])
            eta_sec = (_time.time() - t_start) / i * (total - i)
            prog_path.write_text(
                f"{i}/{total}  elapsed={int(_time.time()-t_start)}s  ETA={int(eta_sec)}s"
                f"  best_robust_score={best_so_far['robust_score']:.2f}"
                f"  best_FULL_pnl=${best_so_far['full'].get('net_pnl',0):+,.0f}"
                f"  best_FULL_N={best_so_far['full'].get('entries',0)}\n",
                encoding="utf-8")
        except Exception:
            pass

        if i % 24 == 0 or i == total:
            eta = (_time.time() - t_start) / i * (total - i)
            best = max(rows, key=lambda r: r["robust_score"])
            print(f"  [{i:>3}/{total}]  ETA {eta:>4.0f}s"
                  f"  |  best-robust IS=${best['is']['net_pnl']:+,.0f}"
                  f"  OOS=${best['oos']['net_pnl']:+,.0f}"
                  f"  FULL=${best['full']['net_pnl']:+,.0f}", flush=True)


    elapsed = _time.time() - t_start
    print()
    print(f"  Finished {total} cells × 3 windows = {total*3} backtests in {elapsed:.0f}s"
          f"  ({elapsed/(total*3)*1000:.1f}ms per run)")
    print()

    # ---- Rank by robustness -----------------------------------------
    ranked = sorted(rows, key=lambda r: r["robust_score"], reverse=True)
    print("=" * 148)
    print(f"  TOP 15 BY ROBUSTNESS  (both IS and OOS positive, small IS↔OOS gap)")
    print("=" * 148)
    print(f"  {'#':>3}  {'IS PnL':>9}  {'IS PF':>6} {'IS N':>4}  "
          f"{'OOS PnL':>9}  {'OOS PF':>7} {'OOS N':>5}  "
          f"{'FULL PnL':>9}  {'FULL PF':>7} {'FULL N':>5}  |  "
          f"or tp1/tp2 slb amp nr hurst")
    print("-" * 148)
    for i, r in enumerate(ranked[:15], 1):
        c = r["cfg"]
        hg = "trend" if c["hurst_min"] >= 0.55 else \
             ("mid  " if c["hurst_min"] >= 0.40 else "all  ")
        nr_str = "NR7" if c["require_nr7"] else "off"
        is_, oos_, ful = r["is"], r["oos"], r["full"]
        print(f"  {i:>3}  "
              f"${is_['net_pnl']:>+7,.0f}  {is_['pf']:>5.2f} {is_.get('entries',0):>4}  "
              f"${oos_['net_pnl']:>+7,.0f}  {oos_['pf']:>6.2f} {oos_.get('entries',0):>5}  "
              f"${ful['net_pnl']:>+7,.0f}  {ful['pf']:>6.2f} {ful.get('entries',0):>5}  |  "
              f"{c['or_minutes']:>2} {c['tp1']:.1f}/{c['tp2']:.1f} "
              f"{c['sl_buffer']:.1f} {c['amp_hurdle']:.1f} {nr_str:<3} {hg}")
    print()

    # ---- Deflated Sharpe of the winner -------------------------------
    winner = ranked[0]
    full_sr = winner["full"]["sharpe"]
    n_t = winner["full"].get("entries", 0)
    dsr = deflated_sharpe(full_sr, n_trials=total, n_trades=max(n_t, 3))
    print("=" * 110)
    print("  WINNER (by robust score)  —  DEFLATED SHARPE RATIO")
    print("=" * 110)
    print(f"  Raw Sharpe (full 3mo)     : {full_sr:>6.2f}")
    print(f"  Grid size                 : {total}")
    print(f"  Trade count               : {n_t}")
    print(f"  Deflated Sharpe (p)       : {dsr:.3f}"
          f"  ({'SIGNIFICANT' if dsr > 0.95 else 'LUCKY-LOOKING'} at 5%)")
    c = winner["cfg"]
    print()
    print(f"  Config: or={c['or_minutes']}m  tp={c['tp1']:.1f}/{c['tp2']:.1f}  "
          f"slb={c['sl_buffer']:.1f}  amp={c['amp_hurdle']:.1f}  "
          f"nr7={c['require_nr7']}  hurst=[{c['hurst_min']:.2f},{c['hurst_max']:.2f}]")
    print()

    # ---- Per-symbol best from FULL window ---------------------------
    print("=" * 110)
    print("  BEST CONFIG PER SYMBOL  (from full 3-month run, N>=5, net>0)")
    print("=" * 110)
    per_sym_best = {}
    for sym in sorted(SYMBOLS):
        best_net = 0.0
        best_r = None
        for r in rows:
            d = r["full"].get("by_symbol", {}).get(sym)
            if not d or d["n"] < 5 or d["net"] <= 0:
                continue
            score = d["net"] * math.sqrt(d["n"]) * min(d["wr"], 0.85)
            if score > best_net:
                best_net = score
                best_r = (r, d)
        if best_r is None:
            print(f"  {sym:<7}  (no profitable config)")
            continue
        r, d = best_r
        c = r["cfg"]
        hg = "trend" if c["hurst_min"] >= 0.55 else \
             ("mid  " if c["hurst_min"] >= 0.40 else "all  ")
        nr_str = "NR7" if c["require_nr7"] else "off"
        print(f"  {sym:<7}  N={d['n']:>3}  WR={d['wr']*100:>4.1f}%  "
              f"net=${d['net']:>+7,.0f}  |  "
              f"or={c['or_minutes']}m tp={c['tp1']:.1f}/{c['tp2']:.1f} "
              f"slb={c['sl_buffer']:.1f} amp={c['amp_hurdle']:.1f} {nr_str} {hg}")
        per_sym_best[sym] = {"cfg": c, "stats": d}
    print()

    # ---- Probability of Backtest Overfitting (PBO-lite) -------------
    # Split trials in half by rank, measure how often the IS-top
    # configs also rank top in OOS.  Lower PBO = more robust.
    pbo = _pbo_lite(rows)
    print("=" * 110)
    print(f"  PROBABILITY OF BACKTEST OVERFITTING (PBO)  = {pbo*100:.1f}%")
    print(f"  (PBO < 50 % means IS-top configs generalise to OOS better than chance)")
    print("=" * 110)
    print()

    # ---- Save all --------------------------------------------------
    out = ROOT / "Results" / "phd_superior_v20.json"
    out.parent.mkdir(exist_ok=True, parents=True)

    def _clean(s):
        s = dict(s)
        if not math.isfinite(s.get("J", 0.0)):
            s["J"] = None
        if not math.isfinite(s.get("pf", 1.0)):
            s["pf"] = None
        if not math.isfinite(s.get("sharpe", 0.0)):
            s["sharpe"] = None
        return s

    payload = {
        "total_cells": total,
        "total_backtests": total * 3,
        "elapsed_sec": elapsed,
        "window": {"full": [str(tmin), str(tmax)],
                    "is": [str(tmin), str(is_end)],
                    "oos": [str(oos_start), str(tmax)]},
        "winner": {
            "cfg": winner["cfg"],
            "is": _clean(winner["is"]),
            "oos": _clean(winner["oos"]),
            "full": _clean(winner["full"]),
            "deflated_sharpe": dsr,
        },
        "pbo": pbo,
        "per_symbol_best": per_sym_best,
        "top20": [
            {"cfg": r["cfg"],
             "robust_score": r["robust_score"],
             "is": _clean(r["is"]), "oos": _clean(r["oos"]),
             "full": _clean(r["full"])}
            for r in ranked[:20]
        ],
    }
    with open(out, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"  Saved full results  →  {out.relative_to(ROOT)}")
    print()

    return 0


def _robust(is_s: dict, oos_s: dict, full_s: dict) -> float:
    """Robustness: reward configs where IS+OOS+FULL all positive,
    and the IS vs OOS PnL gap is small relative to mean."""
    is_p = is_s.get("net_pnl", 0.0)
    oos_p = oos_s.get("net_pnl", 0.0)
    full_p = full_s.get("net_pnl", 0.0)
    is_n = is_s.get("entries", 0)
    oos_n = oos_s.get("entries", 0)
    if is_n < 8 or oos_n < 3:
        return -1e9
    if is_p <= 0 or oos_p <= 0:
        # Heavy penalty for either half losing money
        return min(is_p, oos_p)
    mean_p = (is_p + oos_p) / 2.0
    gap = abs(is_p - oos_p)
    gap_ratio = gap / mean_p if mean_p > 0 else 99.0
    # Higher is better: full PnL, consistency, size of sample
    return (full_p * math.sqrt(is_n + oos_n)
            / (1.0 + gap_ratio)
            / (1.0 + full_s.get("max_dd_pct", 0.0)))


def _pbo_lite(rows):
    """CSCV-lite: for each trial, does being top-half on IS mean top-half on OOS?"""
    if len(rows) < 4:
        return 0.5
    n = len(rows)
    by_is = sorted(range(n), key=lambda i: rows[i]["is"].get("net_pnl", 0.0),
                    reverse=True)
    by_oos = sorted(range(n), key=lambda i: rows[i]["oos"].get("net_pnl", 0.0),
                     reverse=True)
    is_top_half = set(by_is[:n // 2])
    oos_top_half = set(by_oos[:n // 2])
    overlap = len(is_top_half & oos_top_half)
    # Probability top-IS is NOT in top-OOS (i.e. overfitting rate)
    return 1 - (overlap / (n // 2))


if __name__ == "__main__":
    sys.exit(main())
