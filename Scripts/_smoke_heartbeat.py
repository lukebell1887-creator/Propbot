"""
_smoke_heartbeat.py — Stage 2c smoke test.

Two checks:
  1. v30_live.py parses cleanly (no syntax errors from the wiring edits).
  2. heartbeat.write_heartbeat() produces a valid JSON file with all the
     expected top-level keys and an `account.peak_equity` field.

Uses a tiny stub `bot` instead of spinning up MT5 — we just need to verify
the snapshot builder reads the right attributes off the right names.
"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

# Make `import src.*` work when launched from the project root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Reconfigure stdout to UTF-8 so check-marks render on the Windows
# cp1252 cmd shell. Falls back gracefully on platforms where the
# stream doesn't expose `reconfigure`.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

# 1) Parse-check v30_live.py — catches any wiring typos before VPS deploy.
print("=" * 72)
print(" Stage 2c smoke — heartbeat module + v30_live parse check")
print("=" * 72)
try:
    import src.live.v30_live as v30_live   # noqa: F401  (import is the test)
    print("[1/2] v30_live.py imports cleanly  ✓")
except Exception as e:
    print(f"[1/2] v30_live.py import FAILED: {type(e).__name__}: {e}")
    sys.exit(1)

# 2) Stub bot + write a heartbeat
from src.dynamic_sizer_v21 import MertonGZSizer, MertonGZSizerConfig
from src.dd_breaker import DDBreaker
from src.live.heartbeat import write_heartbeat


class _StubBot:
    """Bare-minimum surface area for build_v30_snapshot()."""
    def __init__(self):
        self.dry_run = True
        self.peak_equity = 105_000.0
        self.start_equity = 100_000.0
        self.day_start_equity = 102_000.0
        self.account_killed = False
        self.day_halted = False
        self.merton_sizer = MertonGZSizer(MertonGZSizerConfig(
            base_risk_pct=0.00170, cap_mult=5.0, gamma=3.0,
            ewma_alpha=0.20, warmup_trades=15, dd_cap_pct=0.04,
            pool_symbols=True, no_edge_multiplier=1.0,
        ))
        # Feed it a few trades so per_key has content
        for r in [0.5, -0.3, 1.2, 0.8, -0.4, 1.5]:
            self.merton_sizer.on_trade_closed("DE40", r)
        self.total_dd_breaker_4pct = DDBreaker(halt_pct=0.08)
        for ts, eq in [(time.time() - 3600, 100_000),
                       (time.time() - 1800, 105_000),
                       (time.time() - 600,  103_500)]:
            self.total_dd_breaker_4pct.check(ts, eq)
        self.states = {}      # no open positions in stub
        self.specs = {}
        self.counters = defaultdict(int)
        self.counters["entries"] = 7
        self.counters["block_concurrent_cap"] = 2

    def _current_equity(self):  return 103_500.0
    def _equity_dd_pct(self):   return 1.43
    def _day_dd_pct(self):      return 0.0


bot = _StubBot()
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / "heartbeat_v30.json"
    write_heartbeat(p, bot, started_at_unix=time.time() - 7200)
    assert p.exists(), "heartbeat file not created"
    payload = json.loads(p.read_text(encoding="utf-8"))

    # Verify the schema — all top-level keys must be present
    required = {"schema_version", "timestamp_utc", "timestamp_unix",
                "uptime_seconds", "dry_run", "mode", "account", "sizer",
                "breaker", "positions", "symbols", "counters"}
    missing = required - set(payload.keys())
    assert not missing, f"missing top-level keys: {missing}"

    # Spot-check a few critical numbers
    assert payload["schema_version"] == 1
    assert payload["dry_run"] is True
    assert payload["mode"] == "dry_run"
    assert payload["account"]["peak_equity"] == 105_000.0, \
        f"peak_equity wrong: {payload['account']['peak_equity']}"
    assert payload["account"]["equity"] == 103_500.0
    assert payload["sizer"]["n_trades_seen"] == 6, \
        f"n_trades_seen wrong: {payload['sizer']['n_trades_seen']}"
    assert "_GLOBAL_" in payload["sizer"]["per_key"], \
        f"sizer per_key missing _GLOBAL_: {payload['sizer']['per_key']}"
    assert payload["breaker"]["peak_equity"] == 105_000.0
    assert payload["breaker"]["is_halted"] is False
    assert payload["counters"]["entries"] == 7

    print("[2/2] heartbeat.write_heartbeat() produces valid schema  ✓")
    print()
    print("Sample payload (truncated):")
    print(f"  schema_version  = {payload['schema_version']}")
    print(f"  timestamp_utc   = {payload['timestamp_utc']}")
    print(f"  uptime_seconds  = {payload['uptime_seconds']}")
    print(f"  account.equity  = ${payload['account']['equity']:,.2f}")
    print(f"  account.peak    = ${payload['account']['peak_equity']:,.2f}")
    print(f"  account.dd_pct  = {payload['account']['dd_pct_total']:.2f}%")
    print(f"  sizer.n_trades  = {payload['sizer']['n_trades_seen']}")
    print(f"  sizer mu (G)    = {payload['sizer']['per_key']['_GLOBAL_']['mu_ewma']:+.4f}")
    print(f"  sizer var (G)   = {payload['sizer']['per_key']['_GLOBAL_']['var_ewma']:.4f}")
    print(f"  breaker.peak    = ${payload['breaker']['peak_equity']:,.2f}")
    print(f"  breaker.dd      = {payload['breaker']['current_dd_pct']:.2f}%")
    print()
    print("=" * 72)
    print(" ALL SMOKE CHECKS PASSED ✓")
    print("=" * 72)
