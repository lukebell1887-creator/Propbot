#!/usr/bin/env python3
"""
probe_broker_pip_values.py
==========================
Definitive broker-spec probe.  Asks MT5 (via the running bridge on
127.0.0.1:5555) for the AUTHORITATIVE per-symbol values that determine
real $-P/L per pip per lot:

    trade_tick_size       (price increment per tick)
    trade_tick_value      ($ P/L per 1 tick per 1 lot)  <-- ground truth
    trade_contract_size   (units per lot)
    point                 (smallest price unit)
    digits                (decimal precision)

Then compares the broker's authoritative `$/point/lot` against the bot's
internal `V30_DOLLARS_PER_TICK_PER_LOT` table.  If they disagree, the bot
is sizing wrong — full stop.  If they agree, every trade today is sized
exactly as the backtest assumed.

Also reconciles each of today's live trades:
    real_$_at_stop = lots * |entry-SL| / tick_size * BROKER_tick_value

against the logged `risk_usd`.  Whichever number disagrees with the broker
is the broken one.

Run on the VPS while the bot/bridge is up:
    python Scripts\probe_broker_pip_values.py
"""
from __future__ import annotations

import json
import socket
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 0)  Bot's hardcoded view of the world  (what backtest+sizer assume)
# ---------------------------------------------------------------------------
V30_BROKER_TICK_SIZE = {"DE40": 1.0, "US30": 1.0, "US500": 0.25, "XAUUSD": 0.01}
V30_BROKER_CONTRACT_SIZE = {"DE40": 1.0, "US30": 1.0, "US500": 1.0, "XAUUSD": 100.0}
V30_DOLLARS_PER_TICK_PER_LOT = {
    s: V30_BROKER_TICK_SIZE[s] * V30_BROKER_CONTRACT_SIZE[s]
    for s in V30_BROKER_TICK_SIZE
}
V30_BROKER_NAMES = {
    "DE40":   ["DE40.cash", "DE40", "DAX40"],
    "US30":   ["US30.cash", "US30",  "DJ30"],
    "US500":  ["US500.cash", "US500", "SP500"],
    "XAUUSD": ["XAUUSD"],
}

# ---------------------------------------------------------------------------
# 1)  Tiny ZMQ-style bridge client  (REQ/REP newline-JSON, same as v30 bot)
# ---------------------------------------------------------------------------
class BridgeClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 5555, timeout: float = 5.0):
        self.host, self.port, self.timeout = host, port, timeout

    def _send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        try:
            s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
            buf = b""
            t0 = time.time()
            while time.time() - t0 < self.timeout:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                if b"\n" in buf:
                    break
            line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace")
            return json.loads(line) if line else {}
        finally:
            s.close()

    def symbol_info(self, sym: str) -> Optional[Dict[str, Any]]:
        try:
            r = self._send({"action": "SYMBOL_INFO", "symbol": sym})
            if r.get("ok") and "info" in r:
                return r["info"]
            # alt schema
            if "symbol" in r and ("trade_tick_value" in r or "tick_value" in r):
                return r
        except Exception:
            return None
        return None


def _resolve_broker_name(c: BridgeClient, internal: str) -> Optional[str]:
    for nm in V30_BROKER_NAMES[internal]:
        info = c.symbol_info(nm)
        if info:
            return nm
    return None


# ---------------------------------------------------------------------------
# 2)  Main
# ---------------------------------------------------------------------------
def main() -> int:
    out: List[str] = []
    P = out.append

    P("=" * 96)
    P(f" BROKER PIP-VALUE PROBE  --  {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}")
    P("=" * 96)

    cli = BridgeClient()
    rows: List[Dict[str, Any]] = []
    for internal in ("DE40", "US30", "US500", "XAUUSD"):
        broker = _resolve_broker_name(cli, internal)
        if not broker:
            P(f"  {internal:7s}  no symbol mapping found via bridge — SKIP")
            continue
        info = cli.symbol_info(broker) or {}
        # Many MT5 wrappers spell these slightly differently; try them all.
        ts  = float(info.get("trade_tick_size",   info.get("tick_size",  info.get("point", 0.0))))
        tv  = float(info.get("trade_tick_value",  info.get("tick_value", 0.0)))
        cs  = float(info.get("trade_contract_size", info.get("contract_size", 0.0)))
        # Broker truth: $-PnL per 1 point per 1 lot:
        per_pt_truth = tv / ts if ts > 0 else 0.0
        # Bot's formula's view:
        per_pt_bot = V30_DOLLARS_PER_TICK_PER_LOT[internal] / V30_BROKER_TICK_SIZE[internal]
        rows.append({
            "internal": internal, "broker": broker,
            "ts_truth": ts, "tv_truth": tv, "cs_truth": cs,
            "$/pt_truth": per_pt_truth,
            "ts_bot":   V30_BROKER_TICK_SIZE[internal],
            "$/tick_bot": V30_DOLLARS_PER_TICK_PER_LOT[internal],
            "$/pt_bot":   per_pt_bot,
            "agreement_factor": (per_pt_truth / per_pt_bot) if per_pt_bot > 0 else 0.0,
        })

    P("")
    P("  AUTHORITATIVE BROKER SPECS (from MT5 SymbolInfo via bridge)")
    P("  " + "-" * 92)
    P(f"  {'sym':6s}  {'broker':12s}  "
      f"{'tick_size':>10s}  {'tick_value':>11s}  {'contract':>10s}  "
      f"{'$/pt_TRUTH':>12s}  {'$/pt_BOT':>10s}  {'factor':>8s}")
    P("  " + "-" * 92)
    bad = []
    for r in rows:
        factor = r["agreement_factor"]
        verdict = "OK" if 0.99 < factor < 1.01 else f"OFF {factor:.2f}x"
        if not (0.99 < factor < 1.01):
            bad.append(r)
        P(f"  {r['internal']:6s}  {r['broker']:12s}  "
          f"{r['ts_truth']:>10.4f}  {r['tv_truth']:>11.4f}  {r['cs_truth']:>10.2f}  "
          f"{r['$/pt_truth']:>12.4f}  {r['$/pt_bot']:>10.4f}  {verdict:>8s}")
    P("")

    # -------------------------------------------------------------------
    # 3)  Reconcile today's actual live trades against broker truth
    # -------------------------------------------------------------------
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    p_trades = Path("Results/v30_live_trades.jsonl")
    p_slip   = Path("Results/v30_live_slippage.jsonl")

    if p_trades.exists():
        P("  TODAY'S TRADES vs BROKER TRUTH")
        P("  " + "-" * 92)
        P(f"  {'time':5s}  {'sym':6s}  {'side':5s}  {'lots':>6s}  "
          f"{'|E-SL|':>8s}  {'logged_$':>10s}  {'truth_$':>10s}  {'diff_$':>10s}  verdict")
        P("  " + "-" * 92)
        for line in p_trades.read_text().splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if not row.get("ts", "").startswith(today):
                continue
            if row.get("event") != "ENTRY":
                continue
            sym  = row.get("symbol", "?")
            side = row.get("side",  "?")
            lots = float(row.get("size_lots", row.get("lots", 0.0)))
            ent  = float(row.get("entry_px",  row.get("entry", 0.0)))
            sl   = float(row.get("sl_px",     row.get("sl",    0.0)))
            log_r = float(row.get("risk_usd",  row.get("risk_$", 0.0)))
            esl   = abs(ent - sl)
            spec_truth = next((x for x in rows if x["internal"] == sym), None)
            if not spec_truth:
                continue
            tv_truth = spec_truth["tv_truth"]
            ts_truth = spec_truth["ts_truth"]
            real_dollar = lots * (esl / ts_truth) * tv_truth if ts_truth > 0 else 0.0
            diff = real_dollar - log_r
            verdict = "PASS" if abs(diff) < 0.05 * max(log_r, 1.0) else "MISMATCH"
            tt = row["ts"][11:16]
            P(f"  {tt:5s}  {sym:6s}  {side:5s}  {lots:>6.2f}  "
              f"{esl:>8.4f}  {log_r:>10.2f}  {real_dollar:>10.2f}  {diff:>10.2f}  {verdict}")
        P("")
    else:
        P("  (no Results/v30_live_trades.jsonl found — skipping live reconciliation)")

    # -------------------------------------------------------------------
    # 4)  Slippage budget vs reality
    # -------------------------------------------------------------------
    if p_slip.exists():
        P("  SLIPPAGE BUDGET vs REALITY  (backtest budget = 3.5 ticks)")
        P("  " + "-" * 60)
        worst = {}
        for line in p_slip.read_text().splitlines():
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
            cur["usd"]  += usd
            cur["n"]    += 1
            worst[sym] = cur
        P(f"  {'sym':7s}  {'#fills':>6s}  {'max_ticks':>10s}  {'tot_$':>10s}  budget?")
        for sym in sorted(worst):
            w = worst[sym]
            tag = "OK" if w["ticks"] <= 3.5 else f"OVER ({w['ticks']:.1f}t)"
            P(f"  {sym:7s}  {w['n']:>6d}  {w['ticks']:>10.2f}  {w['usd']:>10.2f}  {tag}")
        P("")

    # -------------------------------------------------------------------
    # 5)  Verdict
    # -------------------------------------------------------------------
    P("=" * 96)
    if bad:
        P(" VERDICT:  ⚠️  BROKER SPEC DISAGREEMENT")
        P("")
        for r in bad:
            P(f"   {r['internal']}: broker says ${r['$/pt_truth']:.4f}/pt, "
              f"bot uses ${r['$/pt_bot']:.4f}/pt  →  "
              f"bot is {r['agreement_factor']:.2f}× off")
        P("")
        P(" If factor > 1: bot is UNDER-sized (less risk than logged)")
        P(" If factor < 1: bot is OVER-sized  (more risk than logged) — STOP TRADING")
    else:
        P(" VERDICT:  ✅  BROKER SPECS MATCH BOT'S INTERNAL TABLE")
        P(" Sizing is consistent with backtest assumptions.")
    P("=" * 96)

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
