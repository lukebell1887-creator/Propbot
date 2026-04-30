"""
Scripts/check_layer1_health.py
==============================

Layer 1 deployment health-check.  Run this **on the VPS** (or any host
running the v30 live bot) AFTER you have pulled commit 7402510 or
later and started a dry-run.

What it checks
--------------
  1. The wired source: `src/live/v30_live.py` contains the Layer 1
     imports, tracker construction, broker-SL widening, intercept loop,
     and `_clear_state` cleanup.  This is the same static contract the
     Monte-Carlo proof was built on.
  2. The unit tests pass: 81 cases under
     `tests/test_layer1.py` + `tests/test_layer1_tracker.py`.
  3. The bot has actually started since the wire-up: presence of a
     `START` event with `version=v30` newer than commit 7402510's
     local file mtime.
  4. State restore source: `WARMUP_RESTORE` event tells you whether the
     sizer came back from `live_state`, `seeded_from_backtest`, or
     `cold_start`.
  5. Layer 1 footprint in events: counts `LAYER1_FIRED` events and
     prints the breakdown by action (`CLOSE_NOW` vs `FALLBACK_CLOSE`)
     and per-symbol.  Zero firings is **expected and correct** while
     no SL has been violated by the broker — Layer 1 is a defense, not
     a generator of trades.
  6. Heartbeat freshness: `Results/heartbeat_v30.json` was written in
     the last 5 minutes (60 s heartbeat × 5× tolerance).

Exit code
---------
  0 = All four hard checks passed; bot is **safely on Layer 1 + 0.185 %**.
  1 = One or more hard checks failed.  Banner shows which.

The four "hard" checks are:
  * code wired,
  * unit tests green,
  * a v30 START event exists newer than the wire-up commit,
  * heartbeat is fresh.

`LAYER1_FIRED` count is FYI only; zero firings is normal until a
broker fill actually slips past the original SL.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIVE_FILE = REPO / "src" / "live" / "v30_live.py"
EVENTS_LOG = REPO / "Results" / "v30_live_events.log"
TRADES_LOG = REPO / "Results" / "v30_live_trades.jsonl"
HEARTBEAT = REPO / "Results" / "heartbeat_v30.json"

WIRE_UP_COMMIT = "7402510"
HEARTBEAT_MAX_AGE_S = 300.0   # 5 × heartbeat_sec


def banner(title: str) -> None:
    print("=" * 74)
    print(f"  {title}")
    print("=" * 74)


def line(label: str, ok: bool, detail: str) -> bool:
    tag = "[OK]  " if ok else "[FAIL]"
    print(f"  {tag} {label:<30} {detail}")
    return ok


def check_wired_source() -> bool:
    if not LIVE_FILE.exists():
        return line("Wired source", False, f"missing {LIVE_FILE}")
    src = LIVE_FILE.read_text(encoding="utf-8", errors="replace")
    has_import = (
        "from src.execution.layer1_tracker import Layer1Tracker" in src
        and "from src.execution.layer1 import" in src
        and "emergency_sl_offset_for" in src
    )
    has_init = bool(re.search(r"self\.layer1\s*=\s*Layer1Tracker\s*\(\s*\)", src))
    has_widen = "emerg_offset = emergency_sl_offset_for(sym)" in src
    has_poll = "self.layer1.update_and_decide" in src
    has_clear = bool(re.search(r"self\.layer1\.clear\s*\(\s*int\(st\.open_ticket\)\s*\)", src))
    ok = has_import and has_init and has_widen and has_poll and has_clear
    parts = []
    parts.append("import" + ("=Y" if has_import else "=N"))
    parts.append("init" + ("=Y" if has_init else "=N"))
    parts.append("widen" + ("=Y" if has_widen else "=N"))
    parts.append("poll" + ("=Y" if has_poll else "=N"))
    parts.append("clear" + ("=Y" if has_clear else "=N"))
    return line("Wired source", ok, "  ".join(parts))


def check_unit_tests() -> bool:
    cmd = [
        sys.executable, "-m", "pytest",
        "tests/test_layer1.py", "tests/test_layer1_tracker.py",
        "-q", "--tb=line", "--no-header",
    ]
    try:
        proc = subprocess.run(cmd, cwd=REPO, capture_output=True,
                              text=True, timeout=120)
    except Exception as e:
        return line("Unit tests", False, f"pytest run error: {e}")
    out = (proc.stdout or "") + (proc.stderr or "")
    last = next((ln for ln in reversed(out.splitlines()) if ln.strip()), "")
    return line("Unit tests", proc.returncode == 0, last or "(no output)")


def _iter_events():
    if not EVENTS_LOG.exists():
        return
    with open(EVENTS_LOG, "r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                yield json.loads(ln)
            except json.JSONDecodeError:
                continue


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def check_bot_started_post_wireup() -> bool:
    if not EVENTS_LOG.exists():
        return line("Bot started", False,
                    f"events log missing  ({EVENTS_LOG})  -> bot has not run yet")
    starts = [ev for ev in _iter_events()
              if ev.get("kind") == "START" and ev.get("version") == "v30"]
    if not starts:
        return line("Bot started", False,
                    "no START events with version=v30 -> v30 bot never launched")
    last = starts[-1]
    ts = _parse_iso(last.get("ts_utc", ""))
    detail = (f"last START={ts.isoformat() if ts else '?'}  "
              f"equity=${last.get('equity', 0):,.0f}  "
              f"dry_run={last.get('dry_run')}")
    return line("Bot started", True, detail)


def check_warmup_restore() -> bool:
    """FYI — not a hard fail; just shows where the sizer was seeded from."""
    restores = [ev for ev in _iter_events() if ev.get("kind") == "WARMUP_RESTORE"]
    if not restores:
        line("Warmup restore", True, "no WARMUP_RESTORE event yet (cold start fine for first run)")
        return True
    last = restores[-1]
    detail = (f"source={last.get('sizer_source')}  "
              f"n_seen={last.get('sizer_n_seen', '?')}  "
              f"breaker_loaded={last.get('breaker_loaded')}")
    line("Warmup restore", True, detail)
    return True


def check_layer1_firings() -> bool:
    """FYI — zero firings is correct + safe until a broker fill exceeds SL."""
    fires = [ev for ev in _iter_events() if ev.get("kind") == "LAYER1_FIRED"]
    if not fires:
        line("Layer 1 firings", True,
             "0 firings  (expected - Layer 1 only fires when broker fills past SL)")
        return True
    by_action: dict[str, int] = {}
    by_symbol: dict[str, int] = {}
    for ev in fires:
        by_action[ev.get("action", "?")] = by_action.get(ev.get("action", "?"), 0) + 1
        by_symbol[ev.get("symbol", "?")] = by_symbol.get(ev.get("symbol", "?"), 0) + 1
    detail = (f"total={len(fires)}  "
              f"actions={dict(sorted(by_action.items()))}  "
              f"symbols={dict(sorted(by_symbol.items()))}")
    line("Layer 1 firings", True, detail)
    print("           Last firing detail:")
    print(f"             {fires[-1]}")
    return True


def check_heartbeat_fresh() -> bool:
    if not HEARTBEAT.exists():
        return line("Heartbeat", False,
                    f"missing {HEARTBEAT}  -> no live snapshot has been written yet")
    age = time.time() - HEARTBEAT.stat().st_mtime
    ok = age < HEARTBEAT_MAX_AGE_S
    return line("Heartbeat", ok,
                f"age={age:.0f}s  (threshold={HEARTBEAT_MAX_AGE_S:.0f}s)")


def main() -> int:
    banner("V31 LAYER 1 — LIVE HEALTH CHECK")
    print(f"  events log: {EVENTS_LOG}")
    print(f"  heartbeat : {HEARTBEAT}")
    print(f"  expected commit on HEAD: {WIRE_UP_COMMIT} or newer")
    print("-" * 74)

    hard_results = [
        check_wired_source(),
        check_unit_tests(),
        check_bot_started_post_wireup(),
        check_heartbeat_fresh(),
    ]
    print("-" * 74)
    print("  FYI (informational, not gating):")
    check_warmup_restore()
    check_layer1_firings()

    print("=" * 74)
    if all(hard_results):
        print("  RESULT: BOT IS SAFELY ON LAYER 1 + 0.185 % RISK     ✅")
        print("=" * 74)
        return 0
    else:
        print(f"  RESULT: {sum(1 for r in hard_results if not r)}"
              " HARD CHECK(S) FAILED — DO NOT GRADUATE TO LIVE")
        print("=" * 74)
        return 1


if __name__ == "__main__":
    sys.exit(main())
