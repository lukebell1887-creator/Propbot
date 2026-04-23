#!/usr/bin/env python3
"""
HONEST PhD sizer shootout — squeeze max PnL with REAL halts active.

Criterion (the user's rules, corrected):
  * DailyHalt @ -4% of day-start-equity                 (firm limit is 5%)
  * DDBreaker @ -4% peak-to-trough                      (firm limit is 10%)
  * Halt fires ≤ 2 times across 3 months                (cadence hygiene)
  * Max DD < 8% (still well inside 10% static)

Compares:
  • Merton-GZ grid: base ∈ {0.11, 0.15, 0.20, 0.25, 0.30}%,
                    cap_mult ∈ {3, 4, 5, 6, 8}, γ ∈ {1.5, 2, 3}
  • 10 PhD sizers from src.sizers_v24 zoo at their own defaults
"""
from __future__ import annotations
import sys, json
from collections import defaultdict
from datetime import datetime, timedelta, date
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from src.sizers_v24 import (
    TradeMeta, History, Sizer,
    FlatSizer, FractionalKellySizer, BayesianKellySizer,
    MertonGZWrapper, GARCHMertonSizer, GrossmanZhouSizer,
    VinceOptimalFSizer, VanTharpInverseVolSizer, CPPISizer,
    HARD_CAP_F,
)
from src.dynamic_sizer_v21 import MertonGZSizerConfig

BALANCE = 100_000.0
TRADE_FILE = ROOT / "Results" / "v24_trades.json"


# --------------------------------------------------------------------
# Load cached trade stream
# --------------------------------------------------------------------
if not TRADE_FILE.exists():
    print("ERROR: run Scripts/phd_sizer_shootout_v24.py first to cache v24_trades.json")
    sys.exit(1)

with open(TRADE_FILE) as f:
    data = json.load(f)

trades = [TradeMeta(**{k: v for k, v in t.items()}) for t in data["trades"]]
trades.sort(key=lambda t: t.entry_time)
print(f"  Loaded {len(trades)} trades over "
      f"{datetime.fromtimestamp(trades[0].entry_time).date()} → "
      f"{datetime.fromtimestamp(trades[-1].entry_time).date()}")


# --------------------------------------------------------------------
# Replay with per-trade halt+breaker ENFORCED (what the live bot will do)
# --------------------------------------------------------------------
def replay_with_halts(sizer: Sizer, trades, start_equity=BALANCE,
                      halt_pct=0.04, breaker_pct=0.04):
    """Replay trades through a sizer with DailyHalt + DDBreaker active.

    Returns: dict with PnL, DD, worst_day, halt_fires, breaker_fires, n_taken.
    """
    sizer.reset()
    hist = History(equity=start_equity, peak=start_equity, start_equity=start_equity)

    # Halt/breaker state
    current_day = None
    day_start_eq = start_equity
    halted_today = False
    breaker_halted = False
    peak = start_equity
    halt_fires = 0
    breaker_fires = 0

    daily_pnls = defaultdict(float)
    per_trade_eq = []
    taken = 0

    for tr in trades:
        ts = tr.entry_time
        dt = datetime.fromtimestamp(ts)
        d = dt.date()

        # Day rollover
        if current_day is None or d != current_day:
            current_day = d
            day_start_eq = hist.equity
            halted_today = False
            breaker_halted = False   # breaker resets at next server-date

        # Skip if halted
        if halted_today or breaker_halted:
            continue

        # Intra-day DD check BEFORE sizing (simulates "would this trade put us over?")
        f = sizer.size(hist, tr)
        f = max(0.0, min(HARD_CAP_F, f))
        risk_usd = hist.equity * f
        pnl = tr.realised_R * risk_usd

        # Would this trade push us past the daily halt?  (conservative: check AT entry,
        # pnl will be realised at exit — if loss, check post-hoc and if breached, cancel)
        hist.feedback(tr, pnl)
        sizer.on_closed(tr, tr.realised_R)
        taken += 1
        daily_pnls[d] += pnl
        per_trade_eq.append(hist.equity)

        if hist.equity > peak:
            peak = hist.equity

        # Daily halt check (after this trade closes, are we down ≥4% day-start?)
        if day_start_eq > 0:
            day_dd = (hist.equity - day_start_eq) / day_start_eq
            if day_dd <= -halt_pct:
                halted_today = True
                halt_fires += 1

        # Breaker check (peak-to-trough)
        if peak > 0:
            peak_dd = (peak - hist.equity) / peak
            if peak_dd >= breaker_pct:
                breaker_halted = True
                breaker_fires += 1

    # Compute metrics
    final_eq = hist.equity
    net_pnl = final_eq - start_equity
    ret_pct = net_pnl / start_equity * 100.0

    # Max DD
    eq_arr = np.array(per_trade_eq, dtype=float) if per_trade_eq else np.array([start_equity])
    running_peak = np.maximum.accumulate(eq_arr)
    max_dd_pct = float(((running_peak - eq_arr) / running_peak).max()) * 100.0

    worst_day_pct = (min(daily_pnls.values()) / start_equity * 100.0
                     if daily_pnls else 0.0)

    return dict(
        n=taken, n_skipped=len(trades) - taken,
        pnl=net_pnl, ret_pct=ret_pct,
        max_dd_pct=max_dd_pct,
        worst_day_pct=worst_day_pct,
        halt_fires=halt_fires,
        breaker_fires=breaker_fires,
        final_eq=final_eq,
    )


# --------------------------------------------------------------------
# Build configs to test
# --------------------------------------------------------------------
configs = []

# --- Merton-GZ grid (wider than before) ---
for base in [0.0011, 0.0015, 0.0020, 0.0025, 0.0030]:
    for cap in [3.0, 4.0, 5.0, 6.0, 8.0]:
        for gamma in [1.5, 2.0, 3.0]:
            name = f"MertonGZ  base={base*100:.3f}% cap={cap:.0f} γ={gamma:.1f}"
            configs.append((name, MertonGZWrapper(
                gamma=gamma, base_f=base, cap_mult=cap, dd_cap=0.04, name=name)))

# --- Other PhD sizers at their best defaults ---
configs.append(("FlatSizer  f=0.40%",
                FlatSizer(fraction=0.0040, name="Flat 0.40%")))
configs.append(("FlatSizer  f=0.50%",
                FlatSizer(fraction=0.0050, name="Flat 0.50%")))
configs.append(("Kelly_Half       ",
                FractionalKellySizer(mult=0.50, name="Kelly_Half")))
configs.append(("Kelly_Quarter    ",
                FractionalKellySizer(mult=0.25, name="Kelly_Quarter")))
configs.append(("BayesKelly_half  ",
                BayesianKellySizer(ci_level=0.10, mult=0.50, name="BayesKelly_half")))
# GARCH-Merton doesn't take cap_mult — its output goes through the zoo's HARD_CAP_F
configs.append(("GARCH-Merton γ=2 ",
                GARCHMertonSizer(gamma=2.0, base_f=0.0011, name="GARCHMerton γ2")))
configs.append(("GARCH-Merton γ=1.5",
                GARCHMertonSizer(gamma=1.5, base_f=0.0011, name="GARCHMerton γ1.5")))
configs.append(("GrossmanZhou η=2 ",
                GrossmanZhouSizer(base_f=0.0020, dd_cap=0.04, eta=2.0,
                                  name="GZ base=0.20% η=2")))
configs.append(("GrossmanZhou η=3 ",
                GrossmanZhouSizer(base_f=0.0025, dd_cap=0.04, eta=3.0,
                                  name="GZ base=0.25% η=3")))
configs.append(("Vince Optimal-f  ",
                VinceOptimalFSizer(fraction_of_optimal=0.20, name="Vince 20% f*")))
configs.append(("VanTharp InvVol  ",
                VanTharpInverseVolSizer(base_f=0.0015, name="VTH base=0.15%")))
configs.append(("CPPI m=4         ",
                CPPISizer(floor_pct=0.95, multiplier=4.0, base_f=0.0012,
                          name="CPPI m4")))


# --------------------------------------------------------------------
# Run all configs
# --------------------------------------------------------------------
print(f"\n{'='*115}")
print(f"  HONEST SIZER SHOOTOUT — {len(configs)} configs, halts@4%, breaker@4%")
print(f"{'='*115}")
print(f"  {'sizer':<38} | {'N':>4}{'skip':>5} | {'PnL':>10} {'ret%':>6} "
      f"{'DD%':>5} {'Wday%':>7} {'halts':>5} {'brk':>3}   verdict")
print("  " + "-" * 113)

rows = []
for name, sizer in configs:
    try:
        r = replay_with_halts(sizer, trades)
    except Exception as e:
        print(f"  {name:<38} ERROR: {e}")
        continue

    # Honest criterion: halt ≤ 2, breaker = 0, max DD < 8%
    ok = (r["halt_fires"] <= 2 and r["breaker_fires"] == 0 and r["max_dd_pct"] < 8.0)
    verdict = "GO " if ok else "FAIL"
    if r["breaker_fires"] > 0:
        verdict = "BRK"
    elif r["halt_fires"] > 2:
        verdict = "HLT"
    elif r["max_dd_pct"] >= 8.0:
        verdict = "DD "

    rows.append({"name": name, **r, "ok": ok})
    print(f"  {name:<38} | {r['n']:>4}{r['n_skipped']:>5} | "
          f"${r['pnl']:>+8,.0f} {r['ret_pct']:>+5.2f}% {r['max_dd_pct']:>4.2f}% "
          f"{r['worst_day_pct']:>+6.2f}% {r['halt_fires']:>5d} {r['breaker_fires']:>3d}   {verdict}")

# --------------------------------------------------------------------
# Rankings
# --------------------------------------------------------------------
viable = [r for r in rows if r["ok"]]
print(f"\n{'='*115}")
print(f"  {len(viable)}/{len(rows)} configs passed the honest gate "
      f"(halt≤2, breaker=0, DD<8%)")
print(f"{'='*115}")
if viable:
    viable.sort(key=lambda r: -r["pnl"])
    print("\n  TOP 10 by PnL (passing gate):")
    print(f"  {'#':>2}  {'sizer':<38}  {'N':>4}  {'PnL':>10}  "
          f"{'DD%':>5}  {'Wday%':>7}  {'halts':>5}")
    print("  " + "-" * 90)
    for i, r in enumerate(viable[:10], 1):
        print(f"  {i:>2}  {r['name']:<38}  {r['n']:>4}  "
              f"${r['pnl']:>+8,.0f}  {r['max_dd_pct']:>4.2f}%  "
              f"{r['worst_day_pct']:>+6.2f}%  {r['halt_fires']:>5d}")

    # Also rank non-viable by PnL to show what we're missing
    non = [r for r in rows if not r["ok"]]
    if non:
        non.sort(key=lambda r: -r["pnl"])
        print("\n  TOP 5 TOO-HOT configs (failed gate):")
        print(f"  {'#':>2}  {'sizer':<38}  {'PnL':>10}  {'DD%':>5}  "
              f"{'Wday%':>7}  {'halts':>5}  {'brk':>3}  reason")
        print("  " + "-" * 100)
        for i, r in enumerate(non[:5], 1):
            reason = ("BREAKER" if r["breaker_fires"] > 0 else
                      f"HALT×{r['halt_fires']}" if r["halt_fires"] > 2 else
                      f"DD={r['max_dd_pct']:.1f}%")
            print(f"  {i:>2}  {r['name']:<38}  ${r['pnl']:>+8,.0f}  "
                  f"{r['max_dd_pct']:>4.2f}%  {r['worst_day_pct']:>+6.2f}%  "
                  f"{r['halt_fires']:>5d}  {r['breaker_fires']:>3d}  {reason}")

# --------------------------------------------------------------------
# Save
# --------------------------------------------------------------------
out_path = ROOT / "Results" / "honest_phd_shootout.json"
with open(out_path, "w", encoding="utf-8") as f:
    json.dump(rows, f, indent=2, default=str)
print(f"\n  Saved: {out_path.relative_to(ROOT)}")
