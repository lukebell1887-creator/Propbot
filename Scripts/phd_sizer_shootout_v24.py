#!/usr/bin/env python3
"""
phd_sizer_shootout_v24.py  —  LITERALLY TEST EVERYTHING.

Pipeline:
    STAGE 1:  Generate the canonical trade stream from run_portfolio()
              (4 symbols: DE40, US30, XAUUSD, US500), apply full safety rails,
              and save Results/v24_trades.json (one-shot, deterministic).
    STAGE 2:  Fit per-symbol 2-state HMMs on daily-range feature for the
              HMM-regime sizer.
    STAGE 3:  Build the sizer zoo (16 pure + 1 HMM = 17 sizers).
    STAGE 4:  Replay every sizer on the trade stream, portfolio-wide.
              For each sizer, compute 15 risk metrics.
    STAGE 5:  5000-path stationary-block bootstrap → ruin@3%, @4%, @5%.
    STAGE 6:  Per-symbol replays (each of the 4 symbols separately).
    STAGE 7:  Build the top-3 ensemble sizer and re-evaluate.
    STAGE 8:  Produce 5 rankings (PnL / DD / Calmar / Ruin@4% / Composite).
    STAGE 9:  Write:
                Results/v24_trades.json                 (raw trade list)
                Results/v24_shootout_portfolio.json     (all-sizer metrics)
                Results/v24_shootout_per_symbol.json    (per-symbol tables)
                Docs/V24_SIZER_SHOOTOUT_RESULTS.md      (human leaderboard)

All metrics, all rankings, all symbols — printed to stdout and persisted.
"""
from __future__ import annotations

import json, math, sys, time
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

# Reuse the trade pipeline
from Scripts.backtest_v22_phase_b import (
    run_portfolio, apply_full_safety_rails, fit_hmm_for_symbol,
)
from Scripts.backtest_v22_lean_uk5 import BALANCE
from src.dynamic_sizer_v21 import MertonGZSizerConfig
from src.sizers_v24 import (
    TradeMeta, History, Sizer, FlatSizer, FractionalKellySizer,
    BayesianKellySizer, MertonGZWrapper, GARCHMertonSizer,
    GrossmanZhouSizer, VinceOptimalFSizer, VanTharpInverseVolSizer,
    HMMRegimeSizer, CPPISizer, EnsembleSizer, build_zoo, HARD_CAP_F,
)

SYMBOLS = ["DE40", "US30", "XAUUSD", "US500"]
TRADE_FILE = ROOT / "Results" / "v24_trades.json"
PORT_FILE = ROOT / "Results" / "v24_shootout_portfolio.json"
SYM_FILE = ROOT / "Results" / "v24_shootout_per_symbol.json"
DOC_FILE = ROOT / "Docs" / "V24_SIZER_SHOOTOUT_RESULTS.md"
HMM_FILE = ROOT / "Results" / "v24_hmm_trend_probs.json"

BOOTSTRAP_PATHS = 5000
BOOTSTRAP_BLOCK = 5.0


# =====================================================================
# STAGE 1: Generate trade stream (one-shot, deterministic)
# =====================================================================
def build_trade_stream(force_regenerate: bool = False
                       ) -> Tuple[List[TradeMeta], Dict[str, Any]]:
    """Run run_portfolio once with a reference sizer, apply rails, extract TradeMetas."""
    if TRADE_FILE.exists() and not force_regenerate:
        print(f"  loading cached trade stream from {TRADE_FILE.name}")
        with open(TRADE_FILE) as f:
            data = json.load(f)
        trades = [TradeMeta(**{k: v for k, v in t.items()}) for t in data["trades"]]
        return trades, data

    print("  generating trade stream via run_portfolio + apply_full_safety_rails ...")
    sizer_cfg = MertonGZSizerConfig(
        base_risk_pct=0.0011, cap_mult=3.0, gamma=2.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )
    raw, wmin, wmax, _, streams = run_portfolio(SYMBOLS, sizer_cfg)
    final = apply_full_safety_rails(raw, slippage_ticks=1.0)

    trades: List[TradeMeta] = []
    for tr in final:
        # Reconstruct realised_R (net_pnl / initial_risk_$).
        # Engine stores .realised_R; apply_full_safety_rails may have scaled
        # .net_pnl (daily kill, weekend flat). We prefer the R the engine
        # saw (pre-rail) AND report the rail-adjusted net_pnl so the
        # shootout replay uses the pure sizing signal.
        R = getattr(tr, "realised_R", 0.0)
        net_pnl = getattr(tr, "net_pnl", 0.0)
        trades.append(TradeMeta(
            symbol=tr.symbol,
            entry_time=float(tr.entry_time),
            exit_time=float(getattr(tr, "exit_time", tr.entry_time)),
            side=int(getattr(tr, "side", 1)),
            entry_price=float(getattr(tr, "entry_price", 0.0)),
            stop_price=float(getattr(tr, "stop_price", 0.0)),
            exit_price=float(getattr(tr, "exit_price", 0.0)),
            realised_R=float(R),
            original_net_pnl=float(net_pnl),
        ))
    trades.sort(key=lambda t: t.entry_time)

    # Save
    TRADE_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "window": [str(wmin), str(wmax)],
        "n": len(trades),
        "symbols": SYMBOLS,
        "sizer_ref": "MertonGZ gamma=2.0 base=0.11pct (baseline, $10,841 run)",
        "trades": [asdict(t) for t in trades],
    }
    with open(TRADE_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"    saved {len(trades)} trades → {TRADE_FILE.relative_to(ROOT)}")

    return trades, {"streams": streams}


# =====================================================================
# STAGE 2: Fit HMMs
# =====================================================================
def fit_all_hmms(streams: Dict[str, List]) -> Dict[str, Dict[Any, float]]:
    """Returns {symbol: {date: P(trend | data≤date)}}."""
    if HMM_FILE.exists():
        print(f"  loading cached HMM probs from {HMM_FILE.name}")
        with open(HMM_FILE) as f:
            raw = json.load(f)
        # Re-cast date keys
        out: Dict[str, Dict[Any, float]] = {}
        for sym, d in raw.items():
            out[sym] = {}
            for date_str, p in d.items():
                try:
                    out[sym][datetime.fromisoformat(date_str).date()] = float(p)
                except Exception:
                    continue
        return out

    print("  fitting per-symbol 2-state HMMs on daily-range feature ...")
    result: Dict[str, Dict[Any, float]] = {}
    for sym, bars in streams.items():
        hmm, probs = fit_hmm_for_symbol(bars)
        if hmm is None:
            print(f"    {sym}: insufficient data, using P=0.5 (neutral)")
            result[sym] = {}
            continue
        result[sym] = probs
        trend_days = sum(1 for v in probs.values() if v >= 0.5)
        print(f"    {sym}: T={len(probs)}d trend-days={trend_days} "
              f"μ_trend={hmm.mu[0]:+.3f} μ_chop={hmm.mu[1]:+.3f}")

    # Cache
    serialisable = {sym: {str(d): p for d, p in dd.items()} for sym, dd in result.items()}
    HMM_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(HMM_FILE, "w") as f:
        json.dump(serialisable, f, indent=2)
    return result


# =====================================================================
# STAGE 4: Replay harness
# =====================================================================
def replay(sizer: Sizer, trades: List[TradeMeta],
           start_equity: float = BALANCE) -> Dict[str, Any]:
    """Replay trades through one sizer. Returns per-trade diary + metrics."""
    sizer.reset()
    hist = History(equity=start_equity, peak=start_equity, start_equity=start_equity)

    diary = []
    for tr in trades:
        f = sizer.size(hist, tr)
        f = max(0.0, min(HARD_CAP_F, f))
        risk_usd = hist.equity * f
        pnl = tr.realised_R * risk_usd   # rails already baked into realised_R
        hist.feedback(tr, pnl)
        sizer.on_closed(tr, tr.realised_R)
        diary.append({
            "t": tr.entry_time,
            "sym": tr.symbol,
            "R": tr.realised_R,
            "f": f,
            "pnl": pnl,
            "equity": hist.equity,
            "peak": hist.peak,
            "dd": hist.dd_pct(),
        })
    return {"diary": diary, "final_equity": hist.equity, "peak": hist.peak}


# =====================================================================
# STAGE 4b: Metrics
# =====================================================================
def compute_metrics(diary: List[Dict], start_equity: float) -> Dict[str, float]:
    """15 metrics per sizer. All inputs are per-trade $ PnL."""
    if not diary:
        return {"n": 0}
    pnls = np.array([d["pnl"] for d in diary], dtype=float)
    eq_curve = np.array([d["equity"] for d in diary], dtype=float)
    dds = np.array([d["dd"] for d in diary], dtype=float)
    final_eq = float(eq_curve[-1])
    net_pnl = final_eq - start_equity
    ret_pct = net_pnl / start_equity * 100.0

    wins = pnls[pnls > 0]
    losses = pnls[pnls < 0]
    n_wins = int((pnls > 0).sum())
    n_trades = int(len(pnls))
    wr = n_wins / n_trades if n_trades else 0.0
    gross_win = float(wins.sum()) if wins.size else 0.0
    gross_loss = float(-losses.sum()) if losses.size else 0.0
    pf = gross_win / gross_loss if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0

    # Max DD (running peak / valley)
    peak_so_far = -np.inf
    max_dd = 0.0
    for v in eq_curve:
        if v > peak_so_far:
            peak_so_far = v
        dd = (peak_so_far - v) / peak_so_far if peak_so_far > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
    max_dd_pct = max_dd * 100.0

    # Daily DD: group by date, take max daily loss
    by_day = defaultdict(float)
    for d in diary:
        day = datetime.fromtimestamp(d["t"]).date()
        by_day[day] += d["pnl"]
    daily_pnls = list(by_day.values())
    worst_day_pct = (min(daily_pnls) / start_equity * 100.0) if daily_pnls else 0.0

    # Ulcer index = sqrt(mean of squared DDs through time)
    ulcer = float(np.sqrt(np.mean(dds ** 2))) * 100.0 if dds.size else 0.0

    # CVaR_95 on trade-level $ PnL (average of worst 5 %)
    sorted_p = np.sort(pnls)
    cvar_n = max(1, int(0.05 * len(sorted_p)))
    cvar95 = float(sorted_p[:cvar_n].mean())

    # Sharpe (annualised assuming ~250 trading days * avg_trades_per_day; use per-trade)
    if pnls.std() > 0:
        sharpe = float(pnls.mean() / pnls.std() * math.sqrt(252))
    else:
        sharpe = 0.0

    # Sortino: downside only
    down = pnls[pnls < 0]
    if down.size > 0 and down.std() > 0:
        sortino = float(pnls.mean() / down.std() * math.sqrt(252))
    else:
        sortino = float("inf") if pnls.mean() > 0 else 0.0

    # Calmar = (period return) / maxDD
    calmar = (ret_pct / max_dd_pct) if max_dd_pct > 0 else float("inf") if ret_pct > 0 else 0.0

    # MAR ≈ monthly return / max DD (approx: ret_pct / 3 months ÷ max_dd)
    mar = ((ret_pct / 3.0) / max_dd_pct) if max_dd_pct > 0 else float("inf") if ret_pct > 0 else 0.0

    # Omega ratio at threshold 0
    gains = pnls[pnls > 0].sum()
    losses_abs = -pnls[pnls < 0].sum()
    omega = float(gains / losses_abs) if losses_abs > 0 else float("inf") if gains > 0 else 0.0

    # Avg R and expectancy (using realised_R from diary)
    Rs = np.array([d["R"] for d in diary])
    avg_R = float(Rs.mean())
    expectancy = float(pnls.mean())

    return {
        "n": n_trades,
        "wr_pct": wr * 100.0,
        "net_pnl": float(net_pnl),
        "ret_pct": float(ret_pct),
        "pf": float(pf) if math.isfinite(pf) else 999.0,
        "max_dd_pct": float(max_dd_pct),
        "worst_day_pct": float(worst_day_pct),
        "ulcer_idx": float(ulcer),
        "cvar_95_dollars": float(cvar95),
        "sharpe": float(sharpe),
        "sortino": float(sortino) if math.isfinite(sortino) else 999.0,
        "calmar": float(calmar) if math.isfinite(calmar) else 999.0,
        "mar": float(mar) if math.isfinite(mar) else 999.0,
        "omega": float(omega) if math.isfinite(omega) else 999.0,
        "avg_R": avg_R,
        "expectancy_dollars": expectancy,
        "final_equity": final_eq,
    }


# =====================================================================
# STAGE 5: Bootstrap ruin probability
# =====================================================================
def bootstrap_ruin(pnls: List[float], start_equity: float,
                    n_paths: int = BOOTSTRAP_PATHS,
                    avg_block: float = BOOTSTRAP_BLOCK,
                    seed: int = 0) -> Dict[str, float]:
    """Stationary-block bootstrap. Returns ruin probabilities at 3/4/5%."""
    if not pnls:
        return {"ruin_3pct": 0.0, "ruin_4pct": 0.0, "ruin_5pct": 0.0,
                "mean_dd": 0.0, "p95_dd": 0.0}
    rng = np.random.default_rng(seed)
    arr = np.array(pnls, dtype=float)
    n = len(arr)
    p = 1.0 / avg_block  # geometric reset probability

    dd_samples = np.zeros(n_paths)
    ruin3 = 0; ruin4 = 0; ruin5 = 0
    for i in range(n_paths):
        path = np.empty(n)
        k = rng.integers(0, n)
        for j in range(n):
            if rng.random() < p:
                k = rng.integers(0, n)
            path[j] = arr[k]
            k = (k + 1) % n
        # Equity curve + max DD on this path
        eq = start_equity + path.cumsum()
        running_peak = np.maximum.accumulate(np.concatenate([[start_equity], eq]))[1:]
        dd = (running_peak - eq) / running_peak
        max_dd = float(dd.max())
        dd_samples[i] = max_dd
        if max_dd >= 0.05: ruin5 += 1
        if max_dd >= 0.04: ruin4 += 1
        if max_dd >= 0.03: ruin3 += 1

    return {
        "ruin_3pct": ruin3 / n_paths * 100.0,
        "ruin_4pct": ruin4 / n_paths * 100.0,
        "ruin_5pct": ruin5 / n_paths * 100.0,
        "mean_dd_pct": float(dd_samples.mean()) * 100.0,
        "p95_dd_pct": float(np.quantile(dd_samples, 0.95)) * 100.0,
        "p99_dd_pct": float(np.quantile(dd_samples, 0.99)) * 100.0,
    }


# =====================================================================
# STAGE 6: IS/OOS walk-forward
# =====================================================================
def is_oos_metrics(diary: List[Dict], start_equity: float) -> Dict[str, Any]:
    """Split diary 50/50 by index; report IS & OOS metrics."""
    if len(diary) < 10:
        return {"is": {}, "oos": {}, "consistency": 0.0}
    split = len(diary) // 2
    is_d = diary[:split]; oos_d = diary[split:]
    # Need to track equity/dd from scratch on each half
    def _replay_half(d):
        eq = start_equity; peak = start_equity; cur = []
        for x in d:
            pnl = x["pnl"]; eq += pnl
            peak = max(peak, eq)
            dd = (peak - eq) / peak if peak > 0 else 0.0
            cur.append({**x, "equity": eq, "peak": peak, "dd": dd})
        return cur
    is_replayed = _replay_half(is_d)
    oos_replayed = _replay_half(oos_d)
    m_is = compute_metrics(is_replayed, start_equity)
    m_oos = compute_metrics(oos_replayed, start_equity)
    # Consistency ratio (OOS PnL / IS PnL)
    cons = (m_oos["net_pnl"] / max(1.0, m_is["net_pnl"])) if m_is.get("net_pnl", 0) != 0 else 0.0
    return {"is": m_is, "oos": m_oos, "consistency_ratio": float(cons)}


# =====================================================================
# STAGE 8: Rankings
# =====================================================================
def composite_score(m: Dict[str, float], mc: Dict[str, float]) -> float:
    """Composite: (Calmar × Sortino × Omega) / max(Ulcer, 1e-6)
    Penalised by ruin@4% (multiplicative)."""
    calmar = min(m.get("calmar", 0.0), 50.0)   # cap at 50 to avoid ∞-domination
    sortino = min(m.get("sortino", 0.0), 50.0)
    omega = min(m.get("omega", 0.0), 10.0)
    ulcer = max(m.get("ulcer_idx", 1e-6), 1e-6)
    numerator = calmar * sortino * omega
    score = numerator / ulcer
    ruin_penalty = 1.0 - mc.get("ruin_4pct", 0.0) / 100.0
    return float(score * ruin_penalty)


def rank_all(results: List[Dict[str, Any]]) -> Dict[str, List[Dict]]:
    """Produce 5 rankings: PnL / DD / Calmar / Ruin@4% / Composite."""
    by_pnl     = sorted(results, key=lambda r: -r["metrics"]["net_pnl"])
    by_dd      = sorted(results, key=lambda r:  r["metrics"]["max_dd_pct"])
    by_calmar  = sorted(results, key=lambda r: -r["metrics"]["calmar"])
    by_ruin    = sorted(results, key=lambda r:  r["mc"]["ruin_4pct"])
    by_comp    = sorted(results, key=lambda r: -r["composite"])
    return {
        "by_pnl": by_pnl, "by_dd": by_dd, "by_calmar": by_calmar,
        "by_ruin4pct": by_ruin, "by_composite": by_comp,
    }


# =====================================================================
# Pretty-print
# =====================================================================
def _fmt_row(r: Dict[str, Any]) -> str:
    m = r["metrics"]; mc = r["mc"]
    return (f"  {r['name']:<28s} N={m['n']:>3d} "
            f"PnL=${m['net_pnl']:>+8,.0f} "
            f"DD={m['max_dd_pct']:>5.2f}% "
            f"PF={m['pf']:>4.2f} "
            f"Shp={m['sharpe']:>+5.2f} "
            f"Srt={m['sortino']:>+5.1f} "
            f"Calm={m['calmar']:>+5.2f} "
            f"Omega={m['omega']:>4.2f} "
            f"Ulcer={m['ulcer_idx']:>4.2f} "
            f"Ruin@4%={mc['ruin_4pct']:>4.1f}% "
            f"comp={r['composite']:>+7.1f}")


def print_ranking(out, title: str, ranking: List[Dict], top: int = 10):
    out(""); out(f"  ## {title}  (top {top})")
    for i, r in enumerate(ranking[:top], 1):
        out(f"  {i:>2d}. {_fmt_row(r)[2:]}")


# =====================================================================
# Main
# =====================================================================
def main():
    out_lines: List[str] = []
    def p(m: str = ""):
        print(m)
        out_lines.append(m)

    p("=" * 140)
    p("  v24 PhD SIZER SHOOTOUT — LITERALLY TEST EVERYTHING")
    p("  " + datetime.now().isoformat(timespec="seconds"))
    p("=" * 140)

    # -------- Stage 1: trade stream --------
    p("\n  [1/9] Building trade stream ...")
    trades, meta = build_trade_stream()
    p(f"    total trades: {len(trades)}   "
      f"symbols: {sorted({t.symbol for t in trades})}   "
      f"time span: {datetime.fromtimestamp(trades[0].entry_time).isoformat()} "
      f"→ {datetime.fromtimestamp(trades[-1].entry_time).isoformat()}")
    by_sym = defaultdict(int)
    for t in trades:
        by_sym[t.symbol] += 1
    p(f"    per-symbol: " + "  ".join(f"{s}={n}" for s, n in sorted(by_sym.items())))

    # -------- Stage 2: HMMs --------
    p("\n  [2/9] Fitting per-symbol HMMs ...")
    trend_p = fit_all_hmms(meta.get("streams", {}))

    # -------- Stage 3: zoo --------
    p("\n  [3/9] Building sizer zoo ...")
    zoo = build_zoo(trend_p_by_symbol_date=trend_p)
    p(f"    zoo size: {len(zoo)} sizers")

    # -------- Stage 4 + 5 + 6: replay + metrics + bootstrap + IS/OOS --------
    p("\n  [4/9] Replaying each sizer on portfolio ...")
    all_results: List[Dict[str, Any]] = []
    for i, sz in enumerate(zoo, 1):
        t0 = time.time()
        rep = replay(sz, trades, start_equity=BALANCE)
        m = compute_metrics(rep["diary"], BALANCE)
        pnl_list = [d["pnl"] for d in rep["diary"]]
        mc = bootstrap_ruin(pnl_list, BALANCE, n_paths=BOOTSTRAP_PATHS, seed=i)
        wf = is_oos_metrics(rep["diary"], BALANCE)
        comp = composite_score(m, mc)
        all_results.append({
            "name": sz.name, "metrics": m, "mc": mc,
            "walkforward": wf, "composite": comp,
        })
        p(f"    [{i:>2d}/{len(zoo)}] {sz.name:<32s} "
          f"net=${m['net_pnl']:>+8,.0f}  DD={m['max_dd_pct']:>5.2f}%  "
          f"ruin@4%={mc['ruin_4pct']:>4.1f}%  ({time.time()-t0:.1f}s)")

    # -------- Stage 7: Ensemble of top-3 by composite (on IS half) --------
    p("\n  [5/9] Building ENSEMBLE of top-3 (selected on IS-half composite) ...")
    # Rank by IS composite (using wf["is"] metrics + MC on IS trade list)
    is_scored = []
    for r, sz in zip(all_results, zoo):
        m_is = r["walkforward"]["is"] if r["walkforward"] else {}
        if not m_is:
            is_scored.append((None, sz))
            continue
        # MC on IS half
        is_pnls = [d["pnl"] for d in replay(sz, trades[:len(trades)//2], BALANCE)["diary"]]
        mc_is = bootstrap_ruin(is_pnls, BALANCE, n_paths=2000, seed=77)
        is_scored.append((composite_score(m_is, mc_is), sz))
    is_scored = [x for x in is_scored if x[0] is not None]
    is_scored.sort(key=lambda x: -x[0])
    top3_sizers = [build_zoo(trend_p_by_symbol_date=trend_p)[
        [s.name for s in build_zoo(trend_p_by_symbol_date=trend_p)].index(x[1].name)
    ] for x in is_scored[:3]]  # fresh instances (state reset)
    if top3_sizers:
        ens = EnsembleSizer(top3_sizers, name=f"Ensemble_top3_" +
                            "+".join(s.name.split('_')[0] for s in top3_sizers))
        rep_e = replay(ens, trades, BALANCE)
        m_e = compute_metrics(rep_e["diary"], BALANCE)
        mc_e = bootstrap_ruin([d["pnl"] for d in rep_e["diary"]], BALANCE,
                                n_paths=BOOTSTRAP_PATHS, seed=999)
        wf_e = is_oos_metrics(rep_e["diary"], BALANCE)
        comp_e = composite_score(m_e, mc_e)
        all_results.append({
            "name": ens.name, "metrics": m_e, "mc": mc_e,
            "walkforward": wf_e, "composite": comp_e,
        })
        p(f"    ensemble {ens.name}:")
        p(f"      net=${m_e['net_pnl']:>+8,.0f}  DD={m_e['max_dd_pct']:>5.2f}%  "
          f"ruin@4%={mc_e['ruin_4pct']:>4.1f}%  comp={comp_e:>+7.1f}")

    # -------- Stage 8: Rankings --------
    p("\n  [6/9] Producing 5 rankings ...")
    rankings = rank_all(all_results)
    for title, key in [
        ("RANKING 1: Highest net PnL (profit-first)", "by_pnl"),
        ("RANKING 2: Lowest max DD (defensive)", "by_dd"),
        ("RANKING 3: Highest Calmar (risk-adj growth)", "by_calmar"),
        ("RANKING 4: Lowest Ruin@4% (prop-firm paranoid)", "by_ruin4pct"),
        ("RANKING 5: Highest Composite (PhD aggregator)", "by_composite"),
    ]:
        print_ranking(p, title, rankings[key], top=len(all_results))

    # -------- Stage 9: Per-symbol replays --------
    p("\n  [7/9] Per-symbol replays ...")
    per_sym: Dict[str, List[Dict]] = {}
    for sym in SYMBOLS:
        s_trades = [t for t in trades if t.symbol == sym]
        if len(s_trades) < 5:
            continue
        p(f"\n    {sym}: {len(s_trades)} trades")
        sym_results = []
        for i, sz in enumerate(build_zoo(trend_p_by_symbol_date=trend_p), 1):
            rep = replay(sz, s_trades, BALANCE)
            m = compute_metrics(rep["diary"], BALANCE)
            mc = bootstrap_ruin([d["pnl"] for d in rep["diary"]], BALANCE,
                                n_paths=2000, seed=i + 100)
            comp = composite_score(m, mc)
            sym_results.append({"name": sz.name, "metrics": m, "mc": mc,
                                 "composite": comp})
        sym_results.sort(key=lambda r: -r["composite"])
        per_sym[sym] = sym_results
        for i, r in enumerate(sym_results[:5], 1):
            p(f"      {i}. {_fmt_row(r)[2:]}")

    # -------- Save --------
    p("\n  [8/9] Saving JSON outputs ...")
    PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(PORT_FILE, "w") as f:
        json.dump({
            "generated": datetime.now().isoformat(),
            "n_trades": len(trades),
            "symbols": SYMBOLS,
            "bootstrap_paths": BOOTSTRAP_PATHS,
            "start_equity": BALANCE,
            "results": all_results,
            "rankings": {k: [{"name": r["name"],
                              "net_pnl": r["metrics"]["net_pnl"],
                              "max_dd_pct": r["metrics"]["max_dd_pct"],
                              "calmar": r["metrics"]["calmar"],
                              "ruin_4pct": r["mc"]["ruin_4pct"],
                              "composite": r["composite"]}
                             for r in v]
                           for k, v in rankings.items()},
        }, f, indent=2, default=str)
    with open(SYM_FILE, "w") as f:
        json.dump(per_sym, f, indent=2, default=str)
    p(f"    wrote {PORT_FILE.relative_to(ROOT)}")
    p(f"    wrote {SYM_FILE.relative_to(ROOT)}")

    # -------- Markdown report --------
    p("\n  [9/9] Writing markdown report ...")
    md_lines = [
        "# V24 SIZER SHOOTOUT — RESULTS",
        "",
        f"Generated: {datetime.now().isoformat(timespec='seconds')}",
        f"Trades: {len(trades)}  |  Symbols: {', '.join(sorted({t.symbol for t in trades}))}  |  "
        f"Start equity: ${BALANCE:,.0f}  |  Bootstrap paths: {BOOTSTRAP_PATHS:,}",
        "",
        "## Winner by ranking",
        "",
        "| Ranking | Winner | Net PnL | Max DD | Calmar | Ruin@4% | Composite |",
        "|---|---|---|---|---|---|---|",
    ]
    for title, key in [
        ("PnL", "by_pnl"), ("DD", "by_dd"), ("Calmar", "by_calmar"),
        ("Ruin@4%", "by_ruin4pct"), ("Composite", "by_composite"),
    ]:
        w = rankings[key][0]
        m = w["metrics"]; mc = w["mc"]
        md_lines.append(f"| {title} | **{w['name']}** | ${m['net_pnl']:+,.0f} | "
                        f"{m['max_dd_pct']:.2f}% | {m['calmar']:+.2f} | "
                        f"{mc['ruin_4pct']:.1f}% | {w['composite']:+.1f} |")

    md_lines.extend(["", "## Portfolio leaderboard (by composite)", "",
                     "| # | Sizer | N | PnL | MaxDD | PF | Sharpe | Sortino | Calmar | Omega | Ulcer | Ruin@4% | Composite |",
                     "|---|---|---|---|---|---|---|---|---|---|---|---|---|"])
    for i, r in enumerate(rankings["by_composite"], 1):
        m = r["metrics"]; mc = r["mc"]
        md_lines.append(f"| {i} | {r['name']} | {m['n']} | ${m['net_pnl']:+,.0f} | "
                        f"{m['max_dd_pct']:.2f}% | {m['pf']:.2f} | {m['sharpe']:+.2f} | "
                        f"{m['sortino']:+.1f} | {m['calmar']:+.2f} | {m['omega']:.2f} | "
                        f"{m['ulcer_idx']:.2f} | {mc['ruin_4pct']:.1f}% | {r['composite']:+.1f} |")

    md_lines.extend(["", "## Per-symbol winners (by composite)", ""])
    for sym in SYMBOLS:
        if sym not in per_sym:
            continue
        md_lines.append(f"### {sym}  ({len(per_sym[sym])} sizers)")
        md_lines.append("")
        md_lines.append("| # | Sizer | PnL | DD | Calmar | Ruin@4% | Comp |")
        md_lines.append("|---|---|---|---|---|---|---|")
        for i, r in enumerate(per_sym[sym][:5], 1):
            m = r["metrics"]; mc = r["mc"]
            md_lines.append(f"| {i} | {r['name']} | ${m['net_pnl']:+,.0f} | "
                            f"{m['max_dd_pct']:.2f}% | {m['calmar']:+.2f} | "
                            f"{mc['ruin_4pct']:.1f}% | {r['composite']:+.1f} |")
        md_lines.append("")

    md_lines.extend(["", "## IS/OOS walk-forward (top-5 by composite)", "",
                     "| Sizer | IS PnL | IS PF | OOS PnL | OOS PF | OOS/IS ratio |",
                     "|---|---|---|---|---|---|"])
    for r in rankings["by_composite"][:5]:
        wf = r["walkforward"]
        m_is = wf.get("is", {}); m_oos = wf.get("oos", {})
        if not m_is:
            continue
        md_lines.append(f"| {r['name']} | ${m_is['net_pnl']:+,.0f} | {m_is['pf']:.2f} | "
                        f"${m_oos['net_pnl']:+,.0f} | {m_oos['pf']:.2f} | "
                        f"{wf.get('consistency_ratio', 0):+.2%} |")

    DOC_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(DOC_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))
    p(f"    wrote {DOC_FILE.relative_to(ROOT)}")

    p("\n" + "=" * 140)
    p("  DONE. Review Docs/V24_SIZER_SHOOTOUT_RESULTS.md to pick your production sizer.")
    p("=" * 140)

    # Also persist the stdout log
    with open(ROOT / "Results" / "v24_shootout.log", "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))


if __name__ == "__main__":
    main()
