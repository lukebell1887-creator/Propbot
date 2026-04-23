#!/usr/bin/env python3
"""
research_new_edges.py — Turn each DNA-survivor into a tradeable micro-strategy
and measure the honest $ PnL after real fees.

Input  : Results/market_dna_edges.json  (124 candidate edges, 18 survivors)
Output : Results/research_new_edges.txt / .json

For each survivor we simulate a simple rule-based strategy on the 3-month
M1 data and apply the EXACT same cost model used by the rest of the bot
(src/smartbb_engine.SMARTBB_UNIVERSE).

Rules:
  * autocorr_hHH_lagL momentum (holdout r > 0):
        at HH:00 UTC, observe last-L-min return r_prev.
        if |r_prev| > 0.4 × ATR_M1(20), enter in direction(r_prev),
        risk = 0.10 % (unit).  Hold L minutes, exit at next bar close.
        SL = 1.0 × ATR(20) against,  TP = 1.5 × ATR(20) with.

  * autocorr_hHH_lagL reversal (holdout r < 0):
        same but enter OPPOSITE to r_prev.

  * followthrough_hHH (holdout effect < 0):
        at HH:00 UTC, if last-5min |r| > 1σ (rolling 60-bar stdev),
        enter opposite.  Hold 15 minutes, exit at close.
        SL=ATR, TP=1×ATR.

  * or_predicts_post_range:  META-EDGE, not a standalone strategy.
        Reported only; not tradeable on its own.

For each edge we report:
  N trades, WR, avg $/trade, net $ PnL, PF, DD%, costs_$, cost/edge ratio.
"""
from __future__ import annotations
import csv, json, math, statistics, sys
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try: sys.stdout.reconfigure(encoding="utf-8")
except Exception: pass

from src.smartbb_engine import SMARTBB_UNIVERSE, SymbolSpec

BALANCE = 100_000.0
RISK_PCT = 0.001                 # unit 0.10% per trade
MONTHS = 3
MIN_N_FOR_KEEP = 10              # minimum trades to even consider the edge


def load_m1(path: Path):
    out = []
    with open(path, "r", newline="") as f:
        for row in csv.DictReader(f):
            try: t = datetime.fromisoformat(row["time"])
            except Exception:
                t = datetime.strptime(row["time"], "%Y-%m-%d %H:%M:%S")
            out.append((t, float(row["open"]), float(row["high"]),
                        float(row["low"]), float(row["close"])))
    return out


def atr20(bars: List[tuple], i: int) -> float:
    """Simple ATR(20) on M1 bars — high-low average, no gap handling."""
    if i < 20: return 0.0
    s = 0.0
    for k in range(i - 20, i):
        _, _, h, l, _ = bars[k]
        s += (h - l)
    return s / 20.0


@dataclass
class EdgeResult:
    edge_name: str
    symbol: str
    kind: str            # "momentum" | "reversal" | "followthrough"
    hour: int
    lag: int
    n: int
    wr: float
    net_pnl: float
    pf: float
    max_dd_pct: float
    avg_per_trade: float
    total_cost: float
    verdict: str         # "KEEP" | "MARGINAL" | "DROP" | "SKIP"


def simulate_autocorr(edge: dict, bars: List[tuple], spec: SymbolSpec,
                      kind: str) -> EdgeResult:
    """Simulate autocorrelation edge at specified hour+lag."""
    name = edge["name"]
    symbol = edge["symbol"]
    # parse hour + lag from name  "autocorr_h{HH}_lag{L}"
    try:
        parts = name.replace("autocorr_h", "").split("_lag")
        hour = int(parts[0])
        lag = int(parts[1])
    except Exception:
        return EdgeResult(name, symbol, kind, 0, 0, 0, 0, 0, 0, 0, 0, 0, "SKIP")

    # Only fire once per (day, hour) — first matching bar each day
    seen_day_hour = set()
    equity = BALANCE; peak = BALANCE; mdd = 0.0
    pnls = []; costs = []
    i = max(lag + 20, 60)
    N = len(bars)
    while i < N - lag - 5:
        t, o, h, l, c = bars[i]
        if t.hour != hour or t.minute != 0:
            i += 1; continue
        key = (t.date(), hour)
        if key in seen_day_hour:
            i += 1; continue
        seen_day_hour.add(key)
        # compute lag return r = close[i] - close[i-lag]
        prev_close = bars[i - lag][4]
        r_prev = c - prev_close
        atr = atr20(bars, i)
        if atr <= 0:
            i += 1; continue
        if abs(r_prev) < 0.4 * atr:
            i += 1; continue
        # entry side
        if kind == "momentum":
            side = 1 if r_prev > 0 else -1
        else:
            side = -1 if r_prev > 0 else 1
        entry = c
        # look forward lag minutes (cap at 15) for exit / SL / TP
        horizon = min(lag, 15) if lag >= 5 else 5
        sl_dist = 1.0 * atr
        tp_dist = 1.5 * atr
        sl_price = entry - side * sl_dist
        tp_price = entry + side * tp_dist
        exit_price = c
        exit_reason = "time"
        for j in range(1, horizon + 1):
            if i + j >= N: break
            tt, oo, hh, ll, cc = bars[i + j]
            # intrabar order: check stop first (conservative)
            if side > 0:
                if ll <= sl_price:
                    exit_price = sl_price; exit_reason = "sl"; break
                if hh >= tp_price:
                    exit_price = tp_price; exit_reason = "tp"; break
            else:
                if hh >= sl_price:
                    exit_price = sl_price; exit_reason = "sl"; break
                if ll <= tp_price:
                    exit_price = tp_price; exit_reason = "tp"; break
            exit_price = cc
        # size: risk_d / sl_dist × pip_value (in lots)
        risk_d = RISK_PCT * equity
        if sl_dist <= 0:
            i += 1; continue
        lots = risk_d / (sl_dist * spec.pip_value)
        lots = max(spec.min_lots, round(lots / spec.lot_step) * spec.lot_step)
        lots = min(lots, spec.max_lots)
        price_move = side * (exit_price - entry)
        gross = price_move * spec.pip_value * lots
        spread_cost = spec.spread_pts * spec.pip_value * lots      # one-way on exit ~ symmetric
        commission = spec.round_trip_commission(entry, lots)
        net = gross - spread_cost - commission
        pnls.append(net)
        costs.append(spread_cost + commission)
        equity += net
        if equity > peak: peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0.0
        mdd = max(mdd, dd)
        i += horizon + 1          # avoid overlapping entries
    n = len(pnls)
    if n == 0:
        return EdgeResult(name, symbol, kind, hour, lag, 0, 0, 0, 0, 0, 0, 0, "SKIP")
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gw = sum(wins); gl = -sum(losses)
    pf = gw / gl if gl > 0 else (99.0 if gw > 0 else 0.0)
    net_pnl = sum(pnls)
    avg = net_pnl / n
    wr = len(wins) / n
    # Verdict rules: keep if N≥10, PF≥1.10, WR≥0.50, PnL>0, DD<2%
    if n < MIN_N_FOR_KEEP:
        v = "SKIP"
    elif net_pnl > 0 and pf >= 1.10 and wr >= 0.50 and mdd < 0.02:
        v = "KEEP"
    elif net_pnl > 0 and pf >= 1.00:
        v = "MARGINAL"
    else:
        v = "DROP"
    return EdgeResult(name, symbol, kind, hour, lag, n, wr, net_pnl, pf,
                      mdd * 100, avg, sum(costs), v)


def simulate_followthrough(edge: dict, bars: List[tuple], spec: SymbolSpec) -> EdgeResult:
    """followthrough_hHH — fade 1σ moves at hour HH."""
    name = edge["name"]
    symbol = edge["symbol"]
    try:
        hour = int(name.replace("followthrough_h", ""))
    except Exception:
        return EdgeResult(name, symbol, "followthrough", 0, 0, 0, 0, 0, 0, 0, 0, 0, "SKIP")
    seen = set()
    equity = BALANCE; peak = equity; mdd = 0.0
    pnls = []; costs = []
    i = 60
    N = len(bars)
    ret_window = deque(maxlen=60)
    while i < N - 15:
        t, o, h, l, c = bars[i]
        prev_close = bars[i - 1][4]
        ret_window.append(c - prev_close)
        if t.hour == hour and t.minute == 0:
            key = t.date()
            if key not in seen and len(ret_window) >= 30:
                seen.add(key)
                last_5m_ret = c - bars[i - 5][4]
                sig = statistics.pstdev(ret_window) if len(ret_window) > 1 else 0.0
                atr = atr20(bars, i)
                if sig > 0 and abs(last_5m_ret) > sig and atr > 0:
                    # fade
                    side = -1 if last_5m_ret > 0 else 1
                    entry = c
                    sl_dist = 1.0 * atr
                    tp_dist = 1.0 * atr
                    sl_price = entry - side * sl_dist
                    tp_price = entry + side * tp_dist
                    exit_price = c
                    for j in range(1, 16):
                        if i + j >= N: break
                        _, _, hh, ll, cc = bars[i + j]
                        if side > 0:
                            if ll <= sl_price: exit_price = sl_price; break
                            if hh >= tp_price: exit_price = tp_price; break
                        else:
                            if hh >= sl_price: exit_price = sl_price; break
                            if ll <= tp_price: exit_price = tp_price; break
                        exit_price = cc
                    risk_d = RISK_PCT * equity
                    lots = risk_d / (sl_dist * spec.pip_value)
                    lots = max(spec.min_lots, round(lots / spec.lot_step) * spec.lot_step)
                    lots = min(lots, spec.max_lots)
                    price_move = side * (exit_price - entry)
                    gross = price_move * spec.pip_value * lots
                    spread_cost = spec.spread_pts * spec.pip_value * lots
                    commission = spec.round_trip_commission(entry, lots)
                    net = gross - spread_cost - commission
                    pnls.append(net); costs.append(spread_cost + commission)
                    equity += net
                    if equity > peak: peak = equity
                    dd = (peak - equity) / peak if peak > 0 else 0.0
                    mdd = max(mdd, dd)
        i += 1
    n = len(pnls)
    if n == 0:
        return EdgeResult(name, symbol, "followthrough", hour, 0, 0, 0, 0, 0, 0, 0, 0, "SKIP")
    wins = [p for p in pnls if p > 0]; gw = sum(wins)
    gl = -sum(p for p in pnls if p <= 0)
    pf = gw / gl if gl > 0 else (99.0 if gw > 0 else 0.0)
    net = sum(pnls); wr = len(wins) / n
    if n < MIN_N_FOR_KEEP: v = "SKIP"
    elif net > 0 and pf >= 1.10 and wr >= 0.50 and mdd < 0.02: v = "KEEP"
    elif net > 0 and pf >= 1.00: v = "MARGINAL"
    else: v = "DROP"
    return EdgeResult(name, symbol, "followthrough", hour, 0, n, wr, net,
                      pf, mdd * 100, net / n, sum(costs), v)


def main():
    out_lines = []
    def p(s=""): print(s); out_lines.append(s)
    p("=" * 110)
    p("  research_new_edges  —  DNA-survivor micro-strategy test")
    p("=" * 110)

    edges_path = ROOT / "Results" / "market_dna_edges.json"
    if not edges_path.exists():
        p(f"ERROR: {edges_path} not found"); return 1
    with open(edges_path) as f: edges = json.load(f)["edges"]
    survivors = [e for e in edges if e.get("survives")]
    p(f"  loaded {len(edges)} candidates, {len(survivors)} survived DNA holdout")
    p("")

    # Load M1 for each symbol that has a surviving edge
    needed_syms = {e["symbol"] for e in survivors}
    data = {}
    for s in needed_syms:
        p_csv = ROOT / "data" / "historical" / f"{s}_M1.csv"
        if not p_csv.exists():
            p(f"  WARN: no data for {s}"); continue
        bars = load_m1(p_csv)
        # restrict to last MONTHS months
        if bars:
            cutoff = bars[-1][0] - timedelta(days=MONTHS * 31)
            bars = [b for b in bars if b[0] >= cutoff]
        data[s] = bars
        p(f"  {s}: {len(bars):,} M1 bars")
    p("")

    results: List[EdgeResult] = []
    for e in survivors:
        sym = e["symbol"]
        if sym not in data or not data[sym]:
            continue
        spec = SMARTBB_UNIVERSE.get(sym)
        if spec is None: continue
        name = e["name"]
        if name.startswith("autocorr_"):
            # is it momentum or reversal based on holdout effect sign?
            kind = "momentum" if e["holdout_effect"] > 0 else "reversal"
            r = simulate_autocorr(e, data[sym], spec, kind)
        elif name.startswith("followthrough_"):
            r = simulate_followthrough(e, data[sym], spec)
        elif name == "or_predicts_post_range":
            r = EdgeResult(name, sym, "meta", 0, 0, 0, 0, 0, 0, 0, 0, 0, "SKIP")
        else:
            r = EdgeResult(name, sym, "other", 0, 0, 0, 0, 0, 0, 0, 0, 0, "SKIP")
        results.append(r)

    # Sort by verdict (KEEP first) then net PnL
    order = {"KEEP": 0, "MARGINAL": 1, "DROP": 2, "SKIP": 3}
    results.sort(key=lambda r: (order[r.verdict], -r.net_pnl))
    p(f"  {'Edge':<30} {'Sym':<7} {'Kind':<14} {'N':>4}  {'WR':>5}  "
      f"{'Net$':>9}  {'PF':>5}  {'DD%':>5}  {'avg$':>7}  verdict")
    p("  " + "-" * 110)
    for r in results:
        wr_s = f"{r.wr*100:.1f}%" if r.n > 0 else "—"
        pf_s = f"{r.pf:.2f}" if r.n > 0 else "—"
        dd_s = f"{r.max_dd_pct:.2f}" if r.n > 0 else "—"
        avg_s = f"{r.avg_per_trade:+.0f}" if r.n > 0 else "—"
        net_s = f"${r.net_pnl:+,.0f}" if r.n > 0 else "—"
        p(f"  {r.edge_name:<30} {r.symbol:<7} {r.kind:<14} {r.n:>4}  "
          f"{wr_s:>5}  {net_s:>9}  {pf_s:>5}  {dd_s:>5}  {avg_s:>7}  {r.verdict}")
    p("")
    # Totals
    keep = [r for r in results if r.verdict == "KEEP"]
    marginal = [r for r in results if r.verdict == "MARGINAL"]
    total_keep = sum(r.net_pnl for r in keep)
    total_marg = sum(r.net_pnl for r in marginal)
    p("=" * 110)
    p(f"  KEEP        : {len(keep)} edges,  total net = ${total_keep:+,.0f}")
    p(f"  MARGINAL    : {len(marginal)} edges,  total net = ${total_marg:+,.0f}")
    p(f"  Sum of edge income (KEEP+MARGINAL): ${total_keep + total_marg:+,.0f} / 3 months")
    p(f"  On $100k at 0.10% risk per trade unit size.")
    p("=" * 110)

    # Save
    out = ROOT / "Results"
    with open(out / "research_new_edges.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(out_lines))
    with open(out / "research_new_edges.json", "w") as f:
        json.dump({
            "generated": datetime.utcnow().isoformat(),
            "results": [
                {"edge": r.edge_name, "symbol": r.symbol, "kind": r.kind,
                 "hour": r.hour, "lag": r.lag, "n": r.n, "wr": r.wr,
                 "net_pnl": r.net_pnl, "pf": r.pf,
                 "max_dd_pct": r.max_dd_pct,
                 "avg_per_trade": r.avg_per_trade,
                 "total_cost": r.total_cost, "verdict": r.verdict}
                for r in results
            ],
            "totals": {
                "n_keep": len(keep), "n_marginal": len(marginal),
                "net_keep": total_keep, "net_marginal": total_marg,
                "net_total": total_keep + total_marg,
            }
        }, f, indent=2, default=str)
    p(f"  Saved: Results/research_new_edges.txt + .json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
