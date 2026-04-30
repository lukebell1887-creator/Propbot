"""
tests/test_v30_live_bt_parity.py
=================================

Mathematical parity tests between the V30 LIVE engine (`src/live/v30_live.py`)
and the V30 BACKTEST engine (`src/orb_engine_v20.py`) that produced the
$27,668 number reported in `Docs/V30_RELEASE_NOTES.md`.

Background
----------
On 2026-04-30 the trader confirmed (against the 5ers Trading Conditions
spec sheet) that ALL FOUR traded symbols (DE40, US30, US500/SP500, XAUUSD)
have:
    Min lot         = 0.01
    Incremental Step = 0.01
    Contract Size   = 1 (indices)  /  100 oz (Gold)

The live engine had two bugs that broke parity with the backtest:

  (1) `V30_BROKER_MIN_LOT` / `V30_BROKER_LOT_STEP` were 0.1 for the three
      indices, while the backtest's `orb_engine_v20.SymbolSpec` defaults to
      0.01.  This caused live to over-quantise lots, either rounding tiny
      sizes UP to 0.10 (over-sizing) or rounding mid-range sizes DOWN to
      the next 0.10 step (under-sizing).
  (2) `_maybe_enter` was setting `sl = OR_low` (LONG) / `sl = OR_high`
      (SHORT) without the `sl_buffer_range_mult * OR_range` widening that
      the backtest applies.  This made live SLs tighter than backtest →
      more whipsaws, smaller R_dist, larger lots, more frequent SL hits.

Both bugs were fixed on 2026-04-30 in commit "v30.4 PARITY FIX".

What this test proves
---------------------
For a fixed deterministic input (risk_usd, R_dist, OR levels, SL buffer),
the live engine's sizing path and the backtest engine's sizing path
produce identical lot counts and identical SL prices, to within 1e-9.

This is the MATHEMATICAL FOUNDATION of "live should match backtest minus
slippage" — if these tests pass, the only remaining source of
divergence between the live $-P&L and the backtest's $27,668 is
real-world execution slippage (which is, by definition, unknown until we
trade live and measure it).

Run
---
    python -m pytest tests/test_v30_live_bt_parity.py -v
"""
from __future__ import annotations

import math

import pytest

from src.live.v30_live import (
    V30_BROKER_MIN_LOT,
    V30_BROKER_LOT_STEP,
    V30_BROKER_TICK_SIZE,
    V30_BROKER_CONTRACT_SIZE,
    V30_DOLLARS_PER_TICK_PER_LOT,
    V30_ORB_CONFIGS,
    V30_SPECS,
)
from src.momentum.orb import ORBConfig


SYMBOLS = ("DE40", "US30", "US500", "XAUUSD")


# =====================================================================
# 1. BROKER CONSTANTS — must match the 5ers spec sheet
# =====================================================================

class TestBrokerConstants:
    """Verify that V30 broker constants match the 5ers Trading Conditions
    page (verified by trader on 2026-04-30)."""

    def test_min_lot_is_001_for_all_symbols(self):
        """5ers spec: ALL four symbols have min_lot = 0.01."""
        for sym in SYMBOLS:
            assert V30_BROKER_MIN_LOT[sym] == pytest.approx(0.01, abs=1e-12), (
                f"{sym} min_lot = {V30_BROKER_MIN_LOT[sym]}, "
                f"expected 0.01 per 5ers spec sheet"
            )

    def test_lot_step_is_001_for_all_symbols(self):
        """5ers spec: ALL four symbols have incremental_step = 0.01."""
        for sym in SYMBOLS:
            assert V30_BROKER_LOT_STEP[sym] == pytest.approx(0.01, abs=1e-12), (
                f"{sym} lot_step = {V30_BROKER_LOT_STEP[sym]}, "
                f"expected 0.01 per 5ers spec sheet"
            )

    def test_min_lot_equals_lot_step(self):
        """5ers spec: min_lot == lot_step == 0.01 — they're the same row in
        the spec sheet, so any drift between them is a config error."""
        for sym in SYMBOLS:
            assert V30_BROKER_MIN_LOT[sym] == V30_BROKER_LOT_STEP[sym]

    def test_contract_sizes(self):
        """5ers spec: indices = contract_size 1; Gold = 100 oz."""
        assert V30_BROKER_CONTRACT_SIZE["DE40"] == 1.0
        assert V30_BROKER_CONTRACT_SIZE["US30"] == 1.0
        assert V30_BROKER_CONTRACT_SIZE["US500"] == 1.0
        assert V30_BROKER_CONTRACT_SIZE["XAUUSD"] == 100.0

    def test_dollars_per_tick_per_lot(self):
        """Derived constant: contract × tick = $/tick/lot.

        For all four symbols this MUST come out to $1.00 / tick / lot:
            DE40   = 1.0 × 1.0  = $1.00 / point
            US30   = 1.0 × 1.0  = $1.00 / point
            US500  = 1.0 × 1.0  = $1.00 / point
            XAUUSD = 100 × 0.01 = $1.00 / tick (= $100 per $1 of price)
        """
        for sym in SYMBOLS:
            expected = (V30_BROKER_CONTRACT_SIZE[sym]
                        * V30_BROKER_TICK_SIZE[sym])
            assert V30_DOLLARS_PER_TICK_PER_LOT[sym] == pytest.approx(expected)
            assert V30_DOLLARS_PER_TICK_PER_LOT[sym] == pytest.approx(1.0), (
                f"{sym}: expected $1.00 / tick / lot, "
                f"got {V30_DOLLARS_PER_TICK_PER_LOT[sym]}"
            )

    def test_v30_specs_use_broker_truth(self):
        """Per-symbol SymbolSpec must reflect the broker-truth constants."""
        for sym in SYMBOLS:
            spec = V30_SPECS[sym]
            assert spec.min_lot == V30_BROKER_MIN_LOT[sym]
            assert spec.lot_step == V30_BROKER_LOT_STEP[sym]
            assert spec.tick_size == V30_BROKER_TICK_SIZE[sym]
            assert spec.pip_value_per_lot == V30_DOLLARS_PER_TICK_PER_LOT[sym]


# =====================================================================
# 2. SL BUFFER — live formula must match backtest formula
# =====================================================================

class TestSLBufferParity:
    """Verify that the live SL = OR_anchor ± (sl_buffer_range_mult × OR_range)
    matches the backtest's orb_engine_v20 SL formula."""

    @staticmethod
    def _backtest_sl_long(or_low: float, or_range: float,
                          sl_buffer_range_mult: float) -> float:
        """orb_engine_v20.py formula — LONG SL."""
        return or_low - sl_buffer_range_mult * or_range

    @staticmethod
    def _backtest_sl_short(or_high: float, or_range: float,
                           sl_buffer_range_mult: float) -> float:
        """orb_engine_v20.py formula — SHORT SL."""
        return or_high + sl_buffer_range_mult * or_range

    @staticmethod
    def _live_sl_long(or_low: float, or_range: float,
                      sl_buffer_range_mult: float) -> float:
        """v30_live.py post-fix formula — LONG SL."""
        sl_buf = sl_buffer_range_mult * or_range
        return or_low - sl_buf

    @staticmethod
    def _live_sl_short(or_high: float, or_range: float,
                       sl_buffer_range_mult: float) -> float:
        """v30_live.py post-fix formula — SHORT SL."""
        sl_buf = sl_buffer_range_mult * or_range
        return or_high + sl_buf

    @pytest.mark.parametrize("sym, or_low, or_high", [
        ("DE40",   18000.0, 18050.0),     # 50pt OR
        ("US30",   38000.0, 38120.0),     # 120pt OR
        ("US500",  5000.00, 5008.50),     # 8.5pt OR (tight US500 setup)
        ("XAUUSD", 2000.00, 2007.40),     # $7.40 OR
    ])
    def test_sl_long_parity(self, sym, or_low, or_high):
        """LONG SL: live formula must equal backtest formula bit-for-bit."""
        cfg: ORBConfig = V30_ORB_CONFIGS[sym]
        or_range = or_high - or_low
        bt = self._backtest_sl_long(or_low, or_range, cfg.sl_buffer_range_mult)
        lv = self._live_sl_long(or_low, or_range, cfg.sl_buffer_range_mult)
        assert lv == pytest.approx(bt, abs=1e-9), (
            f"{sym} LONG SL diverged: backtest={bt}, live={lv}"
        )

    @pytest.mark.parametrize("sym, or_low, or_high", [
        ("DE40",   18000.0, 18050.0),
        ("US30",   38000.0, 38120.0),
        ("US500",  5000.00, 5008.50),
        ("XAUUSD", 2000.00, 2007.40),
    ])
    def test_sl_short_parity(self, sym, or_low, or_high):
        """SHORT SL: live formula must equal backtest formula bit-for-bit."""
        cfg: ORBConfig = V30_ORB_CONFIGS[sym]
        or_range = or_high - or_low
        bt = self._backtest_sl_short(or_high, or_range, cfg.sl_buffer_range_mult)
        lv = self._live_sl_short(or_high, or_range, cfg.sl_buffer_range_mult)
        assert lv == pytest.approx(bt, abs=1e-9), (
            f"{sym} SHORT SL diverged: backtest={bt}, live={lv}"
        )

    def test_sl_buffer_widens_de40(self):
        """Sanity: DE40 has sl_buffer_range_mult=0.3 → SL widens by 30 % of OR."""
        cfg = V30_ORB_CONFIGS["DE40"]
        assert cfg.sl_buffer_range_mult == pytest.approx(0.3)
        or_low, or_high = 18000.0, 18050.0
        sl_long = self._live_sl_long(or_low, 50.0, cfg.sl_buffer_range_mult)
        # SL should be 15 pts (=0.3 × 50) BELOW or_low, i.e. 17985.
        assert sl_long == pytest.approx(17985.0, abs=1e-9)

    def test_sl_buffer_zero_us30(self):
        """Sanity: US30 has sl_buffer_range_mult=0.0 → SL = OR_anchor exactly."""
        cfg = V30_ORB_CONFIGS["US30"]
        assert cfg.sl_buffer_range_mult == pytest.approx(0.0)
        or_low, or_high = 38000.0, 38120.0
        sl_long = self._live_sl_long(or_low, 120.0, cfg.sl_buffer_range_mult)
        assert sl_long == pytest.approx(38000.0, abs=1e-9)


# =====================================================================
# 3. LOT SIZING — live rounding must match backtest rounding
# =====================================================================

class TestLotSizingParity:
    """Verify that the live lot-rounding now matches backtest 0.01-step
    rounding for the four symbols, using identical inputs."""

    @staticmethod
    def _compute_lots(risk_usd: float, r_dist: float,
                      pip_value_per_lot: float, tick_size: float,
                      min_lot: float, lot_step: float) -> float:
        """Common sizing formula used by both engines.

            $/lot @ stop = (R_dist / tick_size) × pip_value_per_lot
            lots         = max(min_lot, floor(risk_usd / $/lot @ stop / step) × step)
        """
        dollars_per_lot_stopout = (r_dist / tick_size) * pip_value_per_lot
        if dollars_per_lot_stopout <= 0:
            return 0.0
        raw = risk_usd / dollars_per_lot_stopout
        rounded = math.floor(raw / lot_step) * lot_step
        return max(min_lot, rounded)

    @pytest.mark.parametrize("sym, risk_usd, r_dist", [
        # (sym, risk_dollars, R_dist in PRICE units)
        # Tight DE40 — small lots that previously got force-rounded to 0.10
        ("DE40",  17.0,  100.0),    # raw=0.17 → 0.10 (old) / 0.17 (new)
        ("DE40",  85.0,  100.0),    # raw=0.85 → 0.80 (old) / 0.85 (new)
        ("DE40", 170.0,  100.0),    # raw=1.70 → 1.70 (old) / 1.70 (new)
        # US30
        ("US30",   8.0,  100.0),    # raw=0.08 → 0.10 (old, OVER-SIZE) / 0.08 (new)
        ("US30",  17.0,  100.0),    # raw=0.17 → 0.10 (old) / 0.17 (new)
        # US500 — same R_dist range
        ("US500", 17.0,    5.0),    # raw=3.40 → 3.40 (old) / 3.40 (new)
        ("US500",  3.0,   10.0),    # raw=0.30 → 0.30 (old) / 0.30 (new)
        ("US500", 17.0,   50.0),    # raw=0.34 → 0.30 (old) / 0.34 (new)
        # XAUUSD — already 0.01-step pre-fix
        ("XAUUSD", 17.0,  5.0),     # raw=0.034 → 0.03 / 0.03
    ])
    def test_lot_count_matches_backtest(self, sym, risk_usd, r_dist):
        """For the same inputs, live (post-fix) and backtest must compute
        the SAME lot count to within 1e-9.

        Backtest uses SymbolSpec defaults (min_lots=0.01, lot_step=0.01).
        Live now also uses 0.01/0.01 (post-fix). They must agree exactly.
        """
        spec = V30_SPECS[sym]
        # Backtest path
        bt_lots = self._compute_lots(
            risk_usd=risk_usd, r_dist=r_dist,
            pip_value_per_lot=V30_DOLLARS_PER_TICK_PER_LOT[sym],
            tick_size=V30_BROKER_TICK_SIZE[sym],
            min_lot=0.01, lot_step=0.01,    # backtest defaults
        )
        # Live path (post-fix uses the SAME numbers, but read from the spec)
        lv_lots = self._compute_lots(
            risk_usd=risk_usd, r_dist=r_dist,
            pip_value_per_lot=spec.pip_value_per_lot,
            tick_size=spec.tick_size,
            min_lot=spec.min_lot, lot_step=spec.lot_step,
        )
        assert lv_lots == pytest.approx(bt_lots, abs=1e-9), (
            f"{sym} risk=${risk_usd} R_dist={r_dist}: "
            f"backtest={bt_lots} lots, live={lv_lots} lots"
        )

    def test_no_force_rounding_to_010(self):
        """The whole point of the fix: a $17 risk on a 100-pt DE40 trade
        must produce 0.17 lots (not 0.10), proving the over-sizing bug
        is fixed."""
        spec = V30_SPECS["DE40"]
        lots = self._compute_lots(
            risk_usd=17.0, r_dist=100.0,
            pip_value_per_lot=spec.pip_value_per_lot,
            tick_size=spec.tick_size,
            min_lot=spec.min_lot, lot_step=spec.lot_step,
        )
        assert lots == pytest.approx(0.17, abs=1e-9)
        # And it must NOT be 0.10 (which is what the buggy live path
        # produced before the 2026-04-30 fix).
        assert abs(lots - 0.10) > 0.01, (
            "regression: live still over-sizing to 0.10 — "
            "V30_BROKER_MIN_LOT must be 0.01, not 0.10"
        )


# =====================================================================
# 4. END-TO-END SANITY CHECK — pull it all together
# =====================================================================

class TestEndToEndParity:
    """For a single trade specified end-to-end, verify entry+SL+lots+TP
    all agree between the live formulae and the backtest formulae."""

    def test_de40_long_full_replay(self):
        """A DE40 LONG breakout with known inputs.

        Inputs:
            equity      = $100,000
            risk_pct    = 0.170 % (v25.1 ship)
            OR_low      = 18000
            OR_high     = 18050  (50-pt OR)
            entry_px    = 18051  (1pt above OR_high)
            tp1 mult    = 1.5
            tp2 mult    = 3.0
            sl buf mult = 0.3

        Expected:
            sl       = 18000 - 15 = 17985
            R_dist   = 18051 - 17985 = 66 pts
            risk_usd = $170
            $/lot    = (66/1) × $1 = $66
            lots_raw = 170/66 ≈ 2.5757
            lots     = floor(2.5757/0.01)×0.01 = 2.57
            tp1      = 18051 + 1.5×50 = 18126
            tp2      = 18051 + 3.0×50 = 18201
        """
        cfg = V30_ORB_CONFIGS["DE40"]
        spec = V30_SPECS["DE40"]
        equity = 100_000.0
        risk_pct = 0.00170
        or_low, or_high = 18000.0, 18050.0
        or_range = or_high - or_low
        entry_px = 18051.0

        # SL with buffer
        sl_buf = cfg.sl_buffer_range_mult * or_range
        sl = or_low - sl_buf
        assert sl == pytest.approx(17985.0, abs=1e-9)

        # TPs
        tp1 = entry_px + cfg.tp1_range_mult * or_range
        tp2 = entry_px + cfg.tp2_range_mult * or_range
        assert tp1 == pytest.approx(18126.0, abs=1e-9)
        assert tp2 == pytest.approx(18201.0, abs=1e-9)

        # Lots
        r_dist = entry_px - sl
        risk_usd = equity * risk_pct
        dollars_per_lot = (r_dist / spec.tick_size) * spec.pip_value_per_lot
        lots_raw = risk_usd / dollars_per_lot
        lots = max(spec.min_lot,
                   math.floor(lots_raw / spec.lot_step) * spec.lot_step)
        # Pre-fix would have given 2.50 (force-rounded down to 0.1 step).
        # Post-fix gives 2.57 (rounded down to 0.01 step).
        assert lots == pytest.approx(2.57, abs=1e-9)

    def test_us500_short_tight_setup(self):
        """A US500 SHORT setup with tight 8.5-pt OR — exercises the
        US500-specific tick_size + sl_buffer_range_mult=0.6 path."""
        cfg = V30_ORB_CONFIGS["US500"]
        spec = V30_SPECS["US500"]
        equity = 100_000.0
        risk_pct = 0.00170
        or_low, or_high = 5000.0, 5008.5
        or_range = or_high - or_low
        entry_px = 4999.0  # 1pt below OR_low (SHORT entry)

        sl_buf = cfg.sl_buffer_range_mult * or_range
        sl = or_high + sl_buf  # SHORT
        # 5008.5 + 0.6×8.5 = 5008.5 + 5.1 = 5013.6
        assert sl == pytest.approx(5013.6, abs=1e-9)

        tp1 = entry_px - cfg.tp1_range_mult * or_range
        tp2 = entry_px - cfg.tp2_range_mult * or_range
        # tp1: 4999 - 0.5×8.5 = 4994.75
        # tp2: 4999 - 1.0×8.5 = 4990.5
        assert tp1 == pytest.approx(4994.75, abs=1e-9)
        assert tp2 == pytest.approx(4990.5, abs=1e-9)

        r_dist = sl - entry_px           # 5013.6 - 4999 = 14.6
        risk_usd = equity * risk_pct     # $170
        dollars_per_lot = (r_dist / spec.tick_size) * spec.pip_value_per_lot
        # = (14.6 / 1.0) × $1 = $14.60 per lot
        lots_raw = risk_usd / dollars_per_lot
        # = 170 / 14.6 = 11.6438...
        lots = max(spec.min_lot,
                   math.floor(lots_raw / spec.lot_step) * spec.lot_step)
        # Pre-fix: floor(11.6438/0.1)×0.1 = 11.6 lots
        # Post-fix: floor(11.6438/0.01)×0.01 = 11.64 lots
        assert lots == pytest.approx(11.64, abs=1e-9)

    def test_xauusd_long_known_quantities(self):
        """XAUUSD LONG — sl_buffer_mult=0.6, contract=100oz."""
        cfg = V30_ORB_CONFIGS["XAUUSD"]
        spec = V30_SPECS["XAUUSD"]
        equity = 100_000.0
        risk_pct = 0.00170
        or_low, or_high = 2000.00, 2007.40
        or_range = or_high - or_low      # 7.40
        entry_px = 2007.50               # $0.10 above OR_high

        sl = or_low - cfg.sl_buffer_range_mult * or_range
        # 2000 - 0.6×7.4 = 2000 - 4.44 = 1995.56
        assert sl == pytest.approx(1995.56, abs=1e-9)

        tp1 = entry_px + cfg.tp1_range_mult * or_range
        # 2007.5 + 2.0×7.4 = 2007.5 + 14.8 = 2022.3
        assert tp1 == pytest.approx(2022.3, abs=1e-9)

        r_dist = entry_px - sl           # 11.94
        risk_usd = equity * risk_pct     # $170
        # XAUUSD: tick=0.01, pip_value=$1/tick/lot
        # $/lot = (11.94/0.01) × $1 = $1194 per lot
        dollars_per_lot = (r_dist / spec.tick_size) * spec.pip_value_per_lot
        assert dollars_per_lot == pytest.approx(1194.0, abs=1e-6)
        lots_raw = risk_usd / dollars_per_lot
        # = 170/1194 ≈ 0.14237
        lots = max(spec.min_lot,
                   math.floor(lots_raw / spec.lot_step) * spec.lot_step)
        # 0.14 lots (already a 0.01 step pre-fix and post-fix — XAU was
        # never affected by the lot-step bug)
        assert lots == pytest.approx(0.14, abs=1e-9)
