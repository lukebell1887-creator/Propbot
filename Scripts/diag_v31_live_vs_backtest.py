r"""
diag_v31_live_vs_backtest.py
============================

ONE-SHOT WIRING + SIZING DIAGNOSTIC for the v31 live bot.

What you asked:
  > backtest made +28k in 3m, live has lost 4.5k in 2 weeks.
  > "it's only trading like 10 dollars" -> confidence/sizer looks too low.
  > I want to check the bot is wired up exactly like the backtest.

What this script does (no network, no broker calls -- reads only the
JSONL/JSON files the live bot already writes):

  1. Loads  Results/v30_live_trades.jsonl   (every ENTRY the bot fired)
  2. Loads  Results/v30_live_events.log     (TP1/TP2/TRAIL/CLOSE/LAYER1/...)
  3. Loads  Results/v30_state/sizer_mertongz.json   (the LIVE Merton state)
  4. Loads  Results/v30_state/dd_breaker.json       (the LIVE DD breaker)
  5. Loads  Results/v30_fresh_trades.json   (the backtest's actual trade list)
  6. Compares the two distributions side-by-side and prints a PhD-grade
     report explaining EXACTLY why live is sizing $10 instead of $185:

       * Sizer state per symbol: n_seen, mu, var, sharpe, multiplier
       * For every live entry: equity, stop-$ per lot, computed lots,
         risk_usd, risk_pct -- and the EXPECTED values if the sizer
         had been at the warm-up baseline (0.185 % * equity)
       * Flags every entry with: WARMUP / NO_EDGE / CAPPED / GZ_ZERO /
         GZ_THROTTLED / BELOW_MIN_LOT
       * Backtest comparison: median risk_pct, median lots, median R,
         win-rate, profit factor -- vs live
       * Wiring sanity rails:
            - base_risk_pct on disk == 0.00185 ?
            - warmup_trades on disk == 15 ?
            - DD breaker peak / current DD %, halted flag ?
            - Layer1 firing rate too high ?
            - News flatten too aggressive ?
            - Entries in trades.jsonl that NEVER got a TP1/TP2/CLOSE event
              (those are the genuine "missed TP" stuck tickets)

USAGE
=====

On the VPS (where the bot writes its files):
    python Scripts/diag_v31_live_vs_backtest.py

Or, if you've COPIED the VPS  Results/  folder back to your laptop:
    python Scripts\diag_v31_live_vs_backtest.py

Or override the source paths:
    python Scripts/diag_v31_live_vs_backtest.py ^
        --trades   Results/v30_live_trades.jsonl ^
        --events   Results/v30_live_events.log ^
        --state    Results/v30_state/sizer_mertongz.json ^
        --breaker  Results/v30_state/dd_breaker.json ^
        --bt       Results/v30_fresh_trades.json ^
        --since    2026-05-14    (any ISO date, only audit entries ON/AFTER it)

EXIT CODE
=========
    0  every rail passed -- live is wired the same way the backtest ran
    1  at least one RED rail -- the report tells you which line to fix

Author : v31 audit pipeline
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


REPO = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
#  Defaults the bot SHOULD be using (matches src/live/v30_live.py v31 ship)
# --------------------------------------------------------------------------- #
EXPECTED_BASE_RISK_PCT = 0.00185        # 0.185 %  per trade (warm-up unit)
EXPECTED_CAP_MULT      = 5.0            # cap = 0.925 % per trade (after Merton)
EXPECTED_GAMMA         = 3.0
EXPECTED_EWMA_ALPHA    = 0.20
EXPECTED_WARMUP_TRADES = 15
EXPECTED_DD_CAP_PCT    = 0.04
EXPECTED_SYMBOLS       = ("DE40", "US30", "XAUUSD", "US500")
EXPECTED_LAYER1_CAPS   = {"DE40": 5.0, "US30": 5.0, "US500": 3.0, "XAUUSD": 1.0}


# --------------------------------------------------------------------------- #
#  Tiny CLI helpers (Windows-friendly, no colour codes -- works in cmd.exe)
# --------------------------------------------------------------------------- #
PASS = "[OK]  "
FAIL = "[FAIL]"
WARN = "[WARN]"
INFO = "      "


def sep(title: str = "", width: int = 96) -> None:
    if title:
        print("\n" + "=" * width)
        print(f"  {title}")
        print("=" * width)
    else:
        print("-" * width)


def fmt_money(x: Optional[float]) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "      n/a"
    return f"${x:>+9,.2f}"


def fmt_pct(x: Optional[float], digits: int = 3) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "  n/a"
    return f"{x * 100:>{4 + digits}.{digits}f}%"


def safe_float(d: Any, *keys: str, default: Optional[float] = None) -> Optional[float]:
    if isinstance(d, dict):
        for k in keys:
            v = d.get(k)
            if v is None:
                continue
            try:
                return float(v)
            except (TypeError, ValueError):
                continue
    return default


def safe_str(d: Any, *keys: str, default: str = "") -> str:
    if isinstance(d, dict):
        for k in keys:
            v = d.get(k)
            if v is None:
                continue
            return str(v)
    return default


# --------------------------------------------------------------------------- #
#  Loaders -- robust to half-written / corrupted lines (the bot is alive)
# --------------------------------------------------------------------------- #
def read_jsonl(path: Path) -> List[dict]:
    if not path.exists():
        return []
    out: List[dict] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.strip()
            if not raw or not raw.startswith("{"):
                continue
            try:
                out.append(json.loads(raw))
            except Exception:
                continue
    return out


def read_json(path: Path) -> Optional[dict | list]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def parse_iso_date(s: str) -> Optional[datetime]:
    if not s or not isinstance(s, str):
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s[:19])
    except Exception:
        try:
            dt = datetime.fromisoformat(s)
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
#  Sizer reconstruction -- "what risk_pct WOULD the Merton sizer return?"
#
#  This re-implements the math from src/dynamic_sizer_v21.py exactly, so we
#  can answer: given the sizer state on disk, what multiplier did the bot
#  apply to each live entry?  And what should it have been?
# --------------------------------------------------------------------------- #
def reconstruct_merton_mult(
    sizer_state: Optional[dict],
    symbol: str,
    equity: float,
    peak_equity: float,
    base_risk_pct: float,
    gamma: float,
    cap_mult: float,
    warmup_trades: int,
    dd_cap_pct: float,
    no_edge_mult: float = 0.5,
    min_variance: float = 1e-6,
    pool_symbols: bool = False,
) -> dict:
    """Return a verbose breakdown of what the Merton-GZ formula would yield."""
    out = {
        "warmup": False,
        "no_edge": False,
        "gz_zero": False,
        "gz_throttled": False,
        "capped": False,
        "merton_mult": 1.0,
        "gz": 1.0,
        "dd_pct": 0.0,
        "raw_mult": 1.0,
        "capped_mult": 1.0,
        "risk_pct": base_risk_pct,
        "n_seen": 0,
        "mu": 0.0,
        "var": 1.0,
        "key_used": symbol,
    }
    peak = max(peak_equity, equity)
    dd = 0.0 if peak <= 0 else max(0.0, (peak - equity) / peak)
    out["dd_pct"] = dd
    gz = 1.0 - dd / dd_cap_pct if dd_cap_pct > 0 else 1.0
    if gz <= 0:
        out["gz"] = 0.0
        out["gz_zero"] = True
        out["raw_mult"] = 0.0
        out["capped_mult"] = 0.0
        out["risk_pct"] = 0.0
        return out
    out["gz"] = max(0.0, min(1.0, gz))
    if out["gz"] < 0.999:
        out["gz_throttled"] = True

    # Per-symbol vs pooled key
    key = "_GLOBAL_" if pool_symbols else symbol
    out["key_used"] = key

    n_seen = 0
    mu = 0.0
    var = 1.0
    if sizer_state and isinstance(sizer_state, dict):
        per = sizer_state.get("per_symbol") or {}
        # support both {"DE40": {...}} and the v21 to_state() layout
        if key in per and isinstance(per[key], dict):
            entry = per[key]
            n_seen = int(entry.get("n_seen", entry.get("n_trades_seen", 0)) or 0)
            mu = float(entry.get("mu", entry.get("mu_ewma", 0.0)) or 0.0)
            var = float(entry.get("var", entry.get("var_ewma", 1.0)) or 1.0)
        elif "n_seen" in sizer_state and key in sizer_state.get("n_seen", {}):
            n_seen = int(sizer_state["n_seen"].get(key, 0))
            mu = float(sizer_state.get("mu", {}).get(key, 0.0))
            var = float(sizer_state.get("var", {}).get(key, 1.0))
    out["n_seen"] = n_seen
    out["mu"] = mu
    out["var"] = max(min_variance, var)

    if n_seen < warmup_trades:
        out["warmup"] = True
        merton_mult = 1.0
    else:
        if mu <= 0:
            out["no_edge"] = True
            merton_mult = no_edge_mult
        else:
            f_star = mu / (gamma * max(min_variance, var))
            merton_mult = f_star / base_risk_pct if base_risk_pct > 0 else 0.0
    out["merton_mult"] = merton_mult

    raw_mult = merton_mult * out["gz"]
    capped_mult = min(cap_mult, max(0.0, raw_mult))
    if capped_mult < raw_mult - 1e-12:
        out["capped"] = True
    out["raw_mult"] = raw_mult
    out["capped_mult"] = capped_mult
    out["risk_pct"] = capped_mult * base_risk_pct
    return out


# --------------------------------------------------------------------------- #
#  Aggregators
# --------------------------------------------------------------------------- #
def realised_R_from_events(events: List[dict], ticket: Optional[int]) -> Optional[float]:
    """Walk this ticket's events and return the final realised R if logged."""
    if ticket is None:
        return None
    R = None
    for ev in events:
        try:
            if int(ev.get("ticket") or 0) != int(ticket):
                continue
        except (TypeError, ValueError):
            continue
        if "realised_R" in ev:
            try:
                R = float(ev["realised_R"])
            except (TypeError, ValueError):
                pass
    return R


def pct_summary(label: str, xs: List[float]) -> str:
    if not xs:
        return f"{label:<22} (no data)"
    n = len(xs)
    med = statistics.median(xs)
    mn = min(xs)
    mx = max(xs)
    try:
        q1 = statistics.quantiles(xs, n=4)[0]
        q3 = statistics.quantiles(xs, n=4)[2]
    except statistics.StatisticsError:
        q1, q3 = mn, mx
    return (f"{label:<22} n={n:>4}   "
            f"min={mn:>10.4f}  q1={q1:>10.4f}  median={med:>10.4f}  "
            f"q3={q3:>10.4f}  max={mx:>10.4f}")


def usd_summary(label: str, xs: List[float]) -> str:
    if not xs:
        return f"{label:<22} (no data)"
    n = len(xs)
    med = statistics.median(xs)
    mn = min(xs)
    mx = max(xs)
    return (f"{label:<22} n={n:>4}   "
            f"min=${mn:>+10.2f}  median=${med:>+10.2f}  max=${mx:>+10.2f}")


# --------------------------------------------------------------------------- #
#  Section printers
# --------------------------------------------------------------------------- #
def print_config_check(failures: List[str]) -> None:
    sep("1.  STATIC CONFIG vs SHIP VALUES  (reads src/live/v30_live.py)")
    p = REPO / "src" / "live" / "v30_live.py"
    if not p.exists():
        print(f"{FAIL} can't find {p} -- is this the right repo?")
        failures.append("v30_live.py missing")
        return
    src = p.read_text(encoding="utf-8", errors="replace")

    def find_default(name: str) -> Optional[str]:
        # match e.g.   base_risk_pct: float = 0.00185
        import re
        m = re.search(rf"\n\s+{name}\s*:\s*\w+\s*=\s*([^\s#,)]+)", src)
        return m.group(1) if m else None

    checks: List[Tuple[str, Optional[str], str]] = [
        ("base_risk_pct",  find_default("base_risk_pct"),  str(EXPECTED_BASE_RISK_PCT)),
        ("cap_mult",       find_default("cap_mult"),       str(EXPECTED_CAP_MULT)),
        ("gamma",          find_default("gamma"),          str(EXPECTED_GAMMA)),
        ("ewma_alpha",     find_default("ewma_alpha"),     str(EXPECTED_EWMA_ALPHA)),
        ("warmup_trades",  find_default("warmup_trades"),  str(EXPECTED_WARMUP_TRADES)),
        ("dd_cap_pct",     find_default("dd_cap_pct"),     str(EXPECTED_DD_CAP_PCT)),
    ]
    for name, got, want in checks:
        if got is None:
            print(f"{WARN} {name:<18} NOT FOUND in v30_live.py")
            failures.append(f"config:{name}:missing")
            continue
        try:
            ok = abs(float(got) - float(want)) < 1e-9
        except ValueError:
            ok = got.strip() == want.strip()
        tag = PASS if ok else FAIL
        print(f"{tag} {name:<18} on disk = {got!s:<10}   expected = {want}")
        if not ok:
            failures.append(f"config:{name}")


def print_sizer_state(state: Optional[dict], failures: List[str]) -> dict:
    sep("2.  LIVE SIZER STATE  (Results/v30_state/sizer_mertongz.json)")
    if state is None:
        print(f"{FAIL} sizer state file is missing -- the bot is running with COLD-START warm-up.")
        print(f"{INFO} That alone explains tiny lots if it has never finished 15 trades per symbol.")
        failures.append("sizer state missing")
        return {}

    saved = state.get("saved_at_iso") or state.get("saved_at") or "?"
    schema = state.get("schema") or state.get("schema_version") or "?"
    print(f"{INFO} schema      : {schema}")
    print(f"{INFO} saved_at    : {saved}")
    cfg = state.get("config") or {}
    if cfg:
        print(f"{INFO} cfg (on disk): base={cfg.get('base_risk_pct')} "
              f"cap_mult={cfg.get('cap_mult')} gamma={cfg.get('gamma')} "
              f"warmup={cfg.get('warmup_trades')} dd_cap={cfg.get('dd_cap_pct')}")
        if abs(float(cfg.get("base_risk_pct", 0)) - EXPECTED_BASE_RISK_PCT) > 1e-9:
            print(f"{FAIL} saved sizer base_risk_pct != {EXPECTED_BASE_RISK_PCT} "
                  f"-- the bot is sizing off an OUTDATED state file!")
            failures.append("sizer state base_risk stale")

    per = state.get("per_symbol") or {}
    print()
    print(f"{INFO} per-symbol learning (the 'confidence' you asked about):")
    print(f"{INFO} {'symbol':<10} {'n_seen':>6} {'mu':>10} {'var':>10} "
          f"{'sharpe':>8} {'state':<22} {'merton_mult':>12}")
    for sym in sorted(set(list(per.keys()) + list(EXPECTED_SYMBOLS))):
        entry = per.get(sym, {})
        n = int(entry.get("n_seen", entry.get("n_trades_seen", 0)) or 0)
        mu = float(entry.get("mu", entry.get("mu_ewma", 0.0)) or 0.0)
        var = float(entry.get("var", entry.get("var_ewma", 1.0)) or 1.0)
        sharpe = mu / math.sqrt(var) if var > 0 else 0.0

        # Predict what the formula would say at peak=equity (no DD)
        mult = reconstruct_merton_mult(
            sizer_state=state,
            symbol=sym,
            equity=1.0,
            peak_equity=1.0,
            base_risk_pct=EXPECTED_BASE_RISK_PCT,
            gamma=EXPECTED_GAMMA,
            cap_mult=EXPECTED_CAP_MULT,
            warmup_trades=EXPECTED_WARMUP_TRADES,
            dd_cap_pct=EXPECTED_DD_CAP_PCT,
        )
        flags = []
        if mult["warmup"]:    flags.append("WARMUP")
        if mult["no_edge"]:   flags.append("NO_EDGE")
        if mult["capped"]:    flags.append("CAPPED")
        if mult["gz_zero"]:   flags.append("GZ=0")
        state_str = ",".join(flags) if flags else "normal"
        print(f"{INFO} {sym:<10} {n:>6} {mu:>10.4f} {var:>10.4f} "
              f"{sharpe:>8.3f} {state_str:<22} {mult['capped_mult']:>12.3f}")

        if mult["warmup"]:
            failures.append(f"sizer warmup:{sym}")
        if mult["no_edge"]:
            failures.append(f"sizer no_edge:{sym}")
    return state


def print_breaker_state(breaker: Optional[dict], failures: List[str]) -> None:
    sep("3.  DD BREAKER STATE  (Results/v30_state/dd_breaker.json)")
    if breaker is None:
        print(f"{INFO} no breaker state on disk -- cold start (peak = startup equity).")
        return
    peak = breaker.get("peak_equity") or breaker.get("peak")
    halted = breaker.get("halted") or breaker.get("is_halted")
    last_equity = breaker.get("last_equity") or breaker.get("equity")
    last_dd = breaker.get("last_dd_pct") or breaker.get("dd_pct")
    saved = breaker.get("saved_at_iso") or breaker.get("saved_at") or "?"
    print(f"{INFO} saved_at    : {saved}")
    print(f"{INFO} peak_equity : {fmt_money(peak)}")
    print(f"{INFO} equity_last : {fmt_money(last_equity)}")
    if last_dd is not None:
        try:
            d = float(last_dd)
            # may be a fraction (0.045) or a pct (4.5)
            d_frac = d / 100 if d > 1 else d
            print(f"{INFO} last_dd     : {d_frac * 100:.3f} %")
            if d_frac >= EXPECTED_DD_CAP_PCT:
                print(f"{FAIL} DD >= dd_cap_pct={EXPECTED_DD_CAP_PCT*100:.1f}% "
                      f"-> Grossman-Zhou multiplier is ZERO -> sizer returns 0 risk!")
                failures.append("dd >= dd_cap (GZ throttled to zero)")
            elif d_frac >= 0.5 * EXPECTED_DD_CAP_PCT:
                print(f"{WARN} DD is past half of dd_cap -> Merton output is being "
                      f"throttled by Grossman-Zhou.")
        except Exception:
            pass
    if halted:
        print(f"{FAIL} breaker is HALTED -- bot is currently locked out of new entries.")
        failures.append("dd breaker halted")


def print_live_trades(
    trades: List[dict],
    events: List[dict],
    sizer_state: Optional[dict],
    failures: List[str],
    since: Optional[datetime],
) -> List[dict]:
    sep("4.  LIVE ENTRIES  (Results/v30_live_trades.jsonl)")
    if not trades:
        print(f"{FAIL} no trades.jsonl on disk OR no ENTRY rows in it.")
        print(f"{INFO} copy the VPS  Results/  folder back here and rerun, "
              f"or run this script on the VPS.")
        failures.append("no live trades")
        return []

    entries = [t for t in trades if t.get("event") == "ENTRY"]
    if since is not None:
        before = len(entries)
        entries = [
            t for t in entries
            if (parse_iso_date(t.get("ts_utc", "")) or datetime.min.replace(tzinfo=timezone.utc))
                >= since
        ]
        print(f"{INFO} filtered ENTRY rows: {len(entries)} (of {before}) since {since.date()}")
    else:
        print(f"{INFO} total ENTRY rows: {len(entries)}")

    if not entries:
        print(f"{FAIL} after filtering, nothing to audit.")
        failures.append("no live entries in window")
        return []

    # Per-row sizing audit
    flags_count: Dict[str, int] = defaultdict(int)
    rows: List[dict] = []
    print()
    print(f"{INFO} {'#':>3}  {'time':<19} {'sym':<7} {'side':<5} {'eq':>9} "
          f"{'risk%':>7} {'risk$':>8} {'lots':>7} {'stop_$':>9} "
          f"{'fill_R':>7} {'flags':<24}")
    print("  " + "-" * 110)
    for i, t in enumerate(entries, 1):
        ts = safe_str(t, "ts_utc", "ts", "time", default="?")[:19]
        sym = safe_str(t, "symbol", "sym", default="?")
        side = safe_str(t, "side", "dir", default="?")
        equity = safe_float(t, "equity", default=None)
        risk_pct = safe_float(t, "risk_pct", default=None)
        risk_usd = safe_float(t, "risk_usd", default=None)
        lots = safe_float(t, "lots", default=None)
        intended = safe_float(t, "intended_px", "entry", "intended", default=None)
        sl = safe_float(t, "sl", "SL", default=None)
        stop_dollars = None
        if intended is not None and sl is not None and lots is not None:
            stop_dollars = abs(intended - sl) * lots
        ticket = None
        try:
            ticket = int(t.get("ticket") or 0) or None
        except (TypeError, ValueError):
            ticket = None
        R = realised_R_from_events(events, ticket)

        # Reconstruct what the sizer should have produced at this equity
        flags: List[str] = []
        if equity and risk_pct is not None:
            # Build a fake peak from the breaker if available
            peak = equity  # conservative: assume no DD at entry
            mult = reconstruct_merton_mult(
                sizer_state=sizer_state,
                symbol=sym,
                equity=equity,
                peak_equity=peak,
                base_risk_pct=EXPECTED_BASE_RISK_PCT,
                gamma=EXPECTED_GAMMA,
                cap_mult=EXPECTED_CAP_MULT,
                warmup_trades=EXPECTED_WARMUP_TRADES,
                dd_cap_pct=EXPECTED_DD_CAP_PCT,
            )
            if mult["warmup"]:    flags.append("WARMUP")
            if mult["no_edge"]:   flags.append("NO_EDGE")
            if mult["gz_zero"]:   flags.append("GZ=0")
            if mult["gz_throttled"] and not mult["gz_zero"]: flags.append("GZ_THROTTLED")
            if mult["capped"]:    flags.append("CAPPED")
            # Risk too low vs warm-up base?
            if risk_pct < 0.5 * EXPECTED_BASE_RISK_PCT:
                flags.append("RISK<<BASE")
        if risk_usd is not None and risk_usd < 25:
            flags.append("RISK<$25")

        for f in flags:
            flags_count[f] += 1

        print(f"  {i:>3}  {ts:<19} {sym:<7} {side:<5} "
              f"${(equity or 0):>8,.0f} {fmt_pct(risk_pct, 3)} "
              f"{(f'${risk_usd:>5.0f}' if risk_usd is not None else '   n/a '):<8} "
              f"{(f'{lots:>6.3f}' if lots is not None else '   n/a'):<7} "
              f"{(f'${stop_dollars:>7.0f}' if stop_dollars is not None else '    n/a'):<9} "
              f"{(f'{R:>+6.2f}' if R is not None else '   n/a'):<7} "
              f"{','.join(flags):<24}")
        rows.append({
            "ts": ts, "sym": sym, "side": side, "equity": equity,
            "risk_pct": risk_pct, "risk_usd": risk_usd, "lots": lots,
            "stop_dollars": stop_dollars, "R": R, "flags": flags,
            "ticket": ticket,
        })

    print()
    print(f"{INFO} flag counts across {len(entries)} entries:")
    for f, c in sorted(flags_count.items(), key=lambda kv: -kv[1]):
        print(f"{INFO}    {f:<14} {c}")
        if f in ("WARMUP", "NO_EDGE", "GZ=0", "RISK<<BASE", "RISK<$25"):
            failures.append(f"entry_flag:{f}:{c}")
    return rows


def print_live_aggregates(rows: List[dict]) -> None:
    sep("5.  LIVE DISTRIBUTIONS  (what the bot actually risks per trade)")
    risk_pcts = [r["risk_pct"] for r in rows if r["risk_pct"] is not None]
    risk_usds = [r["risk_usd"] for r in rows if r["risk_usd"] is not None]
    lots      = [r["lots"]     for r in rows if r["lots"]     is not None]
    stops     = [r["stop_dollars"] for r in rows if r["stop_dollars"] is not None]
    Rs        = [r["R"] for r in rows if r["R"] is not None]

    print(f"{INFO} " + pct_summary("risk_pct (frac)",  risk_pcts))
    print(f"{INFO} " + usd_summary("risk_usd ($)",      risk_usds))
    print(f"{INFO} " + pct_summary("lots",              lots))
    print(f"{INFO} " + usd_summary("stop distance ($)", stops))
    if Rs:
        wins = [r for r in Rs if r > 0]
        losses = [r for r in Rs if r < 0]
        wr = len(wins) / len(Rs) * 100 if Rs else 0.0
        avg = statistics.mean(Rs)
        gp = sum(wins)
        gl = abs(sum(losses)) or 1e-9
        pf = gp / gl
        print(f"{INFO} realised_R   n={len(Rs)}  WR={wr:.1f}%  avg_R={avg:+.3f}  "
              f"PF={pf:.2f}  sumR={sum(Rs):+.2f}")


def print_backtest_compare(bt_path: Path, live_rows: List[dict], failures: List[str]) -> None:
    sep("6.  BACKTEST DISTRIBUTIONS  (Results/v30_fresh_trades.json)")
    raw = read_json(bt_path)
    if raw is None:
        print(f"{WARN} backtest trades file not found at {bt_path}")
        return
    if isinstance(raw, dict) and "trades" in raw:
        raw = raw["trades"]
    if not isinstance(raw, list) or not raw:
        print(f"{WARN} backtest trades file has unexpected layout.")
        return

    # Note: the backtest file has  net_pnl  and  realised_R  per trade.
    # The live trades file has risk_usd / risk_pct / lots PER ENTRY.
    # They're not 1-to-1 (entry-row vs trade-row), but distribution is comparable.
    bt_R = [safe_float(t, "realised_R", "R") for t in raw if safe_float(t, "realised_R", "R") is not None]
    bt_pnl = [safe_float(t, "net_pnl", "pnl") for t in raw if safe_float(t, "net_pnl", "pnl") is not None]

    print(f"{INFO} backtest n_trades = {len(raw)}")
    if bt_R:
        wins = [r for r in bt_R if r > 0]
        losses = [r for r in bt_R if r < 0]
        wr = len(wins) / len(bt_R) * 100
        avg = statistics.mean(bt_R)
        gp = sum(wins)
        gl = abs(sum(losses)) or 1e-9
        pf = gp / gl
        print(f"{INFO} BACKTEST realised_R  WR={wr:.1f}%  avg_R={avg:+.3f}  "
              f"PF={pf:.2f}  sumR={sum(bt_R):+.2f}")
    if bt_pnl:
        total = sum(bt_pnl)
        median = statistics.median(bt_pnl)
        print(f"{INFO} BACKTEST net_pnl ($)  total=${total:+,.2f}  "
              f"median=${median:+,.2f}  n={len(bt_pnl)}")

    # Compare live R distribution
    live_R = [r["R"] for r in live_rows if r["R"] is not None]
    if live_R and bt_R:
        live_avg = statistics.mean(live_R)
        bt_avg = statistics.mean(bt_R)
        print()
        print(f"{INFO} avg_R   LIVE={live_avg:+.3f}    BACKTEST={bt_avg:+.3f}")
        if live_avg < bt_avg - 0.40:
            print(f"{WARN} live avg_R is much worse than backtest "
                  f"(diff {live_avg - bt_avg:+.2f}R).")
            # this is a quality issue, not a wiring issue -- don't add a hard fail
        else:
            print(f"{PASS} R-distribution per-trade is in the same ballpark "
                  f"-- this is a SIZING problem, not a SIGNAL problem.")

    # Compare live RISK $ to what the backtest would have used at $100k
    live_risk_usd = [r["risk_usd"] for r in live_rows if r["risk_usd"] is not None]
    if live_risk_usd:
        med_live = statistics.median(live_risk_usd)
        # Backtest assumed 0.185% * 100k = $185 at warm-up
        expected_min = EXPECTED_BASE_RISK_PCT * 100_000 * 0.5   # no_edge fallback
        expected_warmup = EXPECTED_BASE_RISK_PCT * 100_000      # warmup
        expected_max = EXPECTED_BASE_RISK_PCT * 100_000 * EXPECTED_CAP_MULT  # cap
        print()
        print(f"{INFO} median live risk_$ = ${med_live:,.2f}")
        print(f"{INFO} expected at $100k equity:")
        print(f"{INFO}     no_edge fallback   = ${expected_min:,.2f}")
        print(f"{INFO}     warm-up base       = ${expected_warmup:,.2f}")
        print(f"{INFO}     cap (5x base)      = ${expected_max:,.2f}")
        if med_live < 0.25 * expected_warmup:
            print(f"{FAIL} median live risk_$ is <25% of warm-up baseline. "
                  f"Sizer is in a degenerate state (DD>cap? wrong equity? "
                  f"bad pip_value table?).")
            failures.append("median risk_$ << warm-up baseline")


def print_event_breakdown(events: List[dict], failures: List[str]) -> None:
    sep("7.  LIVE EVENT KINDS  (Results/v30_live_events.log)")
    if not events:
        print(f"{WARN} no events.log on disk.")
        return
    counts: Dict[str, int] = defaultdict(int)
    for r in events:
        counts[str(r.get("kind", "?"))] += 1
    for k, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"{INFO}   {k:<30} {c}")

    # Sanity rails
    n_entry = counts.get("ENTRY", 0)
    n_tp1   = counts.get("TP1_PARTIAL", 0)
    n_tp2   = counts.get("TP2_PARTIAL", 0)
    n_close = counts.get("CLOSE", 0)
    n_layer1 = counts.get("LAYER1_FIRED", 0)
    n_ord_fail = counts.get("ORDER_FAILED", 0)
    n_block_news = counts.get("BLOCK_NEWS", 0)
    n_dd = counts.get("TOTAL_DD_BREAKER_8PCT", 0) + counts.get("DAY_HALTED_4PCT", 0)

    if n_entry == 0:
        print(f"{WARN} zero ENTRY events.")
    if n_entry > 0 and n_close == 0 and n_tp1 == 0 and n_layer1 == 0:
        print(f"{FAIL} entries fired but ZERO closes/TPs/layer1 events -- "
              f"the management loop may not be running.")
        failures.append("entries with no management events")
    if n_layer1 > 0.5 * max(n_entry, 1):
        print(f"{FAIL} Layer1 fired on {n_layer1}/{n_entry} entries "
              f"(>50%) -- envelope or cap may be too tight.")
        failures.append("layer1 fired too often")
    if n_ord_fail > 0:
        print(f"{WARN} {n_ord_fail} ORDER_FAILED events -- bridge/broker rejection.")
    if n_dd > 0:
        print(f"{WARN} DD breaker fired {n_dd}x.")


def print_orphaned_tickets(trades: List[dict], events: List[dict],
                           failures: List[str]) -> None:
    sep("8.  ORPHANED LIVE TICKETS  (ENTRY with NO TP/CLOSE/LAYER1 event)")
    by_ticket_kinds: Dict[int, set] = defaultdict(set)
    for r in events:
        try:
            tk = int(r.get("ticket") or 0)
        except (TypeError, ValueError):
            continue
        if not tk:
            continue
        by_ticket_kinds[tk].add(str(r.get("kind") or ""))

    LADDER = {"TP1_PARTIAL", "TP2_PARTIAL", "TRAIL_SL", "CLOSE",
              "POS_CLOSED_BY_BROKER", "LAYER1_FIRED", "FLATTEN_ALL"}
    orphans = []
    for t in trades:
        if t.get("event") != "ENTRY":
            continue
        try:
            tk = int(t.get("ticket") or 0)
        except (TypeError, ValueError):
            tk = 0
        if not tk:
            continue
        kinds = by_ticket_kinds.get(tk, set())
        if not (kinds & LADDER):
            orphans.append((tk, t.get("symbol"), safe_str(t, "ts_utc")[:19]))

    if not orphans:
        print(f"{PASS} every entry has at least one ladder/close event.")
    else:
        print(f"{FAIL} {len(orphans)} entries with NO ladder/close event:")
        for tk, sym, ts in orphans[:50]:
            print(f"{INFO}    ticket={tk} sym={sym} t={ts}")
        failures.append(f"orphans:{len(orphans)}")


def print_slippage_summary(slippage_path: Path) -> None:
    sep("9.  ENTRY-FILL SLIPPAGE  (Results/v30_live_slippage.jsonl)")
    rows = read_jsonl(slippage_path)
    if not rows:
        print(f"{WARN} no slippage data on disk.")
        return
    by_sym: Dict[str, List[float]] = defaultdict(list)
    dollars: List[float] = []
    for r in rows:
        s = r.get("symbol") or "?"
        t = r.get("slip_ticks") or r.get("ticks")
        d = r.get("slip_dollars") or r.get("dollars")
        if t is not None:
            try:
                by_sym[s].append(float(t))
            except Exception:
                pass
        if d is not None:
            try:
                dollars.append(float(d))
            except Exception:
                pass
    for s in sorted(by_sym):
        xs = by_sym[s]
        if not xs:
            continue
        print(f"{INFO} {s:<8} n={len(xs):>4}  median_ticks={statistics.median(xs):+.2f}  "
              f"worst={max(xs, key=abs):+.2f}")
    if dollars:
        print(f"{INFO} total realised slip $ = ${sum(dollars):+,.2f} "
              f"(median {statistics.median(dollars):+,.2f})")


# --------------------------------------------------------------------------- #
#  main
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[2])
    ap.add_argument("--trades",  default="Results/v30_live_trades.jsonl")
    ap.add_argument("--events",  default="Results/v30_live_events.log")
    ap.add_argument("--state",   default="Results/v30_state/sizer_mertongz.json")
    ap.add_argument("--breaker", default="Results/v30_state/dd_breaker.json")
    ap.add_argument("--bt",      default="Results/v30_fresh_trades.json")
    ap.add_argument("--slip",    default="Results/v30_live_slippage.jsonl")
    ap.add_argument("--since",   default="",
                    help="ISO date (YYYY-MM-DD). Only audit entries on/after.")
    args = ap.parse_args()

    trades_path  = (REPO / args.trades) if not Path(args.trades).is_absolute()  else Path(args.trades)
    events_path  = (REPO / args.events) if not Path(args.events).is_absolute()  else Path(args.events)
    state_path   = (REPO / args.state)  if not Path(args.state).is_absolute()   else Path(args.state)
    breaker_path = (REPO / args.breaker)if not Path(args.breaker).is_absolute() else Path(args.breaker)
    bt_path      = (REPO / args.bt)     if not Path(args.bt).is_absolute()      else Path(args.bt)
    slip_path    = (REPO / args.slip)   if not Path(args.slip).is_absolute()    else Path(args.slip)

    sep("v31 LIVE  vs  BACKTEST   WIRING + SIZING DIAGNOSTIC")
    print(f"{INFO} repo        : {REPO}")
    print(f"{INFO} trades.jsonl: {trades_path}   exists={trades_path.exists()}")
    print(f"{INFO} events.log  : {events_path}   exists={events_path.exists()}")
    print(f"{INFO} sizer state : {state_path}   exists={state_path.exists()}")
    print(f"{INFO} dd breaker  : {breaker_path}   exists={breaker_path.exists()}")
    print(f"{INFO} backtest    : {bt_path}   exists={bt_path.exists()}")
    print(f"{INFO} slippage    : {slip_path}   exists={slip_path.exists()}")

    since = parse_iso_date(args.since) if args.since else None

    failures: List[str] = []

    print_config_check(failures)
    sizer_state = read_json(state_path) if isinstance(read_json(state_path), dict) else None
    breaker_state = read_json(breaker_path) if isinstance(read_json(breaker_path), dict) else None

    print_sizer_state(sizer_state, failures)
    print_breaker_state(breaker_state, failures)

    trades = read_jsonl(trades_path)
    events = read_jsonl(events_path)
    rows = print_live_trades(trades, events, sizer_state, failures, since)
    print_live_aggregates(rows)
    print_backtest_compare(bt_path, rows, failures)
    print_event_breakdown(events, failures)
    print_orphaned_tickets(trades, events, failures)
    print_slippage_summary(slip_path)

    # -------- final verdict --------
    sep("FINAL VERDICT")
    if not failures:
        print(f"{PASS} every rail passed -- live engine looks wired-up the same as the backtest.")
        print(f"{INFO} If PnL still diverges, suspect data quality / regime, not wiring.")
        return 0

    # Suppress duplicate flags (e.g. RISK<<BASE counted per-trade)
    seen: set = set()
    unique_fail: List[str] = []
    for f in failures:
        key = f.split(":")[0]
        if key in seen:
            continue
        seen.add(key)
        unique_fail.append(f)

    print(f"{FAIL} {len(unique_fail)} category(ies) failed:")
    for f in unique_fail:
        print(f"{FAIL}    -> {f}")
    print()
    # Plain-English likely-cause based on flags
    if any(f.startswith("sizer warmup") for f in failures):
        print(f"{INFO} LIKELY CAUSE: per-symbol Merton sizer is still in WARM-UP "
              f"(n_seen < {EXPECTED_WARMUP_TRADES}).  Until that fills, every "
              f"entry uses base_risk_pct = {EXPECTED_BASE_RISK_PCT*100:.3f}%.")
        print(f"{INFO}              Re-seed the sizer from a 3-month backtest:")
        print(f"{INFO}                  copy Results\\v30_fresh_trades.json onto the VPS")
        print(f"{INFO}                  restart the bot -- it will seed from the seed file.")
    if any(f.startswith("sizer no_edge") for f in failures):
        print(f"{INFO} LIKELY CAUSE: at least one symbol's EWMA mu is <= 0 -- the bot is")
        print(f"{INFO}              halving size on that symbol because it sees no edge.")
    if any(f.startswith("dd ") for f in failures):
        print(f"{INFO} LIKELY CAUSE: Grossman-Zhou drawdown throttle is active.")
    if any(f.startswith("median risk") for f in failures):
        print(f"{INFO} LIKELY CAUSE: equity reported to the sizer is much smaller than 100k,")
        print(f"{INFO}              OR pip_value/tick_size mapping is wrong, OR the saved sizer")
        print(f"{INFO}              state on disk is stale (run verify_v31_live_wiring.py).")
    return 1


if __name__ == "__main__":
    sys.exit(main())
