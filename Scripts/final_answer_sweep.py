#!/usr/bin/env python3
"""ONE final sweep. Answers: best return, lowest DD, best pairs. No jargon.

v2 (2026-04-23) — now runs on ALL 8 freshly-pulled 5ers MTB symbols:
  DE40 / US30 / US500 / US100 / UK100 / JP225 / XAUUSD / XAGUSD

Refuses to run unless ``data/historical/_provenance.json`` shows every
symbol was pulled from the live 5ers server (FivePercentOnline-Real).
Commission is modelled per-symbol by ``smartbb_engine.round_trip_commission``
(XAU/XAG pay 0.001%-of-notional per deal × 2 deals; indices pay zero;
slippage pays 1 tick each side of every fill).
"""
import sys, json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from Scripts.backtest_v22_phase_b import (
    SYMBOLS, MertonGZSizerConfig, run_portfolio, apply_full_safety_rails,
)
from Scripts.backtest_v22_lean_uk5 import stats
import numpy as np

BALANCE = 100_000.0
REQUIRED_SERVER = "FivePercentOnline-Real"
REQUIRED_SYMBOLS = ["US30", "US100", "US500", "DE40",
                    "UK100", "JP225", "XAUUSD", "XAGUSD"]
PROVENANCE_FILE = ROOT / "data" / "historical" / "_provenance.json"


def _check_provenance():
    if not PROVENANCE_FILE.exists():
        raise SystemExit(
            f"[X] {PROVENANCE_FILE} missing.\n"
            f"    Run  python Scripts\\download_5ers_3month.py  first."
        )
    with open(PROVENANCE_FILE, "r", encoding="utf-8") as f:
        prov = json.load(f)
    problems = []
    for sym in REQUIRED_SYMBOLS:
        if sym not in prov:
            problems.append(f"  [X] {sym}: NOT in provenance file")
            continue
        p = prov[sym]
        if p.get("server") != REQUIRED_SERVER:
            problems.append(f"  [X] {sym}: server={p.get('server')}  "
                            f"(expected {REQUIRED_SERVER})")
        if p.get("bars", 0) < 50_000:
            problems.append(f"  [!] {sym}: only {p.get('bars',0)} bars")
    if problems:
        print("PROVENANCE CHECK FAILED:")
        for p in problems: print(p)
        raise SystemExit(2)
    print("  [OK] Provenance verified: all 8 symbols from "
          f"{REQUIRED_SERVER} (account "
          f"{prov[REQUIRED_SYMBOLS[0]].get('login','?')})")
    print(f"       downloaded {prov[REQUIRED_SYMBOLS[0]].get('downloaded_utc','?')}")


def _ruin_prob(pnls, n_paths=500, avg_block=5, dd_thresh=4.0, seed=42):
    """Stationary-block bootstrap the PnL stream, compute % paths that touch >dd_thresh% DD."""
    n = len(pnls)
    if n < 10: return float('nan')
    rng = np.random.default_rng(seed)
    p = 1.0 / avg_block
    hits = 0
    for _ in range(n_paths):
        seq = np.empty(n, dtype=float)
        i = 0
        while i < n:
            start = rng.integers(0, n)
            while i < n and (i == 0 or rng.random() >= p):
                seq[i] = pnls[(start + i) % n]  # noqa — want index from start
                i += 1
                if rng.random() < p: break
        equity = BALANCE + np.cumsum(seq)
        peak = np.maximum.accumulate(equity)
        dd_pct = (peak - equity) / peak * 100.0
        if dd_pct.max() > dd_thresh: hits += 1
    return hits / n_paths * 100.0


def run(symbols, base_risk):
    cfg = MertonGZSizerConfig(
        base_risk_pct=base_risk, cap_mult=3.0, gamma=2.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )
    raw, _, _, _, _ = run_portfolio(symbols, cfg)
    tr = apply_full_safety_rails(raw, slippage_ticks=1.0)
    s = stats(tr)
    pnls = np.array([t.net_pnl for t in tr], dtype=float)
    ruin4 = _ruin_prob(pnls) if len(pnls) >= 10 else float('nan')
    return s, ruin4


print("=" * 108)
print("  FINAL ANSWER (v2) — one sweep. 1-tick slippage + full safety rails + "
      "Merton-GZ sizer + provenance gate.")
print("=" * 108)

_check_provenance()

# ----------------------------------------------------------------------
# Portfolio sets  —  v3 comprehensive: solo all 8, plus every meaningful
# multi-symbol combination.  Every symbol pulled fresh from 5ers MTB.
# ----------------------------------------------------------------------
# Solos — answers "does THIS symbol have a live edge on its own?"
SOLO_SETS = [
    ("SOLO  DE40      (London 08:00)               ", ["DE40"]),
    ("SOLO  US30      (NY 14:30)                   ", ["US30"]),
    ("SOLO  US500     (NY 14:30, 15-min OR)        ", ["US500"]),
    ("SOLO  US100     (NY 14:30) [NEW]             ", ["US100"]),
    ("SOLO  UK100     (London 08:00)               ", ["UK100"]),
    ("SOLO  JP225     (Tokyo 00:00) [NEW]          ", ["JP225"]),
    ("SOLO  XAUUSD    (NY 14:30)                   ", ["XAUUSD"]),
    ("SOLO  XAGUSD    (NY 14:30) [NEW]             ", ["XAGUSD"]),
]

# Portfolios
SET_8  = ["DE40", "US30", "US500", "US100", "UK100", "JP225", "XAUUSD", "XAGUSD"]
SET_7A = ["DE40", "US30", "US500", "US100", "UK100", "JP225", "XAUUSD"]   # drop XAG
SET_7B = ["DE40", "US30", "US500", "US100", "UK100", "XAUUSD", "XAGUSD"]  # drop JP
SET_6A = ["DE40", "US30", "US500", "US100", "XAUUSD", "XAGUSD"]           # metals + 4 US
SET_6B = ["DE40", "US30", "US500", "UK100", "JP225", "XAUUSD"]            # diversified
SET_6C = ["DE40", "US30", "US500", "US100", "XAUUSD", "UK100"]            # 4 US + UK + XAU
SET_5A = ["DE40", "US30", "XAUUSD", "US500", "UK100"]                     # Phase-A champ
SET_5B = ["DE40", "US30", "XAUUSD", "US500", "US100"]                     # 4 US + XAU (no UK/JP)
SET_4A = ["DE40", "US30", "XAUUSD", "US500"]                              # core 4
SET_4B = ["DE40", "US30", "US100", "XAUUSD"]                              # 3 US + XAU
SET_3  = ["DE40", "US30", "XAUUSD"]
SET_2  = ["DE40", "US30"]
SET_METALS = ["XAUUSD", "XAGUSD"]                                          # metals only

RISK_LEVELS = [0.00075, 0.0010, 0.00125, 0.0015]   # 0.075% / 0.10% / 0.125% / 0.15%

PORTFOLIO_SETS = [
    ("8 pairs (ALL: 4 US + UK + JP + metals)       ", SET_8),
    ("7 pairs (drop XAG)                           ", SET_7A),
    ("7 pairs (drop JP225)                         ", SET_7B),
    ("6 pairs (4 US + both metals)                 ", SET_6A),
    ("6 pairs (DE+US30+US500+UK+JP+XAU)            ", SET_6B),
    ("6 pairs (4 US + UK + XAU)                    ", SET_6C),
    ("5 pairs (DE40+US30+XAU+US500+UK100) Phase-A  ", SET_5A),
    ("5 pairs (DE40+US30+XAU+US500+US100)          ", SET_5B),
    ("4 pairs (drop UK100) [previous WINNER]       ", SET_4A),
    ("4 pairs (DE40+US30+US100+XAU)                ", SET_4B),
    ("3 pairs (DE40+US30+XAU only)                 ", SET_3),
    ("2 pairs (DE40+US30 only)                     ", SET_2),
    ("2 pairs (XAU+XAG only — metals basket)       ", SET_METALS),
]

SETS = SOLO_SETS + PORTFOLIO_SETS

print(f"\n  {'PAIR SET':<48} {'RISK':>7} {'N':>4} {'PnL':>10} {'Ret%':>6} "
      f"{'DD%':>6} {'PF':>5} {'Sharpe':>7} {'RuinP(DD>4%)':>13}")
print("  " + "-" * 108)
results = []
for label, syms in SETS:
    for r in RISK_LEVELS:
        try:
            s, ruin = run(syms, r)
            row = dict(label=label.strip(), syms=syms, risk=r,
                       **{k:v for k,v in s.items()
                          if k in ('n','net','ret_pct','dd_pct','pf','sharpe','wr')},
                       ruin4=ruin)
            results.append(row)
            print(f"  {label:<48} {r*100:>5.3f}% {s['n']:>4} "
                  f"${s['net']:>+8,.0f} {s['ret_pct']:>+5.1f}% {s['dd_pct']:>5.2f}% "
                  f"{s['pf']:>5.2f} {s['sharpe']:>+6.2f} {ruin:>11.1f}%")
        except Exception as e:
            print(f"  {label:<48} {r*100:>5.3f}% ERROR: {e}")
    print()

# ---------- pick the winners ----------
# Filter A (STRICT, go-live): ruin4 < 2%  AND  DD < 3%  AND  N >= 20
safe = [r for r in results if r['ruin4'] < 2.0 and r['dd_pct'] < 3.0 and r['n'] >= 20]
# Filter B (RELAXED, discovery): ruin4 < 5%  AND  DD < 4%  AND  N >= 20
ok   = [r for r in results if r['ruin4'] < 5.0 and r['dd_pct'] < 4.0 and r['n'] >= 20]

def _line(title, r):
    print(f"\n  {title}")
    print(f"     Pairs    : {', '.join(r['syms'])}")
    print(f"     Risk     : {r['risk']*100:.3f}%  (Merton-GZ anchor)")
    print(f"     PnL      : ${r['net']:+,.0f}  ({r['ret_pct']:+.2f}% on $100k in ~3 months)")
    print(f"     Max DD   : {r['dd_pct']:.2f}%   (prop-firm cap is 4%)")
    print(f"     Profit F.: {r['pf']:.2f}  |  Sharpe: {r['sharpe']:+.2f}  |  WR: {r['wr']*100:.1f}%")
    print(f"     Ruin prob: {r['ruin4']:.1f}% chance of touching 4% DD")
    ann = r['net'] * (12.0 / 3.0)
    print(f"     Projected annual @ this pace: ${ann:+,.0f}  (no compounding)")

if safe:
    print("=" * 108)
    print("  THE WINNERS  —  STRICT filter (ruin<2%, DD<3%, N>=20)  [GO-LIVE SAFE]")
    print("=" * 108)
    _line("[BEST RETURN]",         max(safe, key=lambda r: r['net']))
    _line("[LOWEST DD (safest)]",  min(safe, key=lambda r: r['dd_pct']))
    _line("[BEST SHARPE]",         max(safe, key=lambda r: r['sharpe']))
elif ok:
    print("=" * 108)
    print("  NO CONFIG PASSED STRICT.  Showing RELAXED filter (ruin<5%, DD<4%, N>=20)")
    print("=" * 108)
    _line("[BEST RETURN (relaxed)]",        max(ok, key=lambda r: r['net']))
    _line("[LOWEST DD (relaxed)]",          min(ok, key=lambda r: r['dd_pct']))
    _line("[BEST SHARPE (relaxed)]",        max(ok, key=lambda r: r['sharpe']))
else:
    print("=" * 108)
    print("  NO CONFIG PASSED ANY FILTER. Review raw results above.")
    print("=" * 108)

# save results
Path("Results").mkdir(exist_ok=True)
with open("Results/final_answer_sweep.json", "w") as f:
    json.dump(results, f, indent=2)
print(f"\n  Saved: Results/final_answer_sweep.json")
print("=" * 108)
