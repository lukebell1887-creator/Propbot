#!/usr/bin/env python3
"""
backtest_v19_honest.py  —  side-by-side honesty test

Runs three configurations of the v18 engine on the same 3-month 5%ers feed:

  1. CONTROL        : current v18 default
                      sl_mode = "bb_floored" + v18.1 safety guard
                      same-bar intrabar exits ALLOWED

  2. REV_PROPER     : reversion_proper SL/TP, same-bar allowed
                      -> measures "packaging fix only" impact

  3. REV_PROPER_NSB : reversion_proper SL/TP, NO same-bar exits
                      -> HONEST: the "would-this-survive-live?" test

For each, report: trades, net PnL, win rate, profit factor, max DD,
average bars held, and exit-reason breakdown.

Uses the same data / params / sizer as Scripts/backtest_v18.py.
"""
from __future__ import annotations

import csv
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

from src.live.v15_live import load_v15_params                        # noqa: E402
from src.smartbb_engine import SMARTBB_UNIVERSE                      # noqa: E402
from src.smartbb_engine_v14 import SmartBBV14Config                  # noqa: E402
from src.smartbb_engine_v18 import SmartBBV18Engine                  # noqa: E402
from src.dynamic_sizer_v18 import DynamicSizerV18, SizerV18Config    # noqa: E402
from src.trading_calendar import TradingCalendar                     # noqa: E402

TIER1 = ["US30", "US100", "US500", "DE40", "XAUUSD"]


def load_m1(path, tmin, tmax):
    out = []
    with open(path, "r", newline="") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            try:
                t = datetime.fromisoformat(row["time"])
            except Exception:
                t = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            if tmin and t < tmin:
                continue
            if tmax and t > tmax:
                continue
            out.append((t, float(row["open"]), float(row["high"]),
                           float(row["low"]),  float(row["close"])))
    return out


def common_window(files, months):
    firsts, lasts = {}, {}
    for s, p in files.items():
        with open(p, "r", newline="") as f:
            rdr = csv.reader(f)
            next(rdr)
            rows = [r for r in rdr if r]
        try:
            firsts[s] = datetime.fromisoformat(rows[0][0])
            lasts[s]  = datetime.fromisoformat(rows[-1][0])
        except Exception:
            firsts[s] = datetime.strptime(rows[0][0], "%Y-%m-%d %H:%M:%S")
            lasts[s]  = datetime.strptime(rows[-1][0], "%Y-%m-%d %H:%M:%S")
    end   = min(lasts.values())
    start = max(max(firsts.values()), end - timedelta(days=months * 31))
    return start, end


def run_config(name: str, cfg: SmartBBV14Config, params, specs,
               merged, balance: float) -> dict:
    sizer = DynamicSizerV18(cfg=SizerV18Config(
        alpha_cap             = 0.10,
        kelly_fractional      = 0.50,
        min_trades_for_bucket = 20,
        min_risk_pct          = 0.0020,
        max_risk_pct          = 0.0200,
        cold_start_risk_pct   = 0.0050,
        daily_cap_usd         = 4_000.0,
        total_cap_usd         = 10_000.0,
        daily_safety_frac     = 0.75,
        total_safety_frac     = 0.70,
        kill_losing_buckets   = True,
    ))
    calendar = TradingCalendar()
    eng = SmartBBV18Engine(
        symbols        = specs,
        params         = params,
        cfg            = cfg,
        initial_equity = balance,
        sizer          = sizer,
        calendar       = calendar,
        use_calendar   = True,
    )
    t0 = _time.time()
    for t, s, o, h, l, c in merged:
        eng.on_bar(s, t.timestamp(), t.strftime("%Y-%m-%d"),
                   t.hour, t.minute, o, h, l, c)
    elapsed = _time.time() - t0

    trades = eng.trades
    wins   = [tr for tr in trades if tr.net_pnl > 0]
    losses = [tr for tr in trades if tr.net_pnl <= 0]
    gw = sum(tr.net_pnl for tr in wins)
    gl = -sum(tr.net_pnl for tr in losses)
    pf = gw / gl if gl > 0 else float("inf")
    net = sum(tr.net_pnl for tr in trades)

    eq = balance
    peak = eq
    mdd = 0.0
    for tr in trades:
        eq += tr.net_pnl
        peak = max(peak, eq)
        dd = (peak - eq) / peak if peak > 0 else 0.0
        if dd > mdd:
            mdd = dd

    # Exit-reason breakdown
    by_exit = {}
    for tr in trades:
        d = by_exit.setdefault(tr.exit_reason, {"n": 0, "net": 0.0})
        d["n"] += 1
        d["net"] += tr.net_pnl

    # Same-bar exit diagnostic
    same_bar = sum(1 for tr in trades if tr.bars_held == 0)

    # Wrong-side SL diagnostic (on reversal-side of entry)
    wrong_side = 0
    for tr in trades:
        # reconstruct entry fill side: side=+1 => LONG, compare sl to entry
        # tr.R_dist is positive stop distance; we have exit_price & entry_price
        # but not the SL level directly. Approximate via exit_reason:
        # if reason=='stop_loss' and exit price went in FAVOUR of direction,
        # that's a "wrong-side SL" exit — i.e. a profitable SL hit.
        if tr.exit_reason == "stop_loss":
            # For LONG (side>0): wrong-side if exit_price > entry_price
            # For SHORT(side<0): wrong-side if exit_price < entry_price
            if (tr.side > 0 and tr.exit_price > tr.entry_price) or \
               (tr.side < 0 and tr.exit_price < tr.entry_price):
                wrong_side += 1

    avg_bars = (sum(tr.bars_held for tr in trades) / len(trades)) if trades else 0.0

    return {
        "name"         : name,
        "n_trades"     : len(trades),
        "wins"         : len(wins),
        "losses"       : len(losses),
        "win_rate"     : len(wins) / len(trades) if trades else 0.0,
        "net_pnl"      : net,
        "pct_return"   : (net / balance) * 100.0,
        "pf"           : pf,
        "max_dd_pct"   : mdd * 100.0,
        "avg_bars_held": avg_bars,
        "same_bar_exits": same_bar,
        "wrong_side_sl_exits": wrong_side,
        "by_exit"      : by_exit,
        "elapsed_sec"  : elapsed,
    }


def print_row(r):
    bye_parts = ", ".join(f"{k}={v['n']}" for k, v in r["by_exit"].items())
    print(f"  {r['name']:<18} | N={r['n_trades']:>3}"
          f" WR={r['win_rate']*100:>4.1f}%"
          f" PnL=${r['net_pnl']:>+9,.0f}"
          f" ({r['pct_return']:>+5.1f}%)"
          f" PF={r['pf']:>4.2f}"
          f" DD={r['max_dd_pct']:>4.2f}%"
          f" avgBars={r['avg_bars_held']:>4.1f}"
          f" sameBar={r['same_bar_exits']:>3}"
          f" wrongSL={r['wrong_side_sl_exits']:>3}"
          f" | {bye_parts}")


def main():
    balance = 100_000.0
    months  = 3
    print("=" * 96)
    print("  v19 HONEST test — three SL/TP regimes, same feed, same sizer, same params")
    print(f"  ${balance:,.0f} account  |  {months}-month 5%ers M1 window")
    print("=" * 96)

    data_dir = ROOT / "data" / "historical"
    files = {s: data_dir / f"{s}_M1.csv" for s in TIER1}
    files = {s: p for s, p in files.items() if p.exists()}
    if not files:
        print("ERROR: no {SYMBOL}_M1.csv files in data/historical/")
        return 1

    tmin, tmax = common_window(files, months)
    print(f"  Symbols: {', '.join(sorted(files))}")
    print(f"  Window : {tmin.date()} -> {tmax.date()}")

    tuning_path = ROOT / "Results" / "v15_ultimate_tuning.json"
    params = load_v15_params(str(tuning_path)) if tuning_path.exists() else None

    internal = sorted(files)
    specs    = [SMARTBB_UNIVERSE[s] for s in internal]

    streams = {s: load_m1(files[s], tmin, tmax) for s in internal}
    merged  = []
    for s, bars in streams.items():
        merged.extend((t, s, o, h, l, c) for (t, o, h, l, c) in bars)
    merged.sort(key=lambda r: r[0])
    print(f"  Total M1 bars: {len(merged):,}")
    print()

    # --- CONTROL: current v18 default (bb_floored + v18.1 guard, same-bar OK) ----
    cfg_ctrl = SmartBBV14Config()
    # sl_mode=None so the per-symbol default ("bb_floored") drives it
    cfg_ctrl.sl_mode = None
    cfg_ctrl.no_same_bar_exit = False

    # --- REV_PROPER: honest packaging, same-bar still allowed (isolates packaging) ---
    cfg_revp = SmartBBV14Config()
    cfg_revp.sl_mode = "reversion_proper"
    cfg_revp.real_sl_atr_mult = 1.5
    cfg_revp.no_same_bar_exit = False

    # --- REV_PROPER_NSB: full honest test --------------------------------------
    cfg_nsb = SmartBBV14Config()
    cfg_nsb.sl_mode = "reversion_proper"
    cfg_nsb.real_sl_atr_mult = 1.5
    cfg_nsb.no_same_bar_exit = True

    results = []
    for name, cfg in [("CONTROL (v18)",      cfg_ctrl),
                       ("REV_PROPER",          cfg_revp),
                       ("REV_PROPER + NSB",    cfg_nsb)]:
        print(f"  [{name}] running ...")
        r = run_config(name, cfg, params, specs, merged, balance)
        results.append(r)

    # ----- Side-by-side report -----
    print()
    print("=" * 132)
    print("  RESULTS — three SL/TP regimes, same everything else")
    print("=" * 132)
    for r in results:
        print_row(r)
    print("-" * 132)

    # ----- Interpretation -----
    ctrl, revp, nsb = results
    print()
    print("INTERPRETATION:")
    print(f"  CONTROL PnL        = ${ctrl['net_pnl']:+,.0f}  "
          f"(wrong-side SL exits = {ctrl['wrong_side_sl_exits']})")
    print(f"  REV_PROPER PnL     = ${revp['net_pnl']:+,.0f}  "
          f"<-- same signals, broker-valid orders, same-bar still allowed")
    print(f"  REV_PROPER + NSB   = ${nsb['net_pnl']:+,.0f}  "
          f"<-- plus NO same-bar exits (realistic live test)")
    print()
    diff_ctrl_revp = revp['net_pnl'] - ctrl['net_pnl']
    diff_revp_nsb  = nsb['net_pnl']  - revp['net_pnl']
    print(f"  Packaging-only delta      : ${diff_ctrl_revp:+,.0f}")
    print(f"  Same-bar-cheating delta   : ${diff_revp_nsb:+,.0f}")
    print()
    if nsb['net_pnl'] > 0 and nsb['n_trades'] >= 30 and nsb['pf'] >= 1.3:
        verdict = "(i) Edge is REAL — shippable with reversion_proper + no_same_bar_exit."
    elif nsb['net_pnl'] > 0 and nsb['n_trades'] >= 20:
        verdict = "(ii) Edge is HALF-REAL — positive but thin; needs more data or tuning."
    else:
        verdict = "(iii) Edge is SPURIOUS — do not go live; strategy has no live-viable edge."
    print(f"  VERDICT: {verdict}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
