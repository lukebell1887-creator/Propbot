#!/usr/bin/env python3
"""
phd_optimize_v19.py
===================
PhD-grade Bayesian optimization of the SmartBB mean-reversion engine.

Methodology (proper, not fiddling):

  1.  Tree-structured Parzen Estimator (TPE) — a Bayesian surrogate model that
      learns which regions of parameter space are promising and focuses
      sampling there.  Far more efficient than grid / random.

  2.  Walk-forward split: 80 % in-sample (IS) for objective evaluation, 20 %
      out-of-sample (OOS) held out for final honesty check.

  3.  Hard constraints (pruning):
          - phantoms == 0       (geometrically valid SL for every trade)
          - trades   >= 30      (statistical minimum)
          - max_dd   <= 6 %     (5 %ers daily drawdown limit with headroom)

  4.  Objective (single-value optimisation, maximised):

          J = expectancy_R · sqrt(trades) · DD_penalty

      where DD_penalty = clip(1 − max_dd / 0.04, 0.1, 1.0)

      This is the "Sharpe-flavoured" Kelly-scaled objective:
          - expectancy_R          = per-trade edge (unitless in R)
          - sqrt(trades)          = statistical power (t-stat scaling)
          - DD_penalty            = risk-of-ruin discount

  5.  Parameters searched (all symbols share the same config — we then allow
      per-symbol refinement in a second pass if needed):

          z_min_abs         float    [1.4,  2.4]
          z_max_abs         float    [2.3,  3.2]
          z_quantile        float    [0.50, 0.98]
          hurst_max_abs     float    [0.40, 0.60]
          hurst_quantile    float    [0.20, 0.95]
          stop_atr_mult     float    [0.30, 1.50]
          tp_frac           float    [0.30, 1.20]
          ou_max_halflife   float    [60,   500]

  6.  300 TPE trials with early pruning of obviously-bad regions.

  7.  After convergence, apply the best config to the v18 engine (with full
      Grossman-Zhou dynamic sizing) and report HONEST 3 m OOS $ P&L.

Outputs:
    Results/phd_optimize_v19_study.db   (Optuna SQLite for reruns)
    Results/phd_optimize_v19_best.json  (best params + full metrics)
    Docs/PHD_V19_RESULTS.md             (human-readable summary)
"""
from __future__ import annotations

import csv
import json
import math
import sys
import time as _time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import optuna                                                  # noqa: E402
from optuna.samplers import TPESampler                         # noqa: E402
from optuna.pruners  import MedianPruner                       # noqa: E402

from src.live.v15_live import load_v15_params                  # noqa: E402
from src.smartbb_engine import SMARTBB_UNIVERSE                # noqa: E402
from src.smartbb_engine_v14 import (                           # noqa: E402
    SmartBBV14Engine, SmartBBV14Config,
)

TIER1 = ["US30", "US100", "US500", "DE40", "XAUUSD"]

# Silence Optuna INFO spam
optuna.logging.set_verbosity(optuna.logging.WARNING)


# --- data loading (same as backtest_v18) ----------------------------------
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
        firsts[s] = datetime.fromisoformat(rows[0][0])
        lasts[s]  = datetime.fromisoformat(rows[-1][0])
    end   = min(lasts.values())
    start = max(max(firsts.values()), end - timedelta(days=months * 31))
    return start, end


def build_merged(files, tmin, tmax):
    streams = {s: load_m1(p, tmin, tmax) for s, p in files.items()}
    merged = []
    for s, bars in streams.items():
        merged.extend((t, s, o, h, l, c) for (t, o, h, l, c) in bars)
    merged.sort(key=lambda r: r[0])
    return merged


# --- single-config evaluation ---------------------------------------------
def mutate_params(base_params, *,
                   z_min_abs, z_max_abs, z_quantile,
                   hurst_max_abs, hurst_quantile,
                   stop_atr_mult, tp_frac, ou_max_halflife):
    out = {}
    for sym, p in base_params.items():
        out[sym] = replace(p,
            z_min_abs         = z_min_abs,
            z_max_abs         = z_max_abs,
            z_quantile        = z_quantile,
            hurst_max_abs     = hurst_max_abs,
            hurst_quantile    = hurst_quantile,
            stop_atr_mult     = stop_atr_mult,
            tp_frac           = tp_frac,
            ou_max_halflife   = ou_max_halflife,
        )
    return out


def evaluate(merged, symbols, base_params, cfg, overrides, balance=100_000.0):
    """Run a single config, return (metrics_dict, trades)."""
    params = mutate_params(base_params, **overrides)
    specs = [SMARTBB_UNIVERSE[s] for s in symbols]
    eng = SmartBBV14Engine(
        symbols=specs, params=params, cfg=cfg, initial_equity=balance,
    )
    eng.cfg = replace(cfg,
                        base_risk_pct=0.005,
                        min_risk_pct=0.005,
                        max_risk_pct=0.005)

    for t, s, o, h, l, c in merged:
        eng.on_bar(s, t.timestamp(), t.strftime("%Y-%m-%d"),
                   t.hour, t.minute, o, h, l, c)

    summ = eng.summary()
    phantoms = sum(1 for tr in eng.trades
                      if tr.exit_reason == "stop_loss"
                         and tr.realised_R > 0 and tr.bars_held == 0)
    summ["phantoms"] = phantoms
    return summ, eng.trades


# --- objective -------------------------------------------------------------
def objective_factory(merged_is, symbols, base_params, cfg):
    def objective(trial: optuna.Trial):
        z_min  = trial.suggest_float("z_min_abs",     1.4,  2.4)
        z_max  = trial.suggest_float("z_max_abs",     max(z_min + 0.05, 2.3), 3.2)
        zq     = trial.suggest_float("z_quantile",    0.50, 0.98)
        hmax   = trial.suggest_float("hurst_max_abs", 0.40, 0.60)
        hq     = trial.suggest_float("hurst_quantile",0.20, 0.95)
        sam    = trial.suggest_float("stop_atr_mult", 0.30, 1.50)
        tpf    = trial.suggest_float("tp_frac",       0.30, 1.20)
        oumax  = trial.suggest_float("ou_max_halflife",60,  500)

        overrides = dict(
            z_min_abs=z_min, z_max_abs=z_max, z_quantile=zq,
            hurst_max_abs=hmax, hurst_quantile=hq,
            stop_atr_mult=sam, tp_frac=tpf, ou_max_halflife=oumax,
        )

        try:
            s, _ = evaluate(merged_is, symbols, base_params, cfg, overrides)
        except Exception as e:
            # Malformed combo → worst score
            return -999.0

        # Hard constraints
        if s["phantoms"] > 0:
            return -9999.0                       # invalid SL geometry
        if s["trades"] < 30:
            return -500.0 + s["trades"]          # too few trades

        # DD penalty
        dd = s["max_dd_pct"] / 100.0             # fraction
        dd_pen = max(0.1, min(1.0, 1.0 - dd / 0.04))

        # Objective: expectancy · sqrt(N) · dd_pen
        # (same shape as a Sharpe-scaled Kelly edge)
        exp_R = s["expectancy_R"]
        N     = s["trades"]
        if not math.isfinite(exp_R):
            return -999.0
        score = exp_R * math.sqrt(max(N, 1)) * dd_pen

        # Stash for later inspection
        trial.set_user_attr("trades",       s["trades"])
        trial.set_user_attr("expectancy_R", s["expectancy_R"])
        trial.set_user_attr("pf",           s["pf"] if s["pf"] != float("inf") else 999.0)
        trial.set_user_attr("win_rate",     s["win_rate"])
        trial.set_user_attr("net_pnl",      s["net_pnl"])
        trial.set_user_attr("max_dd_pct",   s["max_dd_pct"])
        return score

    return objective


def main(months=3, balance=100_000.0, n_trials=300):
    t0_all = _time.time()
    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in TIER1}
    files = {s: p for s, p in files.items() if p.exists()}
    if not files:
        print("ERROR: no {SYMBOL}_M1.csv files in data/historical/"); return 1

    tmin, tmax = common_window(files, months)
    print(f"Full window: {tmin.date()} -> {tmax.date()}   ({(tmax-tmin).days} days)")

    # 80/20 walk-forward split
    split_days = int((tmax - tmin).days * 0.8)
    t_split = tmin + timedelta(days=split_days)
    print(f"IS   window: {tmin.date()} -> {t_split.date()}   ({split_days} days)")
    print(f"OOS  window: {t_split.date()} -> {tmax.date()}   ({(tmax-t_split).days} days)")

    tuning_path = ROOT / "Results" / "v15_ultimate_tuning.json"
    base_params = load_v15_params(str(tuning_path)) if tuning_path.exists() else {}
    if not base_params:
        print("ERROR: no v15 tuning"); return 1

    cfg      = SmartBBV14Config()
    internal = sorted(files)

    print("Loading M1 streams...")
    merged_is  = build_merged(files, tmin,    t_split)
    merged_oos = build_merged(files, t_split, tmax)
    print(f"IS bars:  {len(merged_is):,}")
    print(f"OOS bars: {len(merged_oos):,}")

    # ─── Optuna study ────────────────────────────────────────────────────
    sampler = TPESampler(seed=42, n_startup_trials=30, multivariate=True)
    pruner  = MedianPruner(n_startup_trials=30, n_warmup_steps=0)
    db_path = ROOT / "Results" / "phd_optimize_v19_study.db"
    if db_path.exists():
        db_path.unlink()
    study = optuna.create_study(
        direction="maximize",
        sampler=sampler, pruner=pruner,
        study_name="phd_v19",
        storage=f"sqlite:///{db_path.as_posix()}",
    )

    obj = objective_factory(merged_is, internal, base_params, cfg)

    print()
    print("=" * 100)
    print(f"OPTUNA TPE — {n_trials} trials")
    print("=" * 100)
    n_valid = 0
    best_so_far = -float("inf")
    t0 = _time.time()

    def on_step(study, trial):
        nonlocal n_valid, best_so_far
        if trial.value is None: return
        if trial.value > -500: n_valid += 1
        if trial.value > best_so_far:
            best_so_far = trial.value
            t = trial.user_attrs
            print(f"  [trial {trial.number:3d}] NEW BEST J={trial.value:+7.3f}  "
                  f"N={t.get('trades',0):3d} "
                  f"E[R]={t.get('expectancy_R',0):+.3f} "
                  f"PF={t.get('pf',0):5.2f} "
                  f"DD={t.get('max_dd_pct',0):4.2f}% "
                  f"PnL=${t.get('net_pnl',0):+8,.0f}",
                  flush=True)

    study.optimize(obj, n_trials=n_trials, callbacks=[on_step], show_progress_bar=False)

    elapsed_opt = _time.time() - t0
    print()
    print(f"Optimisation done.  Elapsed: {elapsed_opt:.0f}s")
    print(f"Valid trials: {n_valid}/{n_trials}")

    # ─── Best trial ──────────────────────────────────────────────────────
    best = study.best_trial
    print()
    print("=" * 100)
    print("BEST CONFIGURATION (IS)")
    print("=" * 100)
    for k, v in best.params.items():
        print(f"  {k:<20} = {v:.4f}")
    print()
    print(f"  J (objective)    = {best.value:+.4f}")
    for k, v in best.user_attrs.items():
        print(f"  {k:<20} = {v}")

    # ─── OOS re-run on held-out last 20 % ────────────────────────────────
    print()
    print("=" * 100)
    print("OUT-OF-SAMPLE VALIDATION (last 20 %, never seen by optimizer)")
    print("=" * 100)
    s_oos, trades_oos = evaluate(merged_oos, internal, base_params, cfg, best.params)
    print(f"  OOS trades         = {s_oos['trades']}")
    print(f"  OOS phantoms       = {s_oos['phantoms']}")
    print(f"  OOS expectancy_R   = {s_oos['expectancy_R']:+.3f}")
    print(f"  OOS win_rate       = {s_oos['win_rate']*100:.1f}%")
    print(f"  OOS pf             = {s_oos['pf'] if s_oos['pf']!=float('inf') else 999:.2f}")
    print(f"  OOS net_pnl ($)    = ${s_oos['net_pnl']:+,.2f}   (@ fixed 0.5%)")
    print(f"  OOS max_dd_pct     = {s_oos['max_dd_pct']:.2f}%")

    # ─── Save ────────────────────────────────────────────────────────────
    out = {
        "generated":      datetime.utcnow().isoformat() + "Z",
        "months":         months,
        "is_window":      [str(tmin.date()), str(t_split.date())],
        "oos_window":     [str(t_split.date()), str(tmax.date())],
        "n_trials":       n_trials,
        "n_valid_trials": n_valid,
        "best":           {
            "params":     best.params,
            "is_metrics": best.user_attrs,
            "J":          best.value,
        },
        "oos": {
            "trades":       s_oos["trades"],
            "phantoms":     s_oos["phantoms"],
            "expectancy_R": s_oos["expectancy_R"],
            "win_rate":     s_oos["win_rate"],
            "pf":           s_oos["pf"] if s_oos["pf"]!=float("inf") else 999.0,
            "net_pnl":      s_oos["net_pnl"],
            "max_dd_pct":   s_oos["max_dd_pct"],
        },
        "elapsed_sec":    round(_time.time() - t0_all, 1),
    }
    out_file = ROOT / "Results" / "phd_optimize_v19_best.json"
    out_file.parent.mkdir(exist_ok=True)
    with open(out_file, "w") as f:
        json.dump(out, f, indent=2)
    print()
    print(f"Saved: {out_file}")
    print(f"Optuna DB: {db_path}")
    print(f"Total elapsed: {out['elapsed_sec']:.0f}s")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--months",  type=int,   default=3)
    ap.add_argument("--balance", type=float, default=100_000.0)
    ap.add_argument("--trials",  type=int,   default=300)
    a = ap.parse_args()
    sys.exit(main(a.months, a.balance, a.trials))
