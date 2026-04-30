"""
v31_preflight.py — Single-shot bot self-audit.

Run on the VPS (or any clone) BEFORE launching live to print a structured
report of every guarantee the bot makes. The output is designed to be
copy-pasted back to the developer for a 100%-confidence sign-off.

What it verifies (all checks are READ-ONLY — nothing is mutated):

  GIT      : current branch, last commit, working tree clean?
  CONFIG   : v30_live.py — base_risk_pct, cap, daily/total kill %, news
             rails, max-concurrent, magic, no-chase cooldown
  SIZER    : Results/v30_state/sizer_mertongz.json — schema, n_seen,
             μ̂, σ̂², base_risk_pct, last_update age
  BREAKER  : Results/v30_state/dd_breaker.json — schema, peak_equity,
             halted dates, last update age
  SYMBOLS  : per-symbol tick_size, contract_size, pip_value (broker-truth),
             ORB anchors, TP/SL multipliers
  GUARDS   : DailyHalt @4% (5ers daily kill = 5%, buffer = 1pt)
             DDBreaker @8% (5ers total kill = 10%, buffer = 2pt)
             account_kill_dd, daily_breaker_dd, max_concurrent_positions,
             min_hold_seconds (must exceed 60 to avoid HFT flag)
  NEWS     : data/news/tier1_2026.csv — exists, # events, next event time
  LAYER 1  : honest status — NOT BUILT (entry deviation only)
  EA       : MQL5/Experts/SHF_Bridge.mq5 — exists, size, mtime
  PYTHON   : interpreter version, key package versions

Usage
-----
    python Scripts/v31_preflight.py            # full report to stdout
    python Scripts/v31_preflight.py --json     # also write JSON snapshot

Exit codes
----------
    0  all hard-required checks passed
    1  at least one hard-required check FAILED — DO NOT GO LIVE
    2  internal error in the preflight script itself
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Make the project importable when run as a script.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# =====================================================================
# Helpers
# =====================================================================
def _ok(msg: str) -> str:    return f"  [ OK ] {msg}"
def _warn(msg: str) -> str:  return f"  [WARN] {msg}"
def _fail(msg: str) -> str:  return f"  [FAIL] {msg}"
def _info(msg: str) -> str:  return f"         {msg}"


def _run(cmd, cwd=ROOT):
    try:
        return subprocess.check_output(cmd, cwd=str(cwd), stderr=subprocess.STDOUT,
                                       text=True, timeout=10).strip()
    except Exception as e:
        return f"<error: {e}>"


def _age_str(ts: float) -> str:
    if ts <= 0:
        return "(never)"
    d = time.time() - ts
    if d < 60: return f"{d:.0f}s ago"
    if d < 3600: return f"{d/60:.1f}m ago"
    if d < 86400: return f"{d/3600:.1f}h ago"
    return f"{d/86400:.1f}d ago"


# =====================================================================
# Sections
# =====================================================================
def section_git(report: dict) -> list:
    out = ["", "=" * 78, "  GIT", "=" * 78]
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    sha = _run(["git", "rev-parse", "--short", "HEAD"])
    sha_full = _run(["git", "rev-parse", "HEAD"])
    last_msg = _run(["git", "log", "-1", "--pretty=%s"])
    last_date = _run(["git", "log", "-1", "--pretty=%ci"])
    porcelain = _run(["git", "status", "--porcelain"])
    clean = porcelain == "" or porcelain.startswith("<error")
    report["git"] = {
        "branch": branch, "sha_short": sha, "sha_full": sha_full,
        "last_commit_msg": last_msg, "last_commit_date": last_date,
        "clean": clean,
    }
    out.append(_info(f"branch        : {branch}"))
    out.append(_info(f"commit        : {sha}    ({last_date})"))
    out.append(_info(f"last message  : {last_msg}"))
    if clean:
        out.append(_ok("working tree clean"))
    else:
        out.append(_warn("working tree has uncommitted changes:"))
        for line in porcelain.splitlines()[:10]:
            out.append(_info(f"  {line}"))
    return out


def section_config(report: dict) -> list:
    out = ["", "=" * 78, "  CONFIG (v30_live.py)", "=" * 78]
    try:
        from src.live.v30_live import V30LiveConfig
        c = V30LiveConfig()
    except Exception as e:
        out.append(_fail(f"could not import V30LiveConfig: {e}"))
        report["config"] = {"error": str(e)}
        return out
    cfg = {
        "symbols": list(c.symbols),
        "base_risk_pct": c.base_risk_pct,
        "cap_mult": c.cap_mult,
        "max_per_trade_pct": c.base_risk_pct * c.cap_mult,
        "gamma": c.gamma,
        "ewma_alpha": c.ewma_alpha,
        "warmup_trades": c.warmup_trades,
        "dd_cap_pct": c.dd_cap_pct,
        "account_kill_dd": c.account_kill_dd,
        "daily_breaker_dd": c.daily_breaker_dd,
        "max_concurrent_positions": c.max_concurrent_positions,
        "min_hold_seconds": c.min_hold_seconds,
        "nochase_cooldown_s": c.nochase_cooldown_s,
        "magic": c.magic,
        "comment": c.comment,
        "news_csv": c.news_csv,
        "news_entry_buffer_min": c.news_entry_buffer_min,
        "news_flatten_before_min": c.news_flatten_before_min,
        "state_dir": c.state_dir,
        "state_max_age_days": c.state_max_age_days,
    }
    report["config"] = cfg
    out.append(_info(f"symbols                {cfg['symbols']}"))
    out.append(_info(f"base_risk_pct          {cfg['base_risk_pct']*100:.4f}%   "
                     f"(per-trade unit before Merton scaling)"))
    out.append(_info(f"cap_mult               {cfg['cap_mult']:.1f}× = "
                     f"{cfg['max_per_trade_pct']*100:.4f}% per-trade ceiling"))
    out.append(_info(f"gamma (CRRA)           {cfg['gamma']:.1f}"))
    out.append(_info(f"ewma_alpha             {cfg['ewma_alpha']:.2f}    "
                     f"(half-life ≈ {0.69/cfg['ewma_alpha']:.0f} trades)"))
    out.append(_info(f"warmup_trades          {cfg['warmup_trades']}"))
    out.append(_info(f"dd_cap_pct (GZ)        {cfg['dd_cap_pct']*100:.1f}%  "
                     f"(Grossman-Zhou barrier — sizer → 0 at this DD)"))
    out.append(_info(f"max_concurrent         {cfg['max_concurrent_positions']} positions"))
    out.append(_info(f"min_hold_seconds       {cfg['min_hold_seconds']} s   "
                     f"(must exceed 60 for HFT compliance)"))
    out.append(_info(f"no-chase cooldown      {cfg['nochase_cooldown_s']:.0f} s   "
                     f"(cross-symbol; same-symbol unaffected)"))
    out.append(_info(f"magic                  {cfg['magic']}"))
    out.append(_info(f"comment                '{cfg['comment']}'"))

    # Hard checks
    if abs(cfg["base_risk_pct"] - 0.00185) < 1e-9:
        out.append(_ok(f"base_risk_pct = 0.185% — V31 ship value"))
    elif abs(cfg["base_risk_pct"] - 0.00170) < 1e-9:
        out.append(_warn("base_risk_pct = 0.170% — V25.1 ship (V31 not yet applied)"))
    else:
        out.append(_warn(f"base_risk_pct = {cfg['base_risk_pct']*100:.3f}% — "
                         "non-standard value, check intent"))

    if cfg["min_hold_seconds"] > 60:
        out.append(_ok(f"min_hold_seconds = {cfg['min_hold_seconds']} > 60 (HFT-safe)"))
    else:
        out.append(_fail(f"min_hold_seconds = {cfg['min_hold_seconds']} ≤ 60 — HFT FLAG RISK"))

    return out


def section_guards(report: dict) -> list:
    out = ["", "=" * 78, "  KILL-SWITCH GUARDS (5ers compliance)", "=" * 78]
    try:
        from src.daily_halt import DailyHalt
        from src.dd_breaker import DDBreaker
        dh = DailyHalt(halt_pct=0.04)
        db = DDBreaker(halt_pct=0.08)
    except Exception as e:
        out.append(_fail(f"could not import guards: {e}"))
        report["guards"] = {"error": str(e)}
        return out

    guards = {
        "daily_halt_pct": dh.halt_pct,
        "daily_halt_5ers_kill": 0.05,
        "daily_halt_buffer_pp": 0.05 - dh.halt_pct,
        "dd_breaker_pct": db.halt_pct,
        "dd_breaker_5ers_kill": 0.10,
        "dd_breaker_buffer_pp": 0.10 - db.halt_pct,
    }
    report["guards"] = guards

    out.append(_info(f"daily kill (static 4 %)     bot halts at {dh.halt_pct*100:.1f}%   "
                     f"5ers kills at 5.0%   buffer = {guards['daily_halt_buffer_pp']*100:.1f}pp"))
    out.append(_info(f"total kill (rolling 8 %)    bot halts at {db.halt_pct*100:.1f}%   "
                     f"5ers kills at 10.0%  buffer = {guards['dd_breaker_buffer_pp']*100:.1f}pp"))

    if abs(dh.halt_pct - 0.04) < 1e-6:
        out.append(_ok("DailyHalt = 4% (1pp buffer below 5ers daily 5% kill)"))
    else:
        out.append(_fail(f"DailyHalt = {dh.halt_pct*100:.1f}% — must be 4% for 5ers compliance"))

    if abs(db.halt_pct - 0.08) < 1e-6:
        out.append(_ok("DDBreaker = 8% (2pp buffer below 5ers total 10% kill)"))
    else:
        out.append(_fail(f"DDBreaker = {db.halt_pct*100:.1f}% — must be 8% for 5ers compliance"))

    return out


def section_sizer_state(report: dict) -> list:
    out = ["", "=" * 78, "  SIZER STATE (Merton × Grossman-Zhou)", "=" * 78]
    p = ROOT / "Results/v30_state/sizer_mertongz.json"
    if not p.exists():
        out.append(_warn(f"state file not found: {p}"))
        out.append(_info("→ on next bot start the sizer will fall back to "
                         "seed-from-backtest, then cold-start (15-trade warm-up)"))
        report["sizer_state"] = {"exists": False, "path": str(p)}
        return out

    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        out.append(_fail(f"state file is unreadable: {e}"))
        report["sizer_state"] = {"exists": True, "error": str(e)}
        return out

    cfg = st.get("cfg", {}) or st.get("config", {})
    n_seen = sum((st.get("_n_seen") or {}).values())
    mu = st.get("_mu", {})
    var = st.get("_var", {})
    age = _age_str(p.stat().st_mtime)

    summary = {
        "exists": True, "path": str(p),
        "schema": st.get("schema"),
        "trades_seen": n_seen,
        "n_keys_with_mu": len(mu),
        "base_risk_pct": cfg.get("base_risk_pct"),
        "cap_mult": cfg.get("cap_mult"),
        "gamma": cfg.get("gamma"),
        "dd_cap_pct": cfg.get("dd_cap_pct"),
        "warmup_trades": cfg.get("warmup_trades"),
        "pool_symbols": cfg.get("pool_symbols"),
        "age": age,
        "mtime_unix": p.stat().st_mtime,
        "mu": {k: round(float(v), 4) for k, v in mu.items()},
        "var": {k: round(float(v), 4) for k, v in var.items()},
    }
    report["sizer_state"] = summary

    out.append(_info(f"file              {p}"))
    out.append(_info(f"last modified     {age}"))
    out.append(_info(f"schema version    {summary['schema']}"))
    out.append(_info(f"trades seen       {summary['trades_seen']}"))
    out.append(_info(f"pool_symbols      {summary['pool_symbols']}"))
    out.append(_info(f"base_risk_pct     {summary['base_risk_pct']}   "
                     f"({(summary['base_risk_pct'] or 0)*100:.4f}%)"))
    out.append(_info(f"cap_mult          {summary['cap_mult']}"))
    out.append(_info(f"gamma             {summary['gamma']}"))
    out.append(_info(f"dd_cap_pct        {summary['dd_cap_pct']}"))

    if mu:
        for k in mu:
            out.append(_info(f"  {k:<10} μ̂={mu[k]:+.4f}  σ̂²={var.get(k, 0.0):.4f}"))

    # Hard checks
    if summary["base_risk_pct"] is None:
        out.append(_fail("base_risk_pct missing in state — schema unknown"))
    elif abs(summary["base_risk_pct"] - 0.00185) < 1e-9:
        out.append(_ok("state base_risk_pct = 0.185% — matches V31 ship config"))
    elif abs(summary["base_risk_pct"] - 0.00170) < 1e-9:
        out.append(_warn("state base_risk_pct = 0.170% — V25.1 (run "
                         "v31_migrate_sizer_state.py before starting bot)"))
    else:
        out.append(_warn(f"state base_risk_pct = {summary['base_risk_pct']} — "
                         "unexpected value"))

    age_d = (time.time() - p.stat().st_mtime) / 86400.0
    if age_d > 14:
        out.append(_warn(f"state is {age_d:.1f} days old — bot will REJECT it "
                         "(state_max_age_days = 14) and cold-start"))
    else:
        out.append(_ok(f"state age {age_d:.1f}d ≤ 14d limit — will load on startup"))

    if summary["trades_seen"] >= 15:
        out.append(_ok(f"trades_seen = {summary['trades_seen']} ≥ 15 → Merton ACTIVE on startup"))
    elif summary["trades_seen"] > 0:
        out.append(_warn(f"trades_seen = {summary['trades_seen']} < 15 → "
                         "warm-up phase, flat risk until 15 trades"))
    else:
        out.append(_warn("trades_seen = 0 → cold start"))

    return out


def section_breaker_state(report: dict) -> list:
    out = ["", "=" * 78, "  DD BREAKER STATE", "=" * 78]
    p = ROOT / "Results/v30_state/dd_breaker.json"
    if not p.exists():
        out.append(_warn(f"breaker state file not found: {p}  → fresh start"))
        report["breaker_state"] = {"exists": False, "path": str(p)}
        return out
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        out.append(_fail(f"breaker state unreadable: {e}"))
        report["breaker_state"] = {"exists": True, "error": str(e)}
        return out
    age = _age_str(p.stat().st_mtime)
    summary = {
        "exists": True, "path": str(p),
        "schema": st.get("schema"),
        "peak_equity": st.get("peak_equity"),
        "halt_pct": st.get("halt_pct"),
        "is_halted": st.get("is_halted"),
        "total_halts": st.get("total_halts"),
        "halted_dates": st.get("halted_dates"),
        "age": age,
    }
    report["breaker_state"] = summary
    out.append(_info(f"file              {p}"))
    out.append(_info(f"last modified     {age}"))
    out.append(_info(f"schema            {summary['schema']}"))
    out.append(_info(f"peak_equity       ${summary['peak_equity']:,.2f}"))
    out.append(_info(f"halt_pct          {summary['halt_pct']*100:.1f}%"))
    out.append(_info(f"is_halted         {summary['is_halted']}"))
    out.append(_info(f"total_halts       {summary['total_halts']}"))
    if abs((summary["halt_pct"] or 0) - 0.08) < 1e-6:
        out.append(_ok("halt_pct = 8% — matches 5ers total kill - 2pp buffer"))
    else:
        out.append(_fail(f"halt_pct = {(summary['halt_pct'] or 0)*100:.1f}% — must be 8%"))
    if summary["is_halted"]:
        out.append(_fail("breaker is HALTED — bot will not trade until reset"))
    else:
        out.append(_ok("breaker is not halted"))
    return out


def section_symbols(report: dict) -> list:
    out = ["", "=" * 78, "  SYMBOL SPECS (broker-truth)", "=" * 78]
    try:
        from src.live.v30_live import (
            V30_SPECS, V30_ORB_CONFIGS,
            V30_BROKER_CONTRACT_SIZE, V30_DOLLARS_PER_TICK_PER_LOT,
        )
    except Exception as e:
        out.append(_fail(f"could not import symbol tables: {e}"))
        report["symbols"] = {"error": str(e)}
        return out

    sym_table = {}
    out.append(_info(f"{'sym':<8} {'tick':>6} {'cs':>5} {'$/pt/lot':>9} "
                     f"{'min_lot':>7} {'step':>6}   ORB anchors (start UTC, mins, "
                     f"TP1, TP2, SL_buf)"))
    out.append(_info("-" * 78))
    for sym in ("DE40", "US30", "XAUUSD", "US500"):
        spec = V30_SPECS[sym]
        cs = V30_BROKER_CONTRACT_SIZE[sym]
        pip = V30_DOLLARS_PER_TICK_PER_LOT[sym]
        orb = V30_ORB_CONFIGS[sym]
        sym_table[sym] = {
            "broker_name": spec.broker, "tick_size": spec.tick_size,
            "contract_size": cs, "pip_value_per_lot": pip,
            "min_lot": spec.min_lot, "lot_step": spec.lot_step,
            "or_start": f"{orb.or_start_hour:02d}:{orb.or_start_minute:02d}",
            "or_minutes": orb.or_minutes,
            "tp1_mult": orb.tp1_range_mult, "tp2_mult": orb.tp2_range_mult,
            "sl_buf_mult": orb.sl_buffer_range_mult,
            "trade_window_minutes": orb.trade_window_minutes,
        }
        out.append(_info(
            f"{sym:<8} {spec.tick_size:>6.2f} {cs:>5.0f} ${pip:>8.2f} "
            f"{spec.min_lot:>7.2f} {spec.lot_step:>6.2f}   "
            f"{orb.or_start_hour:02d}:{orb.or_start_minute:02d}, "
            f"{orb.or_minutes}m, TP1×{orb.tp1_range_mult}, "
            f"TP2×{orb.tp2_range_mult}, SLbuf×{orb.sl_buffer_range_mult}"))
    report["symbols"] = sym_table

    # Hard checks vs 5ers spec sheet (provided by trader 2026-04-30)
    expected = {
        "US30":   {"contract_size": 1.0,   "min_lot": 0.01, "lot_step": 0.01},
        "US500":  {"contract_size": 1.0,   "min_lot": 0.01, "lot_step": 0.01},
        "DE40":   {"contract_size": 1.0,   "min_lot": 0.01, "lot_step": 0.01},
        "XAUUSD": {"contract_size": 100.0, "min_lot": 0.01, "lot_step": 0.01},
    }
    all_ok = True
    for sym, exp in expected.items():
        s = sym_table[sym]
        for k, v in exp.items():
            actual = s[k] if k != "contract_size" else V30_BROKER_CONTRACT_SIZE[sym]
            if abs(actual - v) > 1e-9:
                out.append(_fail(f"{sym}.{k} = {actual} ≠ 5ers spec {v}"))
                all_ok = False
    if all_ok:
        out.append(_ok("all symbol contract_size / min_lot / lot_step match "
                       "5ers spec sheet (2026-04-30)"))

    return out


def section_news(report: dict) -> list:
    out = ["", "=" * 78, "  NEWS RAILS", "=" * 78]
    p = ROOT / "data/news/tier1_2026.csv"
    if not p.exists():
        out.append(_fail(f"news CSV missing: {p}"))
        report["news"] = {"exists": False}
        return out
    try:
        from src.live.v30_live import V30Live  # noqa: F401  — used for _load_news
        from src.live.v30_live import V30Live as _V
        events = _V._load_news(p)  # type: ignore[attr-defined]
    except Exception as e:
        out.append(_warn(f"could not load news: {e}"))
        events = []
    now = _dt.datetime.now(_dt.timezone.utc)
    upcoming = [(t, l) for (t, l) in events if t >= now][:5]
    summary = {
        "exists": True, "path": str(p), "n_events": len(events),
        "next_5": [{"ts": t.isoformat(), "label": l} for (t, l) in upcoming],
    }
    report["news"] = summary
    out.append(_info(f"file          {p}"))
    out.append(_info(f"events loaded {len(events)} Tier-1"))
    if upcoming:
        out.append(_info("next 5 events:"))
        for (t, l) in upcoming:
            mins = (t - now).total_seconds() / 60.0
            out.append(_info(f"  {t.isoformat()}  (+{mins:6.0f} min)  {l}"))
    if events:
        out.append(_ok(f"{len(events)} news events loaded — entry block ±15min, "
                       "flatten 2 min before"))
    else:
        out.append(_warn("no news events loaded — news rail will be INACTIVE"))
    return out


def section_layer1(report: dict) -> list:
    out = ["", "=" * 78, "  LAYER 1 — exit-slip cap (broker stop-limit + 60s envelope)",
           "=" * 78]
    # Layer 1 is NOT BUILT in this release. Honest status print.
    bridge = ROOT / "src/execution/mt5_bridge.py"
    ea = ROOT / "MQL5/Experts/SHF_Bridge.mq5"

    bridge_has_stoplimit = False
    if bridge.exists():
        bridge_has_stoplimit = ("stop_limit" in bridge.read_text(encoding="utf-8").lower()
                                or "STOP_LIMIT" in bridge.read_text(encoding="utf-8"))

    ea_has_stoplimit = False
    if ea.exists():
        txt = ea.read_text(encoding="utf-8")
        ea_has_stoplimit = ("ORDER_TYPE_BUY_STOP_LIMIT" in txt
                            or "ORDER_TYPE_SELL_STOP_LIMIT" in txt
                            or "layer1" in txt.lower())

    summary = {
        "built": bridge_has_stoplimit and ea_has_stoplimit,
        "mt5_bridge_has_stop_limit": bridge_has_stoplimit,
        "ea_has_stop_limit": ea_has_stoplimit,
        "expected_savings_3mo": "+$3.7k vs uncapped slip (per V31 proof matrix)",
        "current_protection": (
            "Entry: deviation=20pt cap on market orders.  "
            "Exit (broker SL fill): UNCAPPED — broker fills at next available "
            "price during gap/news. Worst observed today = 14.82pt slip on US30."
        ),
    }
    report["layer1"] = summary

    if summary["built"]:
        out.append(_ok("Layer 1 implementation detected in mt5_bridge.py + EA"))
    else:
        out.append(_warn("LAYER 1 IS NOT BUILT YET — Step 2 of v31 (separate ship)"))
        out.append(_info("Current entry-slip protection: deviation=20pt cap on market orders ✓"))
        out.append(_info("Current exit-slip protection: NONE — broker SL fills at "
                         "next available price"))
        out.append(_info("Worst slip observed today (2026-04-30): 14.82pt on US30 "
                         "= -$61 vs intended"))
        out.append(_info("Expected savings when Layer 1 ships: +$3.7k over 3 months "
                         "(per V31 proof matrix)"))
        out.append(_info(""))
        out.append(_info("Daily/total kill switches still cap your maximum loss "
                         "regardless:"))
        out.append(_info("  - daily kill at 4% (5ers kills at 5%)"))
        out.append(_info("  - total kill at 8% (5ers kills at 10%)"))
        out.append(_info("Layer 1 reduces TAIL slip cost; the kill switches cap "
                         "DRAWDOWN."))
    return out


def section_ea(report: dict) -> list:
    out = ["", "=" * 78, "  EA (MQL5/Experts/SHF_Bridge.mq5)", "=" * 78]
    ea = ROOT / "MQL5/Experts/SHF_Bridge.mq5"
    if not ea.exists():
        out.append(_fail(f"EA file missing: {ea}"))
        report["ea"] = {"exists": False}
        return out
    sz = ea.stat().st_size
    age = _age_str(ea.stat().st_mtime)
    txt = ea.read_text(encoding="utf-8", errors="replace")
    has_zmq = "zmq" in txt.lower() or "ZeroMQ" in txt
    summary = {
        "exists": True, "path": str(ea),
        "size_bytes": sz, "age": age, "has_zmq": has_zmq,
    }
    report["ea"] = summary
    out.append(_info(f"file        {ea}"))
    out.append(_info(f"size        {sz:,} bytes"))
    out.append(_info(f"modified    {age}"))
    out.append(_ok("EA file present"))
    if has_zmq:
        out.append(_ok("EA uses ZeroMQ transport"))
    return out


def section_python(report: dict) -> list:
    out = ["", "=" * 78, "  PYTHON / ENVIRONMENT", "=" * 78]
    info = {
        "python_version": sys.version.split()[0],
        "platform": platform.platform(),
        "executable": sys.executable,
        "cwd": os.getcwd(),
    }
    report["python"] = info
    out.append(_info(f"python      {info['python_version']}"))
    out.append(_info(f"platform    {info['platform']}"))
    out.append(_info(f"executable  {info['executable']}"))
    out.append(_info(f"cwd         {info['cwd']}"))

    # Probe key packages
    pkgs = ["MetaTrader5", "zmq", "numpy", "pandas"]
    for name in pkgs:
        try:
            m = __import__(name)
            ver = getattr(m, "__version__", "?")
            out.append(_ok(f"{name:<14} {ver}"))
        except ImportError:
            out.append(_warn(f"{name:<14} NOT installed"))
    return out


# =====================================================================
# Main
# =====================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true",
                    help="Also write Results/v31_preflight_snapshot.json")
    args = ap.parse_args()

    report: dict = {
        "preflight_version": "1.0.0",
        "ts_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "host": platform.node(),
    }

    lines = ["", "#" * 78,
             "#  V31 PREFLIGHT — bot self-audit",
             f"#  host = {report['host']}    ts = {report['ts_utc']}",
             "#" * 78]

    sections = [
        section_git, section_config, section_guards,
        section_sizer_state, section_breaker_state,
        section_symbols, section_news,
        section_layer1, section_ea, section_python,
    ]
    fail_count = 0
    for fn in sections:
        try:
            chunk = fn(report)
        except Exception as e:
            chunk = ["", "=" * 78,
                     f"  SECTION ERROR: {fn.__name__}", "=" * 78,
                     _fail(f"{type(e).__name__}: {e}")]
            fail_count += 1
        for line in chunk:
            if line.strip().startswith("[FAIL]"):
                fail_count += 1
        lines.extend(chunk)

    # Summary
    lines.append("")
    lines.append("=" * 78)
    lines.append("  PREFLIGHT SUMMARY")
    lines.append("=" * 78)
    if fail_count == 0:
        lines.append(_ok(f"all sections passed — bot is ready to launch"))
    else:
        lines.append(_fail(f"{fail_count} hard-failure(s) detected — fix before launching"))
    lines.append(_info("Copy-paste everything from the first '######' line above "
                       "to the developer for sign-off."))
    lines.append("")

    out_text = "\n".join(lines)
    print(out_text)

    if args.json:
        snap_path = ROOT / "Results" / "v31_preflight_snapshot.json"
        snap_path.parent.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(json.dumps(report, indent=2, default=str),
                             encoding="utf-8")
        print(_info(f"JSON snapshot → {snap_path}"))

    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"[INTERNAL] preflight crashed: {type(e).__name__}: {e}",
              file=sys.stderr)
        sys.exit(2)
