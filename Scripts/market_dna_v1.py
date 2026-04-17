#!/usr/bin/env python3
"""
MARKET DNA v1 — Evidence-First Characterization Study.

Instead of hypothesising (CUSUM/ORB both failed), we MEASURE structural
properties of the last 90 days of M1 data on US100, DE40, XAUUSD:

    Phase A — when does the market move?
      1. Realized-vol heatmap  (σ of log-return)  hour × weekday × symbol
      2. ATR per M1 bar       (absolute range)   same grid
      3. Spread-to-ATR ratio  (tradeability)     same grid

    Phase B — does momentum or reversal exist, at what horizon?
      4. Autocorrelation of returns at lags 1,3,5,10,20,60 — per hour
      5. Hurst exponent per session
      6. Follow-through after a 1σ 5-min move

    Phase C — session opens special?
      7. OR-5 range vs next-55-min range correlation
      8. OR breakout raw win-rate (hit +1R before -1R)
      9. Gap statistics

    Phase D — what kills us?
      10. Conditional mean return after a 1σ move, per hour (whipsaw map)
      11. Day-of-week directional bias

    Phase E — physical R:R ceiling
      12. MAE / MFE distribution at 30, 60, 180 min hold
      13. Oracle-trader P&L (perfect exit) — upper bound on any strategy
      14. Theoretical PF if we stop at the 75th-percentile MAE and take
          the 60th-percentile MFE — realistic "very good strategy" target.

Each statistically-significant finding is then RE-TESTED on the last
30 days (hold-out).  An edge only counts if:

    * train p < 0.05 (two-sided t / binomial)
    * holdout effect size >= 50 % of train effect size
    * minimum 30 observations on each side

The ranked, holdout-validated edges are written to:
    Results/market_dna_edges.json
    Results/market_dna_report.md

Inputs:
    data/historical/{US100,DE40,XAUUSD}_M1.csv   — same files v7 uses

Usage:
    python Scripts/market_dna_v1.py
    python Scripts/market_dna_v1.py --months 3 --train-days 60
    python Scripts/market_dna_v1.py --min-samples 30 --alpha 0.05
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ======================================================================
#  Broker / cost profile  (matches v7_5ers backtest)
# ======================================================================

SPREAD_PTS = {"US100": 1.5, "DE40": 1.5, "XAUUSD": 0.30}
PIP_VALUE  = {"US100": 1.0, "DE40": 1.0, "XAUUSD": 100.0}

# Our 3-month backtest covered Nov-Feb (winter UTC)
ORB_WINDOW_UTC = {
    "US100":  (14 * 60 + 30, 14 * 60 + 35),   # NY cash open 14:30 UTC, 5-min
    "DE40":   (8 * 60,  8 * 60 + 15),          # Xetra 08:00 UTC, 15-min
    "XAUUSD": (14 * 60 + 30, 14 * 60 + 45),   # NY open, 15-min
}


# ======================================================================
#  Tiny stats (no scipy dependency needed, we roll our own)
# ======================================================================

def welch_t_test(a: list[float], b: list[float]) -> tuple[float, float]:
    """Return (t_stat, two-sided p-value) via Welch's t.  Normal-approx p."""
    if len(a) < 2 or len(b) < 2:
        return 0.0, 1.0
    ma = statistics.fmean(a)
    mb = statistics.fmean(b)
    va = statistics.variance(a)
    vb = statistics.variance(b)
    na, nb = len(a), len(b)
    se = math.sqrt(va / na + vb / nb)
    if se == 0:
        return 0.0, 1.0
    t = (ma - mb) / se
    # Welch-Satterthwaite df + approximation via std normal for large-n
    # Our samples are large (thousands), normal is fine
    # p = 2 * (1 - Phi(|t|))
    p = 2.0 * (1.0 - _phi(abs(t)))
    return t, p


def one_sample_t_test(x: list[float], mu0: float = 0.0) -> tuple[float, float]:
    if len(x) < 2:
        return 0.0, 1.0
    m = statistics.fmean(x)
    s = statistics.stdev(x)
    n = len(x)
    se = s / math.sqrt(n)
    if se == 0:
        return 0.0, 1.0
    t = (m - mu0) / se
    p = 2.0 * (1.0 - _phi(abs(t)))
    return t, p


def binomial_p_two_sided(k: int, n: int, p0: float = 0.5) -> float:
    """Two-sided binomial p-value using normal approximation (large n)."""
    if n == 0:
        return 1.0
    mu = n * p0
    sd = math.sqrt(n * p0 * (1.0 - p0))
    if sd == 0:
        return 1.0
    z = (k - mu) / sd
    return 2.0 * (1.0 - _phi(abs(z)))


def _phi(x: float) -> float:
    """Std normal CDF via erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    ys = sorted(xs)
    idx = q * (len(ys) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return ys[lo]
    frac = idx - lo
    return ys[lo] * (1 - frac) + ys[hi] * frac


def autocorr(x: list[float], lag: int) -> float:
    if len(x) <= lag + 1:
        return 0.0
    m = statistics.fmean(x)
    num = sum((x[i] - m) * (x[i + lag] - m) for i in range(len(x) - lag))
    den = sum((xi - m) ** 2 for xi in x)
    return num / den if den > 0 else 0.0


def hurst_rs(x: list[float]) -> float:
    """R/S Hurst estimator.  Returns 0.5-ish for random, >0.5 trending, <0.5 MR."""
    n = len(x)
    if n < 100:
        return 0.5
    # Use powers-of-2 windows
    sizes = []
    k = 16
    while k <= n // 2:
        sizes.append(k)
        k *= 2
    if not sizes:
        return 0.5
    log_rs = []
    log_n = []
    for s in sizes:
        rs_vals = []
        for start in range(0, n - s + 1, s):
            seg = x[start:start + s]
            m = statistics.fmean(seg)
            dev = [seg[i] - m for i in range(s)]
            cum = [sum(dev[:i + 1]) for i in range(s)]
            R = max(cum) - min(cum)
            S = statistics.stdev(seg) if s > 1 else 0.0
            if S > 0:
                rs_vals.append(R / S)
        if rs_vals:
            log_rs.append(math.log(statistics.fmean(rs_vals)))
            log_n.append(math.log(s))
    if len(log_n) < 2:
        return 0.5
    # linear regression slope = Hurst exponent
    mx = statistics.fmean(log_n)
    my = statistics.fmean(log_rs)
    num = sum((log_n[i] - mx) * (log_rs[i] - my) for i in range(len(log_n)))
    den = sum((log_n[i] - mx) ** 2 for i in range(len(log_n)))
    return num / den if den > 0 else 0.5


# ======================================================================
#  Data loader
# ======================================================================

@dataclass
class Bar:
    t: datetime
    o: float
    h: float
    l: float
    c: float

    @property
    def minute_of_day(self) -> int:
        return self.t.hour * 60 + self.t.minute


def load_bars(path: Path,
              start: Optional[datetime],
              end: Optional[datetime]) -> list[Bar]:
    out: list[Bar] = []
    with open(path, "r", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try:
                ts = datetime.fromisoformat(row["time"])
            except Exception:
                ts = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            out.append(Bar(ts, float(row["open"]), float(row["high"]),
                           float(row["low"]), float(row["close"])))
    return out


def common_window(files: dict[str, Path], months: int) -> tuple[datetime, datetime]:
    first_per = {}
    last_per = {}
    for sym, p in files.items():
        with open(p, "r", newline="") as f:
            rdr = csv.reader(f)
            next(rdr)
            rows = list(rdr)
        first_per[sym] = _parse(rows[0][0])
        last_per[sym] = _parse(rows[-1][0])
    end = min(last_per.values())
    candidate_start = end - timedelta(days=months * 30)
    start = max(candidate_start, max(first_per.values()))
    return start, end


def _parse(s: str) -> datetime:
    try:
        return datetime.fromisoformat(s)
    except Exception:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


# ======================================================================
#  Edge record
# ======================================================================

@dataclass
class Edge:
    name: str
    symbol: str
    description: str
    train_effect: float           # the key statistic (e.g. mean return in R, WR, ACF)
    train_p: float
    train_n: int
    holdout_effect: Optional[float] = None
    holdout_p: Optional[float] = None
    holdout_n: Optional[int] = None
    survives: bool = False
    notes: str = ""

    @property
    def survival_score(self) -> float:
        """Rank score: combine train effect, holdout consistency, sample size."""
        if not self.survives or self.holdout_effect is None:
            return 0.0
        if abs(self.train_effect) < 1e-9:
            return 0.0
        ratio = self.holdout_effect / self.train_effect
        if ratio < 0:     # sign flipped, worthless
            return 0.0
        # geometric mean of effect sizes weighted by -log(p)
        avg_effect = 0.5 * (abs(self.train_effect) + abs(self.holdout_effect or 0))
        pcombo = max(1e-12, (self.train_p * (self.holdout_p or 1.0)) ** 0.5)
        return avg_effect * (-math.log(pcombo))


# ======================================================================
#  Measurements
# ======================================================================

def phase_a_vol_heatmap(bars: list[Bar]) -> dict:
    """Phase A.1-A.3: vol + ATR + spread/ATR by (hour, weekday)."""
    by_hour_wd: dict[tuple[int, int], list[float]] = {}
    atr_by_hw: dict[tuple[int, int], list[float]] = {}
    prev_c: Optional[float] = None
    for b in bars:
        if prev_c is not None and prev_c > 0:
            r = math.log(b.c / prev_c)
            key = (b.t.hour, b.t.weekday())
            by_hour_wd.setdefault(key, []).append(r)
            atr_by_hw.setdefault(key, []).append(b.h - b.l)
        prev_c = b.c

    vol_grid: dict[str, float] = {}
    atr_grid: dict[str, float] = {}
    for key, rs in by_hour_wd.items():
        hr, wd = key
        label = f"wd{wd}_h{hr:02d}"
        if len(rs) >= 20:
            vol_grid[label] = statistics.stdev(rs) * 1e4  # in bps per M1
            atr_grid[label] = statistics.fmean(atr_by_hw[key])
    return {"vol_bps": vol_grid, "atr_pts": atr_grid}


def phase_b_autocorr(bars: list[Bar], lags: list[int]) -> dict:
    """Phase B.4: autocorr overall + per hour."""
    prev_c: Optional[float] = None
    rets: list[tuple[int, float]] = []   # (hour, r)
    for b in bars:
        if prev_c is not None and prev_c > 0:
            rets.append((b.t.hour, math.log(b.c / prev_c)))
        prev_c = b.c

    all_r = [r for (_, r) in rets]
    overall = {lag: autocorr(all_r, lag) for lag in lags}

    by_hour: dict[int, dict[int, float]] = {}
    for hr in range(24):
        seg = [r for (h, r) in rets if h == hr]
        if len(seg) < 200:
            continue
        by_hour[hr] = {lag: autocorr(seg, lag) for lag in lags}
    return {"overall": overall, "by_hour": by_hour}


def phase_b_hurst(bars: list[Bar]) -> float:
    prev_c: Optional[float] = None
    rs: list[float] = []
    for b in bars:
        if prev_c is not None and prev_c > 0:
            rs.append(math.log(b.c / prev_c))
        prev_c = b.c
    return hurst_rs(rs)


def phase_b_follow_through(bars: list[Bar],
                            sigma_mult: float = 1.0,
                            horizon_min: int = 15) -> dict:
    """
    After a 5-min window with |move| > sigma_mult * σ, measure the
    average same-direction move over the next `horizon_min` minutes.
    Positive = momentum; negative = reversal.
    """
    # Build 5-min aggregate
    agg5: list[tuple[datetime, float, float]] = []   # (bar_start, ret, hour)
    i = 0
    while i + 5 <= len(bars):
        start = bars[i].t
        end_c = bars[i + 4].c
        st_o = bars[i].o
        if st_o > 0:
            agg5.append((start, math.log(end_c / st_o), start.hour))
        i += 5
    if len(agg5) < 200:
        return {"by_hour": {}, "overall_momentum_R": 0.0, "n": 0}

    rets = [r for (_, r, _) in agg5]
    sigma = statistics.stdev(rets)
    by_hour: dict[int, list[float]] = {}
    all_follow: list[float] = []

    # Map bar index -> (datetime)
    bar_t_index: dict[datetime, int] = {b.t: k for k, b in enumerate(bars)}

    for (bt, r, hr) in agg5:
        if abs(r) < sigma_mult * sigma:
            continue
        direction = 1 if r > 0 else -1
        trig_end_idx = bar_t_index.get(bt)
        if trig_end_idx is None or trig_end_idx + 5 + horizon_min >= len(bars):
            continue
        # next-horizon return in same direction
        start_px = bars[trig_end_idx + 5].o
        end_px = bars[trig_end_idx + 5 + horizon_min - 1].c
        if start_px <= 0:
            continue
        follow_r = math.log(end_px / start_px) * direction
        by_hour.setdefault(hr, []).append(follow_r)
        all_follow.append(follow_r)

    by_hour_summary = {
        hr: {
            "mean": statistics.fmean(xs),
            "n": len(xs),
            "t": one_sample_t_test(xs)[0],
            "p": one_sample_t_test(xs)[1],
        }
        for hr, xs in by_hour.items() if len(xs) >= 15
    }
    overall_mean = statistics.fmean(all_follow) if all_follow else 0.0
    return {
        "by_hour": by_hour_summary,
        "overall_mean_r": overall_mean,
        "overall_n": len(all_follow),
        "trigger_sigma_5min": sigma * sigma_mult,
    }


def phase_c_orb(bars: list[Bar], sym: str) -> dict:
    """Phase C.7-8: OR range vs rest-of-session range; OR breakout raw WR."""
    or_start, or_end = ORB_WINDOW_UTC[sym]
    trade_end = or_end + 60

    per_day: dict[str, dict] = {}
    for b in bars:
        day = b.t.strftime("%Y-%m-%d")
        mod = b.minute_of_day
        d = per_day.setdefault(day, {"or_high": None, "or_low": None,
                                      "post_high": None, "post_low": None,
                                      "or_close": None})
        if or_start <= mod < or_end:
            if d["or_high"] is None:
                d["or_high"] = b.h; d["or_low"] = b.l
            else:
                d["or_high"] = max(d["or_high"], b.h)
                d["or_low"] = min(d["or_low"], b.l)
            d["or_close"] = b.c
        elif or_end <= mod < trade_end:
            if d["post_high"] is None:
                d["post_high"] = b.h; d["post_low"] = b.l
            else:
                d["post_high"] = max(d["post_high"], b.h)
                d["post_low"] = min(d["post_low"], b.l)

    # 1) OR range vs post-OR range correlation
    ors = []; posts = []
    for day, d in per_day.items():
        if all(d[k] is not None for k in ("or_high", "or_low", "post_high", "post_low")):
            ors.append(d["or_high"] - d["or_low"])
            posts.append(d["post_high"] - d["post_low"])
    # Pearson r
    r_corr = 0.0
    if len(ors) >= 10:
        mx = statistics.fmean(ors); my = statistics.fmean(posts)
        num = sum((ors[i] - mx) * (posts[i] - my) for i in range(len(ors)))
        denx = math.sqrt(sum((o - mx) ** 2 for o in ors))
        deny = math.sqrt(sum((p - my) ** 2 for p in posts))
        r_corr = num / (denx * deny) if denx * deny > 0 else 0.0

    # 2) Raw OR breakout outcome
    # For each day: did price break OR-high first or OR-low first, and did
    # it then reach +1R (1×OR range) in the break direction before -1R back?
    wins = 0; losses = 0; neither = 0
    for day, d in per_day.items():
        if any(d[k] is None for k in ("or_high", "or_low")):
            continue
        or_range = d["or_high"] - d["or_low"]
        if or_range <= 0:
            continue
        # simulate: walk post-OR bars
        hit_long = False; hit_short = False
        tp_long = d["or_high"] + or_range    # +1R from break
        sl_long = d["or_low"]
        tp_short = d["or_low"] - or_range
        sl_short = d["or_high"]
        for b in bars:
            if b.t.strftime("%Y-%m-%d") != day:
                continue
            mod = b.minute_of_day
            if mod < or_end or mod >= trade_end:
                continue
            # long setup
            if not hit_long and not hit_short:
                if b.h > d["or_high"]:
                    hit_long = True
                    break_px = d["or_high"]
                    # from here, did we hit tp_long or sl_long first (intrabar order unknown; assume worst-case SL first)
                    # walk remaining bars
                    for bb in bars:
                        if bb.t <= b.t: continue
                        if bb.t.strftime("%Y-%m-%d") != day: break
                        mm = bb.minute_of_day
                        if mm >= trade_end + 60:   # extended exit window to session end
                            break
                        if bb.l <= sl_long:
                            losses += 1; break
                        if bb.h >= tp_long:
                            wins += 1; break
                    else:
                        neither += 1
                    break
                if b.l < d["or_low"]:
                    hit_short = True
                    break_px = d["or_low"]
                    for bb in bars:
                        if bb.t <= b.t: continue
                        if bb.t.strftime("%Y-%m-%d") != day: break
                        mm = bb.minute_of_day
                        if mm >= trade_end + 60:
                            break
                        if bb.h >= sl_short:
                            losses += 1; break
                        if bb.l <= tp_short:
                            wins += 1; break
                    else:
                        neither += 1
                    break
        else:
            if not hit_long and not hit_short:
                neither += 1

    total_resolved = wins + losses
    wr = wins / total_resolved if total_resolved > 0 else 0.0
    p_wr = binomial_p_two_sided(wins, total_resolved, 0.5) if total_resolved > 0 else 1.0

    return {
        "or_post_range_corr": r_corr,
        "n_days_correlated": len(ors),
        "orb_wins": wins,
        "orb_losses": losses,
        "orb_neither": neither,
        "orb_wr_at_1R": wr,
        "orb_wr_p_value": p_wr,
        "orb_n_resolved": total_resolved,
    }


def phase_c_gaps(bars: list[Bar]) -> dict:
    """Phase C.9: overnight gap statistics + fill rates."""
    by_day: dict[str, list[Bar]] = {}
    for b in bars:
        by_day.setdefault(b.t.strftime("%Y-%m-%d"), []).append(b)
    days = sorted(by_day.keys())
    gaps = []
    fills = 0; no_fills = 0
    for i in range(1, len(days)):
        prev = by_day[days[i - 1]]
        cur = by_day[days[i]]
        if not prev or not cur:
            continue
        prev_c = prev[-1].c
        cur_o = cur[0].o
        if prev_c <= 0:
            continue
        gap_r = math.log(cur_o / prev_c)
        gaps.append(gap_r)
        # fill = did today touch prev_c?
        lo = min(b.l for b in cur)
        hi = max(b.h for b in cur)
        if lo <= prev_c <= hi:
            fills += 1
        else:
            no_fills += 1
    fill_rate = fills / (fills + no_fills) if (fills + no_fills) > 0 else 0.0
    return {
        "n_days": len(gaps),
        "mean_gap_bps": statistics.fmean(gaps) * 1e4 if gaps else 0.0,
        "stdev_gap_bps": statistics.stdev(gaps) * 1e4 if len(gaps) > 1 else 0.0,
        "gap_fill_rate": fill_rate,
        "pct_upgaps": sum(1 for g in gaps if g > 0) / len(gaps) if gaps else 0.0,
    }


def phase_d_dow_bias(bars: list[Bar]) -> dict:
    """Phase D.11: day-of-week directional bias."""
    per_wd: dict[int, list[float]] = {i: [] for i in range(7)}
    by_day: dict[str, list[Bar]] = {}
    for b in bars:
        by_day.setdefault(b.t.strftime("%Y-%m-%d"), []).append(b)
    for day, bs in by_day.items():
        if not bs:
            continue
        o = bs[0].o; c = bs[-1].c
        if o <= 0:
            continue
        r = math.log(c / o)
        per_wd[bs[0].t.weekday()].append(r)
    out = {}
    for wd, xs in per_wd.items():
        if len(xs) < 5:
            continue
        t, p = one_sample_t_test(xs)
        out[wd] = {
            "n": len(xs),
            "mean_bps": statistics.fmean(xs) * 1e4,
            "t": t, "p": p,
            "wins": sum(1 for x in xs if x > 0),
        }
    return out


def phase_e_mae_mfe(bars: list[Bar], horizons: list[int]) -> dict:
    """
    Phase E.12-14: MAE / MFE distribution walking every 5-min bar's close
    forward by each horizon.  Computes the physical ceiling of ANY strategy
    on this data.
    """
    results = {}
    for horizon in horizons:
        maes = []; mfes = []
        for i in range(0, len(bars) - horizon, 5):   # step 5 min to de-correlate
            entry = bars[i].c
            window = bars[i + 1:i + 1 + horizon]
            if not window or entry <= 0:
                continue
            hi = max(b.h for b in window)
            lo = min(b.l for b in window)
            # "Long-side" MAE = worst drawdown below entry; MFE = best up-excursion
            mae = (lo - entry) / entry * 1e4     # bps, negative
            mfe = (hi - entry) / entry * 1e4     # bps, positive
            maes.append(mae); mfes.append(mfe)
        if not maes:
            continue
        results[horizon] = {
            "n": len(maes),
            "mae_median": percentile(maes, 0.5),
            "mae_q25": percentile(maes, 0.25),
            "mae_q10": percentile(maes, 0.10),
            "mfe_median": percentile(mfes, 0.5),
            "mfe_q75": percentile(mfes, 0.75),
            "mfe_q90": percentile(mfes, 0.90),
            # Oracle trader (enter long at every bar, exit at bar-window high):
            "oracle_mfe_bps_per_entry": statistics.fmean(mfes),
            # Realistic target: 75th pct MAE as SL, 60th pct MFE as TP
            "target_rr_60_75": (
                percentile(mfes, 0.60) / abs(percentile(maes, 0.25))
                if percentile(maes, 0.25) < 0 else 0.0
            ),
        }
    return results


# ======================================================================
#  Edge extraction from raw measurements
# ======================================================================

def extract_edges_from_measurements(sym: str, m: dict) -> list[Edge]:
    """Walk the raw measurements and emit every candidate edge with p-value."""
    edges: list[Edge] = []

    # -- B.4 per-hour momentum/reversal at lag-1, -3, -5 --------------
    for hr, lagmap in m["phase_b"]["autocorr"]["by_hour"].items():
        for lag, ac in lagmap.items():
            if abs(ac) < 0.02:
                continue
            # approx 2-sided z-test on autocorr: se = 1/sqrt(N)
            n = 60 * 30   # rough: 30 days * 60 bars/hour
            z = ac * math.sqrt(n)
            p = 2.0 * (1.0 - _phi(abs(z)))
            if p < 0.10:
                edges.append(Edge(
                    name=f"autocorr_h{hr:02d}_lag{lag}",
                    symbol=sym,
                    description=(f"Lag-{lag} M1 autocorrelation at hour {hr:02d}:00 UTC "
                                 f"= {ac:+.4f} "
                                 f"({'momentum' if ac > 0 else 'reversal'})"),
                    train_effect=ac,
                    train_p=p,
                    train_n=n,
                ))

    # -- B.6 follow-through by hour ------------------------------------
    for hr, stats in m["phase_b"]["follow_through"]["by_hour"].items():
        if stats["n"] >= 15 and stats["p"] < 0.20:
            edges.append(Edge(
                name=f"followthrough_h{hr:02d}",
                symbol=sym,
                description=(f"After a 1σ 5-min move at hour {hr:02d}:00 UTC, "
                             f"next-15-min same-direction return "
                             f"= {stats['mean']*1e4:+.1f} bps over {stats['n']} samples"),
                train_effect=stats["mean"],
                train_p=stats["p"],
                train_n=stats["n"],
            ))

    # -- C.8 ORB raw WR -----------------------------------------------
    orb = m["phase_c"]["orb"]
    if orb["orb_n_resolved"] >= 20:
        edges.append(Edge(
            name="orb_raw_wr",
            symbol=sym,
            description=(f"ORB raw win-rate at +1R (session open): "
                         f"{orb['orb_wr_at_1R']*100:.1f}% on {orb['orb_n_resolved']} resolved setups "
                         f"(p={orb['orb_wr_p_value']:.3f})"),
            train_effect=orb["orb_wr_at_1R"] - 0.5,
            train_p=orb["orb_wr_p_value"],
            train_n=orb["orb_n_resolved"],
        ))

    # -- C.7 OR-post range correlation --------------------------------
    corr = m["phase_c"]["orb"]["or_post_range_corr"]
    n_days = m["phase_c"]["orb"]["n_days_correlated"]
    if n_days >= 20:
        # approx p via Fisher z
        if abs(corr) < 0.9999:
            z = 0.5 * math.log((1 + corr) / (1 - corr))
            se = 1.0 / math.sqrt(n_days - 3)
            p = 2.0 * (1.0 - _phi(abs(z) / se))
            edges.append(Edge(
                name="or_predicts_post_range",
                symbol=sym,
                description=(f"OR range vs next-55-min range correlation: "
                             f"r={corr:+.3f} on {n_days} days "
                             f"— {'wide OR = wide day' if corr > 0 else 'wide OR = fade'}"),
                train_effect=corr,
                train_p=p,
                train_n=n_days,
            ))

    # -- D.11 day-of-week bias ----------------------------------------
    for wd, stats in m["phase_d"]["dow_bias"].items():
        if stats["p"] < 0.15 and stats["n"] >= 6:
            days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            edges.append(Edge(
                name=f"dow_{days[wd]}",
                symbol=sym,
                description=(f"{days[wd]} close-vs-open bias: {stats['mean_bps']:+.1f} bps "
                             f"({stats['wins']}/{stats['n']} up days, p={stats['p']:.3f})"),
                train_effect=stats["mean_bps"] / 1e4,
                train_p=stats["p"],
                train_n=stats["n"],
            ))

    return edges


# ======================================================================
#  Holdout re-test
# ======================================================================

def retest_edge_on_holdout(edge: Edge, holdout_bars: list[Bar], sym: str,
                            measurements_holdout: dict,
                            alpha: float) -> Edge:
    """For each edge, re-compute the same measurement on the holdout slice."""
    out = edge
    if edge.name.startswith("autocorr_"):
        parts = edge.name.split("_")
        hr = int(parts[1][1:])
        lag = int(parts[2][3:])
        hh = measurements_holdout["phase_b"]["autocorr"]["by_hour"].get(hr, {})
        if lag in hh:
            ac = hh[lag]
            n = 60 * 15  # holdout ~15 days
            z = ac * math.sqrt(n)
            p = 2.0 * (1.0 - _phi(abs(z)))
            out.holdout_effect = ac
            out.holdout_p = p
            out.holdout_n = n

    elif edge.name.startswith("followthrough_"):
        hr = int(edge.name.split("_h")[1])
        stats = measurements_holdout["phase_b"]["follow_through"]["by_hour"].get(hr)
        if stats:
            out.holdout_effect = stats["mean"]
            out.holdout_p = stats["p"]
            out.holdout_n = stats["n"]

    elif edge.name == "orb_raw_wr":
        orb = measurements_holdout["phase_c"]["orb"]
        if orb["orb_n_resolved"] >= 5:
            out.holdout_effect = orb["orb_wr_at_1R"] - 0.5
            out.holdout_p = orb["orb_wr_p_value"]
            out.holdout_n = orb["orb_n_resolved"]

    elif edge.name == "or_predicts_post_range":
        corr = measurements_holdout["phase_c"]["orb"]["or_post_range_corr"]
        n = measurements_holdout["phase_c"]["orb"]["n_days_correlated"]
        if n >= 5 and abs(corr) < 0.9999:
            z = 0.5 * math.log((1 + corr) / (1 - corr))
            se = 1.0 / math.sqrt(max(n - 3, 1))
            p = 2.0 * (1.0 - _phi(abs(z) / se))
            out.holdout_effect = corr
            out.holdout_p = p
            out.holdout_n = n

    elif edge.name.startswith("dow_"):
        days = {"Mon": 0, "Tue": 1, "Wed": 2, "Thu": 3, "Fri": 4}
        wd = days.get(edge.name.split("_")[1])
        if wd is not None:
            stats = measurements_holdout["phase_d"]["dow_bias"].get(wd)
            if stats:
                out.holdout_effect = stats["mean_bps"] / 1e4
                out.holdout_p = stats["p"]
                out.holdout_n = stats["n"]

    # Survival rule: train p < alpha AND holdout effect in same direction AND
    # holdout effect magnitude >= 50% of train effect
    if (out.train_p < alpha and out.holdout_effect is not None
            and out.holdout_n is not None and out.holdout_n >= 10):
        same_sign = (out.train_effect * out.holdout_effect) > 0
        magnitude_ok = abs(out.holdout_effect) >= 0.5 * abs(out.train_effect)
        out.survives = same_sign and magnitude_ok
        if out.survives:
            out.notes = "SURVIVED HOLDOUT"
        elif not same_sign:
            out.notes = "sign flipped on holdout — fake edge"
        else:
            out.notes = "magnitude collapsed on holdout — weak edge"
    else:
        out.notes = ("failed train alpha" if out.train_p >= alpha
                     else "insufficient holdout sample")
    return out


# ======================================================================
#  Report generator
# ======================================================================

def build_report(measurements: dict, edges: list[Edge],
                  bt_start, bt_end, train_end,
                  out_md: Path) -> None:
    lines: list[str] = []
    lines.append("# MARKET DNA v1 — Evidence-First Characterization Report")
    lines.append("")
    lines.append(f"**Window:** {bt_start} → {bt_end}")
    lines.append(f"**Train slice:** {bt_start} → {train_end}")
    lines.append(f"**Holdout slice:** {train_end} → {bt_end}")
    lines.append("")
    lines.append("## Executive summary")
    lines.append("")
    n_surv = sum(1 for e in edges if e.survives)
    lines.append(f"- Total candidate edges tested: **{len(edges)}**")
    lines.append(f"- Edges surviving holdout validation: **{n_surv}**")
    lines.append(f"- Survival rule: train p<0.05, holdout same-sign, "
                 "holdout |effect| ≥ 50% of train |effect|, holdout n ≥ 10.")
    lines.append("")

    # Ranked surviving edges
    surv = sorted([e for e in edges if e.survives],
                  key=lambda e: -e.survival_score)
    lines.append("## Surviving edges (ranked by survival score)")
    lines.append("")
    if not surv:
        lines.append("**NONE.**  No measurement on this 90-day window produced a "
                     "statistically-significant edge that also held up on the "
                     "30-day holdout.  This is itself a finding: the 2025-11 → "
                     "2026-02 window is structurally hard to trade on these "
                     "three instruments at the current cost structure.  "
                     "Recommendation: extend data to ≥ 6 months, widen "
                     "instrument universe (FX majors), or change cost tier.")
    for i, e in enumerate(surv, 1):
        lines.append(f"### {i}. {e.symbol}  —  {e.name}")
        lines.append(f"- **Description**: {e.description}")
        lines.append(f"- Train effect: **{e.train_effect:+.4f}** "
                     f"(p={e.train_p:.4f}, n={e.train_n})")
        lines.append(f"- Holdout effect: **{e.holdout_effect:+.4f}** "
                     f"(p={e.holdout_p:.4f}, n={e.holdout_n})")
        ratio = e.holdout_effect / e.train_effect if abs(e.train_effect) > 1e-9 else 0
        lines.append(f"- Holdout / train magnitude ratio: {ratio:.2f}")
        lines.append(f"- Survival score: {e.survival_score:.2f}")
        lines.append(f"- **{e.notes}**")
        lines.append("")

    # Failed edges (audit trail)
    failed = [e for e in edges if not e.survives]
    if failed:
        lines.append("## Failed candidates (rejected — audit trail)")
        lines.append("")
        lines.append("| Symbol | Edge | Train effect | Train p | Holdout effect | Verdict |")
        lines.append("|---|---|---:|---:|---:|---|")
        for e in sorted(failed, key=lambda e: e.train_p):
            he = f"{e.holdout_effect:+.4f}" if e.holdout_effect is not None else "—"
            lines.append(f"| {e.symbol} | {e.name} | {e.train_effect:+.4f} "
                         f"| {e.train_p:.4f} | {he} | {e.notes} |")
        lines.append("")

    # Per-symbol phase summaries
    lines.append("## Raw phase measurements")
    lines.append("")
    for sym, m in measurements.items():
        lines.append(f"### {sym}")
        lines.append("")
        lines.append(f"- Hurst exponent (train): **{m['phase_b']['hurst']:.3f}** "
                     f"({'trending' if m['phase_b']['hurst'] > 0.52 else 'mean-reverting' if m['phase_b']['hurst'] < 0.48 else 'random'})")
        lines.append(f"- Overall autocorr lag-1: {m['phase_b']['autocorr']['overall'].get(1, 0):+.4f}")
        lines.append(f"- Overall autocorr lag-5: {m['phase_b']['autocorr']['overall'].get(5, 0):+.4f}")
        lines.append(f"- Overall 1σ follow-through: {m['phase_b']['follow_through']['overall_mean_r']*1e4:+.2f} bps")
        lines.append(f"- ORB raw WR@1R: {m['phase_c']['orb']['orb_wr_at_1R']*100:.1f}% "
                     f"(n={m['phase_c']['orb']['orb_n_resolved']})")
        lines.append(f"- OR→post-range correlation: {m['phase_c']['orb']['or_post_range_corr']:+.3f}")
        lines.append(f"- Mean overnight gap: {m['phase_c']['gaps']['mean_gap_bps']:+.2f} bps "
                     f"(fill rate {m['phase_c']['gaps']['gap_fill_rate']*100:.1f}%)")
        # R:R ceiling
        mae_mfe = m["phase_e"]["mae_mfe"]
        for h, r in mae_mfe.items():
            lines.append(f"- {h}-min horizon: MAE q25 {r['mae_q25']:+.1f} bps, "
                         f"MFE q60/q75 {percentile_approx(r)} — target R:R≈{r['target_rr_60_75']:.2f}")
        lines.append("")

    out_md.write_text("\n".join(lines), encoding="utf-8")


def percentile_approx(r: dict) -> str:
    return f"{r['mfe_median']:+.1f}/{r['mfe_q75']:+.1f}"


# ======================================================================
#  Main
# ======================================================================

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--train-days", type=int, default=60,
                    help="Train/holdout split in days (default 60/30).")
    ap.add_argument("--alpha", type=float, default=0.05)
    ap.add_argument("--out", type=Path, default=ROOT / "Results")
    args = ap.parse_args()

    data_dir = ROOT / "data" / "historical"
    files = {
        "US100":  data_dir / "US100_M1.csv",
        "DE40":   data_dir / "DE40_M1.csv",
        "XAUUSD": data_dir / "XAUUSD_M1.csv",
    }
    for sym, p in files.items():
        if not p.exists():
            raise SystemExit(f"missing {p}")

    bt_start, bt_end = common_window(files, args.months)
    train_end = bt_start + timedelta(days=args.train_days)
    print(f"\nWindow: {bt_start} -> {bt_end}")
    print(f"Train:  {bt_start} -> {train_end}")
    print(f"Hold:   {train_end} -> {bt_end}\n")

    all_measurements: dict[str, dict] = {}
    all_holdout: dict[str, dict] = {}
    all_edges: list[Edge] = []

    for sym, p in files.items():
        print(f"  [{sym}] loading…")
        train_bars = load_bars(p, bt_start, train_end)
        hold_bars = load_bars(p, train_end, bt_end)
        print(f"    train {len(train_bars):,} bars   hold {len(hold_bars):,} bars")

        print(f"  [{sym}] measuring train…")
        m_train = {
            "phase_a": phase_a_vol_heatmap(train_bars),
            "phase_b": {
                "autocorr": phase_b_autocorr(train_bars, [1, 3, 5, 10, 20, 60]),
                "hurst": phase_b_hurst(train_bars),
                "follow_through": phase_b_follow_through(train_bars),
            },
            "phase_c": {
                "orb": phase_c_orb(train_bars, sym),
                "gaps": phase_c_gaps(train_bars),
            },
            "phase_d": {
                "dow_bias": phase_d_dow_bias(train_bars),
            },
            "phase_e": {
                "mae_mfe": phase_e_mae_mfe(train_bars, [30, 60, 180]),
            },
        }
        print(f"  [{sym}] measuring holdout…")
        m_hold = {
            "phase_a": phase_a_vol_heatmap(hold_bars),
            "phase_b": {
                "autocorr": phase_b_autocorr(hold_bars, [1, 3, 5, 10, 20, 60]),
                "hurst": phase_b_hurst(hold_bars),
                "follow_through": phase_b_follow_through(hold_bars),
            },
            "phase_c": {
                "orb": phase_c_orb(hold_bars, sym),
                "gaps": phase_c_gaps(hold_bars),
            },
            "phase_d": {
                "dow_bias": phase_d_dow_bias(hold_bars),
            },
            "phase_e": {
                "mae_mfe": phase_e_mae_mfe(hold_bars, [30, 60, 180]),
            },
        }

        all_measurements[sym] = m_train
        all_holdout[sym] = m_hold

        edges = extract_edges_from_measurements(sym, m_train)
        print(f"  [{sym}] {len(edges)} candidate edges found on train")

        # Retest each on holdout
        edges = [retest_edge_on_holdout(e, hold_bars, sym, m_hold, args.alpha)
                 for e in edges]
        all_edges.extend(edges)

    # Write outputs
    out_dir = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    json_path = out_dir / "market_dna_edges.json"
    with open(json_path, "w") as f:
        json.dump({
            "bt_start": str(bt_start),
            "bt_end": str(bt_end),
            "train_end": str(train_end),
            "edges": [asdict(e) for e in all_edges],
            "measurements_train": all_measurements,
            "measurements_holdout": all_holdout,
        }, f, indent=2, default=str)
    print(f"\n  JSON:   {json_path}")

    md_path = out_dir / "market_dna_report.md"
    build_report(all_measurements, all_edges, bt_start, bt_end, train_end, md_path)
    print(f"  Report: {md_path}")

    # Terminal summary
    surv = [e for e in all_edges if e.survives]
    print("\n" + "=" * 72)
    print(f"  MARKET DNA SUMMARY")
    print("=" * 72)
    print(f"  Candidate edges:   {len(all_edges)}")
    print(f"  Survived holdout:  {len(surv)}")
    if surv:
        print("\n  TOP surviving edges:")
        for e in sorted(surv, key=lambda e: -e.survival_score)[:10]:
            print(f"    [{e.symbol}] {e.name:<30} "
                  f"train={e.train_effect:+.4f}(p={e.train_p:.3f}) "
                  f"hold={e.holdout_effect:+.4f}(p={e.holdout_p:.3f})")
    else:
        print("\n  *** NO EDGE SURVIVED HOLDOUT — see report for why ***")
    print("=" * 72)


if __name__ == "__main__":
    main()
