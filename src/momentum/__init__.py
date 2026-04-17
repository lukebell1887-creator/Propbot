"""
SHF v7 — Momentum Scalper Core Math Modules.

Everything in here has a peer-reviewed optimality proof:

  KalmanForecast      — minimum-MSE linear filter (Kalman 1960)
  CUSUMDetector       — minimax-optimal change-point detector (Moustakides 1986)
  HawkesIntensity     — canonical self-exciting point process (Hawkes 1971)
  OptimalStopper      — Shiryaev quickest-detection free boundary (Shiryaev 1963)
  GarchOne            — one-step conditional variance (Bollerslev 1986)
  GpdTail             — extreme-value peaks-over-threshold (Pickands 1975; McNeil et al 2015)
  MicrostructureCluster — round-number / prior-swing liquidity detector
  EVTGarchStop        — composed dynamic tail-aware stop
  BayesianEdge        — Beta-Binomial + Normal-Inverse-Gamma online posteriors
  JamesSteinShrink    — proven MSE-dominant small-sample shrinkage (Stein 1956)
  ThorpKelly          — estimation-error-corrected fractional Kelly (Thorp 2006)
  GrossmanZhouDD      — drawdown-constrained optimal growth (Grossman-Zhou 1993)
  CVaRCap             — coherent expected-shortfall constraint (Rockafellar-Uryasev 2000)
  BayesianSizer       — composes the full sizing stack

All pure Python / NumPy. Vectorised where hot. Designed to be ported to Rust
later (see rust_core/src/momentum_kernel.rs) without any algorithmic change.
"""

from .kalman import KalmanForecast
from .cusum import CUSUMDetector
from .hawkes import HawkesIntensity
from .optimal_stop import OptimalStopper
from .garch import GarchOne
from .gpd import GpdTail
from .microstructure import MicrostructureCluster
from .evt_stop import EVTGarchStop
from .bayesian_edge import BayesianEdge, JamesSteinShrink
from .kelly import ThorpKelly, GrossmanZhouDD, CVaRCap
from .sizer import BayesianSizer
from .orb import ORBConfig, ORB_DEFAULTS, OpeningRangeTracker, NRFilter

__all__ = [
    "KalmanForecast",
    "CUSUMDetector",
    "HawkesIntensity",
    "OptimalStopper",
    "GarchOne",
    "GpdTail",
    "MicrostructureCluster",
    "EVTGarchStop",
    "BayesianEdge",
    "JamesSteinShrink",
    "ThorpKelly",
    "GrossmanZhouDD",
    "CVaRCap",
    "BayesianSizer",
    "ORBConfig",
    "ORB_DEFAULTS",
    "OpeningRangeTracker",
    "NRFilter",
]


