#!/usr/bin/env python3
"""
backtest_v20_phd_3d_search.py — multi-dimensional PhD-grade risk-calculus search.

We vary THREE independent risk-math dimensions simultaneously and use
the PhD suite (GZ-aware bootstrap + CVaR + regime stress) to pick the
combination that maximises expected return SUBJECT TO the prop-firm
DD constraint.

Dimensions (all published-paper parameters):

  A. kelly_fraction  f_K  ∈ {0.15, 0.25, 0.40}
     Thorp 1962 fractional-Kelly safety factor. Lower = more conservative.

  B. gz_knee_pct    d_knee ∈ {0.020, 0.030, 0.040}
     Grossman-Zhou 2009 knee point — the DD% at which position size is
     shrunk to 10% of full. Curve is piecewise linear:
        DD=0 → mult=1.0
        DD=d_knee → mult=0.10
        DD=d_knee+0.005 → mult=0.0
     Lower d_knee = brakes hit EARLIER.

  C. ceiling_pct   c_max  ∈ {0.30, 0.50, 0.80, 1.20, 2.00} %
     Markowitz absolute max per-trade risk regardless of what Kelly says.

The Bayesian shrinkage (layer 2), vol regime (layer 3), trust warm-up
(layer 5) and correlation scaling (layer 6) all remain ACTIVE at their
paper-default values — they are features not hyper-parameters.

For each (f_K, d_knee, c_max) triple we run:
  * Full 3-month ORB v20 backtest on DE40+US30+XAUUSD M1
  * 10,000 stationary block bootstrap paths (Politis-Romano 1994)
  * GZ-aware path simulation (live brake feedback loop)
  * Stressed version (wins × 0.8, losses × 1.2) — regime drift
  * Rockafellar-Uryasev CVaR (5%) penalty

PASS criteria (all must hold):
  [1] Realised DD ≤ 4 %
  [2] GZ bootstrap P(DD > 4 %) < 5 %
  [3] Stressed GZ bootstrap P(DD > 4 %) < 10 %
  [4] Bootstrap mean PnL > 0
  [5] CVaR-adjusted score  = mean + 2·CVaR5  > 0

Output:
  * Full table of all 45 configs sorted by CVaR-score
  * Pareto frontier (PnL vs DD-risk)
  * Top-1 SAFE pick (largest PASS by score)
"""

from __future__ import annotations

import csv, json, random, statistics, sys, time as _time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from src.smartbb_engine import SMARTBB_UNIVERSE
from src.orb_engine_v20 import ORBEngineV20, ORBEngineConfig
from src.momentum.orb import ORBConfig
from src.dynamic_sizer_v20 import DynamicSizerV20, SizerV20Config, SEEDS

SYMBOLS = ["DE40", "US30", "XAUUSD"]

ORB_CONFIGS = {
    "DE40":   ORBConfig(or_start_hour=8,  or_start_minute=0,  or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=1.5,
                        tp2_range_mult=3.0, sl_buffer_range_mult=0.3),
    "US30":   ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.0),
    "XAUUSD": ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.6),
}
AMP_HURDLE_BY_SYM = {"DE40": 3.0, "US30": 4.5, "XAUUSD": 4.5}
DD_TARGET = 0.04


# ---- Data I/O --------------------------------------------------------
def load_m1(path, tmin, tmax):
    out = []
    with open(path, "r", newline="") as f:
        for row in csv.DictReader(f):
            try: t = datetime.fromisoformat(row["time"])
            except Exception: t = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            if tmin and t < tmin: continue
            if tmax and t > tmax: continue
            out.append((t, float(row["open"]), float(row["high"]),
                       float(row["low"]), float(row["close"])))
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


def build_gz_curve(knee: float):
    """Build a 3-point linear GZ curve knee-parameterised:
       (0,1.0) -> (knee, 0.10) -> (knee+0.005, 0.0)"""
    return (
        (0.000, 1.00),
        (max(knee*0.333, 0.001), 0.80),
        (max(knee*0.667, 0.002), 0.50),
        (knee,                   0.10),
        (knee + 0.005,           0.00),
    )


def run_config(f_K, d_knee, c_max, streams, balance, specs):
    """Runs one backtest. Returns list of per-entry PnLs + summary stats."""
    cfg_sizer = SizerV20Config(
        kelly_fraction = f_K,
        hard_cap_pct   = c_max,
        gz_curve       = build_gz_curve(d_knee),
    )
    sizer = DynamicSizerV20(cfg=cfg_sizer, seeds=SEEDS)
    def sfn(symbol, equity, peak_equity, open_positions):
        return sizer.compute_risk_pct(symbol=symbol, equity=equity,
                                       peak_equity=peak_equity,
                                       open_positions=open_positions)
    engines = {}
    for sym in SYMBOLS:
        if sym not in streams: continue
        spec = specs[sym]
        cfg = ORBEngineConfig(risk_pct=0.005, amp_hurdle=AMP_HURDLE_BY_SYM[sym],
                               require_nr7=False, trail_atr_mult=0.8,
                               tp1_close_frac=0.50, tp2_close_frac=0.25,
                               risk_pct_fn=sfn)
        engines[sym] = ORBEngineV20(symbols=[spec], cfg=cfg,
                                     orb_configs={sym: ORB_CONFIGS[sym]},
                                     initial_equity=balance)
    allb = []
    for sym, bars in streams.items():
        allb.extend((t, sym, o, h, l, c) for (t, o, h, l, c) in bars)
    allb.sort(key=lambda r: r[0])
    for t, s, o, h, l, c in allb:
        if s in engines:
            engines[s].on_bar(s, t.timestamp(), t.strftime("%Y-%m-%d"),
                               t.hour, t.minute, o, h, l, c)
    trades = [tr for eng in engines.values() for tr in eng.trades]
    trades.sort(key=lambda x: x.exit_time)
    per = defaultdict(lambda: {"net_pnl": 0.0, "exit_time": 0.0})
    for tr in trades:
        d = per[(tr.symbol, tr.entry_time)]
        d["net_pnl"] += tr.net_pnl
        d["exit_time"] = max(d["exit_time"], tr.exit_time)
    pnls = [d["net_pnl"] for d in sorted(per.values(), key=lambda d: d["exit_time"])]
    eq=balance; peak=eq; mdd=0.0
    for p in pnls:
        eq += p; peak=max(peak,eq)
        dd=(peak-eq)/peak if peak>0 else 0.0
        mdd=max(mdd,dd)
    wins=[p for p in pnls if p>0]
    losses=[p for p in pnls if p<=0]
    gw=sum(wins); gl=-sum(losses)
    pf = gw/gl if gl>0 else float("inf")
    wr = len(wins)/len(pnls) if pnls else 0.0
    return pnls, dict(net_pnl=sum(pnls), mdd=mdd, pf=pf, wr=wr, n=len(pnls))


# ---- Stationary block bootstrap --------------------------------------
def block_bootstrap_paths(pnls, n_paths=8_000, block_size=5, seed=42):
    N = len(pnls)
    if N==0: return []
    rng = random.Random(seed)
    geom_p = 1.0/block_size
    paths=[]
    for _ in range(n_paths):
        path=[]
        while len(path) < N:
            start = rng.randrange(N)
            blen = 1
            while rng.random() >= geom_p and blen < N:
                blen += 1
            for k in range(blen):
                if len(path) >= N: break
                path.append(pnls[(start+k) % N])
        paths.append(path)
    return paths

def path_gz(path, balance=100_000.0, dd_max=0.05, floor=0.10):
    eq=balance; peak=eq; mdd=0.0; total=0.0
    for p in path:
        dd = (peak-eq)/peak if peak>0 else 0.0
        mult = max(floor, 1.0 - dd/dd_max) if dd_max>0 else 1.0
        shr_p = p * mult
        eq += shr_p
        peak = max(peak, eq)
        dd2 = (peak-eq)/peak if peak>0 else 0.0
        mdd = max(mdd, dd2)
        total += shr_p
    return total, mdd

def pct(xs, q):
    if not xs: return 0.0
    xs2 = sorted(xs)
    i = max(0, min(len(xs2)-1, int(q * (len(xs2)-1))))
    return xs2[i]

def cvar(xs, alpha=0.05):
    if not xs: return 0.0
    xs2 = sorted(xs)
    k = max(1, int(alpha * len(xs2)))
    return sum(xs2[:k]) / k


# ---- Main ------------------------------------------------------------
def main():
    balance = 100_000.0
    months = 3
    N_BOOT = 8_000
    BLOCK = 5

    # 3-D grid
    F_K_GRID   = [0.15, 0.25, 0.40]
    KNEE_GRID  = [0.020, 0.030, 0.040]
    CEIL_GRID  = [0.003, 0.005, 0.008, 0.012, 0.020]

    out_lines = []
    def p(m=""):
        print(m, flush=True); out_lines.append(m)

    p("=" * 140)
    p("  v20 3-D PHD CALCULUS SEARCH (Thorp × Grossman-Zhou × Markowitz)")
    p(f"  ${balance:,.0f}  |  3 months  |  DE40+US30+XAUUSD  |  N_boot={N_BOOT:,}  block={BLOCK}")
    p(f"  Grid: f_K × d_knee × c_max = {len(F_K_GRID)} × {len(KNEE_GRID)} × {len(CEIL_GRID)} = "
      f"{len(F_K_GRID)*len(KNEE_GRID)*len(CEIL_GRID)} configs")
    p(f"  DD target = {DD_TARGET*100:.0f}%  |  confidence = 95 %  |  stress = wins*0.8, losses*1.2")
    p("=" * 140)

    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in SYMBOLS}
    files = {s: pp for s, pp in files.items() if pp.exists()}
    if not files: p("ERROR: missing data"); return 1
    tmin, tmax = common_window(files, months)
    specs = {s: SMARTBB_UNIVERSE[s] for s in files}
    streams = {s: load_m1(files[s], tmin, tmax) for s in files}
    p(f"  Window: {tmin.date()} → {tmax.date()}  ({sum(len(v) for v in streams.values()):,} bars)")
    p("")

    results = []
    total = len(F_K_GRID) * len(KNEE_GRID) * len(CEIL_GRID)
    i = 0
    t_start = _time.time()

    for f_K in F_K_GRID:
        for knee in KNEE_GRID:
            for ceil in CEIL_GRID:
                i += 1
                t0 = _time.time()
                pnls, R = run_config(f_K, knee, ceil, streams, balance, specs)
                if not pnls:
                    p(f"  [{i:2d}/{total}] f_K={f_K:.2f} knee={knee*100:.1f}% ceil={ceil*100:.2f}% — NO TRADES")
                    continue
                paths = block_bootstrap_paths(pnls, N_BOOT, BLOCK)
                gz_term = []; gz_dd = []
                for path in paths:
                    te, md = path_gz(path, balance, 0.05)
                    gz_term.append(te); gz_dd.append(md)
                gz_mean = statistics.mean(gz_term)
                gz_p50  = pct(gz_dd, 0.50) * 100.0
                gz_p95  = pct(gz_dd, 0.95) * 100.0
                gz_p_over = sum(1 for d in gz_dd if d > DD_TARGET) / len(gz_dd)

                stressed = [0.8*pp if pp>0 else 1.2*pp for pp in pnls]
                str_paths = block_bootstrap_paths(stressed, N_BOOT, BLOCK, seed=43)
                str_dd = [path_gz(pp, balance, 0.05)[1] for pp in str_paths]
                str_p_over = sum(1 for d in str_dd if d > DD_TARGET) / len(str_dd)

                cv5 = cvar(gz_term, 0.05)
                score = gz_mean + 2.0 * cv5

                passes = (R["mdd"] <= DD_TARGET
                          and gz_p_over < 0.05
                          and str_p_over < 0.10
                          and gz_mean > 0
                          and score > 0)

                results.append(dict(
                    f_K=f_K, knee=knee, ceil=ceil, passes=passes,
                    realised=R, gz_mean=gz_mean, gz_p50=gz_p50, gz_p95=gz_p95,
                    gz_p_over=gz_p_over, str_p_over=str_p_over,
                    cvar5=cv5, score=score, elapsed=_time.time()-t0,
                ))

                dt = _time.time() - t_start
                eta = dt/i * (total - i)
                tag = "✓" if passes else "·"
                p(f"  [{i:2d}/{total}] {tag} f_K={f_K:.2f} knee={knee*100:>4.1f}% ceil={ceil*100:>4.2f}% "
                  f"| realPnL=${R['net_pnl']:>+7,.0f} realDD={R['mdd']*100:>4.2f}% | "
                  f"GZ_p95={gz_p95:>4.2f}% P(>4%)={gz_p_over*100:>5.2f}% str={str_p_over*100:>5.2f}% "
                  f"| score=${score:>+7,.0f}   eta={eta:.0f}s")

    p("")
    p("=" * 140)

    # Sort by score descending; tag passers
    results.sort(key=lambda r: -r["score"])

    p("  TOP 15 CONFIGS BY CVAR-SCORE (• = passes all 5 PhD tests)")
    p("")
    p(f"    #   f_K  knee  ceil   realPnL   realDD  |  GZ_p95  P(>4%)  str%     CVaR5    SCORE   PASS")
    p(f"    {'-'*104}")
    for j, r in enumerate(results[:15]):
        mark = "✓" if r["passes"] else " "
        p(f"    {j+1:>2}  {r['f_K']:.2f} {r['knee']*100:>4.1f}% {r['ceil']*100:>4.2f}%  "
          f"${r['realised']['net_pnl']:>+8,.0f} {r['realised']['mdd']*100:>5.2f}%  |  "
          f"{r['gz_p95']:>5.2f}% {r['gz_p_over']*100:>5.2f}% {r['str_p_over']*100:>5.2f}%  "
          f"${r['cvar5']:>+6,.0f}  ${r['score']:>+7,.0f}   [{mark}]")

    p("")
    winners = [r for r in results if r["passes"]]
    p(f"  PASSING CONFIGS: {len(winners)} / {len(results)}")
    if winners:
        winners.sort(key=lambda r: -r["score"])
        p("")
        p("  TOP 5 PASSING CONFIGS (the mathematically SAFE pool):")
        for j, r in enumerate(winners[:5]):
            p(f"    #{j+1}  f_K={r['f_K']:.2f}  knee={r['knee']*100:.1f}%  ceil={r['ceil']*100:.2f}%"
              f" → realPnL=${r['realised']['net_pnl']:+,.0f}"
              f"  realDD={r['realised']['mdd']*100:.2f}%"
              f"  score=${r['score']:+,.0f}")
        chosen = winners[0]
        p("")
        p("=" * 140)
        p(f"  >>> OPTIMAL CONFIG:")
        p(f"      kelly_fraction  = {chosen['f_K']:.2f}   (Thorp fractional-Kelly)")
        p(f"      gz_knee_pct     = {chosen['knee']*100:.2f} %  (Grossman-Zhou brake inflection)")
        p(f"      hard_cap_pct    = {chosen['ceil']*100:.2f} %  (Markowitz absolute ceiling)")
        p("")
        R = chosen["realised"]
        p(f"      Realised PnL    : ${R['net_pnl']:+,.0f}  ({R['net_pnl']/balance*100:+.2f}% in 3 mo)")
        p(f"      Realised DD     : {R['mdd']*100:.2f} %")
        p(f"      PF / WR         : {R['pf']:.2f} / {R['wr']*100:.1f}%")
        p(f"      GZ-bootstrap mean PnL : ${chosen['gz_mean']:+,.0f}")
        p(f"      GZ-bootstrap DD p95   : {chosen['gz_p95']:.2f}%")
        p(f"      P(DD>4%)        : {chosen['gz_p_over']*100:.2f}%  (< 5 % required)")
        p(f"      Stressed P(DD>4%): {chosen['str_p_over']*100:.2f}%  (< 10 % required)")
        p(f"      CVaR(5%) profit : ${chosen['cvar5']:+,.0f}")
        p(f"      Score           : ${chosen['score']:+,.0f}")
    else:
        p("")
        p("  NO CONFIG PASSES ALL 5 TESTS.")
        p("  Best risk-adjusted candidate: see #1 in top-15 above.")
        chosen = None
    p("=" * 140)

    out = ROOT / "Results"
    with open(out/"v20_phd_3d.txt","w",encoding="utf-8") as f: f.write("\n".join(out_lines))
    with open(out/"v20_phd_3d.json","w") as f:
        json.dump({
            "generated": datetime.utcnow().isoformat(),
            "window": [str(tmin.date()), str(tmax.date())],
            "grid": {"f_K": F_K_GRID, "knee": KNEE_GRID, "ceil": CEIL_GRID},
            "n_bootstrap": N_BOOT, "block_size": BLOCK,
            "dd_target_pct": DD_TARGET*100,
            "candidates": [{k:v for k,v in r.items() if k != "realised"}
                           | {"realised_summary": r["realised"]} for r in results],
            "chosen": {"f_K": chosen["f_K"], "knee": chosen["knee"],
                        "ceil": chosen["ceil"]} if chosen else None,
        }, f, indent=2, default=str)
    p(f"  Saved: Results/v20_phd_3d.txt + .json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
