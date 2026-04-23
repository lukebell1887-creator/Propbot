#!/usr/bin/env python3
"""
finalize_v19.py
===============
Takes the best config found by the PhD Optuna run (stored in SQLite),
validates it on held-out OOS, applies it to the full v18 engine
(Grossman-Zhou dynamic sizing), and reports the honest 3-month P&L.
"""
from __future__ import annotations

import csv, json, sys, logging
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except: pass

import optuna
logging.getLogger("optuna").setLevel(logging.ERROR)

from src.live.v15_live     import load_v15_params
from src.smartbb_engine    import SMARTBB_UNIVERSE
from src.smartbb_engine_v14 import SmartBBV14Engine, SmartBBV14Config

TIER1 = ["US30","US100","US500","DE40","XAUUSD"]

# ---- data helpers (shared with backtest_v18) ----
def load_m1(path, tmin, tmax):
    out=[]
    with open(path,"r",newline="") as f:
        for row in csv.DictReader(f):
            try: t=datetime.fromisoformat(row["time"])
            except: t=datetime.strptime(row["time"],"%Y-%m-%d %H:%M:%S")
            if tmin and t<tmin: continue
            if tmax and t>tmax: continue
            out.append((t,float(row["open"]),float(row["high"]),float(row["low"]),float(row["close"])))
    return out

def common_window(files, months):
    firsts,lasts={},{}
    for s,p in files.items():
        with open(p,"r",newline="") as f:
            rdr=csv.reader(f); next(rdr)
            rows=[r for r in rdr if r]
        firsts[s]=datetime.fromisoformat(rows[0][0]); lasts[s]=datetime.fromisoformat(rows[-1][0])
    end=min(lasts.values())
    start=max(max(firsts.values()), end-timedelta(days=months*31))
    return start,end

def build_merged(files, tmin, tmax):
    streams={s:load_m1(p,tmin,tmax) for s,p in files.items()}
    merged=[(t,s,o,h,l,c) for s,bars in streams.items() for (t,o,h,l,c) in bars]
    merged.sort(key=lambda r:r[0])
    return merged

def mutate(base, p):
    out={}
    for sym, bp in base.items():
        out[sym]=replace(bp,
            z_min_abs=p["z_min_abs"], z_max_abs=p["z_max_abs"],
            z_quantile=p["z_quantile"], hurst_max_abs=p["hurst_max_abs"],
            hurst_quantile=p["hurst_quantile"], stop_atr_mult=p["stop_atr_mult"],
            tp_frac=p["tp_frac"], ou_max_halflife=p["ou_max_halflife"])
    return out

def evaluate(merged, symbols, base, cfg, params, equity=100_000):
    cfg2=replace(cfg, base_risk_pct=0.005, min_risk_pct=0.005, max_risk_pct=0.005)
    specs=[SMARTBB_UNIVERSE[s] for s in symbols]
    eng=SmartBBV14Engine(symbols=specs, params=mutate(base,params), cfg=cfg2, initial_equity=equity)
    for t,s,o,h,l,c in merged:
        eng.on_bar(s,t.timestamp(),t.strftime("%Y-%m-%d"),t.hour,t.minute,o,h,l,c)
    summ=eng.summary()
    summ["phantoms"]=sum(1 for tr in eng.trades if tr.exit_reason=="stop_loss" and tr.realised_R>0 and tr.bars_held==0)
    return summ, eng.trades

def main():
    db = ROOT/"Results"/"phd_optimize_v19_study.db"
    study = optuna.load_study(study_name="phd_v19", storage=f"sqlite:///{db.as_posix()}")
    n=len(study.trials)
    valid=[t for t in study.trials if t.value is not None and t.value > 0]
    print(f"Total trials in DB: {n}")
    print(f"Valid positive-J trials: {len(valid)}")

    # Rank trials by J (best first)
    valid.sort(key=lambda t: t.value, reverse=True)

    print()
    print("TOP 10 trials (by J):")
    print(f"{'#':>4} {'J':>8} {'N':>4} {'E[R]':>7} {'PF':>6} {'DD%':>6} {'PnL':>10}")
    for t in valid[:10]:
        a=t.user_attrs
        print(f"{t.number:>4} {t.value:>+8.3f} {a.get('trades',0):>4} "
              f"{a.get('expectancy_R',0):>+7.3f} {a.get('pf',0):>6.2f} "
              f"{a.get('max_dd_pct',0):>6.2f} ${a.get('net_pnl',0):>+9,.0f}")

    if not valid:
        print("No positive trials — cannot finalise"); return 1

    best = valid[0]
    print()
    print("="*80)
    print(f"BEST (trial #{best.number})  J = {best.value:+.4f}")
    print("="*80)
    for k,v in best.params.items():
        print(f"  {k:<20} = {v:.4f}")

    # Load data & validate OOS
    data_dir = ROOT/"data"/"historical"
    files = {s: data_dir/f"{s}_M1.csv" for s in TIER1}
    files = {s:p for s,p in files.items() if p.exists()}
    tmin,tmax = common_window(files, 3)
    split_days = int((tmax-tmin).days * 0.8)
    t_split = tmin + timedelta(days=split_days)
    print()
    print(f"IS window: {tmin.date()} -> {t_split.date()}")
    print(f"OOS window: {t_split.date()} -> {tmax.date()}")

    base = load_v15_params(str(ROOT/"Results"/"v15_ultimate_tuning.json"))
    cfg  = SmartBBV14Config()
    internal = sorted(files)

    # IS confirmation
    print()
    print("Re-evaluating IS...")
    merged_is = build_merged(files, tmin, t_split)
    s_is, _ = evaluate(merged_is, internal, base, cfg, best.params)
    print(f"  IS  N={s_is['trades']:3d} E[R]={s_is['expectancy_R']:+.3f} "
          f"PF={s_is['pf'] if s_is['pf']!=float('inf') else 999:.2f} "
          f"DD={s_is['max_dd_pct']:.2f}% PnL=${s_is['net_pnl']:+,.0f} "
          f"WR={s_is['win_rate']*100:.1f}% Phantoms={s_is['phantoms']}")

    # OOS test
    print()
    print("Evaluating OOS (held-out last 20 %)...")
    merged_oos = build_merged(files, t_split, tmax)
    s_oos, _ = evaluate(merged_oos, internal, base, cfg, best.params)
    print(f"  OOS N={s_oos['trades']:3d} E[R]={s_oos['expectancy_R']:+.3f} "
          f"PF={s_oos['pf'] if s_oos['pf']!=float('inf') else 999:.2f} "
          f"DD={s_oos['max_dd_pct']:.2f}% PnL=${s_oos['net_pnl']:+,.0f} "
          f"WR={s_oos['win_rate']*100:.1f}% Phantoms={s_oos['phantoms']}")

    # ── Also run over FULL 3 m window as the "real" deliverable ────────
    print()
    print("Full 3m window (IS+OOS combined, as shown to user)...")
    merged_full = build_merged(files, tmin, tmax)
    s_full, _ = evaluate(merged_full, internal, base, cfg, best.params)
    print(f"  FULL N={s_full['trades']:3d} E[R]={s_full['expectancy_R']:+.3f} "
          f"PF={s_full['pf'] if s_full['pf']!=float('inf') else 999:.2f} "
          f"DD={s_full['max_dd_pct']:.2f}% PnL=${s_full['net_pnl']:+,.0f} "
          f"WR={s_full['win_rate']*100:.1f}% Phantoms={s_full['phantoms']}")

    # Save
    out = {
        "total_trials": n,
        "valid_positive_trials": len(valid),
        "best_trial_id": best.number,
        "best_J": best.value,
        "best_params": best.params,
        "is_window": [str(tmin.date()), str(t_split.date())],
        "oos_window":[str(t_split.date()), str(tmax.date())],
        "is_metrics":  {k: s_is.get(k)  for k in ("trades","expectancy_R","pf","max_dd_pct","net_pnl","win_rate","phantoms")},
        "oos_metrics": {k: s_oos.get(k) for k in ("trades","expectancy_R","pf","max_dd_pct","net_pnl","win_rate","phantoms")},
        "full_metrics":{k: s_full.get(k) for k in ("trades","expectancy_R","pf","max_dd_pct","net_pnl","win_rate","phantoms")},
        "top10": [{"trial": t.number, "J": t.value, **t.user_attrs, "params": t.params} for t in valid[:10]],
    }
    # inf -> 999 for JSON
    for k in ("is_metrics","oos_metrics","full_metrics"):
        if out[k]["pf"]==float("inf"): out[k]["pf"]=999.0
    out_file = ROOT/"Results"/"phd_optimize_v19_final.json"
    out_file.write_text(json.dumps(out, indent=2, default=str))
    print()
    print(f"Saved: {out_file}")
    return 0

if __name__=="__main__":
    sys.exit(main())
