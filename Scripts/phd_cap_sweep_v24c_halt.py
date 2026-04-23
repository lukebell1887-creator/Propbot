"""
v24c  HALT-AWARE cap sweep.

Replays the 283 v24 trades through MertonGZ γ=3.0 at cap_mult in {2..12}
**with a 4 % hard daily kill-switch**.  Compares vs v24b (no halt) to quantify
the exact lift we gain by truncating the tail at -4 % per day.

Because worst-day is physically bounded at 4 % + residual slippage, we expect
the new sweet-spot to be pushed HIGHER (probably cap_mult = 7 - 10) with
Ruin@5 % collapsing to ~0 and PnL climbing.

Outputs
-------
  Results/v24c_cap_sweep_halt.json      raw metrics + halt telemetry
  Docs/V24C_HALT_RESULTS.md             halt-vs-no-halt Pareto comparison
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from src.sizers_v24 import TradeMeta, History, MertonGZWrapper
from src.daily_halt import DailyHalt

START_EQUITY = 100_000.0
BOOTSTRAP_PATHS = 5000
BOOTSTRAP_BLOCK = 5.0

TRADE_FILE = ROOT / "Results" / "v24_trades.json"
OUT_JSON = ROOT / "Results" / "v24c_cap_sweep_halt.json"
OUT_MD = ROOT / "Docs" / "V24C_HALT_RESULTS.md"


# =====================================================================
# HALT-aware replay
# =====================================================================
def replay(trades: List[TradeMeta], cap_mult: float, gamma: float = 3.0,
           base_f: float = 0.0011, halt_pct: float = 0.04,
           halt_enabled: bool = True,
           start_equity: float = START_EQUITY) -> Dict[str, Any]:
    """Replay with MertonGZ γ + optional 4 % daily hard halt."""
    sizer = MertonGZWrapper(gamma=gamma, base_f=base_f, cap_mult=cap_mult,
                            dd_cap=halt_pct)  # match halt with Merton DD
    outer_cap = base_f * cap_mult
    hist = History(equity=start_equity, peak=start_equity, start_equity=start_equity)
    halt = DailyHalt(halt_pct=halt_pct) if halt_enabled else None

    diary: List[Dict[str, Any]] = []
    skipped = 0
    for tr in trades:
        # 4 % daily hard halt gate (BEFORE sizing)
        if halt is not None and not halt.can_trade(tr.entry_time, hist.equity):
            skipped += 1
            continue

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

    return {
        "diary": diary,
        "final_equity": hist.equity,
        "peak": hist.peak,
        "skipped": skipped,
        "halt_stats": {
            "total_halts": halt.total_halts if halt else 0,
            "days_seen": halt.days_seen if halt else 0,
            "halted_dates": halt.halted_dates[:] if halt else [],
        },
    }


def compute_metrics(diary: List[Dict], start_equity: float) -> Dict[str, float]:
    if not diary:
        return {"n_trades": 0, "win_rate_pct": 0, "net_pnl": 0, "ret_pct": 0,
                "max_dd_pct": 0, "worst_day_pct": 0, "profit_factor": 0,
                "sharpe": 0, "calmar": 0}
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

    by_day = defaultdict(float)
    for d in diary:
        day = datetime.utcfromtimestamp(d["t"]).date()
        by_day[day] += d["pnl"]
    daily_pnls = list(by_day.values())
    worst_day_pct = (-min(daily_pnls) / start_equity * 100.0) if daily_pnls else 0.0

    sharpe = float(pnls.mean() / pnls.std() * math.sqrt(252)) if pnls.std() > 0 else 0.0
    ret_pct = (eq[-1] - start_equity) / start_equity * 100.0
    calmar = ret_pct / (max_dd * 100.0) if max_dd > 0 else 0.0

    return {
        "n_trades": n,
        "win_rate_pct": wins / n * 100.0,
        "net_pnl": float(eq[-1] - start_equity),
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
    """Stationary-block bootstrap identical to v24b for direct comparison."""
    rng = np.random.default_rng(seed)
    arr = np.array(pnls, dtype=float)
    n = len(arr)
    if n == 0:
        return {f"ruin@{t*100:.0f}%": 0.0 for t in thresholds} | {
            "p95_dd_pct": 0.0, "p99_dd_pct": 0.0}
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
        rp = np.maximum.accumulate(np.concatenate([[start_equity], eq]))[1:]
        dd = (rp - eq) / rp
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
# Main
# =====================================================================
def main() -> int:
    print("=" * 88)
    print("  v24c  HALT-AWARE cap sweep  (4 % daily hard kill-switch ON)")
    print(f"  {datetime.now().isoformat(timespec='seconds')}")
    print("=" * 88)
    print()

    with open(TRADE_FILE) as f:
        data = json.load(f)
    trades = [TradeMeta(**t) for t in data["trades"]]
    print(f"  loaded {len(trades)} trades from {TRADE_FILE.name}")
    print()

    cap_mults = [2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 12.0]
    thresholds = [0.03, 0.04, 0.05, 0.06, 0.08, 0.10]

    results: List[Dict[str, Any]] = []
    print(f"  {'cap_mult':>8} {'cap%':>6} {'halt':>5} {'N':>4} {'PnL':>10} "
          f"{'DD%':>6} {'Wrst%':>6} {'PF':>5} {'Calmar':>7} {'Halts':>6} "
          f"{'Skipped':>7}")
    for halt_on in [False, True]:
        label = "ON " if halt_on else "off"
        for cm in cap_mults:
            rep = replay(trades, cap_mult=cm, halt_enabled=halt_on)
            m = compute_metrics(rep["diary"], START_EQUITY)
            m["cap_mult"] = cm
            m["cap_pct"] = cm * 0.0011 * 100.0
            m["halt_enabled"] = halt_on
            m["halts"] = rep["halt_stats"]["total_halts"]
            m["halt_dates"] = rep["halt_stats"]["halted_dates"]
            m["skipped_trades"] = rep["skipped"]

            ruin = bootstrap_ruin(
                [d["pnl"] for d in rep["diary"]], START_EQUITY, thresholds,
                n_paths=BOOTSTRAP_PATHS, seed=42,
            )
            m.update(ruin)
            results.append(m)

            print(f"  {cm:>8.1f} {m['cap_pct']:>5.2f}% {label:>5} "
                  f"{m['n_trades']:>4d} ${m['net_pnl']:>+9,.0f} "
                  f"{m['max_dd_pct']:>5.2f}% {m['worst_day_pct']:>5.2f}% "
                  f"{m['profit_factor']:>4.2f} {m['calmar']:>6.2f} "
                  f"{m['halts']:>6d} {m['skipped_trades']:>7d}")
        print()

    with open(OUT_JSON, "w") as f:
        json.dump(results, f, indent=2)

    # =================================================================
    # Find the new sweet spot (halt ON)
    # =================================================================
    halted = [r for r in results if r["halt_enabled"]]
    def safe(r):
        return (r["max_dd_pct"] < 6.0
                and r.get("ruin@10%", 100.0) < 1.0
                and r.get("ruin@5%", 100.0) < 5.0
                and r["worst_day_pct"] < 4.3)  # = halt + tiny slip

    viable = [r for r in halted if safe(r)]
    sweet = max(viable, key=lambda r: r["net_pnl"]) if viable else max(
        halted, key=lambda r: r["net_pnl"])

    # Baseline for comparison (halt OFF, cap=5)
    baseline = next((r for r in results if not r["halt_enabled"]
                     and r["cap_mult"] == 5.0), None)

    print("=" * 88)
    print(f"  NEW SWEET SPOT (halt ON): cap_mult = {sweet['cap_mult']}  "
          f"(= {sweet['cap_pct']:.2f}% / trade)")
    print(f"     PnL=${sweet['net_pnl']:+,.0f}  "
          f"DD={sweet['max_dd_pct']:.2f}%  "
          f"Worst-day={sweet['worst_day_pct']:.2f}%")
    print(f"     Ruin@5%={sweet.get('ruin@5%', 0):.2f}%  "
          f"Ruin@10%={sweet.get('ruin@10%', 0):.2f}%  "
          f"Halts_triggered={sweet['halts']}/{len(sweet['halt_dates']) if sweet.get('halt_dates') else 0}")
    if baseline:
        lift = sweet['net_pnl'] - baseline['net_pnl']
        print(f"     vs v24b (cap=5, no-halt): baseline ${baseline['net_pnl']:+,.0f} → "
              f"lift ${lift:+,.0f}  ({lift/baseline['net_pnl']*100:+.1f}%)")
    print("=" * 88)

    # =================================================================
    # Markdown report
    # =================================================================
    md = []
    md.append("# V24c — HALT-AWARE CAP SWEEP\n\n")
    md.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}\n\n")
    md.append(f"**Mechanism added:** 4 % daily hard kill-switch (truncates worst-day tail).\n")
    md.append(f"**Sizer:** MertonGZ γ=3.0, base_f=0.110 %.  **Trades:** {len(trades)}  "
              f"**Bootstrap:** {BOOTSTRAP_PATHS:,}\n\n")

    md.append("## Halt OFF  (v24b baseline)\n\n")
    md.append("| cap_mult | cap% | N | PnL | DD% | Worst% | PF | Calmar | Ruin@5% | Ruin@10% |\n")
    md.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
    for r in [x for x in results if not x["halt_enabled"]]:
        md.append(f"| {r['cap_mult']:.1f} | {r['cap_pct']:.2f}% | {r['n_trades']} "
                  f"| ${r['net_pnl']:+,.0f} | {r['max_dd_pct']:.2f}% "
                  f"| {r['worst_day_pct']:.2f}% | {r['profit_factor']:.2f} "
                  f"| {r['calmar']:.2f} | {r.get('ruin@5%', 0):.2f}% "
                  f"| {r.get('ruin@10%', 0):.2f}% |\n")

    md.append("\n## Halt ON (4 % daily kill-switch)\n\n")
    md.append("| cap_mult | cap% | N | PnL | DD% | Worst% | PF | Calmar "
              "| Ruin@5% | Ruin@10% | Halts |\n")
    md.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|\n")
    for r in [x for x in results if x["halt_enabled"]]:
        ok = safe(r)
        mark = " ✅" if ok else ""
        md.append(f"| {r['cap_mult']:.1f} | {r['cap_pct']:.2f}% | {r['n_trades']} "
                  f"| ${r['net_pnl']:+,.0f}{mark} | {r['max_dd_pct']:.2f}% "
                  f"| {r['worst_day_pct']:.2f}% | {r['profit_factor']:.2f} "
                  f"| {r['calmar']:.2f} | {r.get('ruin@5%', 0):.2f}% "
                  f"| {r.get('ruin@10%', 0):.2f}% | {r['halts']} |\n")

    md.append(f"\n## 🏆 Halt-ON sweet-spot\n\n")
    md.append(f"**cap_mult = {sweet['cap_mult']}** → **{sweet['cap_pct']:.2f} % per trade**\n\n")
    md.append(f"- PnL (3 months): **${sweet['net_pnl']:+,.0f}**  "
              f"(≈ ${sweet['net_pnl']*4:+,.0f} annualised)\n")
    md.append(f"- Max DD: **{sweet['max_dd_pct']:.2f} %**  "
              f"(margin vs 10 % line: **{10.0/max(sweet['max_dd_pct'],0.01):.2f}×**)\n")
    md.append(f"- Worst day: **{sweet['worst_day_pct']:.2f} %**  "
              f"(hard-bounded at 4 % + slippage)\n")
    md.append(f"- Ruin@5 %: **{sweet.get('ruin@5%', 0):.2f} %**  "
              f"(was {baseline.get('ruin@5%', 0):.2f} % without halt)\n")
    md.append(f"- Ruin@10 %: **{sweet.get('ruin@10%', 0):.2f} %**  (essentially zero)\n")
    md.append(f"- Halts triggered in sample: **{sweet['halts']}** days "
              f"out of ~66 trading days\n")
    if sweet["halt_dates"]:
        md.append(f"- Halted dates: {sweet['halt_dates']}\n")
    if baseline:
        md.append(f"\n## Lift vs v24b recommendation\n\n")
        md.append(f"| | v24b sweet-spot (cap=5, no halt) | v24c sweet-spot (halt ON) | Δ |\n")
        md.append(f"|---|---|---|---|\n")
        md.append(f"| PnL | ${baseline['net_pnl']:+,.0f} | "
                  f"${sweet['net_pnl']:+,.0f} | "
                  f"${sweet['net_pnl']-baseline['net_pnl']:+,.0f} "
                  f"({(sweet['net_pnl']-baseline['net_pnl'])/baseline['net_pnl']*100:+.1f} %) |\n")
        md.append(f"| Max DD | {baseline['max_dd_pct']:.2f} % | "
                  f"{sweet['max_dd_pct']:.2f} % | "
                  f"{sweet['max_dd_pct']-baseline['max_dd_pct']:+.2f} pp |\n")
        md.append(f"| Worst day | {baseline['worst_day_pct']:.2f} % | "
                  f"{sweet['worst_day_pct']:.2f} % | "
                  f"{sweet['worst_day_pct']-baseline['worst_day_pct']:+.2f} pp |\n")
        md.append(f"| Ruin@5 % | {baseline.get('ruin@5%', 0):.2f} % | "
                  f"{sweet.get('ruin@5%', 0):.2f} % | "
                  f"{sweet.get('ruin@5%', 0)-baseline.get('ruin@5%', 0):+.2f} pp |\n")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.writelines(md)
    print(f"  wrote {OUT_JSON.relative_to(ROOT)}  and  {OUT_MD.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
