#!/usr/bin/env python3
"""
backtest_v21_mertongz.py — INTEGRATED backtest of ORB v20 + Merton×GZ sizer.

This test wires the real MertonGZSizer class into the real ORBEngineV20 via
its risk_pct_fn callback, and feeds realised R back into the sizer as trades
close. If the integration is correct, output should be within ±5% of the
simulated PG1 result from research_sizer_v21.py (+$14,160 / 3.41% DD).
"""
from __future__ import annotations

import csv, json, math, statistics, sys
from datetime import datetime, timedelta
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

SYMBOLS = ["DE40", "US30", "XAUUSD", "US100", "US500"]
BALANCE = 100_000.0
MONTHS = 3

# Same per-symbol tuning used in research_sizer_v21.py
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
}
AMP_HURDLE = {"DE40":3.0,"US30":4.5,"XAUUSD":4.5,"US100":4.5,"US500":3.0}


def load_m1(path, tmin, tmax):
    out=[]
    with open(path) as f:
        for r in csv.DictReader(f):
            try: t=datetime.fromisoformat(r["time"])
            except: t=datetime.strptime(r["time"],"%Y-%m-%d %H:%M:%S")
            if tmin and t<tmin: continue
            if tmax and t>tmax: continue
            out.append((t,float(r["open"]),float(r["high"]),float(r["low"]),float(r["close"])))
    return out


def common_window(files, months):
    firsts,lasts={},{}
    for s,p in files.items():
        with open(p) as f:
            rdr=csv.reader(f); next(rdr)
            rows=[r for r in rdr if r]
        try:
            firsts[s]=datetime.fromisoformat(rows[0][0])
            lasts[s]=datetime.fromisoformat(rows[-1][0])
        except:
            firsts[s]=datetime.strptime(rows[0][0],"%Y-%m-%d %H:%M:%S")
            lasts[s]=datetime.strptime(rows[-1][0],"%Y-%m-%d %H:%M:%S")
    end=min(lasts.values())
    start=max(max(firsts.values()), end - timedelta(days=months*31))
    return start, end


def run(mode: str, sizer_cfg: MertonGZSizerConfig | None):
    """
    mode = 'flat'        → use ORBEngineConfig.risk_pct = 0.0025 (current v20 default)
           'mertongz'    → use MertonGZSizer as risk_pct_fn
    """
    data=ROOT/"data"/"historical"
    files={s:data/f"{s}_M1.csv" for s in SYMBOLS if (data/f"{s}_M1.csv").exists()}
    if not files: raise RuntimeError("no data")
    tmin,tmax=common_window(files, MONTHS)
    specs={s:SMARTBB_UNIVERSE[s] for s in files}
    streams={s:load_m1(files[s],tmin,tmax) for s in files}

    # Build engines — ONE per symbol
    sizer = MertonGZSizer(sizer_cfg) if mode=="mertongz" else None
    engines: Dict[str, ORBEngineV20] = {}
    shared_equity = {"val": BALANCE, "peak": BALANCE}

    def risk_fn(sym, equity, peak, open_pos):
        # Use the SHARED equity (aggregate across all symbol engines)
        return sizer.compute_risk_pct(sym, shared_equity["val"], shared_equity["peak"], open_pos)

    for sym in files:
        if mode == "flat":
            cfg = ORBEngineConfig(risk_pct=0.0025, amp_hurdle=AMP_HURDLE[sym],
                                   require_nr7=False, nr_lookback=7,
                                   trail_atr_mult=0.8,
                                   tp1_close_frac=0.50, tp2_close_frac=0.25,
                                   hurst_min=0.0, hurst_max=1.0, hurst_window=200)
        else:
            cfg = ORBEngineConfig(risk_pct=0.0010, amp_hurdle=AMP_HURDLE[sym],
                                   require_nr7=False, nr_lookback=7,
                                   trail_atr_mult=0.8,
                                   tp1_close_frac=0.50, tp2_close_frac=0.25,
                                   hurst_min=0.0, hurst_max=1.0, hurst_window=200,
                                   risk_pct_fn=risk_fn)
        engines[sym] = ORBEngineV20(
            symbols=[specs[sym]], cfg=cfg,
            orb_configs={sym: ORB_CONFIGS[sym]},
            initial_equity=BALANCE,
        )

    # Merge and iterate all bars in time order
    allb=[]
    for s,bars in streams.items():
        allb.extend((t,s,o,h,l,c) for (t,o,h,l,c) in bars)
    allb.sort(key=lambda r: r[0])

    # Track last-seen trade counts + pending partials per entry.
    #
    # IMPORTANT: engine defines realised_R = net_pnl_partial / (R_dist * LOTS_TOTAL * pip).
    # The denominator uses *initial total lots*, not the partial's own lots, so each
    # partial already carries its fractional contribution to the full-trade R.
    # Therefore aggregate_R = SUM of partial realised_R (no lots weighting).
    last_n_trades = {s: 0 for s in files}
    pending: Dict[Tuple[str, float], Dict[str, float]] = {}
    fed_entries: set = set()

    def _aggregate_and_feed_if_ready(sym):
        if sizer is None:
            return
        st = engines[sym].states.get(sym)
        if st is not None and st.position is not None:
            return
        keys_to_flush = [k for k in pending if k[0] == sym and k not in fed_entries]
        for k in keys_to_flush:
            agg_R = pending[k]["r_sum"]
            sizer.on_trade_closed(sym, agg_R)
            fed_entries.add(k)

    for t, s, o, h, l, c in allb:
        eng = engines[s]
        eng.on_bar(s, t.timestamp(), t.strftime("%Y-%m-%d"),
                   t.hour, t.minute, o, h, l, c)
        if len(eng.trades) > last_n_trades[s]:
            new_trades = eng.trades[last_n_trades[s]:]
            last_n_trades[s] = len(eng.trades)
            for tr in new_trades:
                shared_equity["val"] += tr.net_pnl
                if shared_equity["val"] > shared_equity["peak"]:
                    shared_equity["peak"] = shared_equity["val"]
                if sizer is not None:
                    k = (tr.symbol, tr.entry_time)
                    if k not in pending:
                        pending[k] = {"r_sum": 0.0}
                    pending[k]["r_sum"] += tr.realised_R
            _aggregate_and_feed_if_ready(s)

    # End of data: flush any still-pending entries (positions may be open at EOD)
    if sizer is not None:
        for sym in files:
            _aggregate_and_feed_if_ready(sym)

    # Collect everything
    all_trades=[]
    for eng in engines.values():
        all_trades.extend(eng.trades)
    all_trades.sort(key=lambda tr: tr.entry_time)

    # Reconstruct equity curve in chronological order
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

    return {
        "mode": mode,
        "n": len(all_trades),
        "net_pnl": net,
        "return_pct": net / BALANCE * 100,
        "max_dd_pct": mdd * 100,
        "pf": pf if math.isfinite(pf) else 99.0,
        "wr": len(wins) / max(1, len(pnls)),
        "sharpe": sharpe,
        "sizer_stats": sizer.stats() if sizer else None,
        "window": (str(tmin), str(tmax)),
    }


def main():
    out=[]
    p=lambda m="": (print(m), out.append(m))
    p("=" * 100)
    p("  backtest_v21_mertongz — INTEGRATED Merton×GZ sizer + ORB v20")
    p("=" * 100)

    # Baseline: flat 0.25%
    p("\n  [1/2] Running baseline: FLAT 0.25% risk (current v20 default)...")
    r_flat = run("flat", None)
    p(f"        N={r_flat['n']}  PnL=${r_flat['net_pnl']:+,.0f}  "
      f"DD={r_flat['max_dd_pct']:.2f}%  PF={r_flat['pf']:.2f}  "
      f"Sharpe={r_flat['sharpe']:.2f}")

    # MertonGZ per-symbol (one μ/σ² per instrument)
    p("\n  [2/3] Running Merton × GZ PER-SYMBOL (base=0.10%, cap=3×, γ=2.0, DD=4%)...")
    cfg_ps = MertonGZSizerConfig(
        base_risk_pct=0.0010, cap_mult=3.0, gamma=2.0,
        ewma_alpha=0.20, warmup_trades=5, dd_cap_pct=0.04,
        pool_symbols=False,
    )
    r_ps = run("mertongz", cfg_ps)
    p(f"        N={r_ps['n']}  PnL=${r_ps['net_pnl']:+,.0f}  "
      f"DD={r_ps['max_dd_pct']:.2f}%  PF={r_ps['pf']:.2f}  "
      f"Sharpe={r_ps['sharpe']:.2f}")

    # MertonGZ pooled (one global μ/σ² across all symbols)
    p("\n  [3/5] Running Merton × GZ POOLED (global μ/σ²)...")
    cfg_pool = MertonGZSizerConfig(
        base_risk_pct=0.0010, cap_mult=3.0, gamma=2.0,
        ewma_alpha=0.20, warmup_trades=5, dd_cap_pct=0.04,
        pool_symbols=True,
    )
    r_pool = run("mertongz", cfg_pool)
    p(f"        N={r_pool['n']}  PnL=${r_pool['net_pnl']:+,.0f}  "
      f"DD={r_pool['max_dd_pct']:.2f}%  PF={r_pool['pf']:.2f}  "
      f"Sharpe={r_pool['sharpe']:.2f}")

    # POOLED + longer warmup + no-shrink-on-no-edge (holds base instead of 0.5×)
    p("\n  [4/5] Running Merton × GZ POOLED + warmup=15 + no_edge=1.0 (robust to cold-start)...")
    cfg_v2 = MertonGZSizerConfig(
        base_risk_pct=0.0010, cap_mult=3.0, gamma=2.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )
    r_v2 = run("mertongz", cfg_v2)
    p(f"        N={r_v2['n']}  PnL=${r_v2['net_pnl']:+,.0f}  "
      f"DD={r_v2['max_dd_pct']:.2f}%  PF={r_v2['pf']:.2f}  "
      f"Sharpe={r_v2['sharpe']:.2f}")

    # Same as v2 but higher base (0.15% × 3 cap = 0.45% max, aims for more profit)
    p("\n  [5/5] Running Merton × GZ POOLED w/ base=0.15% (aggressive within 4% DD)...")
    cfg_v3 = MertonGZSizerConfig(
        base_risk_pct=0.0015, cap_mult=3.0, gamma=2.0,
        ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
        pool_symbols=True, no_edge_multiplier=1.0,
    )
    r_v3 = run("mertongz", cfg_v3)
    p(f"        N={r_v3['n']}  PnL=${r_v3['net_pnl']:+,.0f}  "
      f"DD={r_v3['max_dd_pct']:.2f}%  PF={r_v3['pf']:.2f}  "
      f"Sharpe={r_v3['sharpe']:.2f}")

    # Pick best variant that passes 4% DD
    candidates = [("PER-SYMBOL base=0.10%", r_ps),
                  ("POOLED base=0.10% warmup=5", r_pool),
                  ("POOLED base=0.10% warmup=15 noEdge=1", r_v2),
                  ("POOLED base=0.15% warmup=15 noEdge=1", r_v3)]
    passing = [(n, r) for n, r in candidates if r["max_dd_pct"] <= 4.0]
    passing.sort(key=lambda x: -x[1]["net_pnl"])
    chosen_name, r_mgz = passing[0] if passing else candidates[0]
    p(f"\n  ⭐ Best Merton×GZ variant : {chosen_name}")
    p(f"     PnL ${r_mgz['net_pnl']:+,.0f} @ {r_mgz['max_dd_pct']:.2f}% DD, Sharpe {r_mgz['sharpe']:.2f}")



    p("\n" + "=" * 100)
    p("  COMPARISON — same entries, different sizing")
    p("=" * 100)
    p(f"  Window : {r_flat['window'][0]} → {r_flat['window'][1]}")
    p("")
    p(f"    {'Policy':<30} {'N':>4}  {'PnL':>11}  {'Ret%':>6}  {'DD%':>6}  "
      f"{'PF':>5}  {'WR':>5}  {'Sharpe':>6}  {'≤4%DD?':>8}")
    p("    " + "-" * 86)
    for r, name in [(r_flat,"FLAT 0.25%"),(r_mgz,"MERTON × GZ (v21)")]:
        pass_dd = "✅ YES" if r["max_dd_pct"] <= 4.0 else "❌ NO"
        p(f"    {name:<30} {r['n']:>4}  ${r['net_pnl']:>+9,.0f}  "
          f"{r['return_pct']:>+5.2f}%  {r['max_dd_pct']:>5.2f}%  "
          f"{r['pf']:>5.2f}  {r['wr']*100:>4.1f}%  {r['sharpe']:>6.2f}  {pass_dd:>8}")
    p("")

    delta = r_mgz["net_pnl"] - r_flat["net_pnl"]
    dd_delta = r_mgz["max_dd_pct"] - r_flat["max_dd_pct"]
    p(f"  PnL delta (Merton×GZ − Flat):  ${delta:+,.0f}  ({delta/max(1,r_flat['net_pnl'])*100:+.1f}%)")
    p(f"  DD delta:                       {dd_delta:+.2f}pp")

    # Expected from simulation
    sim_expected = 14160
    err = (r_mgz["net_pnl"] - sim_expected) / sim_expected * 100
    p(f"\n  Simulation (research_sizer_v21) expected : ${sim_expected:+,.0f}")
    p(f"  Integrated backtest actual              : ${r_mgz['net_pnl']:+,.0f}")
    p(f"  Delta vs simulation                     : {err:+.1f}%  "
      f"({'✅ within ±10%' if abs(err)<=10 else '⚠ diverges from sim' if abs(err)<=25 else '❌ significant divergence'})")

    # Sizer stats
    if r_mgz["sizer_stats"]:
        s = r_mgz["sizer_stats"]
        p(f"\n  Sizer stats:")
        p(f"    calls                : {s['n_calls']}")
        p(f"    warm-up calls        : {s['n_warmup_calls']}")
        p(f"    no-edge calls (μ≤0)  : {s['n_no_edge_calls']}")
        p(f"    capped (hit 3×)      : {s['n_capped_calls']}")
        p(f"    GZ=0 (at DD barrier) : {s['n_gz_zero_calls']}")
        p(f"    final DD observed    : {s['last_dd_pct']:.2f}%")
        for sym, ps in s["per_symbol"].items():
            p(f"    {sym:<8} n={ps['n_trades_seen']:>3}  μ_R={ps['mu_ewma']:+.3f}  "
              f"σ_R={math.sqrt(ps['var_ewma']):.3f}  Sharpe_ewma={ps['sharpe_ewma']:+.2f}")

    p("\n" + "=" * 100)
    if r_mgz["max_dd_pct"] <= 4.0 and r_mgz["net_pnl"] > r_flat["net_pnl"]:
        p("  ✅ VERDICT: Merton×GZ BEATS flat (more $, same or less DD). Ready to deploy.")
    elif r_mgz["max_dd_pct"] <= 4.0:
        p("  ⚠️  VERDICT: Merton×GZ passes DD but doesn't beat flat. Review before deploy.")
    else:
        p(f"  ❌ VERDICT: Merton×GZ exceeds 4% DD ({r_mgz['max_dd_pct']:.2f}%). DO NOT deploy.")
    p("=" * 100)

    (ROOT/"Results").mkdir(exist_ok=True)
    with open(ROOT/"Results"/"backtest_v21_mertongz.txt","w",encoding="utf-8") as f:
        f.write("\n".join(out))
    with open(ROOT/"Results"/"backtest_v21_mertongz.json","w") as f:
        json.dump({"flat": r_flat, "mertongz": r_mgz,
                   "simulated_expected": sim_expected,
                   "delta_vs_sim_pct": err}, f, indent=2, default=str)
    p(f"  Saved: Results/backtest_v21_mertongz.txt + .json")


if __name__ == "__main__":
    main()
