"""
v24b  PhD CAP SWEEP — find the Pareto sweet-spot between γ=3.0 and Kelly.

Runs MertonGZ γ=3.0 across 9 cap_mult values (effective cap = base × cap_mult)
and measures, for each cap:

    PnL, Max DD, Worst-Day DD, PF, Calmar, Sharpe,
    Ruin@4%, Ruin@5% (DAILY 5ers line), Ruin@6%, Ruin@8%, Ruin@10% (STATIC MAX 5ers line)

Uses the EXACT same replay + stationary-block bootstrap as v24 shootout to guarantee
apples-to-apples comparison against the v24 leaderboard.

Outputs:
  Results/v24b_cap_sweep.json      — raw metrics per cap
  Docs/V24B_CAP_SWEEP_RESULTS.md   — markdown report with Pareto frontier + sweet-spot
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from src.sizers_v24 import TradeMeta, History, MertonGZWrapper

# 5ers account
START_EQUITY = 100_000.0
BOOTSTRAP_PATHS = 5000
BOOTSTRAP_BLOCK = 5.0

TRADE_FILE = ROOT / "Results" / "v24_trades.json"
OUT_JSON = ROOT / "Results" / "v24b_cap_sweep.json"
OUT_MD = ROOT / "Docs" / "V24B_CAP_SWEEP_RESULTS.md"


# =====================================================================
# Replay (matches v24 shootout harness EXACTLY, modulo dynamic cap)
# =====================================================================
def replay(trades: List[TradeMeta], cap_mult: float, gamma: float = 3.0,
           base_f: float = 0.0011, start_equity: float = START_EQUITY
           ) -> Dict[str, Any]:
    """Replay with MertonGZWrapper, applying the passed cap_mult as outer clip."""
    # MertonGZWrapper itself has an internal cap_mult; we pass the SAME value so
    # its own risk calc is consistent, then apply an OUTER cap of base_f*cap_mult
    # to replace the v24 HARD_CAP_F = 0.5 % ceiling.
    sizer = MertonGZWrapper(gamma=gamma, base_f=base_f, cap_mult=cap_mult,
                            dd_cap=0.04)
    outer_cap = base_f * cap_mult   # e.g. base=0.11% × cap_mult=5 = 0.55% ceiling

    hist = History(equity=start_equity, peak=start_equity, start_equity=start_equity)
    diary: List[Dict[str, Any]] = []
    for tr in trades:
        f = sizer.size(hist, tr)
        f = max(0.0, min(outer_cap, f))
        risk_usd = hist.equity * f
        pnl = float(tr.realised_R) * risk_usd
        hist.feedback(tr, pnl)
        sizer.on_closed(tr, float(tr.realised_R))
        diary.append({
            "t": float(tr.entry_time), "sym": tr.symbol,
            "R": float(tr.realised_R), "f": f, "pnl": pnl,
            "equity": hist.equity, "peak": hist.peak, "dd": hist.dd_pct(),
        })
    return {"diary": diary, "final_equity": hist.equity, "peak": hist.peak,
            "outer_cap_pct": outer_cap * 100.0}


def compute_metrics(diary: List[Dict], start_equity: float) -> Dict[str, float]:
    pnls = np.array([d["pnl"] for d in diary], dtype=float)
    eq = np.array([d["equity"] for d in diary], dtype=float)
    n = len(pnls)
    wins = int((pnls > 0).sum())
    gross_win = float(pnls[pnls > 0].sum()) if (pnls > 0).any() else 0.0
    gross_loss = float(-pnls[pnls < 0].sum()) if (pnls < 0).any() else 0.0
    pf = (gross_win / gross_loss) if gross_loss > 0 else (999.0 if gross_win > 0 else 0.0)

    peak = -np.inf
    max_dd = 0.0
    for v in eq:
        if v > peak: peak = v
        if peak > 0 and (peak - v) / peak > max_dd:
            max_dd = (peak - v) / peak

    # Daily DD: sum per calendar day
    by_day = defaultdict(float)
    for d in diary:
        day = datetime.fromtimestamp(d["t"]).date()
        by_day[day] += d["pnl"]
    daily_pnls = list(by_day.values())
    worst_day_pct = (-min(daily_pnls) / start_equity * 100.0) if daily_pnls else 0.0

    sharpe = float(pnls.mean() / pnls.std() * math.sqrt(252)) if pnls.std() > 0 else 0.0
    ret_pct = (eq[-1] - start_equity) / start_equity * 100.0 if n else 0.0
    calmar = ret_pct / (max_dd * 100.0) if max_dd > 0 else 0.0

    return {
        "n_trades": n,
        "win_rate_pct": wins / n * 100.0 if n else 0.0,
        "net_pnl": float(eq[-1] - start_equity) if n else 0.0,
        "ret_pct": float(ret_pct),
        "max_dd_pct": float(max_dd * 100.0),
        "worst_day_pct": float(worst_day_pct),
        "profit_factor": float(pf),
        "sharpe": float(sharpe),
        "calmar": float(calmar),
    }


def bootstrap_ruin(pnls: List[float], start_equity: float,
                   thresholds: List[float],
                   n_paths: int = BOOTSTRAP_PATHS,
                   avg_block: float = BOOTSTRAP_BLOCK,
                   seed: int = 42) -> Dict[str, float]:
    rng = np.random.default_rng(seed)
    arr = np.array(pnls, dtype=float)
    n = len(arr)
    p = 1.0 / avg_block

    counts = {f"ruin@{t*100:.0f}%": 0 for t in thresholds}
    dd_samples = np.zeros(n_paths)
    for i in range(n_paths):
        path = np.empty(n)
        k = rng.integers(0, n)
        for j in range(n):
            if rng.random() < p:
                k = rng.integers(0, n)
            path[j] = arr[k]
            k = (k + 1) % n
        eq = start_equity + path.cumsum()
        running_peak = np.maximum.accumulate(np.concatenate([[start_equity], eq]))[1:]
        dd = (running_peak - eq) / running_peak
        max_dd = float(dd.max())
        dd_samples[i] = max_dd
        for thr in thresholds:
            if max_dd >= thr:
                counts[f"ruin@{thr*100:.0f}%"] += 1

    out = {k: v / n_paths * 100.0 for k, v in counts.items()}
    out["p95_dd_pct"] = float(np.quantile(dd_samples, 0.95)) * 100.0
    out["p99_dd_pct"] = float(np.quantile(dd_samples, 0.99)) * 100.0
    return out


# =====================================================================
# Main sweep
# =====================================================================
def main() -> int:
    print("=" * 88)
    print("  v24b  PhD CAP SWEEP — finding the Pareto sweet-spot vs REAL 5ers rules")
    print(f"  {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 88)
    print("  5ers rules:  10 % STATIC max DD  |  5 % DAILY loss")
    print("  Internal safety targets:  max_DD ≤ 6 %  |  worst_day ≤ 3 %  |  Ruin@10 % < 1 %")
    print()

    # Load trades from v24 shootout
    with open(TRADE_FILE) as f:
        data = json.load(f)
    trades = [TradeMeta(**{k: v for k, v in t.items()}) for t in data["trades"]]
    print(f"  loaded {len(trades)} trades from {TRADE_FILE.name}  "
          f"window={data.get('window', ['?', '?'])}")
    print()

    # Cap multiples (in units of base_f = 0.11 %)
    # 2 → 0.22 %   3 → 0.33 %   4 → 0.44 %   5 → 0.55 %   6 → 0.66 %
    # 7 → 0.77 %   8 → 0.88 %   9 → 0.99 %  10 → 1.10 %
    cap_mults = [2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    thresholds = [0.04, 0.05, 0.06, 0.08, 0.10]

    results: List[Dict[str, Any]] = []
    for cm in cap_mults:
        rep = replay(trades, cap_mult=cm)
        m = compute_metrics(rep["diary"], START_EQUITY)
        m["cap_mult"] = cm
        m["cap_pct"] = cm * 0.0011 * 100.0
        print(f"  [replay] cap_mult={cm:4.1f}  cap={m['cap_pct']:.2f}%  "
              f"PnL=${m['net_pnl']:+9,.0f}  "
              f"DD={m['max_dd_pct']:5.2f}%  "
              f"worst_day={m['worst_day_pct']:4.2f}%  "
              f"PF={m['profit_factor']:4.2f}  "
              f"Calmar={m['calmar']:5.2f}")

        ruin = bootstrap_ruin(
            [d["pnl"] for d in rep["diary"]], START_EQUITY, thresholds,
            n_paths=BOOTSTRAP_PATHS, seed=42,
        )
        m.update(ruin)
        results.append(m)

    # Write JSON
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_JSON, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n  wrote {OUT_JSON.relative_to(ROOT)}")

    # =================================================================
    # Find sweet spot
    # =================================================================
    def safe(r):
        return (r["max_dd_pct"] < 6.0
                and r.get("ruin@10%", 100.0) < 1.0
                and r.get("ruin@5%", 100.0) < 5.0
                and r["worst_day_pct"] < 3.0)

    viable = [r for r in results if safe(r)]
    if viable:
        sweet = max(viable, key=lambda r: r["net_pnl"])
        verdict = "SWEET-SPOT (safe under REAL 5ers rules with 40 % margin)"
    else:
        viable_relaxed = [r for r in results
                          if r["max_dd_pct"] < 8.0
                          and r.get("ruin@10%", 100.0) < 2.0]
        sweet = max(viable_relaxed, key=lambda r: r["net_pnl"]) if viable_relaxed else results[0]
        verdict = "RELAXED-SWEET (safe vs real rules, thinner margin)"

    print()
    print("=" * 88)
    print(f"  🏆 {verdict}: cap_mult={sweet['cap_mult']}  (= {sweet['cap_pct']:.2f}% per trade)")
    print(f"     PnL=${sweet['net_pnl']:+,.0f}  DD={sweet['max_dd_pct']:.2f}%  "
          f"Worst-day={sweet['worst_day_pct']:.2f}%")
    print(f"     Ruin@5%={sweet.get('ruin@5%', 0):.1f}%  "
          f"Ruin@10%={sweet.get('ruin@10%', 0):.2f}%")
    print("=" * 88)

    # =================================================================
    # Markdown report
    # =================================================================
    md: List[str] = []
    md.append("# V24b — CAP SWEEP: The Pareto Frontier\n\n")
    md.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n\n")
    md.append(f"**Sizer family:** MertonGZ γ=3.0  (base_f=0.110 %)  "
              f"|  **Trades:** {len(trades)}  "
              f"|  **Bootstrap paths:** {BOOTSTRAP_PATHS:,}  "
              f"|  **Start equity:** ${START_EQUITY:,.0f}\n\n")
    md.append("## 5ers rules (hard lines)\n\n")
    md.append("| Rule | Hard line | My safety target (60 % margin) |\n")
    md.append("|---|---|---|\n")
    md.append("| Max DD (static) | **10 %** from initial $100k | observed DD ≤ **6 %** |\n")
    md.append("| Daily loss | **5 %** (resets at EOD on highest of balance/equity) | worst day ≤ **3 %** |\n")
    md.append("| Ruin@10 % (static max) | — | < **1 %** |\n")
    md.append("| Ruin@5 % (daily) | — | < **5 %** |\n\n")

    md.append("## Full sweep\n\n")
    md.append("| cap_mult | cap/trade | PnL | MaxDD | Worst Day | PF | Sharpe | Calmar "
              "| Ruin@4% | Ruin@5% | Ruin@6% | Ruin@8% | Ruin@10% |\n")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|\n")
    for r in results:
        safe_mark = " ✅" if safe(r) else ""
        md.append(
            f"| {r['cap_mult']:.1f} "
            f"| {r['cap_pct']:.2f}% "
            f"| ${r['net_pnl']:+,.0f}{safe_mark} "
            f"| {r['max_dd_pct']:.2f}% "
            f"| {r['worst_day_pct']:.2f}% "
            f"| {r['profit_factor']:.2f} "
            f"| {r['sharpe']:.2f} "
            f"| {r['calmar']:.2f} "
            f"| {r.get('ruin@4%', 0):.1f}% "
            f"| {r.get('ruin@5%', 0):.1f}% "
            f"| {r.get('ruin@6%', 0):.1f}% "
            f"| {r.get('ruin@8%', 0):.1f}% "
            f"| {r.get('ruin@10%', 0):.2f}% |\n"
        )
    md.append("\n")

    md.append("## 🏆 Sweet spot\n\n")
    md.append(f"**{verdict}** — `cap_mult={sweet['cap_mult']}` (= **{sweet['cap_pct']:.2f} %** per trade)\n\n")
    md.append(f"- PnL (3 months, backtest): **${sweet['net_pnl']:+,.0f}**  "
              f"(annualised ≈ ${sweet['net_pnl'] * 4:+,.0f})\n")
    md.append(f"- Max DD: **{sweet['max_dd_pct']:.2f} %**  "
              f"(safety margin vs 10 % cap: **{10.0 / max(sweet['max_dd_pct'], 0.01):.2f}×**)\n")
    md.append(f"- Worst single day: **{sweet['worst_day_pct']:.2f} %**  "
              f"(safety margin vs 5 % cap: **{5.0 / max(sweet['worst_day_pct'], 0.01):.2f}×**)\n")
    md.append(f"- Profit factor: **{sweet['profit_factor']:.2f}**\n")
    md.append(f"- Sharpe (per-trade ann.): **{sweet['sharpe']:.2f}**\n")
    md.append(f"- Calmar: **{sweet['calmar']:.2f}**\n")
    md.append(f"- **Ruin@5 %**  (daily line):  **{sweet.get('ruin@5%', 0):.1f} %**\n")
    md.append(f"- **Ruin@10 %** (static max):  **{sweet.get('ruin@10%', 0):.2f} %**\n")
    md.append("\n")

    md.append("## vs. current live (Flat 0.110 %)\n\n")
    base = next((r for r in results if r["cap_mult"] == 3.0), None)
    if base:
        gain = sweet["net_pnl"] - base["net_pnl"]
        md.append(f"- Current live: **${base['net_pnl']:+,.0f}** @ DD {base['max_dd_pct']:.2f} %\n")
        md.append(f"- Sweet spot: **${sweet['net_pnl']:+,.0f}** @ DD {sweet['max_dd_pct']:.2f} %\n")
        md.append(f"- Lift: **${gain:+,.0f}** ({gain/base['net_pnl']*100:+.1f} %) — "
                  f"DD {sweet['max_dd_pct']-base['max_dd_pct']:+.2f} pp\n\n")

    md.append("## Reading the table\n\n")
    md.append("- Rows marked ✅ satisfy ALL safety constraints (DD<6 %, worst_day<3 %, Ruin@10 %<1 %, Ruin@5 %<5 %).\n")
    md.append("- Sweet spot = highest-PnL row that's ✅.\n")
    md.append("- Ruin@10 % < 0.1 % means realistic-live probability of blowing the prop firm static line is essentially zero.\n\n")

    OUT_MD.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.writelines(md)
    print(f"  wrote {OUT_MD.relative_to(ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
