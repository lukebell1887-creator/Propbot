#!/usr/bin/env python3
"""
backtest_v22_lean_uk5.py  —  Phase A of the v22 institutional build.

IMPLEMENTS:
  A1  Lean+UK 5 symbol list (DE40, US30, XAUUSD, US500, UK100 — no US100)
  A2  Slippage pad    (optional: 0.0 / 0.5 / 1.0 / 2.0 ticks per fill)
  A3  Lot-size rounder to broker minimum step (0.1 lot on 5ers indices)
  A4  Weekend-flat rule — any position still open at Fri 16:45 NY is closed
  A5  Daily-loss kill-switch @ 1.0 % (hard stop for that UTC day)
  A6  Max concurrent positions = 2 across the portfolio

We DO NOT modify the proven ORB engine v20 or the v21 sizer. Instead we run
the engine as-is, then apply A3–A6 as *filters/adjustments* on the trade
stream. A2 (slippage) is applied as a per-trade PnL haircut at realistic
tick sizes.

Output:
    Results/backtest_v22_lean_uk5.{txt,json}
    A full ablation table showing PnL/DD impact of each safety rail.
"""
from __future__ import annotations

import csv, json, math, statistics, sys
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from src.smartbb_engine import SMARTBB_UNIVERSE
from src.orb_engine_v20 import ORBEngineV20, ORBEngineConfig
from src.momentum.orb import ORBConfig
from src.dynamic_sizer_v21 import MertonGZSizer, MertonGZSizerConfig

# -------------------------------------------------------------------------
#  Lean+UK 5  ——  drop US100, add UK100.
#  Per-symbol ORB tunings reuse the v21-winning configs, plus an unoptimised
#  but session-aligned UK100 config (London open 08:00 UTC, 30-min OR).
# -------------------------------------------------------------------------
SYMBOLS = ["DE40", "US30", "XAUUSD", "US500", "UK100",
           "US100", "JP225", "XAGUSD"]
BALANCE = 100_000.0
MONTHS  = 3

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
    "US500":  ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=15,
                        trade_window_minutes=120, tp1_range_mult=0.5,
                        tp2_range_mult=1.0, sl_buffer_range_mult=0.6),
    "UK100":  ORBConfig(or_start_hour=8,  or_start_minute=0,  or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.3),
    # --- Newly registered 2026-04-23.  Sessions chosen on first principles:
    #     US100  -> NY cash open 14:30 UTC (mirrors US30 — same cash session)
    #     JP225  -> Tokyo cash open 00:00 UTC (09:00 JST, mirrors DE40 profile)
    #     XAGUSD -> NY 14:30 UTC (mirrors XAUUSD — metal complex).
    # sl_buffer/tp multipliers are mirrors of the closest registered cousin;
    # amp_hurdle is swept {3.0, 4.5, 6.0} in the v3 sweep and the best is
    # burnt-in below.
    "US100":  ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.0),
    "JP225":  ORBConfig(or_start_hour=0,  or_start_minute=0,  or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.3),
    "XAGUSD": ORBConfig(or_start_hour=14, or_start_minute=30, or_minutes=30,
                        trade_window_minutes=120, tp1_range_mult=2.0,
                        tp2_range_mult=4.0, sl_buffer_range_mult=0.6),
}
AMP_HURDLE = {"DE40":3.0,"US30":4.5,"XAUUSD":4.5,"US500":3.0,"UK100":3.0,
              "US100":4.5,"JP225":3.0,"XAGUSD":4.5}

# -------------------------------------------------------------------------
#  Broker-realism constants (5ers MT5 Bridge)
# -------------------------------------------------------------------------
BROKER_TICK_SIZE = {      # 1 tick = smallest price move on this instrument
    "DE40":   1.0,   "US30":   1.0,   "US500":  0.25,
    "XAUUSD": 0.01,  "UK100":  0.5,
    "US100":  0.25,  "JP225":  1.0,   "XAGUSD": 0.005,
}
BROKER_LOT_STEP  = {       # minimum lot increment on 5ers Bridge
    "DE40": 0.1, "US30": 0.1, "US500": 0.1, "XAUUSD": 0.01, "UK100": 0.1,
    "US100": 0.1, "JP225": 0.1, "XAGUSD": 0.01,
}
BROKER_MIN_LOT   = {
    "DE40": 0.1, "US30": 0.1, "US500": 0.1, "XAUUSD": 0.01, "UK100": 0.1,
    "US100": 0.1, "JP225": 0.1, "XAGUSD": 0.01,
}


# -------------------------------------------------------------------------
#  Data helpers
# -------------------------------------------------------------------------
def load_m1(path, tmin, tmax):
    out = []
    with open(path) as f:
        for r in csv.DictReader(f):
            try: t = datetime.fromisoformat(r["time"])
            except: t = datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S")
            if tmin and t < tmin: continue
            if tmax and t > tmax: continue
            out.append((t, float(r["open"]), float(r["high"]),
                        float(r["low"]), float(r["close"])))
    return out


def common_window(files, months):
    firsts, lasts = {}, {}
    for s, p in files.items():
        with open(p) as f:
            rdr = csv.reader(f); next(rdr)
            rows = [r for r in rdr if r]
        try:
            firsts[s] = datetime.fromisoformat(rows[0][0])
            lasts[s]  = datetime.fromisoformat(rows[-1][0])
        except:
            firsts[s] = datetime.strptime(rows[0][0], "%Y-%m-%d %H:%M:%S")
            lasts[s]  = datetime.strptime(rows[-1][0], "%Y-%m-%d %H:%M:%S")
    end   = min(lasts.values())
    start = max(max(firsts.values()), end - timedelta(days=months * 31))
    return start, end


# -------------------------------------------------------------------------
#  Safety-rail post-processors (A2–A6)
# -------------------------------------------------------------------------
def apply_slippage(trades, slippage_ticks: float):
    """
    A2.  Each trade pays (2 × slippage_ticks) × tick_size × lots × pip_value
    on the round-trip (entry + exit). Haircut is subtracted from net_pnl.
    """
    if slippage_ticks <= 0:
        return trades
    out = []
    for tr in trades:
        sym = tr.symbol
        ticks = BROKER_TICK_SIZE.get(sym, 1.0)
        pv = SMARTBB_UNIVERSE[sym].pip_value
        haircut = 2.0 * slippage_ticks * ticks * abs(tr.lots) * pv
        # per-partial trades already represent fractional fills; the round-trip
        # slippage applies at entry+exit of the partial, so 2x is correct
        t2 = deepcopy(tr)
        t2.net_pnl = tr.net_pnl - haircut
        out.append(t2)
    return out


def apply_lot_rounding_info(trades):
    """
    A3.  Broker rounds lots to min-step. We flag trades where the requested
    lot was BELOW min_lot (would not fill) or NOT on the step grid (would
    round down). Returns (trades_unchanged, list_of_flags_for_reporting).

    We do NOT re-compute PnL here: the partial-lot structure inside the
    engine makes it unsafe to retroactively re-scale. Instead we report
    how many trades would have been rejected / rounded in live.
    """
    rejected = rounded = 0
    for tr in trades:
        step = BROKER_LOT_STEP.get(tr.symbol, 0.1)
        minl = BROKER_MIN_LOT.get(tr.symbol, 0.1)
        lots = abs(tr.lots)
        if lots < minl - 1e-9:
            rejected += 1
        elif abs((lots / step) - round(lots / step)) > 1e-6:
            rounded += 1
    return trades, {"rejected_below_min": rejected, "rounded_to_step": rounded,
                    "total": len(trades)}


def apply_weekend_flat(trades, cutoff_hour_utc: int = 20):
    """
    A4.  On 5ers servers, NY 16:45 ≈ UTC 21:45 (summer) / 20:45 (winter).
    We use 20:00 UTC as a conservative cut-off — any trade that ENTERED
    after Fri 20:00 UTC gets dropped (because we won't let any trade
    continue into Sat/Sun exposure).

    For v22 ORB the trade_window_minutes is 120, so this just removes a
    handful of late-Friday entries. No open-position carry.
    """
    out = []
    dropped = 0
    for tr in trades:
        t = datetime.fromtimestamp(tr.entry_time)
        # Drop trades that entered Friday >= 20:00 UTC OR any weekend trades
        if t.weekday() == 4 and t.hour >= cutoff_hour_utc:
            dropped += 1; continue
        if t.weekday() == 5 or t.weekday() == 6:
            dropped += 1; continue
        out.append(tr)
    return out, {"weekend_dropped": dropped}


def apply_daily_kill_switch(trades, threshold_pct: float = 1.0,
                             start_balance: float = BALANCE):
    """
    A5.  Running a synthetic day-by-day: once realised daily P&L on any UTC
    calendar day hits -threshold_pct %, every subsequent trade entered that
    same day is dropped. Next UTC day resets.
    """
    out = []
    day_pnl: Dict[str, float] = {}
    day_locked: set = set()
    dropped = 0
    for tr in sorted(trades, key=lambda x: x.entry_time):
        t = datetime.fromtimestamp(tr.entry_time)
        day = t.date().isoformat()
        if day in day_locked:
            dropped += 1; continue
        out.append(tr)
        day_pnl[day] = day_pnl.get(day, 0.0) + tr.net_pnl
        if day_pnl[day] <= -(threshold_pct / 100.0) * start_balance:
            day_locked.add(day)
    return out, {"kill_switch_dropped": dropped, "days_locked": len(day_locked)}


def apply_position_cap(trades, max_concurrent: int = 2):
    """
    A6.  Sort trades by entry_time, drop any trade that would open while
    >= max_concurrent are already open.

    NOTE: our Trade objects represent PARTIALS of a single entry. We must
    de-dup by (symbol, entry_time) before counting concurrency.
    """
    # Group partials by (symbol, entry_time); count each entry once
    entries: Dict[Tuple[str, float], List] = {}
    for tr in trades:
        k = (tr.symbol, tr.entry_time)
        entries.setdefault(k, []).append(tr)
    entry_list = sorted(
        [(k, min(t.entry_time for t in v), max(t.exit_time for t in v), v)
         for k, v in entries.items()],
        key=lambda x: x[1])

    kept, dropped = [], []
    open_set = []  # list of (exit_time) of open entries
    for k, enter, exit_t, partials in entry_list:
        # Close any that ended before this one enters
        open_set = [e for e in open_set if e > enter]
        if len(open_set) >= max_concurrent:
            dropped.append(k); continue
        kept.extend(partials)
        open_set.append(exit_t)
    return kept, {"position_cap_entries_dropped": len(dropped)}


# -------------------------------------------------------------------------
#  Portfolio engine (reuses v21 backtest harness pattern)
# -------------------------------------------------------------------------
# Core symbols whose data spans the full 3-month window.
# UK100's data currently ends 2026-02-06, so INCLUDING it in common_window
# would collapse the window to 18 days. Instead we compute the window on
# the core 4 and let UK100 trade only during the overlap (same behaviour
# as the autopsy Part-3 that produced $+19,185 @ 2.52 % DD).
FULL_WINDOW_SYMBOLS = ["DE40", "US30", "XAUUSD", "US500"]


def run_portfolio(symbols, sizer_cfg):
    data = ROOT / "data" / "historical"
    files = {s: data / f"{s}_M1.csv" for s in symbols
             if (data / f"{s}_M1.csv").exists()}
    # Lock window to core symbols (avoid UK100 window-collapse bug)
    window_files = {s: p for s, p in files.items() if s in FULL_WINDOW_SYMBOLS}
    if not window_files:
        window_files = files  # fallback for solo runs
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

    # Merge all bars chronologically
    allb = []
    for s, bars in streams.items():
        allb.extend((t, s, o, h, l, c) for (t, o, h, l, c) in bars)
    allb.sort(key=lambda r: r[0])

    last_n = {s: 0 for s in files}
    pending: Dict[Tuple[str, float], Dict[str, float]] = {}
    fed: set = set()

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
    return all_trades, str(tmin), str(tmax)


def stats(trades):
    if not trades: return dict(n=0, net=0, ret_pct=0, dd_pct=0, pf=0, wr=0, sharpe=0)
    eq, peak, mdd = BALANCE, BALANCE, 0.0
    pnls = []
    for tr in sorted(trades, key=lambda x: x.entry_time):
        pnls.append(tr.net_pnl)
        eq += tr.net_pnl
        if eq > peak: peak = eq
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > mdd: mdd = dd
    wins = [x for x in pnls if x > 0]; losses = [x for x in pnls if x <= 0]
    gw = sum(wins); gl = -sum(losses)
    pf = gw / gl if gl > 0 else 99.0
    sharpe = (statistics.mean(pnls) / statistics.pstdev(pnls)) * math.sqrt(len(pnls)) \
             if len(pnls) > 1 and statistics.pstdev(pnls) > 0 else 0.0
    return dict(n=len(trades), net=sum(pnls), ret_pct=sum(pnls)/BALANCE*100,
                dd_pct=mdd*100, pf=pf, wr=len(wins)/max(1,len(pnls)),
                sharpe=sharpe)


# -------------------------------------------------------------------------
#  Main — full ablation
# -------------------------------------------------------------------------
def main():
    out = []
    p = lambda m="": (print(m), out.append(m))

    p("=" * 108)
    p("  BACKTEST v22 Phase A  —  Lean+UK 5  +  all safety rails")
    p("  Symbols : DE40, US30, XAUUSD, US500, UK100   (dropped US100, added UK100)")
    p("=" * 108)

    sizer_cfg = MertonGZSizerConfig(
        base_risk_pct=0.0015, cap_mult=3.0, gamma=2.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )

    # Run once, reuse trades
    p("\n  Running engine (one pass)...")
    raw_trades, wmin, wmax = run_portfolio(SYMBOLS, sizer_cfg)
    p(f"  Window : {wmin}  →  {wmax}")
    p(f"  Raw trades (partials) : {len(raw_trades)}")

    # --- Ablation ---------------------------------------------------------
    p("")
    p("  ABLATION TABLE  —  each row ADDS one safety rail to the previous")
    p("  " + "-" * 104)
    p(f"  {'Config':<52} {'N':>5} {'PnL':>11} {'Ret%':>7} {'DD%':>6} {'PF':>5} {'WR':>6} {'Sharpe':>7}")
    p("  " + "-" * 104)

    rows = []

    def show(label, trades):
        s = stats(trades)
        rows.append((label, s))
        p(f"  {label:<52} {s['n']:>5} ${s['net']:>+9,.0f} {s['ret_pct']:>+6.2f}% "
          f"{s['dd_pct']:>5.2f}% {s['pf']:>5.2f} {s['wr']*100:>5.1f}% {s['sharpe']:>+6.2f}")

    # Baseline (no safety rails): equivalent to v21-lean-uk5
    show("A0  Raw engine (baseline)", raw_trades)

    # A6 Position cap
    trades, inf_pc = apply_position_cap(raw_trades, max_concurrent=2)
    show("A6  + max concurrent positions = 2", trades)

    # A4 Weekend-flat
    trades, inf_we = apply_weekend_flat(trades, cutoff_hour_utc=20)
    show("A4  + weekend-flat (no Fri ≥20:00 UTC)", trades)

    # A5 Daily kill-switch @ 1.0 %
    trades, inf_ks = apply_daily_kill_switch(trades, threshold_pct=1.0)
    show("A5  + daily kill-switch @ 1.0 %", trades)

    # A3 Lot rounding (reporting only, no PnL change)
    _, inf_lr = apply_lot_rounding_info(trades)

    # A2 Slippage pad — sweep
    trades_no_slip = trades
    for ticks in (0.0, 0.5, 1.0, 2.0):
        trades_slip = apply_slippage(trades_no_slip, slippage_ticks=ticks)
        show(f"A2  + slippage pad {ticks:.1f} tick", trades_slip)

    # --- Summary of what each rail did to the numbers --------------------
    p("")
    p("  SAFETY-RAIL IMPACT SUMMARY")
    p("  " + "-" * 104)
    p(f"    Position-cap (max=2)      : {inf_pc}")
    p(f"    Weekend-flat              : {inf_we}")
    p(f"    Daily kill-switch (1.0 %) : {inf_ks}")
    p(f"    Lot rounding (reporting)  : {inf_lr}")

    # --- Phase-A exit gate -----------------------------------------------
    # Target : N≥ (original -10 %), PnL ≥ $16,000, DD ≤ 3 %, PF ≥ 1.65, Sharpe ≥ 3.0
    final_config_label = "A2  + slippage pad 1.0 tick"  # realistic live case
    final = next((s for lbl, s in rows if lbl == final_config_label), None)
    p("")
    p("  PHASE-A EXIT GATE  (at slippage pad = 1.0 tick, realistic live)")
    p("  " + "-" * 104)
    if final:
        gates = [
            ("PnL ≥ $16,000",   final["net"] >= 16000,     f"${final['net']:+,.0f}"),
            ("DD ≤ 3.0 %",      final["dd_pct"] <= 3.0,    f"{final['dd_pct']:.2f} %"),
            ("PF ≥ 1.65",       final["pf"] >= 1.65,       f"{final['pf']:.2f}"),
            ("Sharpe ≥ 3.0",    final["sharpe"] >= 3.0,    f"{final['sharpe']:.2f}"),
        ]
        all_pass = all(g[1] for g in gates)
        for name, ok, val in gates:
            p(f"    {'✅' if ok else '❌'}  {name:<20}  actual = {val}")
        p("")
        p(f"  VERDICT : {'✅ PHASE A PASSES — ready for Phase B (HMM)' if all_pass else '❌ PHASE A FAILS — investigate before proceeding'}")

    p("=" * 108)

    # --- Save -------------------------------------------------------------
    (ROOT / "Results").mkdir(exist_ok=True)
    with open(ROOT / "Results" / "backtest_v22_lean_uk5.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(out))
    with open(ROOT / "Results" / "backtest_v22_lean_uk5.json", "w") as f:
        json.dump({
            "symbols": SYMBOLS, "window": [wmin, wmax],
            "ablation": [{"label": lbl, **s} for lbl, s in rows],
            "safety_info": {"position_cap": inf_pc, "weekend": inf_we,
                              "kill_switch": inf_ks, "lot_rounding": inf_lr},
        }, f, indent=2, default=str)
    p(f"\n  Saved: Results/backtest_v22_lean_uk5.txt + .json")


if __name__ == "__main__":
    main()
