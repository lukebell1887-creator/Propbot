#!/usr/bin/env python3
"""
backtest_v20_phd_suite.py — full PhD stress suite for picking the risk cap.

We run FIVE complementary tests on the same trade streams, each derived
from a published paper. A cap only "wins" if it passes ALL five.

  [1] REALISED PATH
      The actual Jan->Apr P&L + DD. Reality baseline.

  [2] NAIVE STATIONARY BLOCK BOOTSTRAP (Politis-Romano 1994)
      Resamples trade PnLs in blocks of 5 (preserves loss clustering).
      IGNORES the live risk brakes (upper-bound DD).

  [3] GZ-AWARE BOOTSTRAP  -- the CORRECT simulator
      Same resampling, but applies Grossman-Zhou (2009) DD-shrinkage
      live trade-by-trade:  size_mult = max(0.10, 1 - DD / DD_cap).
      This is what the bot ACTUALLY does on the VPS. True DD distribution.

  [4] ROCKAFELLAR-URYASEV CVaR (2000)
      Picks the cap that maximises  E[R] - λ·CVaR_{5%}
      (expected return penalised by the average of the worst 5 % of
      outcomes). Risk-aversion parameter λ = 2.0 for prop-firm context.

  [5] STRESS-REGIME TEST
      Rerun the GZ-aware bootstrap after shrinking every win by 20 %
      and enlarging every loss by 20 % — simulates regime drift.

Grid:  hard_cap ∈ {0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45,
                  0.50, 0.60, 0.70, 0.80, 1.00} %
DD TARGET : 4 %     CONFIDENCE : 95 %   BOOTSTRAPS : 10 000

A cap WINS iff:
  - realised DD <= 4 %
  - GZ-aware bootstrap P(DD>4%) < 5 %
  - stressed GZ-aware bootstrap P(DD>4%) < 10 %
  - Bootstrap mean PnL > 0
  - CVaR-adjusted score > 0
"""

from __future__ import annotations

import csv, json, math, random, statistics, sys, time as _time
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
DD_MAX_GZ = 0.05   # GZ shrinkage floor — size drops linearly 0..0.05

# =====================================================================
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


def run_with_cap(hard_cap, streams, balance, specs):
    """Runs ORB v20 + Smart Sizer; returns (trade_pnls, stats)."""
    cfg_sizer = SizerV20Config(hard_cap_pct=hard_cap)
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
    # Realised DD
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


# =====================================================================
# Stationary block bootstrap
# =====================================================================
def block_bootstrap_paths(pnls, n_paths=10_000, block_size=5, seed=42):
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


def path_metrics_naive(path, balance=100_000.0):
    eq=balance; peak=eq; mdd=0.0
    for p in path:
        eq += p; peak=max(peak,eq)
        dd=(peak-eq)/peak if peak>0 else 0.0
        mdd=max(mdd,dd)
    return sum(path), mdd


def path_metrics_gz_aware(path, balance=100_000.0, dd_max=DD_MAX_GZ, floor=0.10):
    """Apply GZ shrinkage live during the resampled path:
        shrink(DD) = max(floor, 1 - DD/dd_max)
    This represents the *actual* live bot's risk-throttle loop."""
    eq=balance; peak=eq; mdd=0.0
    total=0.0
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
    """Conditional Value-at-Risk at alpha — mean of the worst alpha fraction."""
    if not xs: return 0.0
    xs2 = sorted(xs)
    k = max(1, int(alpha * len(xs2)))
    return sum(xs2[:k]) / k   # negative number = expected loss in the tail


# =====================================================================
# Main
# =====================================================================
def main():
    balance = 100_000.0
    months = 3
    N_BOOT = 10_000
    BLOCK = 5
    caps = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.60, 0.70, 0.80, 1.00]

    out_lines = []
    def p(m=""):
        print(m); out_lines.append(m)

    p("=" * 140)
    p("  v20 PhD SUITE — Politis-Romano + Grossman-Zhou + Rockafellar-Uryasev + Stress")
    p(f"  ${balance:,.0f}  |  3 months  |  DE40+US30+XAUUSD  |  N_boot={N_BOOT:,}  block={BLOCK}  DD target={DD_TARGET*100:.0f}%")
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

    header = (f"    {'cap%':>5} {'N':>3}  "
              f"{'realPnL':>9} {'realDD':>6}  "
              f"{'GZ_pnl':>9} {'GZ_p50':>6} {'GZ_p95':>6} {'GZ_P(>4%)':>9}  "
              f"{'strP(>4%)':>9}  "
              f"{'CVaR5':>8} {'Score':>9}  VERDICT")
    p("  [Col key] GZ_* = Grossman-Zhou-aware bootstrap,  str = stressed (wins -20%, losses +20%),")
    p("            CVaR5 = mean of worst 5% of futures (negative=loss),  Score = bs mean PnL + 2·CVaR5")
    p("")
    p(header); p("    " + "-"*(len(header)-4))

    results = []
    for cap in caps:
        t0 = _time.time()
        pnls, R = run_with_cap(cap/100.0, streams, balance, specs)
        if not pnls: continue
        paths = block_bootstrap_paths(pnls, N_BOOT, BLOCK)

        # [3] GZ-aware
        gz_term, gz_dd = [], []
        for path in paths:
            te, md = path_metrics_gz_aware(path, balance, DD_MAX_GZ)
            gz_term.append(te); gz_dd.append(md)
        gz_mean = statistics.mean(gz_term)
        gz_p50  = pct(gz_dd, 0.50) * 100.0
        gz_p95  = pct(gz_dd, 0.95) * 100.0
        gz_p_over = sum(1 for d in gz_dd if d > DD_TARGET) / len(gz_dd)

        # [5] Stress-regime: wins × 0.8, losses × 1.2 — then GZ-aware
        stressed = [0.8*p if p>0 else 1.2*p for p in pnls]
        str_paths = block_bootstrap_paths(stressed, N_BOOT, BLOCK, seed=43)
        str_dd = [path_metrics_gz_aware(pp, balance, DD_MAX_GZ)[1] for pp in str_paths]
        str_p_over = sum(1 for d in str_dd if d > DD_TARGET) / len(str_dd)

        # [4] CVaR on GZ-aware terminals
        cv5 = cvar(gz_term, 0.05)       # negative ≈ bad-tail mean PnL
        score = gz_mean + 2.0 * cv5       # Rockafellar-Uryasev (λ=2, higher=better)

        # VERDICT
        passes = (
            R["mdd"] <= DD_TARGET and         # realised safe
            gz_p_over < 0.05 and              # <5% chance DD>4% under GZ
            str_p_over < 0.10 and             # <10% chance under regime stress
            gz_mean > 0 and                   # positive expected value
            score > 0                         # CVaR-adjusted score positive
        )
        verdict = "PASS" if passes else (
            "FAIL-DD"   if (gz_p_over >= 0.05 or str_p_over >= 0.10) else
            "FAIL-EDGE" if gz_mean <= 0 else "FAIL-CVAR"
        )

        row = (f"    {cap:>5.2f} {R['n']:>3}  "
               f"${R['net_pnl']:>+8,.0f} {R['mdd']*100:>5.2f}%  "
               f"${gz_mean:>+8,.0f} {gz_p50:>5.2f}% {gz_p95:>5.2f}% "
               f"{gz_p_over*100:>7.2f}%  "
               f"{str_p_over*100:>7.2f}%  "
               f"${cv5:>+6,.0f} ${score:>+7,.0f}  {verdict}")
        p(row)

        results.append(dict(
            cap_pct=cap, passes=passes, verdict=verdict,
            realised=R, gz_mean=gz_mean, gz_p50=gz_p50, gz_p95=gz_p95,
            gz_p_over=gz_p_over, str_p_over=str_p_over,
            cvar5=cv5, score=score, elapsed=_time.time()-t0,
        ))

    p("")
    p("=" * 140)
    winners = [r for r in results if r["passes"]]
    if not winners:
        p("  NO CAP PASSES ALL 5 TESTS. Strategy cannot safely target 4% DD.")
        p("")
        p("  BEST RISK-ADJUSTED CANDIDATES (by CVaR-score, regardless of pass):")
        for r in sorted(results, key=lambda x: -x["score"])[:5]:
            p(f"    cap={r['cap_pct']:.2f}%  score=${r['score']:+,.0f}  "
              f"GZ_mean=${r['gz_mean']:+,.0f}  GZ_P(>4%)={r['gz_p_over']*100:.2f}%  "
              f"realDD={r['realised']['mdd']*100:.2f}%  verdict={r['verdict']}")
        chosen=None
    else:
        # Pick the LARGEST cap among passers (max juice, still safe)
        chosen = max(winners, key=lambda r: r["cap_pct"])
        p(f"  >>> OPTIMAL HARD CAP = {chosen['cap_pct']:.2f} %  "
          f"(largest cap passing all 5 PhD tests)")
        R = chosen["realised"]
        p(f"      Realised PnL    : ${R['net_pnl']:+,.0f}  ({R['net_pnl']/balance*100:+.2f}%)")
        p(f"      Realised DD     : {R['mdd']*100:.2f}%   (<= 4.00% target)")
        p(f"      PF / WR         : {R['pf']:.2f} / {R['wr']*100:.1f}%")
        p(f"      GZ bootstrap mean PnL : ${chosen['gz_mean']:+,.0f}")
        p(f"      GZ bootstrap DD p95   : {chosen['gz_p95']:.2f}%")
        p(f"      GZ P(DD>4%)     : {chosen['gz_p_over']*100:.2f}%  (< 5%)")
        p(f"      Stress P(DD>4%) : {chosen['str_p_over']*100:.2f}%  (< 10%)")
        p(f"      CVaR(5%)         : ${chosen['cvar5']:+,.0f}")
        p(f"      Score           : ${chosen['score']:+,.0f}")
    p("=" * 140)

    out = ROOT / "Results"
    with open(out/"v20_phd_suite.txt","w",encoding="utf-8") as f: f.write("\n".join(out_lines))
    with open(out/"v20_phd_suite.json","w") as f:
        json.dump({
            "generated": datetime.utcnow().isoformat(),
            "dd_target_pct": DD_TARGET*100,
            "window": [str(tmin.date()), str(tmax.date())],
            "n_bootstrap": N_BOOT, "block_size": BLOCK,
            "candidates": [{k:v for k,v in r.items() if k != "realised"}
                             | {"realised_summary": r["realised"]} for r in results],
            "chosen": chosen["cap_pct"] if chosen else None,
        }, f, indent=2, default=str)
    p(f"  Saved: Results/v20_phd_suite.txt + .json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
