#!/usr/bin/env python3
"""
per_symbol_autopsy_v21.py  —  which symbols carry the book, which drag it?

For each symbol we compute, using the REAL ORB engine + REAL Merton×GZ sizer:
    • N trades, WR, PF, net PnL ($ and % of balance)
    • Standalone max-DD (the DD if only this symbol traded)
    • Sharpe, avg R, expectancy
    • "drag / carrier" verdict

Then we test ADDING candidate symbols (UK100, JP225, XAGUSD) one at a time
to see whether the expanded portfolio (5 + 1) beats the 5-only portfolio on
BOTH PnL and DD.  We explicitly DO NOT test oils (x10 weekend swap risk) or
forex (different microstructure, needs its own tuning).

Output:
    Results/per_symbol_autopsy_v21.{txt,json}

PnL, DD, PF numbers below are computed at the v21 winning sizer config:
    base=0.15% · cap=3× · γ=2 · EWMA α=0.20 · warmup=15 · DD-cap=4% · pooled.
"""
from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.smartbb_engine import SMARTBB_UNIVERSE
from src.orb_engine_v20 import ORBEngineV20, ORBEngineConfig
from src.momentum.orb import ORBConfig
from src.dynamic_sizer_v21 import MertonGZSizer, MertonGZSizerConfig

BALANCE = 100_000.0
MONTHS = 3

# --- Current deployed 5 ------------------------------------------------------
CORE_5 = ["DE40", "US30", "XAUUSD", "US100", "US500"]

# --- Candidates we WILL test (cheap indices + silver) ------------------------
# We skip:
#   USOIL, XBRUSD, XTIUSD  → x10 weekend swap is prop-firm suicide
#   FOREX pairs           → $4/lot round-trip commission demands different tuning
CANDIDATES = ["UK100", "JP225", "XAGUSD"]

# --- Per-symbol ORB tunings --------------------------------------------------
# Core 5 = same tunings as Scripts/backtest_v21_mertongz.py (those are the ones
# that produced the +$14,622 / 3.36 % DD result).  Candidates use reasonable
# session-aligned defaults; they are unoptimised on purpose so we see their
# RAW edge, not a curve-fit number.
ORB_CONFIGS: Dict[str, ORBConfig] = {
    "DE40":   ORBConfig(or_start_hour=8,  or_start_minute=0,  or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=1.5,
                        tp2_range_mult=3.0, sl_buffer_range_mult=0.3),
    "US30":   ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.0),
    "XAUUSD": ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.6),
    "US100":  ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=5,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.0),
    "US500":  ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=15,
                        trade_window_minutes=120, tp1_range_mult=0.5,
                        tp2_range_mult=1.0, sl_buffer_range_mult=0.6),
    # --- Candidates (unoptimised, session-aligned defaults) ---------------
    "UK100":  ORBConfig(or_start_hour=8,  or_start_minute=0,  or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.3),
    "JP225":  ORBConfig(or_start_hour=0,  or_start_minute=0,  or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.3),
    "XAGUSD": ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.6),
}
AMP_HURDLE = {
    "DE40": 3.0, "US30": 4.5, "XAUUSD": 4.5, "US100": 4.5, "US500": 3.0,
    "UK100": 3.0, "JP225": 3.0, "XAGUSD": 4.5,
}


# ---------------------------------------------------------------------------
#  Data helpers (identical to backtest_v21_mertongz.py)
# ---------------------------------------------------------------------------
def load_m1(path, tmin, tmax):
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                t = datetime.fromisoformat(r["time"])
            except Exception:
                t = datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S")
            if tmin and t < tmin: continue
            if tmax and t > tmax: continue
            out.append((t, float(r["open"]), float(r["high"]),
                        float(r["low"]), float(r["close"])))
    return out


def common_window(files, months):
    firsts, lasts = {}, {}
    for s, p in files.items():
        with open(p) as f:
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


# ---------------------------------------------------------------------------
#  Portfolio runner — one (or many) symbols, real sizer
# ---------------------------------------------------------------------------
def run_portfolio(symbols: List[str], sizer_cfg: MertonGZSizerConfig | None,
                    lock_window: Tuple[datetime, datetime] | None = None) -> dict:
    """
    If lock_window is provided, every symbol is clipped to that exact window.
    Symbols with NO bars inside the window still create an engine (which simply
    never trades) so we can honestly report "0 trades due to no data overlap".

    If lock_window is None we compute the 3-month common window across the
    *requested* symbols (original behaviour, correct for solo / baseline runs).
    """
    data = ROOT / "data" / "historical"
    files = {s: data / f"{s}_M1.csv" for s in symbols
             if (data / f"{s}_M1.csv").exists()}
    if not files:
        raise RuntimeError(f"no data for {symbols}")

    if lock_window is not None:
        tmin, tmax = lock_window
    else:
        tmin, tmax = common_window(files, MONTHS)

    specs = {s: SMARTBB_UNIVERSE[s] for s in files}
    streams = {s: load_m1(files[s], tmin, tmax) for s in files}

    sizer = MertonGZSizer(sizer_cfg) if sizer_cfg else None
    engines: Dict[str, ORBEngineV20] = {}
    shared = {"val": BALANCE, "peak": BALANCE}

    def risk_fn(sym, equity, peak, open_pos):
        return sizer.compute_risk_pct(sym, shared["val"], shared["peak"], open_pos)

    for sym in files:
        if sizer:
            cfg = ORBEngineConfig(risk_pct=0.0010, amp_hurdle=AMP_HURDLE[sym],
                                   require_nr7=False, nr_lookback=7,
                                   trail_atr_mult=0.8,
                                   tp1_close_frac=0.50, tp2_close_frac=0.25,
                                   hurst_min=0.0, hurst_max=1.0, hurst_window=200,
                                   risk_pct_fn=risk_fn)
        else:
            cfg = ORBEngineConfig(risk_pct=0.0025, amp_hurdle=AMP_HURDLE[sym],
                                   require_nr7=False, nr_lookback=7,
                                   trail_atr_mult=0.8,
                                   tp1_close_frac=0.50, tp2_close_frac=0.25,
                                   hurst_min=0.0, hurst_max=1.0, hurst_window=200)
        engines[sym] = ORBEngineV20(
            symbols=[specs[sym]], cfg=cfg,
            orb_configs={sym: ORB_CONFIGS[sym]},
            initial_equity=BALANCE,
        )

    # Merge bars chronologically
    allb = []
    for s, bars in streams.items():
        allb.extend((t, s, o, h, l, c) for (t, o, h, l, c) in bars)
    allb.sort(key=lambda r: r[0])

    last_n = {s: 0 for s in files}
    pending: Dict[Tuple[str, float], Dict[str, float]] = {}
    fed: set = set()

    def flush(sym):
        if sizer is None: return
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
                if sizer is not None:
                    k = (tr.symbol, tr.entry_time)
                    pending.setdefault(k, {"r_sum": 0.0})
                    pending[k]["r_sum"] += tr.realised_R
            last_n[s] = len(eng.trades)
            flush(s)
    if sizer is not None:
        for sym in files:
            flush(sym)

    # Gather everything
    all_trades = []
    for eng in engines.values():
        all_trades.extend(eng.trades)
    all_trades.sort(key=lambda tr: tr.entry_time)

    # Aggregate equity curve
    eq = BALANCE; peak = BALANCE; mdd = 0.0
    pnls = []
    for tr in all_trades:
        pnls.append(tr.net_pnl)
        eq += tr.net_pnl
        if eq > peak: peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > mdd: mdd = dd

    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw = sum(wins); gl = -sum(losses)
    pf = gw / gl if gl > 0 else float("inf")
    net = sum(pnls)
    sharpe = (statistics.mean(pnls) / statistics.pstdev(pnls)) * math.sqrt(len(pnls)) \
             if len(pnls) > 1 and statistics.pstdev(pnls) > 0 else 0.0

    # --- Per-symbol breakdown of THIS portfolio --------------------------
    by_sym: Dict[str, List[float]] = {s: [] for s in files}
    for tr in all_trades:
        by_sym.setdefault(tr.symbol, []).append(tr.net_pnl)

    per_symbol = {}
    for sym, ps in by_sym.items():
        if not ps:
            per_symbol[sym] = {"n": 0, "pnl": 0.0, "wr": 0.0, "pf": 0.0,
                                "sharpe": 0.0, "solo_dd_pct": 0.0}
            continue
        w = [x for x in ps if x > 0]
        l = [x for x in ps if x <= 0]
        pf_s = sum(w) / (-sum(l)) if l and sum(l) < 0 else (99.0 if w else 0.0)
        sh = (statistics.mean(ps) / statistics.pstdev(ps)) * math.sqrt(len(ps)) \
             if len(ps) > 1 and statistics.pstdev(ps) > 0 else 0.0
        # Standalone DD — what the DD looks like if ONLY this symbol traded
        e = BALANCE; p = BALANCE; m = 0.0
        for x in ps:
            e += x
            if e > p: p = e
            d = (p - e) / p if p > 0 else 0.0
            if d > m: m = d
        per_symbol[sym] = {
            "n": len(ps),
            "pnl": sum(ps),
            "wr": len(w) / len(ps),
            "pf": pf_s,
            "sharpe": sh,
            "solo_dd_pct": m * 100,
        }

    return {
        "symbols": list(files.keys()),
        "n": len(all_trades),
        "net_pnl": net,
        "return_pct": net / BALANCE * 100,
        "max_dd_pct": mdd * 100,
        "pf": pf if math.isfinite(pf) else 99.0,
        "wr": len(wins) / max(1, len(pnls)),
        "sharpe": sharpe,
        "per_symbol": per_symbol,
        "window": (str(tmin), str(tmax)),
    }


def v21_winning_sizer() -> MertonGZSizerConfig:
    return MertonGZSizerConfig(
        base_risk_pct=0.0015, cap_mult=3.0, gamma=2.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------
def main():
    out = []
    p = lambda m="": (print(m), out.append(m))

    p("=" * 100)
    p("  PER-SYMBOL AUTOPSY — v21 Merton×GZ")
    p("  (3-month window, base=0.15 %, cap=3×, pooled)")
    p("=" * 100)

    # ----- PART 1: Full 5-sym portfolio, per-symbol breakdown ---------------
    p("\n  PART 1 / Current 5 symbols — per-symbol contribution in the live portfolio")
    p("  " + "-" * 96)
    r_full = run_portfolio(CORE_5, v21_winning_sizer())
    p(f"  Window : {r_full['window'][0]}  →  {r_full['window'][1]}")
    p(f"  Portfolio total : N={r_full['n']}  PnL=${r_full['net_pnl']:+,.0f}  "
      f"DD={r_full['max_dd_pct']:.2f}%  PF={r_full['pf']:.2f}  "
      f"Sharpe={r_full['sharpe']:.2f}")
    p("")
    p(f"    {'Symbol':<8} {'N':>4} {'PnL $':>10} {'Share%':>7} {'WR':>6} "
      f"{'PF':>5} {'Sharpe':>6} {'SoloDD%':>8}  Verdict")
    p("    " + "-" * 88)
    tot = max(1.0, sum(v["pnl"] for v in r_full["per_symbol"].values()))
    drags, carriers = [], []
    for sym in CORE_5:
        ps = r_full["per_symbol"].get(sym, {})
        share = ps.get("pnl", 0) / tot * 100
        verdict = ("CARRIER" if ps.get("pnl", 0) > 500 and ps.get("pf", 0) >= 1.2
                   else "MARGINAL" if ps.get("pnl", 0) > 0
                   else "DRAG"    if ps.get("pnl", 0) < -200
                   else "NEUTRAL")
        if verdict == "CARRIER": carriers.append(sym)
        if verdict == "DRAG":    drags.append(sym)
        p(f"    {sym:<8} {ps.get('n',0):>4} "
          f"{ps.get('pnl',0):>+10,.0f} "
          f"{share:>+6.1f}% "
          f"{ps.get('wr',0)*100:>5.1f}% "
          f"{ps.get('pf',0):>5.2f} "
          f"{ps.get('sharpe',0):>+6.2f} "
          f"{ps.get('solo_dd_pct',0):>7.2f}%  {verdict}")

    p("")
    p(f"  → Carriers  : {carriers or '(none)'}")
    p(f"  → Drags     : {drags    or '(none)'}")

    # ----- PART 2: Solo run per symbol (pure per-symbol edge) ---------------
    p("\n")
    p("  PART 2 / Solo standalone PnL (one symbol at a time, base risk only)")
    p("  Purpose  : strip portfolio interactions — pure edge of each instrument")
    p("  " + "-" * 96)
    p(f"    {'Symbol':<8} {'N':>4} {'PnL $':>10} {'DD%':>6} {'WR':>6} "
      f"{'PF':>5} {'Sharpe':>6}  Verdict")
    p("    " + "-" * 72)
    solo_rows = {}
    for sym in CORE_5 + CANDIDATES:
        # Solo runs use a fresh sizer (independent warmup per symbol)
        r = run_portfolio([sym], v21_winning_sizer())
        ps = r["per_symbol"].get(sym, {})
        verdict = ("KEEP / ADD" if r["net_pnl"] > 500 and r["max_dd_pct"] <= 4.0 and r["pf"] >= 1.2
                   else "MARGINAL" if r["net_pnl"] > 0
                   else "SKIP")
        solo_rows[sym] = {**r, "verdict": verdict}
        p(f"    {sym:<8} {r['n']:>4} "
          f"{r['net_pnl']:>+10,.0f} "
          f"{r['max_dd_pct']:>5.2f}% "
          f"{r['wr']*100:>5.1f}% "
          f"{r['pf']:>5.2f} "
          f"{r['sharpe']:>+6.2f}  {verdict}")

    # ----- PART 3: Add each candidate to the core-5 portfolio ---------------
    # CRITICAL: we LOCK the window to the core-5 window so adding a candidate
    # doesn't shrink the test period (UK100/JP225/XAGUSD have different history).
    core5_window = (datetime.fromisoformat(r_full["window"][0]),
                    datetime.fromisoformat(r_full["window"][1]))
    p("\n")
    p("  PART 3 / Portfolio expansion — does adding each candidate HELP or HURT?")
    p("           Window LOCKED to core-5 range: "
      f"{core5_window[0].date()}  →  {core5_window[1].date()}")
    p("  " + "-" * 96)
    p(f"    {'Portfolio':<24} {'N':>4} {'PnL $':>10} {'Ret%':>7} "
      f"{'DD%':>6} {'PF':>5} {'Sharpe':>6}  Note")
    p("    " + "-" * 82)
    base_pnl = r_full["net_pnl"]; base_dd = r_full["max_dd_pct"]
    p(f"    {'5-only (baseline)':<24} {r_full['n']:>4} "
      f"{r_full['net_pnl']:>+10,.0f} {r_full['return_pct']:>+6.2f}% "
      f"{r_full['max_dd_pct']:>5.2f}% {r_full['pf']:>5.2f} "
      f"{r_full['sharpe']:>+6.2f}  —")
    add_rows = {}
    for cand in CANDIDATES:
        r = run_portfolio(CORE_5 + [cand], v21_winning_sizer(),
                          lock_window=core5_window)
        d_pnl = r["net_pnl"] - base_pnl
        d_dd = r["max_dd_pct"] - base_dd
        add_rows[cand] = {"result": r, "delta_pnl": d_pnl, "delta_dd": d_dd}
        cand_n = r["per_symbol"].get(cand, {}).get("n", 0)
        note = ("NO DATA OVERLAP" if cand_n == 0 else
                f"{cand} trades={cand_n} ΔP ${d_pnl:+,.0f} ΔDD {d_dd:+.2f}pp")
        label = f"5 + {cand}"
        p(f"    {label:<24} {r['n']:>4} "
          f"{r['net_pnl']:>+10,.0f} {r['return_pct']:>+6.2f}% "
          f"{r['max_dd_pct']:>5.2f}% {r['pf']:>5.2f} "
          f"{r['sharpe']:>+6.2f}  {note}")

    # ----- PART 3b: Candidates on their NATIVE window (where data exists) --
    p("\n")
    p("  PART 3b / Candidate standalone edge on their NATIVE data window")
    p("             (no core-5 data available here; shows raw instrument edge only)")
    p("  " + "-" * 96)
    p(f"    {'Symbol':<8} {'Native Window':<22} {'N':>4} {'PnL $':>10} "
      f"{'DD%':>6} {'PF':>5} {'Sharpe':>6}  Verdict")
    p("    " + "-" * 82)
    native_rows = {}
    for cand in CANDIDATES:
        # Use common_window on the symbol alone (its 3-month native slice)
        r = run_portfolio([cand], v21_winning_sizer())
        verdict = ("HAS EDGE" if r["net_pnl"] > 500 and r["pf"] >= 1.2
                   else "MARGINAL" if r["net_pnl"] > 0
                   else "NO EDGE")
        w0, w1 = str(r["window"][0])[:10], str(r["window"][1])[:10]
        native_rows[cand] = r
        p(f"    {cand:<8} {w0+' → '+w1:<22} {r['n']:>4} "
          f"{r['net_pnl']:>+10,.0f} {r['max_dd_pct']:>5.2f}% "
          f"{r['pf']:>5.2f} {r['sharpe']:>+6.2f}  {verdict}")

    # ----- PART 4: Test removing DRAGS or MARGINAL symbols ------------------
    p("\n")
    p("  PART 4 / Pruning test — does REMOVING weak symbols HELP?")
    p("  " + "-" * 96)
    # Identify marginals (positive but PF<1.2 or solo_dd>=2%)
    marginals = []
    for sym in CORE_5:
        ps = r_full["per_symbol"].get(sym, {})
        if (0 < ps.get("pnl", 0) < 2000) and (ps.get("pf", 0) < 1.3
                                                or ps.get("solo_dd_pct", 0) >= 2.0):
            marginals.append(sym)

    prune_results = []
    p(f"    {'Portfolio':<32} {'N':>4} {'PnL $':>10} {'Ret%':>7} "
      f"{'DD%':>6} {'PF':>5} {'Sharpe':>6}  Δ vs 5")
    p("    " + "-" * 86)
    p(f"    {'5-only (baseline)':<32} {r_full['n']:>4} "
      f"{r_full['net_pnl']:>+10,.0f} {r_full['return_pct']:>+6.2f}% "
      f"{r_full['max_dd_pct']:>5.2f}% {r_full['pf']:>5.2f} "
      f"{r_full['sharpe']:>+6.2f}  —")

    # Drop each drag/marginal individually, then all at once
    prune_candidates = drags + marginals
    for sym in prune_candidates:
        leaner = [s for s in CORE_5 if s != sym]
        r = run_portfolio(leaner, v21_winning_sizer(), lock_window=core5_window)
        d_pnl = r["net_pnl"] - base_pnl; d_dd = r["max_dd_pct"] - base_dd
        prune_results.append((sym, r, d_pnl, d_dd))
        p(f"    {'Drop '+sym:<32} {r['n']:>4} "
          f"{r['net_pnl']:>+10,.0f} {r['return_pct']:>+6.2f}% "
          f"{r['max_dd_pct']:>5.2f}% {r['pf']:>5.2f} "
          f"{r['sharpe']:>+6.2f}  ΔP ${d_pnl:+,.0f} ΔDD {d_dd:+.2f}pp")
    if len(prune_candidates) > 1:
        leaner = [s for s in CORE_5 if s not in prune_candidates]
        r = run_portfolio(leaner, v21_winning_sizer(), lock_window=core5_window)
        d_pnl = r["net_pnl"] - base_pnl; d_dd = r["max_dd_pct"] - base_dd
        p(f"    {'Drop all marginals':<32} {r['n']:>4} "
          f"{r['net_pnl']:>+10,.0f} {r['return_pct']:>+6.2f}% "
          f"{r['max_dd_pct']:>5.2f}% {r['pf']:>5.2f} "
          f"{r['sharpe']:>+6.2f}  ΔP ${d_pnl:+,.0f} ΔDD {d_dd:+.2f}pp")

    # ----- PART 5: Final recommendation -------------------------------------
    p("\n")
    p("=" * 100)
    p("  RECOMMENDATION")
    p("=" * 100)

    keep_symbols = list(CORE_5)
    if drags:
        keep_symbols = [s for s in keep_symbols if s not in drags]

    add_good = []
    for cand, row in add_rows.items():
        r = row["result"]
        if (row["delta_pnl"] > 500 and r["max_dd_pct"] <= 4.0
            and row["delta_dd"] <= 0.5):
            add_good.append(cand)

    final = keep_symbols + add_good
    r_final = run_portfolio(final, v21_winning_sizer()) if set(final) != set(CORE_5) else r_full

    p(f"  Keep         : {keep_symbols}")
    p(f"  Drop (drag)  : {drags or '(none)'}")
    p(f"  Add (helpful): {add_good or '(none — candidates did not clear the bar)'}")
    p(f"")
    p(f"  Final universe : {final}")
    p(f"    N={r_final['n']}  PnL=${r_final['net_pnl']:+,.0f} ({r_final['return_pct']:+.2f}%)")
    p(f"    DD={r_final['max_dd_pct']:.2f}%  PF={r_final['pf']:.2f}  "
      f"Sharpe={r_final['sharpe']:.2f}")
    pass_dd = "✅" if r_final['max_dd_pct'] <= 4.0 else "❌"
    p(f"    4% DD gate   : {pass_dd}")
    p("")
    p("  Excluded without testing (PROP-FIRM HAZARDS, not re-evaluated here):")
    p("    USOIL, XBRUSD, XTIUSD   — x10 weekend swap (unsafe over Fri→Mon)")
    p("    All FX pairs           — $4/lot round-trip commission needs its own tuning")

    p("=" * 100)

    (ROOT / "Results").mkdir(exist_ok=True)
    with open(ROOT / "Results" / "per_symbol_autopsy_v21.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    with open(ROOT / "Results" / "per_symbol_autopsy_v21.json", "w") as f:
        json.dump({
            "window": r_full["window"],
            "core_5_portfolio": r_full,
            "solo": solo_rows,
            "expansion": {k: {"net_pnl": v["result"]["net_pnl"],
                              "max_dd_pct": v["result"]["max_dd_pct"],
                              "delta_pnl": v["delta_pnl"],
                              "delta_dd": v["delta_dd"]}
                           for k, v in add_rows.items()},
            "recommended_universe": final,
            "recommended_portfolio": r_final,
        }, f, indent=2, default=str)
    p(f"\n  Saved: Results/per_symbol_autopsy_v21.txt + .json")


if __name__ == "__main__":
    main()
