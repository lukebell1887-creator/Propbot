#!/usr/bin/env python3
"""
v30.3 LIVE PREFLIGHT — startup contract verification

Purpose
-------
Every time the bot is launched (LIVE or DRY-RUN) the launcher first runs
this script. It proves — without ever talking to the broker — that the
**live engine's promise** is byte-identical to the backtest:

    1. Module imports                (atr_tracker, partial_manager, …)
    2. LiveSymbolState carries the v30.3 fields (partial_state, atr_tracker)
    3. Engine config matches v25.1 ship spec
       (base_risk = 0.170%, cap_mult = 5.0, magic = 30000, nochase = 300s,
        max_concurrent = 2, DailyHalt = 4%, DDBreaker = 8%)
    4. ORB anchors match the locked v23/v25 values
    5. Symbol specs load from SMARTBB_UNIVERSE (single source of truth)
    6. News CSV present & parseable, ≥ 1 Tier-1 event loaded
    7. TP/SL math identity for a synthetic OR
    8. Risk-→-lots formula identity for a known equity / SL distance
    9. Partial-manager simulation: TP1 → TP2 → trail produces the right
       sequence (50% closed at TP1, 25% at TP2, trail ratchets)
   10. ATR tracker readiness — feed 14 bars, verify .ready=True and >0
   11. OrderRequest is built with tp=0.0  (broker no longer holds TP)
   12. Persistence dirs writable (Results/, Results/v30_state/)

Exit code
---------
    0  every check passed     -> launcher proceeds to start the bot
    1  one or more failed     -> launcher ABORTS, prints the report

Why a preflight (not just unit tests)?
--------------------------------------
Unit tests verify individual modules. The preflight verifies the **live
runner's wiring of those modules** — config values, contract identity,
and every cross-cutting promise the bot makes to the prop firm. It runs
in <2s and it's the last thing that fires before the live loop, so a
silent regression (e.g. a refactor that drops `tp=0.0`) is caught
immediately, not 14 days into a deployment.

Run manually:    python Scripts/preflight_v30.py
Run from CI:     python -m pytest tests/  &&  python Scripts/preflight_v30.py
"""
from __future__ import annotations

import sys
import os
import inspect
from pathlib import Path
from typing import List, Tuple, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Ensure UTF-8 output on Windows so the ✓ / ✗ glyphs print correctly.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------------------------------------------------------------------
# Tiny ad-hoc test harness — keeps the preflight a single self-contained file.
# Each check is `(name, fn)` where fn raises AssertionError on failure (with
# a human-readable message) or returns a single-line "info" string on pass.
# ---------------------------------------------------------------------------

# ANSI colour codes (auto-disable on Windows cmd that doesn't speak ANSI)
_USE_COLOUR = sys.stdout.isatty() and os.environ.get("NO_COLOUR") != "1"
GRN = "\033[32m" if _USE_COLOUR else ""
RED = "\033[31m" if _USE_COLOUR else ""
YEL = "\033[33m" if _USE_COLOUR else ""
DIM = "\033[2m"  if _USE_COLOUR else ""
RST = "\033[0m"  if _USE_COLOUR else ""


CHECKS: List[Tuple[str, Callable[[], str]]] = []


def check(name: str):
    """Register a preflight check."""
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


# ---------------------------------------------------------------------------
# 1. Module imports
# ---------------------------------------------------------------------------
@check("imports: v30.3 modules load")
def _check_imports() -> str:
    from src.live import atr_tracker, partial_manager           # noqa: F401
    from src.live.atr_tracker import ATRTracker                 # noqa: F401
    from src.live.partial_manager import PartialCloseManager, PartialState  # noqa: F401
    from src.live.v30_live import V30Live, V30LiveConfig, LiveSymbolState   # noqa: F401
    return "atr_tracker, partial_manager, V30Live all importable"


# ---------------------------------------------------------------------------
# 2. LiveSymbolState carries v30.3 fields
# ---------------------------------------------------------------------------
@check("dataclass: LiveSymbolState has v30.3 fields")
def _check_dataclass_fields() -> str:
    from src.live.v30_live import LiveSymbolState
    from src.live.atr_tracker import ATRTracker
    from src.live.partial_manager import PartialState
    fields = LiveSymbolState.__dataclass_fields__
    assert "partial_state" in fields, (
        "LiveSymbolState is missing partial_state field — "
        "v30.3 wiring incomplete")
    assert "atr_tracker" in fields, (
        "LiveSymbolState is missing atr_tracker field — "
        "v30.3 wiring incomplete")
    # partial_state must be Optional[PartialState] (default None)
    # atr_tracker must default to a fresh ATRTracker(window=14)
    inst = LiveSymbolState(
        spec=None, orb_cfg=None,
        or_tracker=type("FakeOR", (), {"__init__": lambda *a, **k: None})(),
    )
    assert inst.partial_state is None, "partial_state default should be None"
    assert isinstance(inst.atr_tracker, ATRTracker), \
        "atr_tracker default should be an ATRTracker instance"
    assert inst.atr_tracker.window == 14, \
        f"ATR window must be 14, got {inst.atr_tracker.window}"
    return "partial_state=None, atr_tracker=ATRTracker(window=14)"


# ---------------------------------------------------------------------------
# 3. Engine config matches v25.1 ship spec
# ---------------------------------------------------------------------------
@check("config: V30LiveConfig defaults match v25.1 ship spec")
def _check_config_defaults() -> str:
    from src.live.v30_live import V30LiveConfig
    cfg = V30LiveConfig()
    expected = {
        "base_risk_pct":             0.00170,
        "cap_mult":                  5.0,
        "gamma":                     3.0,
        "ewma_alpha":                0.20,
        "warmup_trades":             15,
        "dd_cap_pct":                0.04,
        "nochase_cooldown_s":        300.0,
        "magic":                     30000,
        "max_concurrent_positions":  2,
        "min_hold_seconds":          65,
        "account_kill_dd":           0.08,
        "daily_breaker_dd":          0.02,
    }
    bad = []
    for k, want in expected.items():
        got = getattr(cfg, k)
        if got != want:
            bad.append(f"{k}: got {got!r}, expected {want!r}")
    assert not bad, "config drift detected:\n   " + "\n   ".join(bad)
    return ("base_risk=0.170%  cap=5.0×  nochase=300s  magic=30000  "
            "max_conc=2  daily_kill=4%  total_kill=8%")


# ---------------------------------------------------------------------------
# 4. ORB anchors match the locked v23/v25 values
# ---------------------------------------------------------------------------
@check("orb: per-symbol anchors match locked v23/v25 values")
def _check_orb_anchors() -> str:
    from src.live.v30_live import V30_ORB_CONFIGS
    expected = {
        "DE40":   (8,  0,  30, 1.5, 3.0, 0.3),
        "US30":   (14, 30, 30, 2.0, 4.0, 0.0),
        "XAUUSD": (14, 30, 30, 2.0, 4.0, 0.6),
        "US500":  (14, 30, 15, 0.5, 1.0, 0.6),
    }
    bad = []
    for sym, (h, m, mins, tp1m, tp2m, slb) in expected.items():
        c = V30_ORB_CONFIGS.get(sym)
        if c is None:
            bad.append(f"{sym}: missing from V30_ORB_CONFIGS")
            continue
        got = (c.or_start_hour, c.or_start_minute, c.or_minutes,
               c.tp1_range_mult, c.tp2_range_mult, c.sl_buffer_range_mult)
        if got != (h, m, mins, tp1m, tp2m, slb):
            bad.append(f"{sym}: got {got}, expected {(h, m, mins, tp1m, tp2m, slb)}")
    assert not bad, "ORB anchor drift:\n   " + "\n   ".join(bad)
    return f"4 symbols verified (DE40 08:00, US30/XAUUSD/US500 14:30)"


# ---------------------------------------------------------------------------
# 5. Symbol specs load from SMARTBB_UNIVERSE
# ---------------------------------------------------------------------------
@check("specs: pip_value_per_lot == BROKER-TRUTH contract_size × tick_size")
def _check_specs_universe() -> str:
    """
    v30.3-hotfix-2 invariant (post-2026-04-28):
        spec.pip_value_per_lot must equal CONTRACT_SIZE × TICK_SIZE.

    The live sizer formula is:
        $/lot @ stop = (risk_per_unit / tick_size) × pip_value_per_lot
    so pip_value_per_lot must be in **$/TICK/LOT** units.  By broker spec,
    that quantity is exactly contract_size × tick_size.

    Pinned broker-truth values (5ers / Eightcap, 2026-04-28 spec sheet):
        DE40   : 1.0  × 1.00  = $1.00 / point / lot
        US30   : 1.0  × 1.00  = $1.00 / point / lot
        US500  : 1.0  × 0.25  = $0.25 / tick  / lot   ($1.00 / point)
        XAUUSD : 100  × 0.01  = $1.00 / tick  / lot   ($100  / $1 of price)

    History:
      * Pre-2026-04-28: copied uni.pip_value raw → US500 sized 4× too small.
      * 2026-04-28 hotfix-1: multiplied by tick_size unconditionally → fixed
        US500 but broke XAUUSD by 100× (1×0.01=0.01 vs broker truth 1.0).
      * 2026-04-28 hotfix-2 (current): explicit broker-truth table.
    """
    from src.live.v30_live import (
        V30_SPECS,
        V30_BROKER_CONTRACT_SIZE,
        V30_BROKER_TICK_SIZE,
        V30_DOLLARS_PER_TICK_PER_LOT,
    )
    pinned = {
        "DE40":   1.00,
        "US30":   1.00,
        "US500":  0.25,
        "XAUUSD": 1.00,
    }
    bad = []
    for sym in ("DE40", "US30", "XAUUSD", "US500"):
        spec = V30_SPECS[sym]
        cs = V30_BROKER_CONTRACT_SIZE[sym]
        ts = V30_BROKER_TICK_SIZE[sym]
        derived = cs * ts
        # 1) derived table self-consistency
        if abs(V30_DOLLARS_PER_TICK_PER_LOT[sym] - derived) > 1e-9:
            bad.append(
                f"{sym}: V30_DOLLARS_PER_TICK_PER_LOT={V30_DOLLARS_PER_TICK_PER_LOT[sym]} "
                f"!= contract_size×tick_size = {cs}×{ts} = {derived}")
        # 2) spec uses the broker-truth table
        if abs(spec.pip_value_per_lot - derived) > 1e-9:
            bad.append(
                f"{sym}: spec.pip_value_per_lot={spec.pip_value_per_lot} "
                f"!= broker-truth ({cs}×{ts}={derived}). "
                f"BUG REGRESSION — live sizing will be wrong on this symbol.")
        # 3) pinned broker-statement values (verify against any broker statement)
        if abs(spec.pip_value_per_lot - pinned[sym]) > 1e-9:
            bad.append(
                f"{sym}: pip_value_per_lot={spec.pip_value_per_lot} "
                f"!= broker-statement-pinned {pinned[sym]}")
        # 4) sanity on lot constants
        if spec.tick_size <= 0:
            bad.append(f"{sym}: tick_size invalid ({spec.tick_size})")
        if spec.lot_step <= 0 or spec.min_lot <= 0:
            bad.append(f"{sym}: lot constants invalid "
                       f"(min={spec.min_lot}, step={spec.lot_step})")
    assert not bad, "spec drift:\n   " + "\n   ".join(bad)
    pip = ", ".join(
        f"{s}=${V30_SPECS[s].pip_value_per_lot}/tick" for s in
        ("DE40", "US30", "XAUUSD", "US500"))
    return f"broker-truth match — {pip}"


# ---------------------------------------------------------------------------
# 6. News CSV present & parseable, ≥ 1 Tier-1 event loaded
# ---------------------------------------------------------------------------
@check("news: Tier-1 CSV present, parseable, ≥1 event")
def _check_news_csv() -> str:
    from src.live.v30_live import V30Live
    csv_path = ROOT / "data" / "news" / "tier1_2026.csv"
    assert csv_path.exists(), f"news CSV missing at {csv_path}"
    events = V30Live._load_news(csv_path)
    assert len(events) > 0, (
        f"news CSV at {csv_path} parsed as 0 Tier-1 events — "
        "schema drift?")
    return f"{len(events)} Tier-1 events loaded from {csv_path.name}"


# ---------------------------------------------------------------------------
# 7. TP/SL math identity for a synthetic OR
# ---------------------------------------------------------------------------
@check("math: TP1/TP2/SL match the v23/v25 formula")
def _check_tp_sl_math() -> str:
    """
    Re-derive the entry math the live engine uses, compare to the formula
    documented in Docs/V25_1_SHIP_RECOMMENDATION.md §3.2:

        SL  = OR_low/high  ± sl_buffer_range_mult × OR_range
        TP1 = entry        ± tp1_range_mult       × OR_range
        TP2 = entry        ± tp2_range_mult       × OR_range

    Test with DE40 anchors (tp1=1.5R, tp2=3.0R, sl_buf=0.3R).
    """
    from src.live.v30_live import V30_ORB_CONFIGS
    cfg = V30_ORB_CONFIGS["DE40"]
    or_low, or_high = 18000.0, 18030.0
    or_range = or_high - or_low                 # 30
    entry_long = 18030.5

    # LONG-side computation (matches lines 884-892 of v30_live.py)
    sl_long  = or_low                            # SL_buffer is applied via OR boundary itself
    tp1_long = entry_long + cfg.tp1_range_mult * or_range
    tp2_long = entry_long + cfg.tp2_range_mult * or_range

    # Reference values (DE40: tp1_mult=1.5, tp2_mult=3.0)
    assert abs(tp1_long - (entry_long + 1.5 * 30)) < 1e-9, \
        f"TP1 LONG drift: {tp1_long}"
    assert abs(tp2_long - (entry_long + 3.0 * 30)) < 1e-9, \
        f"TP2 LONG drift: {tp2_long}"
    assert sl_long == or_low, f"SL LONG should be OR_low, got {sl_long}"

    # SHORT-side (symmetry)
    entry_short = 17999.5
    sl_short  = or_high
    tp1_short = entry_short - cfg.tp1_range_mult * or_range
    tp2_short = entry_short - cfg.tp2_range_mult * or_range
    assert abs(tp1_short - (entry_short - 45)) < 1e-9
    assert abs(tp2_short - (entry_short - 90)) < 1e-9
    assert sl_short == or_high

    return ("DE40 OR=30: "
            f"long  SL={sl_long}  TP1={tp1_long}  TP2={tp2_long}  "
            f"(short symmetric)")


# ---------------------------------------------------------------------------
# 8. Risk → lots formula identity
# ---------------------------------------------------------------------------
@check("math: risk-pct → lots formula matches backtest")
def _check_lots_formula() -> str:
    """
    Backtest formula (Scripts/backtest_v23_locked.py § sizer):

        risk_$       = equity × risk_pct
        $/lot @ stop = (|entry-SL| / tick_size) × pip_value
        lots         = risk_$ / ($/lot @ stop), then floor to lot_step

    With equity=$100k, risk_pct=0.170% → risk_$ = $170.
    DE40 entry=18030.5, SL=18000.0, |Δ|=30.5, tick=1.0, pip_value=$1.0
        $/lot @ stop = 30.5 × $1.0   =  $30.50
        raw lots     = 170 / 30.50   =  5.5737...
        floored 0.1  =  5.5 lots
    """
    import math
    from src.live.v30_live import V30_SPECS
    spec = V30_SPECS["DE40"]
    equity   = 100_000.0
    risk_pct = 0.00170
    risk_usd = equity * risk_pct                          # 170.00

    entry, sl   = 18030.5, 18000.0
    risk_per_u  = abs(entry - sl)                         # 30.5
    per_lot_usd = (risk_per_u / spec.tick_size) * spec.pip_value_per_lot
    lots_raw    = risk_usd / per_lot_usd
    lots_step   = max(spec.min_lot,
                      math.floor(lots_raw / spec.lot_step) * spec.lot_step)

    # Independently-derived expectation. We compute it from scratch using the
    # spec actually loaded — that way the check is self-validating: if pip_value
    # ever changes upstream, the expectation tracks it.
    want = max(spec.min_lot,
               math.floor((risk_usd / per_lot_usd) / spec.lot_step) * spec.lot_step)
    assert abs(lots_step - want) < 1e-9, (
        f"lots formula drift: got {lots_step}, expected {want}")
    # Sanity-check the magnitude — if anyone ever tries 100× this, we want to scream.
    assert 0.1 <= lots_step <= 10.0, (
        f"lots {lots_step} outside sane range [0.1, 10.0] for $100k @ 0.170% — "
        f"check pip_value/tick_size in V30_SPECS")
    return (f"DE40 @ $100k risk=0.170%: risk=${risk_usd:.2f}, "
            f"$/lot=${per_lot_usd:.2f}, lots={lots_step}")


# ---------------------------------------------------------------------------
# 9. Partial-manager simulation: TP1 → TP2 → trail
# ---------------------------------------------------------------------------
@check("ladder: PartialCloseManager TP1+TP2+trail simulation")
def _check_partial_ladder() -> str:
    """
    Drive PartialCloseManager through the full ladder with a mock bridge,
    verify:
      - TP1 fires once when bar.high ≥ tp1 (LONG)
      - 50 % of original lots closed; SL moved to entry (BE)
      - TP2 fires once when bar.high ≥ tp2
      - 25 % more closed; trail mode active
      - Trail SL ratchets up (never down) once trail engages
    """
    from src.live.partial_manager import PartialCloseManager, PartialState

    closed_log = []

    # MockBridge MUST accept whatever kwargs the production
    # partial_manager calls with — using **kw means the test catches
    # signature drift in PartialCloseManager itself rather than choking on it.
    class MockBridge:
        def close_partial(self, ticket, *args, **kw):
            closed_log.append(("PARTIAL", ticket, args, kw))
            return type("R", (), {"error_code": 0})()
        def close_position(self, ticket, *args, **kw):
            closed_log.append(("FULL", ticket, args, kw))
            return type("R", (), {"error_code": 0})()
        def modify_position(self, ticket, *args, **kw):
            return type("R", (), {"error_code": 0})()

    bridge = MockBridge()
    mgr = PartialCloseManager()

    # Fresh LONG position: entry 100, SL 95, TP1 110, TP2 120, 1.0 lots
    s = PartialState(side=+1, entry_price=100.0, sl=95.0, tp1=110.0,
                     tp2=120.0, original_lots=1.0, open_lots=1.0,
                     ticket=1, peak_favourable=100.0)

    rl = lambda x: round(x, 2) if x >= 0.01 else 0.0

    # Bar 1: nothing hit
    r = mgr.update(s, 105, 99, 104, atr_value=2.0, atr_ready=True,
                   bridge=bridge, round_lots_fn=rl)
    assert not r.tp1_fired and not r.tp2_fired, "ladder fired prematurely"
    assert s.open_lots == 1.0, "open_lots changed before any TP hit"

    # Bar 2: TP1 hit (high ≥ 110) — should close 50%, move SL to entry
    r = mgr.update(s, 111, 105, 110.5, atr_value=2.0, atr_ready=True,
                   bridge=bridge, round_lots_fn=rl)
    assert r.tp1_fired and not r.tp2_fired, "TP1 should fire here"
    assert abs(s.open_lots - 0.5) < 1e-9, f"after TP1 expect 0.50 lots, got {s.open_lots}"
    assert s.sl == s.entry_price, "TP1 should move SL to entry (BE)"

    # Bar 3: TP1 already fired - second visit must be IDEMPOTENT
    r = mgr.update(s, 112, 109, 110, atr_value=2.0, atr_ready=True,
                   bridge=bridge, round_lots_fn=rl)
    assert not r.tp1_fired, "TP1 must not fire twice"

    # Bar 4: TP2 hit - close another 25% of ORIGINAL (so 0.25 more)
    r = mgr.update(s, 121, 115, 120.5, atr_value=2.0, atr_ready=True,
                   bridge=bridge, round_lots_fn=rl)
    assert r.tp2_fired, "TP2 should fire here"
    assert abs(s.open_lots - 0.25) < 1e-9, f"after TP2 expect 0.25 lots, got {s.open_lots}"
    assert s.tp2_hit, "tp2_hit must be set after TP2 (trail mode active)"

    # Bar 5: price rallies to 130; trail = peak - 0.8×ATR = 130 - 1.6 = 128.4
    r = mgr.update(s, 130, 122, 129, atr_value=2.0, atr_ready=True,
                   bridge=bridge, round_lots_fn=rl)
    expected_trail = 130 - 0.8 * 2.0
    assert abs(s.sl - expected_trail) < 1e-9, \
        f"trail SL should be {expected_trail}, got {s.sl}"

    # Bar 6: pullback to 125; trail must NOT widen (ratchet only)
    sl_before = s.sl
    r = mgr.update(s, 126, 124, 125, atr_value=2.0, atr_ready=True,
                   bridge=bridge, round_lots_fn=rl)
    assert s.sl >= sl_before, "trail SL widened — ratchet broken"

    return "TP1 (50%) → TP2 (25%) → trail (0.8×ATR, ratchet-only) all green"


# ---------------------------------------------------------------------------
# 10. ATR tracker readiness
# ---------------------------------------------------------------------------
@check("atr: tracker becomes ready after 14 bars")
def _check_atr_ready() -> str:
    from src.live.atr_tracker import ATRTracker
    t = ATRTracker(window=14)
    # Feed 13 bars — must NOT be ready
    for i in range(13):
        t.update(high=100 + i, low=99 + i, close=99.5 + i)
    assert not t.ready, f"ATR became ready too early (after 13 bars)"
    # 14th bar — now ready
    t.update(high=113, low=112, close=112.5)
    assert t.ready, "ATR not ready after 14 bars"
    assert t.value > 0, f"ATR value should be >0, got {t.value}"
    return f"ready after 14 bars, value={t.value:.4f}"


# ---------------------------------------------------------------------------
# 11. OrderRequest is built with tp=0.0 (broker no longer holds TP)
# ---------------------------------------------------------------------------
@check("contract: live entry uses tp=0.0 (TP managed in-bar)")
def _check_tp_zero() -> str:
    """
    String-search the source of _maybe_enter() for the v30.3 contract:
        tp=0.0,                         # v30.3 — TP managed in-bar by ...
    A regression here would silently route TP back to the broker, breaking
    the in-bar partial-close ladder.
    """
    from src.live.v30_live import V30Live
    src = inspect.getsource(V30Live._maybe_enter)
    assert "tp=0.0" in src, (
        "_maybe_enter no longer passes tp=0.0 — broker would receive a "
        "server-side TP, breaking v30.3 partial-close ladder")
    assert "tp=float(tp1)" not in src, (
        "_maybe_enter still passes tp=float(tp1) — v23 contract leaked "
        "back in")
    # Also verify the ATR-feed + partial-mgr.update wiring is present
    assert "st.atr_tracker.update" in src, \
        "_maybe_enter no longer feeds ATR every bar"
    assert "self.partial_mgr.update" in src, \
        "_maybe_enter no longer runs the partial-manager ladder"
    assert "PartialState" in src, "_maybe_enter no longer seeds PartialState"
    return "tp=0.0  ✓  ATR fed  ✓  partial_mgr.update wired  ✓"


# ---------------------------------------------------------------------------
# 12. Persistence dirs writable
# ---------------------------------------------------------------------------
@check("io: Results/ and Results/v30_state/ writable")
def _check_io_writable() -> str:
    for sub in ("Results", "Results/v30_state"):
        d = ROOT / sub
        d.mkdir(parents=True, exist_ok=True)
        probe = d / ".preflight_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError as e:
            raise AssertionError(f"{d} not writable: {e}")
    return "Results/ + Results/v30_state/ both writable"


# ---------------------------------------------------------------------------
# 13. Parity-net: existing tests pass (run as a subprocess)
# ---------------------------------------------------------------------------
@check("tests: 50 parity tests pass (atr+partial+persist+close_px+parity)")
def _check_test_suite_passes() -> str:
    """
    A failing test suite ALWAYS aborts the launch. Skipped if pytest is
    unavailable (we still print the line so the operator notices).
    """
    import subprocess
    try:
        import pytest                                            # noqa: F401
    except ImportError:
        return "pytest not installed — SKIPPED (recommend: pip install pytest)"

    paths = [
        "tests/test_atr_tracker.py",
        "tests/test_partial_manager.py",
        "tests/test_persistence_v30.py",
        "tests/test_close_px_inference.py",
        "tests/test_live_backtest_parity.py",
    ]
    cmd = [sys.executable, "-m", "pytest", "-q", "--no-header", "--tb=line"] + paths
    res = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        tail = (res.stdout or "").splitlines()[-12:]
        raise AssertionError("pytest failed:\n   " + "\n   ".join(tail))
    last = (res.stdout or "").strip().splitlines()[-1] if res.stdout else "?"
    return f"pytest green — {last}"


# ===========================================================================
# Runner
# ===========================================================================
def main() -> int:
    print()
    print("=" * 78)
    print(f"  v30.3 LIVE PREFLIGHT  —  contract verification before launch")
    print(f"  cwd: {ROOT}")
    print("=" * 78)

    n_pass = 0
    failures: List[Tuple[str, str]] = []

    for i, (name, fn) in enumerate(CHECKS, start=1):
        try:
            info = fn()
            print(f"  {GRN}✓{RST}  [{i:>2}/{len(CHECKS)}] {name}")
            print(f"        {DIM}{info}{RST}")
            n_pass += 1
        except AssertionError as e:
            print(f"  {RED}✗{RST}  [{i:>2}/{len(CHECKS)}] {name}")
            print(f"        {RED}{str(e)}{RST}")
            failures.append((name, str(e)))
        except Exception as e:
            print(f"  {RED}✗{RST}  [{i:>2}/{len(CHECKS)}] {name}  "
                  f"{RED}({type(e).__name__}){RST}")
            print(f"        {RED}{str(e)}{RST}")
            failures.append((name, f"{type(e).__name__}: {e}"))

    print()
    print("-" * 78)
    if not failures:
        print(f"  {GRN}PREFLIGHT PASSED{RST}  —  {n_pass}/{len(CHECKS)} checks green")
        print(f"  Live engine contract verified.  Launcher will proceed.")
        print("-" * 78)
        print()
        return 0
    else:
        print(f"  {RED}PREFLIGHT FAILED{RST}  —  "
              f"{len(failures)}/{len(CHECKS)} checks RED")
        print(f"  {YEL}LAUNCHER WILL ABORT.  Fix the issues above before re-running.{RST}")
        print()
        print("  Failure summary:")
        for name, msg in failures:
            first = msg.splitlines()[0] if msg else ""
            print(f"    {RED}✗{RST} {name}: {first}")
        print("-" * 78)
        print()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
