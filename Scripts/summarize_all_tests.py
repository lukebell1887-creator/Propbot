#!/usr/bin/env python3
"""summarize_all_tests.py  —  Aggregate all PhD-grade test outputs into ONE
paste-back-ready summary you can send straight to Cline.

Reads:
  Results/phd_superior_v20.json       (ORB grid search: 648 cells, IS+OOS+FULL,
                                       Deflated Sharpe, PBO, per-symbol best)
  Results/_honest_ref.txt             (SmartBB v18 honest 3-way reference,
                                       from backtest_v19_honest.py stdout)

Writes to stdout  — redirect to file via  > Results\\PASTE_BACK_TO_CLINE.txt
"""
from __future__ import annotations
import json, re, sys
from pathlib import Path
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "Results"

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def sep(ch="=", n=80): return ch * n


def fmt_pnl(v):
    try: return f"${float(v):+,.0f}"
    except Exception: return "n/a"

def fmt_pf(v):
    try:
        f = float(v)
        if f != f or f > 99: return ">99"
        return f"{f:.2f}"
    except Exception: return "n/a"

def fmt_pct(v):
    try: return f"{float(v):.2f}%"
    except Exception: return "n/a"


def load_grid():
    p = RESULTS / "phd_superior_v20.json"
    if not p.exists():
        return None
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def load_honest_ref():
    """Best-effort extraction of the 3-way SmartBB honest summary table."""
    p = RESULTS / "_honest_ref.txt"
    if not p.exists():
        return None
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    # Grab the last ~80 lines which usually contain the summary
    lines = txt.splitlines()
    tail = "\n".join(lines[-120:])
    return tail


# =====================================================================
#  REPORT BUILDERS
# =====================================================================

def report_header():
    print(sep("="))
    print("  PHD-GRADE FULL-SCENARIO TEST REPORT")
    print(f"  generated : {datetime.now().isoformat(timespec='seconds')}")
    print(sep("="))
    print()


def report_honest_ref(tail: str | None):
    print(sep("-"))
    print("  PART 1 / SmartBB v18 — 3-way HONEST reference backtest")
    print(sep("-"))
    if not tail:
        print("  [MISSING]  Results/_honest_ref.txt was not produced.")
        print("  Run Scripts/backtest_v19_honest.py manually.")
        print()
        return
    # Try to find the summary table; else just dump the tail
    keep = []
    capturing = False
    for ln in tail.splitlines():
        if "CONTROL" in ln or "REV_PROPER" in ln or "same-bar" in ln.lower():
            capturing = True
        if capturing:
            keep.append(ln)
    if not keep:
        keep = tail.splitlines()[-40:]
    for ln in keep[-60:]:
        print("  " + ln.rstrip())
    print()


def report_grid(g: dict | None):
    print(sep("-"))
    print("  PART 2 / ORB v20 — PhD GRID SEARCH  (walk-forward IS/OOS/FULL)")
    print(sep("-"))
    if not g:
        print("  [MISSING]  Results/phd_superior_v20.json was not produced.")
        print("  Run Scripts/phd_superior_v20.py manually.")
        print()
        return

    print(f"  Cells tested       : {g.get('total_cells', '?')}")
    print(f"  Backtests run      : {g.get('total_backtests', '?')}")
    print(f"  Window (FULL)      : {g.get('window', {}).get('full', '?')}")
    print(f"  Window (IS)        : {g.get('window', {}).get('is', '?')}")
    print(f"  Window (OOS)       : {g.get('window', {}).get('oos', '?')}")
    print(f"  PBO (overfit prob) : {g.get('pbo', 0.0)*100:.1f}%"
          f"   {'(GOOD: generalises)' if g.get('pbo', 1.0) < 0.5 else '(BAD: overfits IS→OOS)'}")
    print()

    w = g.get("winner", {})
    c = w.get("cfg", {})
    print("  WINNER CONFIG (by robust score, IS+OOS+FULL all positive):")
    if c:
        print(f"    or_minutes   : {c.get('or_minutes')}")
        print(f"    tp1 / tp2    : {c.get('tp1')} / {c.get('tp2')}")
        print(f"    sl_buffer    : {c.get('sl_buffer')}")
        print(f"    amp_hurdle   : {c.get('amp_hurdle')}")
        print(f"    require_nr7  : {c.get('require_nr7')}")
        print(f"    hurst window : [{c.get('hurst_min')}, {c.get('hurst_max')}]")
    else:
        print("    (none — no config passed the robustness gate)")
    print()
    print("  WINNER PERFORMANCE:")
    for lbl, key in (("IS  ", "is"), ("OOS ", "oos"), ("FULL", "full")):
        s = w.get(key, {})
        if not s:
            print(f"    {lbl}  : (missing)")
            continue
        print(f"    {lbl}  N={s.get('entries', 0):>4}  "
              f"WR={s.get('wr', 0.0)*100:>5.1f}%  "
              f"PnL={fmt_pnl(s.get('net_pnl', 0)):>10}  "
              f"PF={fmt_pf(s.get('pf', 0.0)):>6}  "
              f"DD={fmt_pct(s.get('max_dd_pct', 0.0)):>6}")
    print()
    print(f"  DEFLATED SHARPE p  : {w.get('deflated_sharpe', 0.0):.3f}"
          f"   {'(REAL edge at 5%)' if w.get('deflated_sharpe', 0.0) > 0.95 else '(lucky-looking)'}")
    print()


def report_top15(g: dict | None):
    if not g:
        return
    rows = g.get("top20", [])[:15]
    if not rows:
        return
    print(sep("-"))
    print("  PART 3 / Top-15 robust configs  (all must be +ve on IS AND OOS)")
    print(sep("-"))
    print("   #  IS  PnL / N / PF       OOS PnL / N / PF      FULL PnL / N / PF     |  or tp    sl amp nr7 hurst")
    for i, r in enumerate(rows, 1):
        c  = r.get("cfg", {})
        is_, oos_, ful = r.get("is", {}), r.get("oos", {}), r.get("full", {})
        hg = "trend" if c.get("hurst_min", 0) >= 0.55 else \
             ("mid  " if c.get("hurst_min", 0) >= 0.40 else "all  ")
        nr = "NR7" if c.get("require_nr7") else "off"
        print(f"  {i:>2}  "
              f"{fmt_pnl(is_.get('net_pnl')):>8}/{is_.get('entries',0):>3}/{fmt_pf(is_.get('pf')):>5}  "
              f"{fmt_pnl(oos_.get('net_pnl')):>8}/{oos_.get('entries',0):>3}/{fmt_pf(oos_.get('pf')):>5}  "
              f"{fmt_pnl(ful.get('net_pnl')):>8}/{ful.get('entries',0):>3}/{fmt_pf(ful.get('pf')):>5}  | "
              f"{c.get('or_minutes',0):>2} {c.get('tp1',0)}/{c.get('tp2',0)} "
              f"{c.get('sl_buffer',0)} {c.get('amp_hurdle',0)} {nr} {hg}")
    print()


def report_per_symbol(g: dict | None):
    if not g:
        return
    ps = g.get("per_symbol_best", {})
    if not ps:
        return
    print(sep("-"))
    print("  PART 4 / Best config PER SYMBOL (FULL 3-month window, N>=5, net>0)")
    print(sep("-"))
    for sym in sorted(ps):
        rec  = ps[sym]
        c    = rec.get("cfg", {})
        st   = rec.get("stats", {})
        nr   = "NR7" if c.get("require_nr7") else "off"
        hg = "trend" if c.get("hurst_min", 0) >= 0.55 else \
             ("mid  " if c.get("hurst_min", 0) >= 0.40 else "all  ")
        print(f"  {sym:<7}  N={st.get('n',0):>3}  "
              f"WR={st.get('wr', 0)*100:>5.1f}%  "
              f"net={fmt_pnl(st.get('net', 0)):>8}  | "
              f"or={c.get('or_minutes')}m  tp={c.get('tp1')}/{c.get('tp2')}  "
              f"slb={c.get('sl_buffer')}  amp={c.get('amp_hurdle')}  {nr}  {hg}")
    for sym in sorted(["US30", "US100", "US500", "DE40", "XAUUSD"]):
        if sym not in ps:
            print(f"  {sym:<7}  (no profitable config on FULL)")
    print()


def report_footer(g: dict | None, honest_ref_ok: bool):
    print(sep("-"))
    print("  INTERPRETATION GUIDE (quick)")
    print(sep("-"))
    print("   * PBO < 50%  → IS-top configs DO generalise to OOS (good)")
    print("   * PBO >= 50% → pure overfitting, ignore IS winners")
    print("   * Deflated Sharpe p > 0.95 → edge is statistically real at 5%")
    print("   * For LIVE deployment: prefer configs where IS PF >= 1.3 AND OOS PF >= 1.1")
    print("     AND per-symbol WR >= 55% on N >= 10 trades")
    print()
    print(sep("="))
    print("  END OF REPORT — copy EVERYTHING above and paste into Cline.")
    print(sep("="))


def main():
    report_header()
    report_honest_ref(load_honest_ref())
    g = load_grid()
    report_grid(g)
    report_top15(g)
    report_per_symbol(g)
    report_footer(g, load_honest_ref() is not None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
