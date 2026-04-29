"""
Regression tests for the 2026-04-28 SymbolSpec unit-mismatch saga.

THE BUG (in plain English)
--------------------------
SMARTBB_UNIVERSE.pip_value is documented in src/smartbb_engine.py as:
    "$ per POINT per lot"

…but it is internally INCONSISTENT.  Indices store $/POINT (correct per
the comment) while XAUUSD stores $/$1-of-price by accident — the
backtest's R-multiple maths happens to hide both errors because lot
counts and per-tick P&L scale-cancel.

The downstream live-sizing formula in src/live/v30_live.py is:
    dollars_per_lot_stopout = (risk_per_unit / tick_sz) * pip_val_per_lot

i.e. it requires `pip_val_per_lot` to be in **$/TICK/LOT** units.  That
quantity is, by broker spec, exactly:
    pip_value_per_lot = contract_size × tick_size

For 5ers (Eightcap):
    DE40   : 1.0 × 1.0   = $1.00 / point / lot
    US30   : 1.0 × 1.0   = $1.00 / point / lot
    US500  : 1.0 × 0.25  = $0.25 / tick  / lot   ($1.00 / point)
    XAUUSD : 100 × 0.01  = $1.00 / tick  / lot   ($100  / $1 of price)

History of fixes
----------------
* PRE-2026-04-28 (commit < daaeade)
    Used `uni.pip_value` raw → US500 was sized 4× TOO SMALL (tick=0.25
    silently ignored).  XAUUSD was correct purely by coincidence
    (universe.pip_value=1 == broker $/tick/lot=1).

* 2026-04-28 hotfix-1 (commit daaeade)
    Multiplied by tick_size unconditionally → fixed US500, but BROKE
    XAUUSD by 100× (1 × 0.01 = 0.01 vs broker truth = 1.0).

* 2026-04-28 hotfix-2 (THIS COMMIT)
    Stop trusting the universe entirely.  Use broker-truth
    contract_size × tick_size table.  All four symbols now match
    physical broker dollars.
"""
from __future__ import annotations

import math
import pytest

from src.live.v30_live import (
    V30_SPECS,
    V30_BROKER_TICK_SIZE,
    V30_BROKER_CONTRACT_SIZE,
    V30_DOLLARS_PER_TICK_PER_LOT,
)


# ---------------------------------------------------------------------------
# Test 1 — broker-truth contract-size table is correct.
# ---------------------------------------------------------------------------
def test_contract_sizes_match_5ers_spec_sheet():
    """
    Source of truth: 5ers / Eightcap symbol spec sheet, supplied by trader,
    2026-04-28.

    A regression here means the live bot will mis-size every trade on the
    affected symbol, in proportion to the wrong contract size.
    """
    expected = {
        "DE40":   1.0,    # 1 lot = 1 index unit
        "US30":   1.0,    # 1 lot = 1 index unit
        "US500":  1.0,    # 1 lot = 1 index unit
        "XAUUSD": 100.0,  # 1 lot = 100 troy ounces  ★ critical
    }
    assert V30_BROKER_CONTRACT_SIZE == expected, (
        f"Broker contract sizes drifted from 5ers spec sheet:\n"
        f"  expected: {expected}\n"
        f"  got:      {V30_BROKER_CONTRACT_SIZE}"
    )


# ---------------------------------------------------------------------------
# Test 2 — derived $/tick/lot table equals contract_size × tick_size.
# ---------------------------------------------------------------------------
def test_dollars_per_tick_per_lot_is_contract_x_tick():
    for sym in V30_BROKER_CONTRACT_SIZE:
        cs = V30_BROKER_CONTRACT_SIZE[sym]
        ts = V30_BROKER_TICK_SIZE[sym]
        want = cs * ts
        got = V30_DOLLARS_PER_TICK_PER_LOT[sym]
        assert math.isclose(got, want, rel_tol=1e-12), (
            f"{sym}: V30_DOLLARS_PER_TICK_PER_LOT={got} != "
            f"contract_size × tick_size = {cs} × {ts} = {want}"
        )


# ---------------------------------------------------------------------------
# Test 3 — V30_SPECS.pip_value_per_lot pulls from the broker-truth table,
#          NOT from the (inconsistent) SMARTBB_UNIVERSE.
# ---------------------------------------------------------------------------
def test_specs_use_broker_truth_dollars_per_tick():
    for sym, spec in V30_SPECS.items():
        want = V30_DOLLARS_PER_TICK_PER_LOT[sym]
        assert math.isclose(spec.pip_value_per_lot, want, rel_tol=1e-12), (
            f"{sym}: SymbolSpec.pip_value_per_lot={spec.pip_value_per_lot} "
            f"!= V30_DOLLARS_PER_TICK_PER_LOT={want}.  "
            f"BUG REGRESSION — live sizing will be wrong on this symbol."
        )


# ---------------------------------------------------------------------------
# Test 4 — concrete pinned values for all four live symbols.
# ---------------------------------------------------------------------------
def test_pinned_pip_values_per_lot():
    """
    Pinned values that an outsider can verify against any 5ers/Eightcap
    statement.  ANY change here without an explicit broker-spec change
    is a bug.
    """
    expected = {
        "DE40":   1.0,    # $1 per index point per lot
        "US30":   1.0,    # $1 per index point per lot
        # 2026-04-29 hotfix-3: US500 tick changed 0.25 → 1.0 to match the
        # 5ers / Eightcap broker spec (contract_size=1, identical to the
        # other indices).  Both encodings give $1/pt, but tick=1.0 prevents
        # downstream sizer code from accidentally applying a 4× factor.
        "US500":  1.0,    # $1 per index point per lot (was 0.25 pre-hotfix-3)
        "XAUUSD": 1.0,    # $1 per $0.01 of gold price per lot ($100/$ of price)
    }

    for sym, want in expected.items():
        got = V30_SPECS[sym].pip_value_per_lot
        assert math.isclose(got, want, rel_tol=1e-12), (
            f"{sym}: pip_value_per_lot={got}, broker truth={want}"
        )


# ---------------------------------------------------------------------------
# Test 5 — the live sizing formula gives the *expected broker P&L per lot*
#          for a 1-tick adverse move on every symbol.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sym, expected_pnl_one_tick", [
    ("DE40",   1.00),     # $1 per 1 index point
    ("US30",   1.00),     # $1 per 1 index point
    ("US500",  1.00),     # $1 per 1 index point (post hotfix-3, was 0.25 per 0.25 tick)
    ("XAUUSD", 1.00),     # $1 per $0.01 of gold price
])

def test_one_tick_pnl_per_lot(sym: str, expected_pnl_one_tick: float):
    """
    For 1.0 lot, a 1-tick adverse move must equal V30_DOLLARS_PER_TICK_PER_LOT.
    Verifies the LIVE FORMULA:
        dollars_per_lot_stopout = (risk_per_unit / tick_sz) * pip_value_per_lot
    """
    spec = V30_SPECS[sym]
    risk_per_unit = spec.tick_size           # 1 tick adverse
    dpl = (risk_per_unit / spec.tick_size) * spec.pip_value_per_lot
    assert math.isclose(dpl, expected_pnl_one_tick, rel_tol=1e-12), (
        f"{sym}: 1-tick $/lot = {dpl}, broker truth = {expected_pnl_one_tick}"
    )


# ---------------------------------------------------------------------------
# Test 6 — the specific live trade that exposed the original bug.
# US500 short, 9.55-pt SL, 0.85 % of $99,031 equity = $841.76 risk.
# Backtest convention says: $/lot = 9.55 × $1/pt = $9.55, so lots ≈ 88.1.
# Pre-fix the bot computed 22.0 lots (4× under-sized); post-fix ≈ 88.
# ---------------------------------------------------------------------------
def test_us500_real_trade_sizes_post_fix():
    sym = "US500"
    spec = V30_SPECS[sym]
    equity = 99031.18
    risk_pct = 0.0085
    risk_usd = equity * risk_pct
    sl_pts = 9.55                            # in price units, NOT ticks

    dpl = (sl_pts / spec.tick_size) * spec.pip_value_per_lot
    lots_raw = risk_usd / dpl
    step = 0.1
    lots = max(spec.min_lot, math.floor(lots_raw / step) * step)

    # Post-fix expectation: ~88 lots (was 22 lots pre-fix).
    assert 80.0 <= lots <= 95.0, (
        f"US500 sizing now = {lots} lots; expected ~88 lots after fix."
    )
    # Realised $-risk should match intended within one lot-step of slop.
    realised_risk = lots * dpl
    assert abs(realised_risk - risk_usd) < dpl * step, (
        f"realised $-risk {realised_risk} vs intended {risk_usd}: "
        f"too far apart, fix is wrong."
    )


# ---------------------------------------------------------------------------
# Test 7 — XAUUSD sizing is NOT 100× too small (the hotfix-1 regression).
# ---------------------------------------------------------------------------
def test_xauusd_sized_correctly_post_hotfix2():
    """
    Hotfix-1 (commit daaeade) accidentally divided XAUUSD's pip_value_per_lot
    by 100, which would have made the live bot place gold trades at 1/100th
    their intended size.  Pin the post-hotfix-2 expectation so this regression
    can never silently come back.

    Realistic gold trade: $100k equity, 0.85% risk = $850, SL distance = $5.00
    of price (typical XAUUSD ATR-based stop).  Broker-truth lot count:
        lots = $850 / ($5 × $100/$ × 1 lot) = 850 / 500 = 1.7 lots
    We must NOT produce 0.017 lots (the hotfix-1 bug).
    """
    sym = "XAUUSD"
    spec = V30_SPECS[sym]
    equity = 100_000.0
    risk_pct = 0.0085
    risk_usd = equity * risk_pct      # $850
    sl_dist = 5.00                    # $5 of gold price ($1900 → $1895)

    dpl = (sl_dist / spec.tick_size) * spec.pip_value_per_lot
    lots_raw = risk_usd / dpl
    step = spec.lot_step              # 0.01
    lots = max(spec.min_lot, math.floor(lots_raw / step) * step)

    # Broker truth: 1.7 lots (16,750 oz × $5 = $85,000 notional, $850 risk).
    # Hotfix-1 would have produced 0.017 lots — assert we are NOT in that
    # ballpark.
    assert lots >= 1.0, (
        f"XAUUSD sized {lots} lots — looks like hotfix-1 regression "
        f"(100× under-sized).  Expected ≈ 1.7 lots."
    )
    assert 1.5 <= lots <= 2.0, (
        f"XAUUSD sized {lots} lots; expected ≈ 1.7 lots from broker truth."
    )
    # Realised $-risk within one lot-step of slop.
    realised_risk = lots * dpl
    assert abs(realised_risk - risk_usd) < dpl * step, (
        f"XAUUSD realised $-risk {realised_risk} vs intended {risk_usd}"
    )
