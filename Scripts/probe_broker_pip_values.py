#!/usr/bin/env python3
"""
probe_broker_pip_values.py  (v2 — direct MT5 query)
====================================================
Asks the LOCAL MT5 terminal directly via the official `MetaTrader5`
Python package for AUTHORITATIVE per-symbol values:

    point          (smallest price unit)
    trade_tick_size, trade_tick_value, trade_contract_size
    digits, spread, trade_stops_level

These are the broker's source-of-truth.  We then compare to the bot's
internal `V30_DOLLARS_PER_TICK_PER_LOT` table and reconcile each of
today's live trades against broker reality.

Run on the VPS while MT5 terminal is open (it doesn't matter if the bot
is connected via ZMQ EA — the Python `mt5` API can attach to the same
terminal in read-only mode):

    python Scripts\probe_broker_pip_values.py

If `mt5.initialize()` fails, the script falls back to printing a manual
checklist you can fill in by hand from MT5's "Specification" dialog
(right-click symbol in Market Watch → Specification).
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 0)  Bot's hardcoded view (what backtest+sizer assume)
# ---------------------------------------------------------------------------
V30_BROKER_TICK_SIZE = {"DE40": 1.0, "US30": 1.0, "US500": 0.25, "XAUUSD": 0.01}
V30_BROKER_CONTRACT_SIZE = {"DE40": 1.0, "US30": 1.0, "US500": 1.0, "XAUUSD": 100.0}
V30_DOLLARS_PER_TICK_PER_LOT = {
    s: V30_BROKER_TICK_SIZE[s] * V30_BROKER_CONTRACT_SIZE[s]
    for s in V30_BROKER_TICK_SIZE
}
# All possible broker name spellings to try
BROKER_NAME_CANDIDATES = {
    "DE40":   ["DE40.cash", "DE40", "DAX40", "GER40",  "DAX",    "DE40m"],
    "US30":   ["US30.cash", "US30", "DJ30",  "WS30",   "DOW30",  "US30m"],
    "US500":  ["US500.cash","US500","SP500", "SPX500", "SPX",    "US500m"],
    "XAUUSD": ["XAUUSD",    "GOLD", "XAUUSD.s","XAUUSDm"],
}


def try_init_mt5() -> Any:
    """Best-effort MT5 init.  Returns the module on success, None on failure."""
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception as e:
        print(f"[!] MetaTrader5 package not available: {e}")
        print("    Install with:  pip install MetaTrader5")
        return None
    if not mt5.initialize():
        err = mt5.last_error()
        print(f"[!] mt5.initialize() failed: {err}")
        print("    Make sure the MT5 terminal is open & logged in on this VPS.")
        return None
    return mt5


def find_broker_name(mt5: Any, internal: str) -> Optional[str]:
    """Pick the first broker spelling that resolves to a real symbol."""
    for nm in BROKER_NAME_CANDIDATES[internal]:
        info = mt5.symbol_info(nm)
        if info is not None and info.visible:
            return nm
        # try forcing it into Market Watch
        if info is not None and not info.visible:
            mt5.symbol_select(nm, True)
            info = mt5.symbol_info(nm)
            if info is not None:
                return nm
    return None


def main() -> int:
    out: List[str] = []
    P = out.append

    P("=" * 96)
    P(f" BROKER PIP-VALUE PROBE v2  --  {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    P("=" * 96)

    mt5 = try_init_mt5()
    if mt5 is None:
        P("")
        P("  *** Cannot reach MT5 directly. ***")
        P("  Manual fallback: open MT5 terminal, right-click each symbol in Market Watch")
        P("  → Specification, and tell me the values:")
        P("")
        P("    DE40    -- tick_size? tick_value? contract_size?")
        P("    US30    -- tick_size? tick_value? contract_size?")
        P("    US500   -- tick_size? tick_value? contract_size?  ← CRITICAL")
        P("    XAUUSD  -- tick_size? tick_value? contract_size?")
        P("")
        P("  These are visible as 'Tick size', 'Tick value', and 'Contract size'.")
        print("\n".join(out))
        return 1

    # -------------------------------------------------------------------
    # 1)  Pull broker truth for each internal symbol
    # -------------------------------------------------------------------
    rows: List[Dict[str, Any]] = []
    P("")
    P("  AUTHORITATIVE BROKER SPECS (from MT5 SymbolInfo, this terminal)")
    P("  " + "-" * 92)
    P(f"  {'sym':6s} {'broker':14s} {'tick_size':>10s} {'tick_value':>11s} "
      f"{'contract':>10s} {'$/pt_TRUTH':>12s} {'$/pt_BOT':>10s} {'verdict':>10s}")
    P("  " + "-" * 92)

    bad: List[Dict[str, Any]] = []
    for internal in ("DE40", "US30", "US500", "XAUUSD"):
        broker = find_broker_name(mt5, internal)
        if not broker:
            P(f"  {internal:6s} (no broker mapping found — SKIP)")
            continue
        info = mt5.symbol_info(broker)
        ts = float(info.trade_tick_size or info.point or 0.0)
        tv = float(info.trade_tick_value or 0.0)
        cs = float(info.trade_contract_size or 0.0)
        per_pt_truth = tv / ts if ts > 0 else 0.0
        per_pt_bot = V30_DOLLARS_PER_TICK_PER_LOT[internal] / V30_BROKER_TICK_SIZE[internal]
        factor = (per_pt_truth / per_pt_bot) if per_pt_bot > 0 else 0.0
        verdict = "OK" if 0.99 < factor < 1.01 else f"OFF {factor:.2f}x"
        if not (0.99 < factor < 1.01):
            bad.append({"sym": internal, "broker": broker,
                        "truth_$/pt": per_pt_truth, "bot_$/pt": per_pt_bot,
                        "factor": factor})
        rows.append({"internal": internal, "broker": broker,
                     "ts_truth": ts, "tv_truth": tv, "cs_truth": cs,
                     "$/pt_truth": per_pt_truth})
        P(f"  {internal:6s} {broker:14s} {ts:>10.4f} {tv:>11.4f} "
          f"{cs:>10.2f} {per_pt_truth:>12.4f} {per_pt_bot:>10.4f} {verdict:>10s}")
    P("")

    # -------------------------------------------------------------------
    # 2)  Reconcile today's live trades against broker truth
    # -------------------------------------------------------------------
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    p_trades = Path("Results/v30_live_trades.jsonl")

    if p_trades.exists():
        P("  TODAY'S TRADES vs BROKER TRUTH")
        P("  " + "-" * 92)
        P(f"  {'time':5s} {'sym':6s} {'side':5s} {'lots':>6s} "
          f"{'|E-SL|':>8s} {'logged_$':>10s} {'truth_$':>10s} {'diff_$':>10s} verdict")
        P("  " + "-" * 92)
        for line in p_trades.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not row.get("ts", "").startswith(today):
                continue
            if row.get("event") != "ENTRY":
                continue
            sym = row.get("symbol", "?")
            side = row.get("side", "?")
            lots = float(row.get("size_lots", row.get("lots", 0.0)))
            ent = float(row.get("entry_px", row.get("entry", 0.0)))
            sl = float(row.get("sl_px", row.get("sl", 0.0)))
            log_r = float(row.get("risk_usd", row.get("risk_$", 0.0)))
            esl = abs(ent - sl)
            spec = next((x for x in rows if x["internal"] == sym), None)
            if not spec or spec["ts_truth"] <= 0:
                continue
            real_dollar = lots * (esl / spec["ts_truth"]) * spec["tv_truth"]
            diff = real_dollar - log_r
            v = "PASS" if abs(diff) < 0.05 * max(log_r, 1.0) else "MISMATCH"
            tt = row["ts"][11:16]
            P(f"  {tt:5s} {sym:6s} {side:5s} {lots:>6.2f} {esl:>8.4f} "
              f"{log_r:>10.2f} {real_dollar:>10.2f} {diff:>10.2f} {v}")
        P("")

    # -------------------------------------------------------------------
    # 3)  Slippage budget vs reality
    # -------------------------------------------------------------------
    p_slip = Path("Results/v30_live_slippage.jsonl")
    if p_slip.exists():
        P("  SLIPPAGE BUDGET vs REALITY  (backtest budget = 3.5 ticks)")
        P("  " + "-" * 60)
        worst: Dict[str, Dict[str, float]] = {}
        for line in p_slip.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except Exception:
                continue
            if not r.get("ts", "").startswith(today):
                continue
            sym = r.get("symbol", "?")
            tk = abs(float(r.get("ticks_slip", 0.0)))
            usd = float(r.get("dollars_slip", 0.0))
            cur = worst.get(sym, {"ticks": 0.0, "usd": 0.0, "n": 0})
            cur["ticks"] = max(cur["ticks"], tk)
            cur["usd"] += usd
            cur["n"] += 1
            worst[sym] = cur
        P(f"  {'sym':7s} {'#fills':>6s} {'max_ticks':>10s} {'tot_$':>10s} verdict")
        for sym in sorted(worst):
            w = worst[sym]
            tag = "OK" if w["ticks"] <= 3.5 else f"OVER ({w['ticks']:.1f}t)"
            P(f"  {sym:7s} {int(w['n']):>6d} {w['ticks']:>10.2f} {w['usd']:>10.2f} {tag}")
        P("")

    # -------------------------------------------------------------------
    # 4)  Verdict
    # -------------------------------------------------------------------
    P("=" * 96)
    if bad:
        P(" VERDICT:  ⚠️  BROKER SPEC DISAGREEMENT")
        for b in bad:
            P(f"   {b['sym']}: broker says ${b['truth_$/pt']:.4f}/pt, "
              f"bot uses ${b['bot_$/pt']:.4f}/pt  →  bot is {b['factor']:.2f}× off")
        P("")
        P(" If factor > 1: bot UNDER-sized (real risk is less than logged — safe but inefficient)")
        P(" If factor < 1: bot OVER-sized  (real risk MORE than logged — STOP TRADING)")
    else:
        P(" VERDICT:  ✅  BROKER SPECS MATCH BOT'S INTERNAL TABLE")
        P(" Sizing is consistent with backtest assumptions.")
    P("=" * 96)

    mt5.shutdown()
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
