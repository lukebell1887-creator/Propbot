"""
test_persistence_v30.py — Stage 3 of the v30 perfect plan.

Five unit tests covering the persistence + seeding layer added in Stage 1:

    1. Round-trip                  : save → fresh sizer → load → state identical
    2. Seed-replay equivalence     : seed_from_trades([R..]) ≡ on_trade_closed loop
    3. Corrupt-file recovery       : garbled JSON → load returns False, no crash
    4. Stale-file rejection        : state file mtime >14 days → treated as missing
    5. Live → restart → live cont. : split run vs all-in-one → state identical to 1e-9

Plus mirror tests for DDBreaker (round-trip + peak preservation across restart).

Run with:    pytest tests/test_persistence_v30.py -v
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from src.dynamic_sizer_v21 import MertonGZSizer, MertonGZSizerConfig
from src.dd_breaker import DDBreaker


# =====================================================================
#  Fixtures
# =====================================================================
def _make_sizer(pool: bool = True) -> MertonGZSizer:
    """Match v30 ship-config: pool_symbols=True, alpha=0.20, warmup=15."""
    return MertonGZSizer(MertonGZSizerConfig(
        base_risk_pct=0.00170,
        cap_mult=5.0,
        gamma=3.0,
        ewma_alpha=0.20,
        warmup_trades=15,
        dd_cap_pct=0.04,
        pool_symbols=pool,
        no_edge_multiplier=1.0,
    ))


def _seed_trades(n: int = 30, seed: int = 42):
    """Deterministic synthetic R-values mimicking ORB outcomes
    (mostly small wins, occasional 2R, occasional -1R)."""
    import random
    rng = random.Random(seed)
    syms = ["DE40", "US30", "XAUUSD", "US500"]
    out = []
    for _ in range(n):
        sym = rng.choice(syms)
        # bimodal: 65% win at +0.8R..+2.5R, 35% loss at -1.0R..-0.3R
        R = rng.uniform(0.8, 2.5) if rng.random() < 0.65 else -rng.uniform(0.3, 1.0)
        out.append({"symbol": sym, "realised_R": R})
    return out


# =====================================================================
#  TEST 1 — Round-trip: save → load → state identical
# =====================================================================
def test_round_trip_save_load(tmp_path: Path):
    s1 = _make_sizer()
    for t in _seed_trades(40, seed=1):
        s1.on_trade_closed(t["symbol"], t["realised_R"])

    state_path = tmp_path / "sizer_state.json"
    s1.save_state(state_path)
    assert state_path.exists()

    s2 = _make_sizer()
    ok, msg = s2.load_state(state_path, max_age_seconds=14 * 86400)
    assert ok is True, f"load failed: {msg}"

    # The pool-key μ̂/σ̂² and n_seen must match exactly (same JSON, same floats)
    assert dict(s1._mu) == dict(s2._mu)
    assert dict(s1._var) == dict(s2._var)
    assert dict(s1._n_seen) == dict(s2._n_seen)
    assert sum(s2._n_seen.values()) == 40


# =====================================================================
#  TEST 2 — Seed-replay equivalence
# =====================================================================
#  seed_from_trades([R1..R200]) must produce the exact same μ̂/σ̂² as
#  running on_trade_closed() 200 times in the same order.  The whole point of
#  the seed-from-backtest step is that "replayed history" is mathematically
#  indistinguishable from "real live history".
# =====================================================================
def test_seed_replay_equivalence():
    trades = _seed_trades(200, seed=2)

    # Path A: live-style — call on_trade_closed for each trade
    live = _make_sizer()
    for t in trades:
        live.on_trade_closed(t["symbol"], t["realised_R"])

    # Path B: seed-style — replay via seed_from_trades
    seeded = _make_sizer()
    n_replayed = seeded.seed_from_trades(trades)
    assert n_replayed == 200

    # μ̂ / σ̂² / n_seen should be byte-identical (same float ops, same order)
    assert dict(live._mu) == dict(seeded._mu)
    assert dict(live._var) == dict(seeded._var)
    assert dict(live._n_seen) == dict(seeded._n_seen)


# =====================================================================
#  TEST 3 — Corrupt-file recovery
# =====================================================================
#  Garbled JSON must NOT crash the live runner. load_state returns
#  (False, reason) and the sizer's state is left untouched.
# =====================================================================
def test_corrupt_file_recovery(tmp_path: Path):
    bad = tmp_path / "garbled.json"
    bad.write_text("{ this is not valid JSON !!! ", encoding="utf-8")

    s = _make_sizer()
    # Pre-populate so we can assert state wasn't wiped on failed load
    s.on_trade_closed("DE40", 1.5)
    n_before = sum(s._n_seen.values())
    mu_before = dict(s._mu)

    ok, reason = s.load_state(bad)
    assert ok is False
    assert isinstance(reason, str) and len(reason) > 0

    # State must be intact — a corrupt file cannot silently zero our learning
    assert sum(s._n_seen.values()) == n_before
    assert dict(s._mu) == mu_before


# =====================================================================
#  TEST 4 — Stale-file rejection
# =====================================================================
#  A state file older than max_age_seconds must be rejected so the bot
#  falls back to the (fresher) backtest seed instead of resuming on a
#  view of the world that's weeks out of date.
# =====================================================================
def test_stale_file_rejected(tmp_path: Path):
    s1 = _make_sizer()
    for t in _seed_trades(20, seed=3):
        s1.on_trade_closed(t["symbol"], t["realised_R"])
    p = tmp_path / "stale.json"
    s1.save_state(p)

    # Staleness is measured by the embedded `saved_at_unix` field in the JSON
    # itself (more reliable than file mtime, which can be touched by syncs/
    # backups). Backdate that field by 30 days, well past the 14-day cutoff.
    payload = json.loads(p.read_text(encoding="utf-8"))
    payload["saved_at_unix"] = time.time() - 30 * 86400
    p.write_text(json.dumps(payload), encoding="utf-8")
    # Also push file mtime back, belt-and-braces
    old = time.time() - 30 * 86400
    os.utime(p, (old, old))

    s2 = _make_sizer()
    ok, reason = s2.load_state(p, max_age_seconds=14 * 86400)
    assert ok is False, f"stale file should have been rejected (got reason: {reason!r})"
    assert any(k in reason.lower() for k in ("stale", "old", "age", "expired", "days")), \
        f"reason should mention stale/old/age/expired/days, got: {reason}"
    # Sizer remains in cold-start state
    assert sum(s2._n_seen.values()) == 0


# =====================================================================
#  TEST 5 — Live → restart → live continuity (the BIG one)
# =====================================================================
#  This is the test that proves persistence actually works end-to-end:
#
#       Reference run : 30 trades all in one session
#       Split run     : 15 trades → save → kill instance → new instance →
#                       load → 15 more trades
#
#  Final μ̂/σ̂² must match within float precision (1e-9). This is what
#  guarantees that a VPS restart is mathematically a no-op for sizing.
# =====================================================================
def test_live_restart_live_continuity(tmp_path: Path):
    trades = _seed_trades(30, seed=4)
    first_half, second_half = trades[:15], trades[15:]

    # --- Reference: one continuous session ---
    ref = _make_sizer()
    for t in trades:
        ref.on_trade_closed(t["symbol"], t["realised_R"])

    # --- Split: 15 → save → kill → load → 15 ---
    a = _make_sizer()
    for t in first_half:
        a.on_trade_closed(t["symbol"], t["realised_R"])
    p = tmp_path / "mid.json"
    a.save_state(p)
    del a   # simulate process death

    b = _make_sizer()
    ok, _ = b.load_state(p, max_age_seconds=14 * 86400)
    assert ok is True
    for t in second_half:
        b.on_trade_closed(t["symbol"], t["realised_R"])

    # μ̂, σ̂², n_seen must all match within 1e-9 across every key
    assert set(ref._mu.keys()) == set(b._mu.keys())
    for k in ref._mu:
        assert abs(ref._mu[k] - b._mu[k]) < 1e-9, \
            f"mu drift on key {k}: ref={ref._mu[k]} vs split={b._mu[k]}"
        assert abs(ref._var[k] - b._var[k]) < 1e-9, \
            f"var drift on key {k}: ref={ref._var[k]} vs split={b._var[k]}"
        assert ref._n_seen[k] == b._n_seen[k]


# =====================================================================
#  TEST 6 — DDBreaker round-trip + peak preservation
# =====================================================================
#  CRITICAL: the breaker's peak_equity must survive restarts. If a
#  restart ever resets peak to the (lower) current equity, the 8 % DD
#  measurement silently goes back to 0 % and the safeguard is gone.
# =====================================================================
def test_ddbreaker_peak_preserved_across_restart(tmp_path: Path):
    b1 = DDBreaker(halt_pct=0.08)
    # Simulate equity curve climbing then dropping
    t0 = time.time()
    for i, eq in enumerate([100_000, 102_000, 105_000, 103_500, 102_800]):
        b1.check(t0 + i * 60, eq)
    assert b1.peak_equity == 105_000

    p = tmp_path / "breaker.json"
    b1.save_state(p)

    b2 = DDBreaker(halt_pct=0.08)
    ok, msg = b2.load_state(p)
    assert ok, f"breaker load failed: {msg}"
    assert b2.peak_equity == 105_000, "peak must be preserved across restart"

    # Now drop equity to 99,000 — DD vs preserved peak = (105000-99000)/105000 = 5.71%
    halted, dd = b2.check(t0 + 999 * 60, 99_000)
    assert dd > 0.05  # should see real DD relative to preserved peak
    # 5.71% < 8% so we're not halted yet, but the math is using the right peak
    assert not halted

    # Drop further to trip the breaker (DD ≥ 8% of 105_000 → equity ≤ 96_600)
    halted, dd = b2.check(t0 + 1000 * 60, 96_000)
    assert halted is True
    assert dd >= 0.08


# =====================================================================
#  TEST 7 — DDBreaker corrupt-file does not wipe peak
# =====================================================================
def test_ddbreaker_corrupt_file_safe(tmp_path: Path):
    b = DDBreaker(halt_pct=0.08)
    b.check(time.time(), 100_000)
    b.check(time.time() + 60, 110_000)
    assert b.peak_equity == 110_000

    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    ok, _ = b.load_state(bad)
    assert ok is False
    assert b.peak_equity == 110_000  # unchanged


if __name__ == "__main__":
    # Allow `python tests/test_persistence_v30.py` for quick smoke
    pytest.main([__file__, "-v"])
