#!/usr/bin/env python3
"""
backtest_v23_locked.py - FINAL LOCKED configuration.

Layers on top of v22 Phase B (Lean UK-5 + HMM gate + full rails):

  1.  Risk bumped 0.0075%  ->  0.0110%    (Pareto sweet spot)
      with cap_mult tightened 3.0 -> 2.5  (hard max per trade = 0.275%)
  2.  NEW rail: apply_news_flatten()  -  truncate OPEN positions 2 min
      before each Tier-1 news event, using the M1 close price at that
      timestamp. Exit reason marked "news_flatten".
  3.  NEW rail: apply_news_entry_block()  -  drop any trade whose
      entry_time falls inside [event - 15min, event + 15min].
  4.  Final verification:
        5000-path stationary-block bootstrap
        worst-day & worst-daily-drawdown sanity
        ruin @ 3/4/5 % thresholds
        duration histogram (HFT-classifier audit)

Pass gates:
    Max DD <= 4.0%   (user-specified requirement)
    Ruin@5% <= 0.5% (must stay below 5ers cap in 99.5% of parallel universes)
    Net PnL >= $8k / 3 months
    Sub-60s trade count == 0 (no HFT flags)

Output: Results/v23_locked.{txt,json}
"""
from __future__ import annotations

import csv, json, math, statistics, sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple, Optional

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

# --- Reuse all existing machinery ----------------------------------------
from Scripts.backtest_v22_lean_uk5 import (
    SYMBOLS, ORB_CONFIGS, AMP_HURDLE, BALANCE, MONTHS,
    FULL_WINDOW_SYMBOLS, BROKER_TICK_SIZE,
    load_m1, common_window, stats,
    apply_slippage, apply_weekend_flat, apply_daily_kill_switch,
    apply_position_cap,
)
from Scripts.backtest_v22_phase_b import (
    run_portfolio, build_daily_ohlc, fit_hmm_for_symbol,
    HMM_TREND_THRESHOLD,
)
from src.smartbb_engine import SMARTBB_UNIVERSE
from src.orb_engine_v20 import ORBEngineV20, ORBEngineConfig
from src.momentum.orb import ORBConfig
from src.dynamic_sizer_v21 import MertonGZSizer, MertonGZSizerConfig
from src.stats.validation import deflated_sharpe_ratio, mc_bootstrap_dd

# Universe of pairs per user's final lock (UK-5)
LOCKED_SYMBOLS = ["DE40", "US100", "US30", "XAUUSD", "US500"]


# =========================================================================
#  News calendar utilities
# =========================================================================
def load_news_events(csv_path: Path) -> List[Tuple[float, str]]:
    """Parse the tier1_2026.csv file. Returns [(epoch_utc, label), ...] sorted."""
    events: List[Tuple[float, str]] = []
    if not csv_path.exists():
        return events
    with open(csv_path, "r", encoding="utf-8") as f:
        for raw in f:
            raw = raw.strip()
            if not raw or raw.startswith("#") or raw.lower().startswith("timestamp"):
                continue
            parts = [p.strip() for p in raw.split(",")]
            if len(parts) < 3:
                continue
            ts_str, impact, label = parts[0], parts[1], ",".join(parts[2:])
            if impact.lower() != "high":
                continue
            # ISO with trailing Z
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                events.append((ts.timestamp(), label))
            except ValueError:
                continue
    events.sort()
    return events


def build_price_lookup(bars_by_sym: Dict[str, List[Tuple[datetime, float, float, float, float]]]
                       ) -> Dict[str, Tuple[List[float], List[float]]]:
    """Build per-symbol parallel arrays (epoch_utc, close) sorted for bisect."""
    out = {}
    for sym, bars in bars_by_sym.items():
        ts = [b[0].timestamp() for b in bars]
        cl = [b[4] for b in bars]
        out[sym] = (ts, cl)
    return out


def price_at_or_before(ts_arr: List[float], cl_arr: List[float], target: float) -> Optional[float]:
    """Return the close price of the latest M1 bar whose timestamp <= target.
       Returns None if there is no such bar (target pre-history)."""
    import bisect
    i = bisect.bisect_right(ts_arr, target) - 1
    if i < 0:
        return None
    # sanity: if the found bar is more than 10 minutes stale vs target, treat as missing
    # (e.g. overnight gap on futures — don't try to truncate there)
    if target - ts_arr[i] > 10 * 60:
        return None
    return cl_arr[i]


# =========================================================================
#  NEW rails
# =========================================================================
def apply_news_entry_block(trades, events: List[Tuple[float, str]],
                           buffer_min: int = 15):
    """A6.  Drop any trade whose entry_time falls within +/- buffer_min of
    any High-impact event.  Returns (kept_trades, dropped_count)."""
    if not events:
        return trades, {"news_entries_blocked": 0}
    ev_ts = [e[0] for e in events]
    buf = buffer_min * 60
    import bisect
    kept = []
    dropped = 0
    for tr in trades:
        et = tr.entry_time
        # find nearest event
        i = bisect.bisect_left(ev_ts, et)
        nearest = min(
            [abs(et - ev_ts[j]) for j in (i-1, i) if 0 <= j < len(ev_ts)] or [1e18]
        )
        if nearest <= buf:
            dropped += 1
            continue
        kept.append(tr)
    return kept, {"news_entries_blocked": dropped}


def apply_news_flatten(trades, events: List[Tuple[float, str]],
                       price_lookup: Dict[str, Tuple[List[float], List[float]]],
                       minutes_before: int = 2):
    """A7.  Any trade whose lifetime straddles  [event_ts - Nmin]  is
    truncated at that timestamp using the M1 close at that minute.

    Works on the per-partial _ORBTrade objects — only fires when the
    partial is still open when the news cut-off is reached.
    """
    if not events:
        return trades, {"news_flattens": 0}
    ev_cutoffs = sorted(e[0] - minutes_before * 60 for e in events)
    import bisect
    out = []
    flattened = 0
    for tr in trades:
        t2 = deepcopy(tr)
        # find any cut-off that lies strictly inside (entry, exit)
        i = bisect.bisect_right(ev_cutoffs, tr.entry_time)
        cut = None
        if i < len(ev_cutoffs) and ev_cutoffs[i] < tr.exit_time:
            cut = ev_cutoffs[i]
        if cut is None:
            out.append(t2)
            continue
        # Look up truncation price
        sym = tr.symbol
        if sym not in price_lookup:
            out.append(t2)
            continue
        ts_arr, cl_arr = price_lookup[sym]
        px = price_at_or_before(ts_arr, cl_arr, cut)
        if px is None:
            out.append(t2)
            continue
        # Recompute PnL at the truncation price
        spec = SMARTBB_UNIVERSE[sym]
        # side = +1 long / -1 short
        gross = tr.side * (px - tr.entry_price) * abs(tr.lots) * spec.pip_value
        # Keep the entry-side spread + commission that were already booked.
        # We're essentially closing earlier, so spread/commission pair is
        # unchanged (entry+exit both paid; exit is now a market sell at px).
        t2.exit_time = cut
        t2.exit_price = px
        t2.gross_pnl = gross
        t2.net_pnl = gross - tr.spread_cost - tr.commission
        # Realised R in R-units — recompute against the original R distance
        orig_R = abs(tr.gross_pnl) / max(abs(tr.realised_R), 1e-9) if tr.realised_R else None
        t2.realised_R = gross / orig_R if orig_R else tr.realised_R
        t2.exit_reason = "news_flatten"
        flattened += 1
        out.append(t2)
    return out, {"news_flattens": flattened}


# =========================================================================
#  Full v23 safety rails
# =========================================================================
def apply_v23_rails(trades, bars_by_sym, events, slippage_ticks=1.0):
    """Applied IN ORDER — later rails see the effects of earlier ones."""
    audit = {}

    # (a) position cap
    trades, a1 = apply_position_cap(trades, max_concurrent=2)
    audit["position_cap_dropped"] = a1

    # (b) weekend flat
    trades, a2 = apply_weekend_flat(trades, cutoff_hour_utc=20)
    audit["weekend_flat_dropped"] = a2

    # (c) NEW: block entries in +/-15min news window
    trades, a3 = apply_news_entry_block(trades, events, buffer_min=15)
    audit.update(a3)

    # (d) NEW: flatten open positions 2min before news
    pl = build_price_lookup(bars_by_sym)
    trades, a4 = apply_news_flatten(trades, events, pl, minutes_before=2)
    audit.update(a4)

    # (e) daily kill-switch @ 1% (post-news truncation so uses realistic PnL)
    trades, a5 = apply_daily_kill_switch(trades, threshold_pct=1.0)
    audit.update(a5)

    # (f) slippage haircut (pay round-trip spread on every partial)
    trades = apply_slippage(trades, slippage_ticks=slippage_ticks)

    return trades, audit


# =========================================================================
#  Stationary-block bootstrap
# =========================================================================
def daily_pnl_series(trades, window_start: datetime, window_end: datetime) -> List[float]:
    """Bucket trade net_pnl by UTC calendar day across [window_start, window_end]."""
    days = {}
    for tr in trades:
        d = datetime.fromtimestamp(tr.exit_time, tz=timezone.utc).date().isoformat()
        days[d] = days.get(d, 0.0) + tr.net_pnl
    # fill gaps with zeros
    one = timedelta(days=1)
    cur = window_start.date()
    end = window_end.date()
    out = []
    while cur <= end:
        out.append(days.get(cur.isoformat(), 0.0))
        cur += one
    return out


def bootstrap_ruin(daily_pnl: List[float], balance: float,
                   thresholds_pct=(3.0, 4.0, 5.0),
                   n_paths: int = 5000, block_mean: int = 5,
                   rng_seed: int = 42):
    """Stationary-block bootstrap of daily PnL.  Returns:
        { thresh_pct: P(max intraperiod DD crosses thresh_pct) }
    Also returns the bootstrap distribution of max DD (percentiles).
    """
    if not daily_pnl:
        return {t: 0.0 for t in thresholds_pct}, {"dd_p50": 0.0, "dd_p95": 0.0, "dd_p99": 0.0}
    rng = np.random.default_rng(rng_seed)
    arr = np.asarray(daily_pnl, dtype=float)
    n = len(arr)
    # geometric block length with mean block_mean
    max_dds = np.empty(n_paths, dtype=float)
    for k in range(n_paths):
        out = np.empty(n, dtype=float)
        i = 0
        while i < n:
            start = rng.integers(0, n)
            L = rng.geometric(1.0 / block_mean)
            for j in range(L):
                if i >= n: break
                out[i] = arr[(start + j) % n]
                i += 1
        eq = balance + np.cumsum(out)
        peak = np.maximum.accumulate(eq)
        dd_pct = ((peak - eq) / peak) * 100.0
        max_dds[k] = dd_pct.max()
    hits = {t: float((max_dds >= t).mean() * 100.0) for t in thresholds_pct}
    pct = {
        "dd_p50": float(np.percentile(max_dds, 50)),
        "dd_p95": float(np.percentile(max_dds, 95)),
        "dd_p99": float(np.percentile(max_dds, 99)),
    }
    return hits, pct


# =========================================================================
#  Main
# =========================================================================
def main():
    print("=" * 120)
    print("  BACKTEST v23 LOCKED — risk=0.110% + cap_mult=2.5 + news-flatten + news-block + HMM + daily kill")
    print("=" * 120)

    # 1. Sizer — LOCKED config (MertonGZSizerConfig has no `min_bars` field)
    sizer_cfg = MertonGZSizerConfig(
        base_risk_pct=0.00110,    # PARETO SWEET SPOT
        cap_mult=2.5,             # => hard per-trade cap = 0.275%
        gamma=2.0,                # risk aversion
    )
    print(f"  base_risk_pct     = {sizer_cfg.base_risk_pct*100:.3f} %")
    print(f"  cap_mult          = {sizer_cfg.cap_mult}")
    print(f"  hard per-trade    = {sizer_cfg.base_risk_pct*sizer_cfg.cap_mult*100:.3f} %")

    # 2. Load news events
    news_csv = ROOT / "data" / "news" / "tier1_2026.csv"
    events = load_news_events(news_csv)
    print(f"  tier-1 events loaded : {len(events)}")

    # 3. Run portfolio — raw trades + M1 streams (rails NOT applied inside)
    #    fit HMM gates inline using fit_hmm_for_symbol on each symbol's M1 stream
    print("\n  Running portfolio engine (no rails) + fitting HMMs inline ...")
    raw_trades, tmin_s, tmax_s, dropped_hmm_early, streams = run_portfolio(
        LOCKED_SYMBOLS, sizer_cfg,
        hmm_gates=None,                 # we'll HMM-gate below manually
        hmm_threshold=HMM_TREND_THRESHOLD,
    )
    print(f"  raw engine trades   : {len(raw_trades)}")
    print(f"  backtest window     : {tmin_s}  ->  {tmax_s}")
    print(f"  streams loaded      : {sorted(streams.keys())}")

    # 3b. Fit HMM per symbol + apply HMM gate
    hmm_gates: Dict[str, Dict] = {}
    for sym, bars in streams.items():
        hmm_model, trend_p = fit_hmm_for_symbol(bars)
        hmm_gates[sym] = trend_p if trend_p else {}
    fitted = [s for s, g in hmm_gates.items() if g]
    print(f"  HMMs fitted         : {len(fitted)} / {len(LOCKED_SYMBOLS)}  ({fitted})")

    dropped_hmm = 0
    hmm_trades = []
    for tr in raw_trades:
        d = datetime.fromtimestamp(tr.entry_time).date()
        p_trend = hmm_gates.get(tr.symbol, {}).get(d, 1.0)
        if p_trend < HMM_TREND_THRESHOLD:
            dropped_hmm += 1
            continue
        hmm_trades.append(tr)
    print(f"  after HMM gate      : {len(hmm_trades)}  (dropped {dropped_hmm})")

    # 4. Apply v23 rails (news flatten/block + position cap + weekend + daily kill + slippage)
    print("\n  Applying v23 safety rails ...")
    trades, audit = apply_v23_rails(hmm_trades, streams, events, slippage_ticks=1.0)
    audit["dropped_hmm"] = dropped_hmm
    print(f"  after rails         : {len(trades)}  (audit: {audit})")

    # Window datetimes for daily-PnL bucketing
    ws = datetime.fromisoformat(tmin_s)
    we = datetime.fromisoformat(tmax_s)

    # 5. Per-trade stats (stats() only takes trades)
    s = stats(trades)
    net = s["net"]
    dd_pct = s["dd_pct"]
    # count distinct entries (symbol, entry_time) for reporting
    entries_set = set((tr.symbol, tr.entry_time) for tr in trades)
    avg_bars = (sum((tr.exit_time - tr.entry_time) / 60.0 for tr in trades)
                / max(1, len(trades)))
    print(f"\n  FINAL trades        : {s['n']}  (distinct entries: {len(entries_set)})")
    print(f"  net PnL             : ${net:+,.2f}   ({net/BALANCE*100:+.2f}%)")
    print(f"  PF                  : {s['pf']:.2f}")
    print(f"  WR (partials)       : {s['wr']*100:.1f}%")
    print(f"  Sharpe (trade)      : {s['sharpe']:.2f}")
    print(f"  max static DD       : {dd_pct:.2f}%")
    print(f"  avg bars held       : {avg_bars:.1f}")


    # 8. Daily-PnL + worst day
    daily = daily_pnl_series(trades, ws, we)
    if daily:
        worst = min(daily) / BALANCE * 100.0
        days_lt_2pct = sum(1 for d in daily if d / BALANCE * 100.0 <= -2.0)
        days_lt_3pct = sum(1 for d in daily if d / BALANCE * 100.0 <= -3.0)
    else:
        worst = 0.0; days_lt_2pct = days_lt_3pct = 0
    print(f"  worst UTC day       : {worst:+.2f}%")
    print(f"  days <= -2%         : {days_lt_2pct}")
    print(f"  days <= -3%         : {days_lt_3pct}")

    # 9. Bootstrap ruin
    print("\n  Running 5000-path stationary-block bootstrap ...")
    hits, pct = bootstrap_ruin(daily, BALANCE, (3.0, 4.0, 5.0),
                                n_paths=5000, block_mean=5, rng_seed=42)
    print(f"  ruin@3% = {hits[3.0]:.2f}%   ruin@4% = {hits[4.0]:.2f}%   ruin@5% = {hits[5.0]:.2f}%")
    print(f"  DD percentiles — p50={pct['dd_p50']:.2f}%  p95={pct['dd_p95']:.2f}%  p99={pct['dd_p99']:.2f}%")

    # 10. Duration audit (HFT flag)
    durs = [tr.exit_time - tr.entry_time for tr in trades]
    sub60 = sum(1 for d in durs if d < 60)
    sub30 = sum(1 for d in durs if d < 30)
    print(f"  duration audit      : sub-30s = {sub30}   sub-60s = {sub60}")

    # 11. GATE verdict
    print("\n" + "=" * 120)
    print("  LOCK GATE (all 4 must PASS)")
    print("=" * 120)
    gates = {
        "Max DD <= 4.0%":      (dd_pct     <= 4.0),
        "Ruin@5% <= 0.5%":     (hits[5.0]  <= 0.5),
        "Net PnL >= $8k":      (net        >= 8000),
        "HFT sub-60s == 0":    (sub60      == 0),
    }
    all_ok = True
    for name, ok in gates.items():
        mark = "[OK]" if ok else "[FAIL]"
        print(f"    {mark}  {name}")
        all_ok = all_ok and ok
    print("")
    if all_ok:
        print("    ***  v23 LOCK CONFIRMED — safe to ship  ***")
    else:
        print("    !!!  v23 LOCK FAILED — review before shipping  !!!")

    # 12. Compounding projection
    m_ret = (1 + net/BALANCE) ** (1/3) - 1
    y1_ret = (1 + m_ret) ** 12 - 1
    y1_bal = BALANCE * (1 + y1_ret)
    print(f"\n  monthly compound    : {m_ret*100:+.2f}%")
    print(f"  year-1 projection   : balance ${y1_bal:,.0f}   (+{y1_ret*100:.1f}%)")

    # 13. Dump JSON
    out_json = ROOT / "Results" / "v23_locked.json"
    out_json.parent.mkdir(exist_ok=True, parents=True)
    payload = {
        "symbols": LOCKED_SYMBOLS,
        "balance": BALANCE,
        "window": [str(ws), str(we)],
        "sizer": {
            "base_risk_pct": sizer_cfg.base_risk_pct,
            "cap_mult": sizer_cfg.cap_mult,
            "max_per_trade_pct": sizer_cfg.base_risk_pct * sizer_cfg.cap_mult,
        },
        "news_events": len(events),
        "rails_audit": audit,
        "trades": s["n"],
        "entries": len(entries_set),
        "net_pnl": net,
        "pf": s["pf"],
        "wr": s["wr"],
        "sharpe": s["sharpe"],
        "max_dd_pct": dd_pct,
        "worst_day_pct": worst,
        "days_lt_2pct": days_lt_2pct,
        "days_lt_3pct": days_lt_3pct,
        "bootstrap": {"ruin": hits, "dd_percentiles": pct, "n_paths": 5000},
        "durations": {"sub_30s": sub30, "sub_60s": sub60, "total": len(durs)},
        "compound": {"monthly_pct": m_ret*100, "year1_pct": y1_ret*100, "year1_balance": y1_bal},
        "gates": {k: bool(v) for k, v in gates.items()},
        "all_ok": all_ok,
    }
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\n  Saved: {out_json}")
    print("=" * 120)


if __name__ == "__main__":
    main()
