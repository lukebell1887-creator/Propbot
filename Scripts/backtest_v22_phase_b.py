#!/usr/bin/env python3
"""
backtest_v22_phase_b.py  —  Phase B of the v22 institutional build.

Pipeline:
  1.  Reproduce the Phase-A baseline (Lean+UK 5 + all safety rails + 1-tick
      slippage) on the full window.      [reference point]
  2.  B2 — HMM GATE: fit 2-state Gaussian HMM to daily-range feature per
      symbol, filter each day's ORB signal through P(trend|past) >= θ.
  3.  B3 — Deflated Sharpe Ratio on the final trade-level PnL stream.
      Uses n_trials = 648 (the Phase-1 ORB grid-search budget) to correct
      for multiple-testing from all the prior research.
  4.  B4 — Monte-Carlo stationary-bootstrap (1000 paths) for max-DD and
      ruin-probability (@ 4 % threshold) confidence intervals.
  5.  B5 — IS/OOS time-split walk-forward: first 50 % of window is IS,
      second 50 % is OOS.  Report PnL/DD/PF/WR on both and the ratio.

Exit gate (all must pass):
  PnL_OOS   >= $8 k   at 1-tick slippage                    (≈ half of $16 k)
  DD_OOS    <= 2.5 %                                        
  PF_OOS    >= 1.6                                          
  Sharpe_OOS>= 3.0                                          
  DSR       >= 0.95  (after n_trials = 648 correction)      
  Ruin prob <= 2.0 %  (P(DD>4 %) from 1000-path MC)         
  HMM gate  must NOT reduce PnL by > 20 %  vs no-gate baseline

Output:  Results/backtest_v22_phase_b.{txt,json}
"""
from __future__ import annotations

import csv, json, math, statistics, sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

# Reuse all the Phase-A machinery
from Scripts.backtest_v22_lean_uk5 import (
    SYMBOLS, ORB_CONFIGS, AMP_HURDLE, BALANCE, MONTHS,
    FULL_WINDOW_SYMBOLS, BROKER_TICK_SIZE,
    load_m1, common_window, stats,
    apply_slippage, apply_weekend_flat, apply_daily_kill_switch,
    apply_position_cap,
)
from src.smartbb_engine import SMARTBB_UNIVERSE
from src.orb_engine_v20 import ORBEngineV20, ORBEngineConfig
from src.momentum.orb import ORBConfig
from src.dynamic_sizer_v21 import MertonGZSizer, MertonGZSizerConfig

from src.regime.hmm2 import fit_hmm2, daily_range_feature, HMM2
from src.stats.validation import (
    deflated_sharpe_ratio, mc_bootstrap_dd, observed_sharpe,
)

# How many configs were tested historically, for DSR multiple-testing adj.
PRIOR_TRIALS = 648   # the ORB grid-search used 648 cells per symbol

# HMM gate threshold: only allow entries when P(trend) >= this
HMM_TREND_THRESHOLD = 0.55


# -----------------------------------------------------------------------
#  Build per-symbol daily OHLC from M1 stream
# -----------------------------------------------------------------------
def build_daily_ohlc(bars_m1):
    """bars_m1 = [(datetime, o, h, l, c), ...] sorted ascending."""
    days = {}
    for t, o, h, l, c in bars_m1:
        d = t.date()
        if d not in days:
            days[d] = [o, h, l, c]
        else:
            rec = days[d]
            rec[1] = max(rec[1], h)
            rec[2] = min(rec[2], l)
            rec[3] = c
    ordered_dates = sorted(days.keys())
    opens  = np.array([days[d][0] for d in ordered_dates])
    highs  = np.array([days[d][1] for d in ordered_dates])
    lows   = np.array([days[d][2] for d in ordered_dates])
    closes = np.array([days[d][3] for d in ordered_dates])
    return ordered_dates, opens, highs, lows, closes


def fit_hmm_for_symbol(bars_m1):
    """
    Returns:
        hmm : fitted HMM2 model (on all available days with a warm-up of 20)
        trend_prob_by_date : dict {date -> P(trend|past data)} using online filter
    None, None if insufficient data.
    """
    dates, opens, highs, lows, closes = build_daily_ohlc(bars_m1)
    if len(dates) < 40:
        return None, {}
    feat_all = daily_range_feature(highs, lows, atr_window=20)
    # Drop warmup NaNs
    valid_mask = ~np.isnan(feat_all)
    feat_valid = feat_all[valid_mask]
    dates_valid = [d for d, m in zip(dates, valid_mask) if m]
    if len(feat_valid) < 20:
        return None, {}
    # Fit on full window (we'll also do IS/OOS below)
    hmm = fit_hmm2(feat_valid, n_iter=40, seed=0)
    # Online filter → causal P(trend) per day
    filt = hmm.filter(feat_valid)     # (T, 2)
    trend_p = {d: float(filt[i, 0]) for i, d in enumerate(dates_valid)}
    return hmm, trend_p


# -----------------------------------------------------------------------
#  Portfolio engine (identical to Phase-A, plus HMM gate)
# -----------------------------------------------------------------------
def run_portfolio(symbols, sizer_cfg, *, hmm_gates: Dict[str, Dict] = None,
                  hmm_threshold: float = HMM_TREND_THRESHOLD,
                  window_override: Tuple[datetime, datetime] = None):
    """
    If hmm_gates is None: runs without HMM gate (same as Phase-A baseline).
    If hmm_gates[sym][date] < hmm_threshold on the entry date → trade is
        dropped POST-ENGINE (entry already happened, but we filter it out).
        This keeps state evolution identical and isolates the HMM gate as
        a pure post-processor.
    """
    data = ROOT / "data" / "historical"
    files = {s: data / f"{s}_M1.csv" for s in symbols
             if (data / f"{s}_M1.csv").exists()}
    window_files = {s: p for s, p in files.items() if s in FULL_WINDOW_SYMBOLS}
    if not window_files:
        window_files = files
    if window_override:
        tmin, tmax = window_override
    else:
        tmin, tmax = common_window(window_files, MONTHS)

    specs = {s: SMARTBB_UNIVERSE[s] for s in files}
    streams = {s: load_m1(files[s], tmin, tmax) for s in files}

    sizer = MertonGZSizer(sizer_cfg)
    engines: Dict[str, ORBEngineV20] = {}
    shared = {"val": BALANCE, "peak": BALANCE}

    def risk_fn(sym, equity, peak, open_pos):
        return sizer.compute_risk_pct(sym, shared["val"], shared["peak"], open_pos)

    for sym in files:
        cfg = ORBEngineConfig(
            risk_pct=0.0010, amp_hurdle=AMP_HURDLE[sym],
            require_nr7=False, nr_lookback=7,
            trail_atr_mult=0.8,
            tp1_close_frac=0.50, tp2_close_frac=0.25,
            hurst_min=0.0, hurst_max=1.0, hurst_window=200,
            risk_pct_fn=risk_fn,
        )
        engines[sym] = ORBEngineV20(
            symbols=[specs[sym]], cfg=cfg,
            orb_configs={sym: ORB_CONFIGS[sym]},
            initial_equity=BALANCE,
        )

    allb = []
    for s, bars in streams.items():
        allb.extend((t, s, o, h, l, c) for (t, o, h, l, c) in bars)
    allb.sort(key=lambda r: r[0])

    last_n = {s: 0 for s in files}
    pending = {}; fed = set()

    def flush(sym):
        st = engines[sym].states.get(sym)
        if st is not None and st.position is not None: return
        for k in [k for k in pending if k[0] == sym and k not in fed]:
            sizer.on_trade_closed(sym, pending[k]["r_sum"])
            fed.add(k)

    for t, s, o, h, l, c in allb:
        eng = engines[s]
        eng.on_bar(s, t.timestamp(), t.strftime("%Y-%m-%d"),
                   t.hour, t.minute, o, h, l, c)
        if len(eng.trades) > last_n[s]:
            for tr in eng.trades[last_n[s]:]:
                shared["val"] += tr.net_pnl
                if shared["val"] > shared["peak"]:
                    shared["peak"] = shared["val"]
                k = (tr.symbol, tr.entry_time)
                pending.setdefault(k, {"r_sum": 0.0})
                pending[k]["r_sum"] += tr.realised_R
            last_n[s] = len(eng.trades)
            flush(s)
    for sym in files: flush(sym)

    all_trades = []
    for eng in engines.values():
        all_trades.extend(eng.trades)
    all_trades.sort(key=lambda tr: tr.entry_time)

    # HMM gate (optional): drop entries on chop days
    dropped_hmm = 0
    if hmm_gates:
        gated = []
        for tr in all_trades:
            d = datetime.fromtimestamp(tr.entry_time).date()
            p_trend = hmm_gates.get(tr.symbol, {}).get(d, 1.0)
            if p_trend < hmm_threshold:
                dropped_hmm += 1
                continue
            gated.append(tr)
        all_trades = gated

    return all_trades, str(tmin), str(tmax), dropped_hmm, streams


# -----------------------------------------------------------------------
#  Apply full safety-rail stack (A4 + A5 + A6 + A2@1tick)
# -----------------------------------------------------------------------
def apply_full_safety_rails(trades, slippage_ticks=1.0):
    trades, _ = apply_position_cap(trades, max_concurrent=2)
    trades, _ = apply_weekend_flat(trades, cutoff_hour_utc=20)
    trades, _ = apply_daily_kill_switch(trades, threshold_pct=1.0)
    trades = apply_slippage(trades, slippage_ticks=slippage_ticks)
    return trades


# -----------------------------------------------------------------------
#  Main Phase-B pipeline
# -----------------------------------------------------------------------
def main():
    out = []
    p = lambda m="": (print(m), out.append(m))

    p("=" * 120)
    p("  v22 PHASE B  —  HMM regime gate  +  Deflated Sharpe  +  MC stress  +  IS/OOS walk-forward")
    p("=" * 120)

    sizer_cfg = MertonGZSizerConfig(
        base_risk_pct=0.0015, cap_mult=3.0, gamma=2.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )

    # -------- 1. Baseline (no HMM gate) --------
    p("\n  [1/5] Running baseline (no HMM gate, full safety rails, 1-tick slip)...")
    raw_base, wmin, wmax, _, streams = run_portfolio(SYMBOLS, sizer_cfg)
    base = apply_full_safety_rails(raw_base, slippage_ticks=1.0)
    sb = stats(base)
    p(f"    Window : {wmin} → {wmax}")
    p(f"    Baseline: N={sb['n']}  PnL=${sb['net']:+,.0f}  DD={sb['dd_pct']:.2f}%  PF={sb['pf']:.2f}  Sharpe={sb['sharpe']:.2f}")

    # -------- 2. Fit per-symbol HMMs on FULL window (for B2 comparison) --------
    p("\n  [2/5] Fitting per-symbol 2-state HMMs on daily-range feature...")
    hmm_gates: Dict[str, Dict] = {}
    hmm_models: Dict[str, HMM2] = {}
    for sym, bars in streams.items():
        hmm, probs = fit_hmm_for_symbol(bars)
        if hmm is None:
            p(f"    {sym}: insufficient data, skipping HMM (will always allow)")
            hmm_gates[sym] = {}     # empty = always allow
            continue
        hmm_models[sym] = hmm
        hmm_gates[sym] = probs
        # Summarise
        trend_days = sum(1 for v in probs.values() if v >= HMM_TREND_THRESHOLD)
        total_days = len(probs)
        p(f"    {sym}: T={total_days}d  μ_trend={hmm.mu[0]:+.3f}  μ_chop={hmm.mu[1]:+.3f}  "
          f"A00={hmm.A[0,0]:.2f} A11={hmm.A[1,1]:.2f}  trend-days={trend_days}/{total_days}")

    # -------- 3. Re-run with HMM gate --------
    p(f"\n  [3/5] Re-running WITH HMM gate (threshold P(trend) >= {HMM_TREND_THRESHOLD})...")
    raw_hmm, _, _, drop, _ = run_portfolio(SYMBOLS, sizer_cfg, hmm_gates=hmm_gates,
                                              hmm_threshold=HMM_TREND_THRESHOLD)
    hmm_trades = apply_full_safety_rails(raw_hmm, slippage_ticks=1.0)
    sh = stats(hmm_trades)
    p(f"    With HMM: N={sh['n']}  PnL=${sh['net']:+,.0f}  DD={sh['dd_pct']:.2f}%  PF={sh['pf']:.2f}  Sharpe={sh['sharpe']:.2f}")
    p(f"    HMM dropped {drop} raw trades ({100*drop/max(1,len(raw_base)):.1f}% of base)")

    # Decide which track is the CHAMPION for B3/B4/B5
    if sh['sharpe'] > sb['sharpe']:
        p(f"    → HMM gate IMPROVES Sharpe ({sh['sharpe']:.2f} > {sb['sharpe']:.2f}). Using HMM track.")
        champ_trades, champ_stats = hmm_trades, sh
        use_hmm = True
    else:
        p(f"    → HMM gate does NOT improve Sharpe ({sh['sharpe']:.2f} ≤ {sb['sharpe']:.2f}). "
          f"Using baseline (HMM gate OFF).")
        champ_trades, champ_stats = base, sb
        use_hmm = False

    # -------- 4. Deflated Sharpe Ratio --------
    p(f"\n  [4/5] Deflated Sharpe Ratio (corrected for multiple-testing, n_trials={PRIOR_TRIALS})...")
    pnls = [tr.net_pnl for tr in champ_trades]
    dsr = deflated_sharpe_ratio(pnls, n_trials=PRIOR_TRIALS)
    p(f"    Observed per-trade Sharpe  : {dsr.observed_sr:+.4f}")
    p(f"    Expected max-Sharpe (null) : {dsr.sr_zero_threshold:+.4f}  (M={PRIOR_TRIALS} trials)")
    p(f"    Skew / Excess Kurt         : {dsr.skew:+.3f}  /  {dsr.kurt_excess:+.3f}")
    p(f"    DEFLATED SHARPE p-value    : {dsr.dsr:.4f}   "
      f"{'[REAL edge @ 5 %]' if dsr.dsr >= 0.95 else '[FAIL — not deflated-significant]'}")

    # -------- 5. Monte-Carlo bootstrap stress --------
    p(f"\n  [5/5] Monte-Carlo stationary-bootstrap stress (1000 paths, avg-block=5)...")
    mc = mc_bootstrap_dd(pnls, n_paths=1000, avg_block_len=5.0,
                          ruin_threshold=0.04, start_balance=BALANCE, seed=0)
    p(f"    PnL  mean / median / p05 / p95      : "
      f"${mc.mean_pnl:+,.0f} / ${mc.median_pnl:+,.0f} / ${mc.p05_pnl:+,.0f} / ${mc.p95_pnl:+,.0f}")
    p(f"    DD   mean / median / p95 / p99      : "
      f"{mc.mean_dd*100:.2f}% / {mc.median_dd*100:.2f}% / {mc.p95_dd*100:.2f}% / {mc.p99_dd*100:.2f}%")
    p(f"    Ruin probability (DD > {mc.ruin_threshold*100:.1f} %) : {mc.ruin_prob*100:.2f} %   "
      f"{'[PASS]' if mc.ruin_prob <= 0.02 else '[FAIL — above 2 %]'}")

    # -------- 6. IS / OOS time-split walk-forward --------
    p(f"\n  [6/6] IS/OOS walk-forward time-split (first 50% IS, last 50% OOS)...")
    ch_sorted = sorted(champ_trades, key=lambda x: x.entry_time)
    if len(ch_sorted) > 10:
        split = len(ch_sorted) // 2
        is_tr = ch_sorted[:split]
        oos_tr = ch_sorted[split:]
        si = stats(is_tr); so = stats(oos_tr)
        p(f"    IS  : N={si['n']:>3}  PnL=${si['net']:+,.0f}  DD={si['dd_pct']:.2f}%  PF={si['pf']:.2f}  Sharpe={si['sharpe']:+.2f}")
        p(f"    OOS : N={so['n']:>3}  PnL=${so['net']:+,.0f}  DD={so['dd_pct']:.2f}%  PF={so['pf']:.2f}  Sharpe={so['sharpe']:+.2f}")
        pbo_like = 1.0 if (si['pf'] >= 1.3 and so['pf'] < 1.0) else 0.0
        pnl_ratio = so['net'] / max(1.0, si['net'])
        p(f"    OOS/IS PnL ratio : {pnl_ratio:+.2%}   (1.0 = identical, negative = edge flipped)")
    else:
        si = {"n":0, "net":0, "dd_pct":0, "pf":0, "sharpe":0}
        so = {"n":0, "net":0, "dd_pct":0, "pf":0, "sharpe":0}
        p("    INSUFFICIENT trades for IS/OOS split.")

    # -------- 7. Phase-B exit gates --------
    p("\n" + "-" * 120)
    p("  PHASE-B EXIT GATES")
    p("-" * 120)
    gates = [
        ("PnL_OOS   >= $8,000",   so["net"]    >= 8000,   f"${so['net']:+,.0f}"),
        ("DD_OOS    <= 2.5 %",    so["dd_pct"] <= 2.5,    f"{so['dd_pct']:.2f} %"),
        ("PF_OOS    >= 1.6",      so["pf"]     >= 1.6,    f"{so['pf']:.2f}"),
        ("Sharpe_OOS>= 3.0",      so["sharpe"] >= 3.0,    f"{so['sharpe']:+.2f}"),
        ("DSR       >= 0.95",     dsr.dsr      >= 0.95,   f"{dsr.dsr:.4f}"),
        ("Ruin prob <= 2.0 %",    mc.ruin_prob <= 0.02,   f"{mc.ruin_prob*100:.2f} %"),
        ("HMM degrade <= 20 %",   sh["net"]    >= 0.80 * sb["net"],
                                    f"HMM ${sh['net']:+,.0f} vs base ${sb['net']:+,.0f}"),
    ]
    for name, ok, val in gates:
        p(f"    {'✅' if ok else '❌'}  {name:<22}  actual = {val}")
    all_pass = all(g[1] for g in gates)
    p("")
    p(f"  VERDICT : {'✅ PHASE B PASSES' if all_pass else '❌ PHASE B FAILS — see failing gates above'}")
    p("=" * 120)

    # -------- Save --------
    (ROOT / "Results").mkdir(exist_ok=True)
    with open(ROOT / "Results" / "backtest_v22_phase_b.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    with open(ROOT / "Results" / "backtest_v22_phase_b.json", "w") as f:
        json.dump({
            "window": [wmin, wmax],
            "baseline": sb,
            "hmm": sh,
            "hmm_used": use_hmm,
            "hmm_dropped": drop,
            "champion": champ_stats,
            "dsr": {
                "observed_sr": dsr.observed_sr,
                "dsr": dsr.dsr,
                "sr_zero_threshold": dsr.sr_zero_threshold,
                "n_trials": dsr.n_trials,
                "skew": dsr.skew, "kurt_excess": dsr.kurt_excess,
            },
            "mc": {
                "n_paths": mc.n_paths,
                "mean_pnl": mc.mean_pnl, "median_pnl": mc.median_pnl,
                "p05_pnl": mc.p05_pnl, "p95_pnl": mc.p95_pnl,
                "mean_dd": mc.mean_dd, "median_dd": mc.median_dd,
                "p95_dd": mc.p95_dd, "p99_dd": mc.p99_dd,
                "ruin_prob": mc.ruin_prob, "ruin_threshold": mc.ruin_threshold,
            },
            "walkforward": {"is": si, "oos": so},
            "exit_gates": [{"name": n, "pass": bool(ok), "actual": v}
                           for (n, ok, v) in gates],
            "all_pass": bool(all_pass),
        }, f, indent=2, default=str)
    p(f"\n  Saved: Results/backtest_v22_phase_b.txt + .json")


if __name__ == "__main__":
    main()
