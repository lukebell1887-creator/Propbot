"""
SHF v8 Edge Registry — the 18 micro-edges that survived market_dna_v1.

Every edge here was discovered by Scripts/market_dna_v1.py and survived a
blind 30-day holdout (same sign, ≥50% magnitude, n ≥ 10).

DO NOT add edges by hand.  Re-run market_dna with new data and regenerate.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class EdgeSpec:
    """A single micro-edge.

    Two flavours via `kind`:
      - "autocorr"      : trigger = sign of return at bar t-`lag_min`.
                          trade in same direction (sign>0) or opposite (sign<0).
      - "fade1sigma"    : trigger = abs 5-min agg return > 1 sigma.
                          trade OPPOSITE the move (always a fade).
    """
    name: str
    symbol: str
    hour_utc: int            # the hour during which this edge fires
    sign: int                # +1 = momentum / continuation; -1 = reversal
    effect_size: float       # |holdout effect| (autocorr coef or fade %)
    holdout_p: float
    kind: str = "autocorr"
    lag_min: int = 1         # for autocorr edges
    requires_or_filter: bool = True   # require wide OR-range to trade


# ====================================================================
#  The 18 surviving edges (from Results/market_dna_report.md)
# ====================================================================

EDGES: list[EdgeSpec] = [
    # ---- US100 momentum / reversal ---------------------------------
    EdgeSpec("US100_h23_lag1_mom",  "US100",  23, +1, 0.0879, 0.0083, "autocorr", 1),
    EdgeSpec("US100_h14_lag1_mom",  "US100",  14, +1, 0.0333, 0.3174, "autocorr", 1),
    EdgeSpec("US100_h21_lag1_rev",  "US100",  21, -1, 0.0616, 0.0646, "autocorr", 1),
    EdgeSpec("US100_h07_lag5_rev",  "US100",   7, -1, 0.0446, 0.1813, "autocorr", 5),
    EdgeSpec("US100_h06_lag5_rev",  "US100",   6, -1, 0.0388, 0.2446, "autocorr", 5),
    EdgeSpec("US100_h21_lag10_mom", "US100",  21, +1, 0.0302, 0.3647, "autocorr", 10),

    # ---- DE40 momentum --------------------------------------------
    EdgeSpec("DE40_h06_lag3_mom",   "DE40",    6, +1, 0.0860, 0.0099, "autocorr", 3,
             requires_or_filter=False),   # DE40 OR filter rejected on holdout
    EdgeSpec("DE40_h20_lag3_mom",   "DE40",   20, +1, 0.0593, 0.0755, "autocorr", 3,
             requires_or_filter=False),

    # ---- XAUUSD momentum / reversal -------------------------------
    EdgeSpec("XAU_h08_lag5_rev",    "XAUUSD",  8, -1, 0.0922, 0.0056, "autocorr", 5),
    EdgeSpec("XAU_h05_lag3_mom",    "XAUUSD",  5, +1, 0.0856, 0.0102, "autocorr", 3),
    EdgeSpec("XAU_h14_lag5_rev",    "XAUUSD", 14, -1, 0.0579, 0.0823, "autocorr", 5),
    EdgeSpec("XAU_h07_lag20_mom",   "XAUUSD",  7, +1, 0.0417, 0.2108, "autocorr", 20),
    EdgeSpec("XAU_h11_lag1_mom",    "XAUUSD", 11, +1, 0.0467, 0.1615, "autocorr", 1),
    EdgeSpec("XAU_h11_lag3_rev",    "XAUUSD", 11, -1, 0.0369, 0.2689, "autocorr", 3),

    # ---- XAUUSD 1-sigma fade -------------------------------------
    EdgeSpec("XAU_h21_fade1sigma",  "XAUUSD", 21, -1, 0.0014, 0.2087, "fade1sigma"),
    EdgeSpec("XAU_h03_fade1sigma",  "XAUUSD",  3, -1, 0.0004, 0.6120, "fade1sigma"),
]


# ====================================================================
#  Per-instrument R:R parameters (from market_dna 30-min MAE/MFE table)
# ====================================================================
#
# stop_dist  = 0.5 * |MAE_q25| in basis-points of price
# tp_dist    = 1.0 *  MFE_q60  in basis-points of price
#
# This gives realistic R:R ≈ 0.95-1.10 — within the physical ceiling
# we measured.  No more chasing 1.5R-2R that doesn't exist.

INSTRUMENT_RR_BPS = {
    "US100":  {"stop_bps":  6.10, "tp_bps":  5.80, "horizon_min": 30},
    "DE40":   {"stop_bps":  5.25, "tp_bps":  5.60, "horizon_min": 30},
    "XAUUSD": {"stop_bps":  8.70, "tp_bps":  9.70, "horizon_min": 30},
}


# ====================================================================
#  OR (opening-range) windows for the volatility filter
# ====================================================================
#
# Reused from market_dna ORB study; the OR-range vs post-OR-range
# correlation is the strongest validated edge, used as a *sizing
# scaler* not a directional trigger.

ORB_WINDOW_UTC = {
    "US100":  (14 * 60 + 30, 14 * 60 + 35),
    "DE40":   (8 * 60,  8 * 60 + 15),
    "XAUUSD": (14 * 60 + 30, 14 * 60 + 45),
}


def edges_for_symbol_hour(sym: str, hour: int) -> list[EdgeSpec]:
    """Fast lookup for the engine."""
    return [e for e in EDGES if e.symbol == sym and e.hour_utc == hour]
