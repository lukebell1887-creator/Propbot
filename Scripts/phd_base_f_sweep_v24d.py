"""
v24d — base_f × γ sweep  WITH halt  AND  adversarial stress-test.

Why this exists
---------------
v24c discovered that `cap_mult` is saturated: Merton-γ internally recommends
size below the 0.55 % outer cap on every trade, so raising the cap does
NOTHING.  The true lever is `base_f` — the reference fraction that Merton
scales its dynamic f* output from.

This script does THREE things in one run:

  (1) base_f × γ × halt 3-D sweep          (24 cells × 5 k bootstrap)
        finds the new Pareto-optimal (base_f, γ) with halt ON
        subject to max_dd < 4 % and worst_day < 4.3 %.

  (2) Scenario stress test on the sweet-spot config:
        a.  Normal     — the v24 trades as-is
        b.  Edge decay — multiply every R by 0.70 (30 % slippage haircut)
        c.  Fat tail   — inject a -5 R event every 20 trades
        d.  Vol spike  — 10 % of trades get abs(R) doubled
        e.  Streak     — sort so the 10 worst R's happen consecutively

  (3) Walk-forward within the 3-month window:
        train on trades[:70 %]  → pick (base_f, γ) with lowest ruin@4 %
        test  on trades[70 %:]  → report OOS PnL, DD, Calmar

Outputs
-------
  Results/v24d_base_f_sweep.json   raw 3-D grid
  Results/v24d_stress.json         5 scenarios
  Results/v24d_walkforward.json    IS-pick + OOS-perf
  Docs/V24D_FINAL_SIZING.md        one-page summary + recommendation
"""
from __future__ import annotations

import copy
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from src.sizers_v24 import TradeMeta, History, MertonGZWrapper
from src.daily_halt import DailyHalt

START_EQUITY = 100_000.0
BOOTSTRAP_PATHS = 3000
BOOTSTRAP_BLOCK = 5.0

TRADE_FILE = ROOT / "Results" / "v24_trades.json"
OUT_SWEEP = ROOT / "Results" / "v24d_base_f_sweep.json"
OUT_STRESS = ROOT / "Results" / "v24d_stress.json"
OUT_WF = ROOT / "Results" / "v24d_walkforward.json"
OUT_MD = ROOT / "Docs" / "V24D_FINAL_SIZING.md"


# =====================================================================
# Core replay  (halt-aware, Merton-γ sizer)
# =====================================================================
def replay(trades: List[TradeMeta], base_f: float, gamma: float,
           cap_mult: float = 5.0, halt_pct: float = 0.04,
           halt_enabled: bool = True,
           start_equity: float = START_EQUITY) -> Dict[str, Any]:
    sizer = MertonGZWrapper(gamma=gamma, base_f=base_f, cap_mult=cap_mult,
                            dd_cap=halt_pct)
    outer_cap = base_f * cap_mult
    hist = History(equity=start_equity, peak=start_equity, start_equity=start_equity)
    halt = DailyHalt(halt_pct=halt_pct) if halt_enabled else None

    diary = []
    skipped = 0
    for tr in trades:
        if halt is not None and not halt.can_trade(tr.entry_time, hist.equity):
            skipped += 1
            continue
        f = sizer.size(hist, tr)
        f = max(0.0, min(outer_cap, f))
        risk = hist.equity * f
        pnl = float(tr.realised_R) * risk
        hist.feedback(tr, pnl)
        sizer.on_closed(tr, float(tr.realised_R))
        diary.append({"t": float(tr.entry_time), "sym": tr.symbol,
                      "R": float(tr.realised_R), "f": f, "pnl": pnl,
                      "equity": hist.equity, "peak": hist.peak,
                      "dd": hist.dd_pct()})
    return {"diary": diary, "skipped": skipped,
            "halts": halt.total_halts if halt else 0,
            "halt_dates": halt.halted_dates[:] if halt else [],
            "final_equity": hist.equity, "peak": hist.peak}


def metrics(diary, start_equity=START_EQUITY):
    if not diary:
        return {"n": 0, "wr": 0, "pnl": 0, "dd": 0, "wrst": 0,
                "pf": 0, "sharpe": 0, "calmar": 0}
    pnls = np.array([d["pnl"] for d in diary])
    eq = np.array([d["equity"] for d in diary])
    wins = (pnls > 0).sum()
    gw = pnls[pnls > 0].sum(); gl = -pnls[pnls < 0].sum()
    pf = gw / gl if gl > 0 else (999.0 if gw > 0 else 0)
    peak = -np.inf; dd = 0
    for v in eq:
        if v > peak: peak = v
        if peak > 0 and (peak - v)/peak > dd: dd = (peak - v)/peak
    by_day = defaultdict(float)
    for d in diary:
        day = datetime.fromtimestamp(d["t"], tz=timezone.utc).date()
        by_day[day] += d["pnl"]
    wrst = (-min(by_day.values())/start_equity*100) if by_day else 0
    sharpe = pnls.mean()/pnls.std()*math.sqrt(252) if pnls.std()>0 else 0
    ret = (eq[-1]-start_equity)/start_equity*100
    cal = ret/(dd*100) if dd>0 else 0
    return {"n": int(len(pnls)), "wr": float(wins/len(pnls)*100),
            "pnl": float(eq[-1]-start_equity), "ret_pct": float(ret),
            "dd": float(dd*100), "wrst": float(wrst),
            "pf": float(pf), "sharpe": float(sharpe), "calmar": float(cal)}


def bootstrap_ruin(pnls, start_equity, thresholds,
                   n_paths=BOOTSTRAP_PATHS, avg_block=BOOTSTRAP_BLOCK, seed=42):
    rng = np.random.default_rng(seed)
    arr = np.array(pnls, dtype=float); n = len(arr)
    if n == 0: return {f"ruin@{t*100:.0f}%": 0 for t in thresholds}
    p = 1.0/avg_block
    counts = {f"ruin@{t*100:.0f}%": 0 for t in thresholds}
    for _ in range(n_paths):
        path = np.empty(n); k = rng.integers(0, n)
        for j in range(n):
            if rng.random() < p: k = rng.integers(0, n)
            path[j] = arr[k]; k = (k+1) % n
        eq = start_equity + path.cumsum()
        rp = np.maximum.accumulate(np.concatenate([[start_equity], eq]))[1:]
        md = float(((rp-eq)/rp).max())
        for thr in thresholds:
            if md >= thr: counts[f"ruin@{thr*100:.0f}%"] += 1
    return {k: v/n_paths*100 for k, v in counts.items()}


# =====================================================================
# PART 1 — base_f × γ × halt sweep
# =====================================================================
def part1_sweep(trades: List[TradeMeta]) -> List[Dict[str, Any]]:
    print("\n" + "="*88)
    print("  PART 1 — base_f × γ × halt sweep")
    print("="*88)
    base_fs = [0.0011, 0.0015, 0.0020, 0.0025, 0.0030, 0.0040]
    gammas = [1.5, 2.0, 3.0]
    halts = [True]   # halt always ON for live config
    thresholds = [0.03, 0.04, 0.05, 0.06, 0.08, 0.10]

    rows = []
    print(f"  {'base_f':>8} {'γ':>4} {'halt':>5} "
          f"{'N':>4} {'PnL':>10} {'DD%':>6} {'Wrst%':>6} "
          f"{'PF':>5} {'Calmar':>7} {'R@4%':>6} {'R@5%':>6} {'Halts':>5}")
    for bf in base_fs:
        for g in gammas:
            for h in halts:
                rep = replay(trades, base_f=bf, gamma=g, halt_enabled=h)
                m = metrics(rep["diary"])
                ru = bootstrap_ruin([d["pnl"] for d in rep["diary"]],
                                    START_EQUITY, thresholds)
                row = {**m, **ru, "base_f": bf, "gamma": g, "halt": h,
                       "base_f_pct": bf*100,
                       "halts_triggered": rep["halts"],
                       "halt_dates": rep["halt_dates"],
                       "skipped": rep["skipped"]}
                rows.append(row)
                print(f"  {bf*100:>7.3f}% {g:>4.1f} {str(h):>5} "
                      f"{m['n']:>4d} ${m['pnl']:>+9,.0f} "
                      f"{m['dd']:>5.2f}% {m['wrst']:>5.2f}% "
                      f"{m['pf']:>4.2f} {m['calmar']:>6.2f} "
                      f"{ru.get('ruin@4%',0):>5.1f}% "
                      f"{ru.get('ruin@5%',0):>5.1f}% "
                      f"{rep['halts']:>5d}")
    return rows


# =====================================================================
# PART 2 — adversarial stress-test scenarios
# =====================================================================
def _apply_edge_decay(trades, factor=0.70):
    out = []
    for t in trades:
        t2 = copy.copy(t)
        t2.realised_R = float(t.realised_R) * factor
        out.append(t2)
    return out


def _apply_fat_tail(trades, shock_R=-5.0, every=20):
    out = []
    for i, t in enumerate(trades):
        if i > 0 and i % every == 0:
            shock = copy.copy(t); shock.realised_R = shock_R
            out.append(shock)
        out.append(t)
    return out


def _apply_vol_spike(trades, fraction=0.10, factor=2.0, seed=7):
    rng = np.random.default_rng(seed)
    n = len(trades); k = int(n * fraction)
    idx = set(rng.choice(n, size=k, replace=False).tolist())
    out = []
    for i, t in enumerate(trades):
        if i in idx:
            t2 = copy.copy(t)
            t2.realised_R = float(t.realised_R) * factor
            out.append(t2)
        else:
            out.append(t)
    return out


def _apply_streak(trades, k=10):
    sorted_t = sorted(trades, key=lambda t: t.realised_R)
    worst = sorted_t[:k]
    rest = [t for t in trades if t not in worst]
    worst_sorted = sorted(worst, key=lambda t: t.entry_time)
    if len(rest) < len(trades) // 3:
        mid = len(trades) // 2
        return trades[:mid-k//2] + worst_sorted + trades[mid+k//2:]
    insertion = len(rest) // 2
    return rest[:insertion] + worst_sorted + rest[insertion:]


def part2_stress(trades: List[TradeMeta], base_f: float, gamma: float
                 ) -> Dict[str, Any]:
    print("\n" + "="*88)
    print(f"  PART 2 — adversarial stress-test  (base_f={base_f*100:.3f}%, γ={gamma})")
    print("="*88)
    scenarios = {
        "Normal": trades,
        "EdgeDecay_70pct": _apply_edge_decay(trades, 0.70),
        "FatTail_-5R_every20": _apply_fat_tail(trades, -5.0, 20),
        "VolSpike_10pct_2x": _apply_vol_spike(trades, 0.10, 2.0),
        "WorstStreak_10": _apply_streak(trades, 10),
    }
    thresholds = [0.04, 0.05, 0.10]
    results = {}
    print(f"  {'Scenario':<24} {'N':>4} {'PnL':>10} {'DD%':>6} {'Wrst%':>6} "
          f"{'PF':>5} {'R@4%':>6} {'R@5%':>6} {'R@10%':>6} {'Halts':>5}")
    for name, trs in scenarios.items():
        rep = replay(list(trs), base_f=base_f, gamma=gamma, halt_enabled=True)
        m = metrics(rep["diary"])
        ru = bootstrap_ruin([d["pnl"] for d in rep["diary"]],
                            START_EQUITY, thresholds)
        row = {**m, **ru, "halts": rep["halts"],
               "halt_dates": rep["halt_dates"],
               "skipped": rep["skipped"]}
        results[name] = row
        print(f"  {name:<24} {m['n']:>4d} ${m['pnl']:>+9,.0f} "
              f"{m['dd']:>5.2f}% {m['wrst']:>5.2f}% {m['pf']:>4.2f} "
              f"{ru.get('ruin@4%',0):>5.1f}% {ru.get('ruin@5%',0):>5.1f}% "
              f"{ru.get('ruin@10%',0):>5.1f}% {rep['halts']:>5d}")
    return results


# =====================================================================
# PART 3 — walk-forward IS→OOS validation
# =====================================================================
def part3_walkforward(trades: List[TradeMeta]) -> Dict[str, Any]:
    print("\n" + "="*88)
    print("  PART 3 — walk-forward  (IS 70 % → OOS 30 %)")
    print("="*88)
    # trades are already in chrono order within each symbol; order by entry_time
    ordered = sorted(trades, key=lambda t: t.entry_time)
    split = int(len(ordered) * 0.70)
    is_tr = ordered[:split]; oos_tr = ordered[split:]
    print(f"  IS  trades: {len(is_tr)}  ({datetime.fromtimestamp(is_tr[0].entry_time, tz=timezone.utc).date()} → {datetime.fromtimestamp(is_tr[-1].entry_time, tz=timezone.utc).date()})")
    print(f"  OOS trades: {len(oos_tr)}  ({datetime.fromtimestamp(oos_tr[0].entry_time, tz=timezone.utc).date()} → {datetime.fromtimestamp(oos_tr[-1].entry_time, tz=timezone.utc).date()})")

    # IS: sweep, pick Pareto champion — highest PnL such that ruin@4 % < 2
    base_fs = [0.0011, 0.0015, 0.0020, 0.0025, 0.0030]
    gammas = [1.5, 2.0, 3.0]
    is_rows = []
    for bf in base_fs:
        for g in gammas:
            rep = replay(is_tr, base_f=bf, gamma=g, halt_enabled=True)
            m = metrics(rep["diary"])
            ru = bootstrap_ruin([d["pnl"] for d in rep["diary"]],
                                START_EQUITY, [0.04, 0.05, 0.10])
            is_rows.append({**m, **ru, "base_f": bf, "gamma": g,
                            "base_f_pct": bf*100})
    # Pick: highest PnL where ruin@4 % < 2 %
    viable = [r for r in is_rows if r.get("ruin@4%", 100) < 2.0 and r["dd"] < 4.0]
    pick = max(viable, key=lambda r: r["pnl"]) if viable else max(
        is_rows, key=lambda r: r["pnl"])

    print(f"\n  IS champion:  base_f={pick['base_f']*100:.3f}%  γ={pick['gamma']}  "
          f"PnL=${pick['pnl']:+,.0f}  DD={pick['dd']:.2f}%  "
          f"ruin@4 %={pick.get('ruin@4%',0):.1f}%")

    # OOS: evaluate IS pick on unseen 30 %
    rep_oos = replay(oos_tr, base_f=pick["base_f"], gamma=pick["gamma"],
                     halt_enabled=True)
    m_oos = metrics(rep_oos["diary"])
    ru_oos = bootstrap_ruin([d["pnl"] for d in rep_oos["diary"]],
                            START_EQUITY, [0.04, 0.05, 0.10])
    print(f"  OOS perform: PnL=${m_oos['pnl']:+,.0f}  DD={m_oos['dd']:.2f}%  "
          f"Wrst={m_oos['wrst']:.2f}%  ruin@4 %={ru_oos.get('ruin@4%',0):.1f}%  "
          f"halts={rep_oos['halts']}")

    # Anti-overfitting ratio:  OOS.pnl / IS.pnl should stay reasonable
    is_per_trade = pick["pnl"] / max(pick["n"], 1)
    oos_per_trade = m_oos["pnl"] / max(m_oos["n"], 1)
    ratio = oos_per_trade / is_per_trade if is_per_trade else 0
    print(f"  IS→OOS $/trade decay ratio: {ratio:.2f}  "
          f"(< 0.3 = overfit, > 0.7 = robust)")

    return {"is_pick": pick, "is_rows": is_rows,
            "oos": {**m_oos, **ru_oos, "halts": rep_oos["halts"]},
            "is_oos_ratio": ratio}


# =====================================================================
# Main
# =====================================================================
def main() -> int:
    print("="*88)
    print("  v24d  —  base_f × γ sweep  +  stress-test  +  walk-forward")
    print(f"  {datetime.now().isoformat(timespec='seconds')}")
    print("="*88)
    with open(TRADE_FILE) as f: trades = [TradeMeta(**t) for t in json.load(f)["trades"]]
    print(f"  loaded {len(trades)} trades")

    # Part 1
    sweep = part1_sweep(trades)
    with open(OUT_SWEEP, "w") as f: json.dump(sweep, f, indent=2)

    # Pick sweep champion
    viable = [r for r in sweep if r.get("ruin@4%", 100) < 2.0
              and r["dd"] < 4.0 and r["wrst"] < 4.3]
    champion = max(viable, key=lambda r: r["pnl"]) if viable else max(
        sweep, key=lambda r: r["pnl"])
    print("\n" + "="*88)
    print(f"  SWEEP CHAMPION: base_f={champion['base_f']*100:.3f}%  "
          f"γ={champion['gamma']}")
    print(f"     PnL=${champion['pnl']:+,.0f}  DD={champion['dd']:.2f}%  "
          f"Wrst={champion['wrst']:.2f}%  PF={champion['pf']:.2f}  "
          f"Calmar={champion['calmar']:.2f}")
    print(f"     Ruin@4%={champion.get('ruin@4%',0):.2f}%  "
          f"Ruin@5%={champion.get('ruin@5%',0):.2f}%  "
          f"Ruin@10%={champion.get('ruin@10%',0):.2f}%")
    print("="*88)

    # Part 2
    stress = part2_stress(trades, champion["base_f"], champion["gamma"])
    with open(OUT_STRESS, "w") as f: json.dump(stress, f, indent=2)

    # Part 3
    wf = part3_walkforward(trades)
    with open(OUT_WF, "w") as f: json.dump(wf, f, indent=2)

    # =================================================================
    # Markdown report
    # =================================================================
    md = [f"# V24d — FINAL SIZING RECOMMENDATION\n\n"]
    md.append(f"Generated {datetime.now().isoformat(timespec='seconds')}\n\n")
    md.append("## 🏆 CHAMPION CONFIG\n\n")
    md.append(f"| | value |\n|---|---|\n")
    md.append(f"| **base_f** | **{champion['base_f']*100:.3f} %** "
              f"(live: `base_risk_pct = {champion['base_f']:.4f}`) |\n")
    md.append(f"| **γ (risk aversion)** | **{champion['gamma']}** |\n")
    md.append(f"| **cap_mult** | 5 (outer safety belt — rarely bites) |\n")
    md.append(f"| **daily halt** | 4 % hard kill-switch (always ON) |\n")
    md.append(f"\n### 3-month performance\n\n")
    md.append(f"| metric | value |\n|---|---|\n")
    md.append(f"| PnL | **${champion['pnl']:+,.0f}** "
              f"(annualised ≈ ${champion['pnl']*4:+,.0f}) |\n")
    md.append(f"| Max DD | **{champion['dd']:.2f} %** "
              f"(vs 10 % line: {10/max(champion['dd'],0.01):.2f}× margin) |\n")
    md.append(f"| Worst day | **{champion['wrst']:.2f} %** "
              f"(vs 5 % line: {5/max(champion['wrst'],0.01):.2f}× margin) |\n")
    md.append(f"| Profit factor | {champion['pf']:.2f} |\n")
    md.append(f"| Calmar ratio | {champion['calmar']:.2f} |\n")
    md.append(f"| Ruin@4 % | {champion.get('ruin@4%',0):.2f} % |\n")
    md.append(f"| Ruin@5 % | {champion.get('ruin@5%',0):.2f} % |\n")
    md.append(f"| Ruin@10 % | {champion.get('ruin@10%',0):.2f} % |\n")

    md.append(f"\n## Full base_f × γ sweep\n\n")
    md.append("| base_f | γ | N | PnL | DD% | Wrst% | PF | Calmar | R@4% | R@5% | Halts |\n")
    md.append("|---|---|---|---|---|---|---|---|---|---|---|\n")
    for r in sweep:
        star = " ⭐" if (r["base_f"] == champion["base_f"]
                         and r["gamma"] == champion["gamma"]) else ""
        md.append(f"| {r['base_f']*100:.3f}%{star} | {r['gamma']} | {r['n']} "
                  f"| ${r['pnl']:+,.0f} | {r['dd']:.2f}% "
                  f"| {r['wrst']:.2f}% | {r['pf']:.2f} "
                  f"| {r['calmar']:.2f} | {r.get('ruin@4%',0):.1f}% "
                  f"| {r.get('ruin@5%',0):.1f}% | {r['halts_triggered']} |\n")

    md.append(f"\n## Adversarial stress test\n\n")
    md.append(f"Champion config hit with FIVE nasty scenarios:\n\n")
    md.append("| Scenario | N | PnL | DD% | Wrst% | PF | R@4% | R@5% | R@10% |\n")
    md.append("|---|---|---|---|---|---|---|---|---|\n")
    for name, r in stress.items():
        md.append(f"| {name} | {r['n']} | ${r['pnl']:+,.0f} | {r['dd']:.2f}% "
                  f"| {r['wrst']:.2f}% | {r['pf']:.2f} "
                  f"| {r.get('ruin@4%',0):.1f}% | {r.get('ruin@5%',0):.1f}% "
                  f"| {r.get('ruin@10%',0):.1f}% |\n")

    md.append(f"\n## Walk-forward IS → OOS\n\n")
    p = wf["is_pick"]; o = wf["oos"]
    md.append(f"- IS pick:   base_f = **{p['base_f']*100:.3f} %**, γ = **{p['gamma']}** "
              f"(PnL ${p['pnl']:+,.0f}, DD {p['dd']:.2f}%)\n")
    md.append(f"- OOS test:  PnL **${o['pnl']:+,.0f}** (DD {o['dd']:.2f}%, "
              f"Wrst {o['wrst']:.2f}%, halts {o['halts']})\n")
    md.append(f"- IS→OOS $/trade decay ratio: **{wf['is_oos_ratio']:.2f}**  "
              f"(< 0.3 overfit  /  > 0.7 robust  /  0.3-0.7 normal edge decay)\n")

    md.append(f"\n## Recommendation\n\n")
    stress_worst_dd = max(r["dd"] for r in stress.values())
    stress_worst_ruin = max(r.get("ruin@4%", 0) for r in stress.values())
    all_stress_profitable = all(r["pnl"] > 0 for r in stress.values())
    wf_robust = wf["is_oos_ratio"] > 0.3 and o["dd"] < 4.0
    if stress_worst_dd < 4.0 and stress_worst_ruin < 5.0 and \
       all_stress_profitable and wf_robust:
        md.append("**✅ GO LIVE** — all three validations passed:\n")
        md.append(f"- every stress scenario profitable AND DD < 4 %\n")
        md.append(f"- IS→OOS decay ratio {wf['is_oos_ratio']:.2f} (> 0.3)\n")
        md.append(f"- OOS DD {o['dd']:.2f} % (< 4 %)\n\n")
        md.append(f"### Action\n")
        md.append(f"1. Edit `src/live/v23_live.py`: set `base_risk_pct = "
                  f"{champion['base_f']:.4f}` and `gamma = {champion['gamma']}`\n")
        md.append(f"2. Enable daily halt module in live engine\n")
        md.append(f"3. Dry-run smoke test → commit → push → VPS\n")
    else:
        md.append("**⚠️ HOLD** — one of the gates failed.  Review in detail:\n")
        md.append(f"- worst stress-scenario DD: {stress_worst_dd:.2f} % "
                  f"{'❌' if stress_worst_dd >= 4.0 else '✅'}\n")
        md.append(f"- worst stress-scenario ruin@4 %: {stress_worst_ruin:.1f} % "
                  f"{'❌' if stress_worst_ruin >= 5.0 else '✅'}\n")
        md.append(f"- all stress scenarios profitable: {all_stress_profitable} "
                  f"{'✅' if all_stress_profitable else '❌'}\n")
        md.append(f"- OOS DD < 4 %: {o['dd']:.2f} % "
                  f"{'✅' if o['dd'] < 4 else '❌'}\n")
        md.append(f"- IS→OOS ratio > 0.3: {wf['is_oos_ratio']:.2f} "
                  f"{'✅' if wf['is_oos_ratio'] > 0.3 else '❌'}\n")

    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.writelines(md)
    print(f"\n  wrote {OUT_MD.relative_to(ROOT)}")
    print(f"         {OUT_SWEEP.relative_to(ROOT)}")
    print(f"         {OUT_STRESS.relative_to(ROOT)}")
    print(f"         {OUT_WF.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
