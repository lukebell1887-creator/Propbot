"""
heartbeat.py — Stage 2 of the v30 perfect plan.

Atomic JSON snapshot of *everything* that matters about live bot state, written
to disk every heartbeat tick (cfg.heartbeat_sec, 30s by default).

Why this exists
---------------
If anything ever goes wrong with the live bot — silent halt, weird sizing,
unexpected DD, broken news feed — we need a forensic record we can reach for
*after the fact*. Logs are great, but logs are line-by-line; a heartbeat is a
single coherent snapshot of the whole world at one moment.

This module writes:
    Results/heartbeat_v30.json

Schema (always present, never breaking changes — only additive):
    {
      "schema_version"     : 1,
      "timestamp_utc"      : "2026-04-28T08:35:12Z",
      "timestamp_unix"     : 1735459112.7,
      "uptime_seconds"     : 8421.3,
      "dry_run"            : false,
      "mode"               : "live",        # "live" | "dry_run"

      "account": {
          "equity"         : 100268.46,
          "balance"        : 100200.00,
          "peak_equity"    : 105820.00,
          "start_equity"   : 100000.00,
          "day_start_eq"   : 100050.00,
          "dd_pct_total"   : 5.25,         # vs all-time peak, %
          "dd_pct_today"   : 0.00,
          "pnl_today"      : 218.46,
          "account_killed" : false,
          "day_halted"     : false
      },

      "sizer": {
          "n_trades_seen"  : 312,
          "alpha"          : 0.20,
          "warmup_trades"  : 15,
          "pool_symbols"   : true,
          "per_key": {
              "_GLOBAL_": {
                  "n"             : 312,
                  "mu_ewma"       : 0.94,
                  "var_ewma"      : 0.81,
                  "sharpe_ewma"   : 1.04
              }
          }
      },

      "breaker": {
          "halt_pct"       : 0.04,
          "peak_equity"    : 105820.00,
          "is_halted"      : false,
          "current_dd_pct" : 5.25
      },

      "positions": {
          "n_open"         : 1,
          "open": [
              {"symbol":"DE40", "side":"LONG", "size_lots":0.40,
               "entry":18420.5, "open_at":"08:14:33Z", "open_R": 0.42}
          ]
      },

      "symbols": {
          "DE40":   {"last_bar_utc":"08:34:00", "or_low":18395.0,"or_high":18441.5},
          "US30":   {"last_bar_utc":"08:34:00", "or_low":null,   "or_high":null},
          ...
      },

      "counters": { "entries":42, "block_concurrent_cap":3, ... }
    }
"""
from __future__ import annotations

import json
import os
import tempfile
import time as _time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

SCHEMA_VERSION = 1


# =====================================================================
#  Atomic write — never leave a half-written heartbeat on disk
# =====================================================================
def atomic_json_write(path: os.PathLike | str, payload: Dict[str, Any]) -> None:
    """Write payload as JSON to `path` atomically.

    Strategy: write to a sibling temp file, fsync, then os.replace().
    On Windows os.replace() is atomic for files on the same volume, which
    is what we get since the temp file is in the same directory as the
    target. A reader that opens the heartbeat file at the wrong moment
    will see either the *old* full JSON or the *new* full JSON — never a
    truncated mix.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    # Write to a unique sibling temp file
    fd, tmp_name = tempfile.mkstemp(prefix=p.name + ".tmp.", dir=str(p.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"),
                      default=str)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass  # not critical, replace() still atomic on same volume
        os.replace(tmp, p)  # atomic on POSIX and Windows (same volume)
    except Exception:
        # Best-effort cleanup of the temp file on failure
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        raise


# =====================================================================
#  Snapshot builder — turns a live v30 bot into the heartbeat dict
# =====================================================================
def build_v30_snapshot(bot, *, started_at_unix: Optional[float] = None) -> Dict[str, Any]:
    """Build the full snapshot dict from a v30 LiveTrader instance.

    Defensive against missing attributes — if a field can't be read, it goes
    to None or 0 rather than raising. Heartbeat must NEVER crash the live loop.
    """
    now = _time.time()
    now_utc = datetime.now(timezone.utc)

    # ---- account block ----
    try:
        eq = float(bot._current_equity())
    except Exception:
        eq = 0.0
    try:
        ai = bot.bridge.get_account_info() if hasattr(bot, "bridge") else None
        balance = float(getattr(ai, "balance", 0.0)) if ai else 0.0
    except Exception:
        balance = 0.0
    peak = float(getattr(bot, "peak_equity", 0.0) or 0.0)
    start_eq = float(getattr(bot, "start_equity", 0.0) or 0.0)
    day_start = float(getattr(bot, "day_start_equity", 0.0) or 0.0)

    try:
        dd_total = float(bot._equity_dd_pct())
    except Exception:
        dd_total = 0.0
    try:
        dd_day = float(bot._day_dd_pct())
    except Exception:
        dd_day = 0.0
    pnl_today = round(eq - day_start, 2) if day_start > 0 else 0.0

    account = {
        "equity":         round(eq, 2),
        "balance":        round(balance, 2),
        "peak_equity":    round(peak, 2),
        "start_equity":   round(start_eq, 2),
        "day_start_eq":   round(day_start, 2),
        "dd_pct_total":   round(dd_total, 3),
        "dd_pct_today":   round(dd_day, 3),
        "pnl_today":      pnl_today,
        "account_killed": bool(getattr(bot, "account_killed", False)),
        "day_halted":     bool(getattr(bot, "day_halted", False)),
    }

    # ---- sizer block ----
    sizer_block: Dict[str, Any] = {"n_trades_seen": 0, "per_key": {}}
    try:
        s = bot.merton_sizer
        cfg = s.cfg
        n_total = sum(s._n_seen.values())
        per_key: Dict[str, Any] = {}
        import math
        for k, n in s._n_seen.items():
            mu = float(s._mu.get(k, 0.0))
            var = float(s._var.get(k, 0.0))
            sharpe = mu / math.sqrt(var) if var > 0 else 0.0
            per_key[k] = {
                "n":           int(n),
                "mu_ewma":     round(mu, 6),
                "var_ewma":    round(var, 6),
                "sharpe_ewma": round(sharpe, 4),
            }
        sizer_block = {
            "n_trades_seen":  int(n_total),
            "alpha":          float(cfg.ewma_alpha),
            "warmup_trades":  int(cfg.warmup_trades),
            "pool_symbols":   bool(cfg.pool_symbols),
            "base_risk_pct":  float(cfg.base_risk_pct),
            "cap_mult":       float(cfg.cap_mult),
            "gamma":          float(cfg.gamma),
            "dd_cap_pct":     float(cfg.dd_cap_pct),
            "per_key":        per_key,
        }
    except Exception as e:
        sizer_block["error"] = f"{type(e).__name__}: {e}"

    # ---- breaker block ----
    breaker_block: Dict[str, Any] = {}
    try:
        b = bot.total_dd_breaker_4pct
        b_peak = float(getattr(b, "peak_equity", 0.0) or 0.0)
        cur_dd = ((b_peak - eq) / b_peak) if b_peak > 0 else 0.0
        breaker_block = {
            "halt_pct":       float(getattr(b, "halt_pct", 0.0)),
            "peak_equity":    round(b_peak, 2),
            "is_halted":      bool(getattr(b, "is_halted", False)),
            "current_dd_pct": round(cur_dd * 100.0, 3),
        }
    except Exception as e:
        breaker_block["error"] = f"{type(e).__name__}: {e}"

    # ---- positions block ----
    open_list = []
    n_open = 0
    try:
        for sym, st in bot.states.items():
            if st.open_ticket is None:
                continue
            n_open += 1
            R_now = 0.0
            try:
                if (st.open_entry and st.last_m1_close and st.open_risk_usd
                        and sym in bot.specs):
                    mv = (st.last_m1_close - st.open_entry) if st.open_side == "LONG" \
                         else (st.open_entry - st.last_m1_close)
                    pip_val = bot.specs[sym].pip_value_per_lot
                    tick_sz = bot.specs[sym].tick_size
                    pnl_now = (mv / tick_sz) * pip_val * (st.open_size_lots or 0)
                    R_now = pnl_now / st.open_risk_usd if st.open_risk_usd else 0.0
            except Exception:
                pass
            open_list.append({
                "symbol":    sym,
                "side":      st.open_side,
                "size_lots": float(st.open_size_lots or 0.0),
                "entry":     float(st.open_entry or 0.0),
                "open_at":   st.open_at.isoformat() if st.open_at else None,
                "open_R":    round(R_now, 3),
            })
    except Exception:
        pass

    # ---- per-symbol last-bar + OR ----
    syms_block: Dict[str, Any] = {}
    try:
        for sym, st in bot.states.items():
            orb = getattr(st, "or_tracker", None)
            syms_block[sym] = {
                "last_bar_utc": (st.last_m1_time.isoformat()
                                 if getattr(st, "last_m1_time", None) else None),
                "last_close":   float(st.last_m1_close) if getattr(st, "last_m1_close", None) else None,
                "or_low":       float(orb.or_low)  if orb and orb.or_low  is not None else None,
                "or_high":      float(orb.or_high) if orb and orb.or_high is not None else None,
            }
    except Exception:
        pass

    # ---- counters ----
    counters = {}
    try:
        counters = {k: int(v) for k, v in bot.counters.items()}
    except Exception:
        pass

    return {
        "schema_version":  SCHEMA_VERSION,
        "timestamp_utc":   now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "timestamp_unix":  round(now, 3),
        "uptime_seconds":  round(now - started_at_unix, 1) if started_at_unix else None,
        "dry_run":         bool(getattr(bot, "dry_run", False)),
        "mode":            "dry_run" if getattr(bot, "dry_run", False) else "live",
        "account":         account,
        "sizer":           sizer_block,
        "breaker":         breaker_block,
        "positions":       {"n_open": n_open, "open": open_list},
        "symbols":         syms_block,
        "counters":        counters,
    }


# =====================================================================
#  Convenience: build + write in one call (the main public entry point)
# =====================================================================
def write_heartbeat(path: os.PathLike | str,
                    bot,
                    *,
                    started_at_unix: Optional[float] = None) -> None:
    """Build a snapshot of `bot` and write it atomically to `path`.

    Designed to be wrapped in a try/except by the caller so that any
    heartbeat failure (disk full, permissions, etc.) NEVER takes down the
    live trading loop.
    """
    payload = build_v30_snapshot(bot, started_at_unix=started_at_unix)
    atomic_json_write(path, payload)
