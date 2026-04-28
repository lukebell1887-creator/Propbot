"""
Regression test for the 2026-04-28 SymbolSpec unit-mismatch bug.

THE BUG (in plain English)
--------------------------
SMARTBB_UNIVERSE.pip_value is documented in src/smartbb_engine.py as:
    "$ per POINT per lot"

The downstream live-sizing formula in src/live/v30_live.py is:
    dollars_per_lot_stopout = (risk_per_unit / tick_sz) * pip_val_per_lot

That formula is correct **only** if `pip_val_per_lot` is in $/TICK units
(because it cancels the `/tick_sz`). For tick_size != 1.0 (e.g. US500
with tick=0.25, XAUUSD with tick=0.01), passing the universe value as-is
produces lot sizes that are too SMALL by a factor of 1/tick_size.

Real-world impact (live trades, 2026-04-28):
    * US500 short: bot sized 22.3 lots vs. backtest-implied ~89.2 lots.
                   $-risk realised was ~25% of intended.
    * XAUUSD     : would size 100× too small (tick=0.01).
    * US30, DE40 : tick=1.0, no bug visible (lucky).

FIX
---
At SymbolSpec construction we multiply by tick_size, converting the
$/POINT value from the universe into the $/TICK value the rest of the
file expects. This file pins the resulting numbers so the bug cannot
silently come back.
"""
from __future__ import annotations

import math
from src.live.v30_live import (
    V30_SPECS,
    V30_BROKER_TICK_SIZE,
)
from src.smartbb_engine import SMARTBB_UNIVERSE


# Test 1 ─ pip_value_per_lot must equal universe_pip × tick_size.
def test_pip_value_per_lot_is_dollars_per_tick():
    for sym, spec in V30_SPECS.items():
        uni = SMARTBB_UNIVERSE[sym]
        expected = float(uni.pip_value) * V30_BROKER_TICK_SIZE[sym]
        assert math.isclose(spec.pip_value_per_lot, expected, rel_tol=1e-12), (
            f"{sym}: SymbolSpec.pip_value_per_lot={spec.pip_value_per_lot} "
            f"!= universe.pip_value×tick_size={expected}. "
            f"BUG REGRESSION: live sizing will be off by 1/tick_size."
        )


# Test 2 ─ end-to-end: stop-out $/lot computed by the live formula must
# equal the simple "(distance × pip_value_per_point × 1 lot)" used in
# the backtest engines (see src/momentum_engine.py et al.).
def test_dollars_per_lot_stopout_matches_backtest_convention():
    """
    Live formula:    dpl_live = (risk_pts / tick_sz) * pip_val_per_lot
    Backtest:        dpl_bt   = risk_pts * universe_pip_value
    With the fix, pip_val_per_lot = universe_pip × tick_sz, so:
        dpl_live = (risk_pts / tick_sz) * (universe_pip × tick_sz)
                 = risk_pts * universe_pip
                 = dpl_bt    ✓
    """
    risk_pts = 9.55  # US500 SL distance from today's live trade
    for sym, spec in V30_SPECS.items():
        uni = SMARTBB_UNIVERSE[sym]
        dpl_live = (risk_pts / spec.tick_size) * spec.pip_value_per_lot
        dpl_bt = risk_pts * float(uni.pip_value)
        assert math.isclose(dpl_live, dpl_bt, rel_tol=1e-9), (
            f"{sym}: live $/lot stopout ({dpl_live}) != backtest ({dpl_bt})"
        )


# Test 3 ─ the specific live trade that exposed the bug.
# US500 short, 9.55-pt SL, 0.85 % of $99,031 equity = $841.76 risk
# Backtest convention says: $/lot = 9.55 × $1/pt = $9.55, so lots ≈ 88.1.
# (Actually: 0.85% × 99031 / 9.55 = 88.13 lots)
# Pre-fix the bot computed 22.0 lots (4× under-sized); post-fix ≈ 88.1.
def test_us500_real_trade_sizes_post_fix():
    sym = "US500"
    spec = V30_SPECS[sym]
    equity = 99031.18
    risk_pct = 0.0085
    risk_usd = equity * risk_pct
    sl_pts = 9.55

    dpl = (sl_pts / spec.tick_size) * spec.pip_value_per_lot
    lots_raw = risk_usd / dpl
    # broker step rounding
    step = 0.1
    lots = max(spec.min_lot, math.floor(lots_raw / step) * step)

    # Post-fix expectation: ~88 lots (was 22 lots pre-fix).
    assert 80.0 <= lots <= 95.0, (
        f"US500 sizing now = {lots} lots; expected ~88 lots after fix.")
    # And the realised $-risk should now equal the intended $-risk
    # (within one lot-step of slop).
    realised_risk = lots * dpl
    assert abs(realised_risk - risk_usd) < dpl * step, (
        f"realised $-risk {realised_risk} vs intended {risk_usd}: "
        f"too far apart, fix is wrong.")
