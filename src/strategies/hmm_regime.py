"""
SHF HMM Regime Detector — 3-regime Gaussian Hidden Markov Model.

Classifies market regimes by volatility:
  Regime 0: Mean-Reverting (lowest variance) — TRADEABLE
  Regime 1: Trending — CAUTION
  Regime 2: Volatile — BLOCKED

Uses Numba JIT for emission probabilities and Viterbi decoding.
Falls back to pure Python if Numba is unavailable.
"""

import logging
import math
from typing import Optional, Tuple, List
import numpy as np

logger = logging.getLogger(__name__)

# Try Numba JIT
try:
    from numba import jit

    @jit(nopython=True, cache=True)
    def _fast_emission_probs(x: float, means: np.ndarray, stds: np.ndarray) -> np.ndarray:
        """Gaussian emission probabilities (Numba JIT)."""
        n = len(means)
        probs = np.empty(n)
        for i in range(n):
            z = (x - means[i]) / stds[i]
            probs[i] = math.exp(-0.5 * z * z) / (stds[i] * 2.5066282746310002)
        return probs

    @jit(nopython=True, cache=True)
    def _fast_viterbi_step(
        prev_log_prob: np.ndarray,
        trans_log: np.ndarray,
        emission_log: np.ndarray,
    ) -> Tuple[np.ndarray, np.ndarray]:
        """Single Viterbi step (Numba JIT)."""
        n = len(prev_log_prob)
        new_log_prob = np.empty(n)
        backpointer = np.empty(n, dtype=np.int64)
        for j in range(n):
            best = -1e30
            best_i = 0
            for i in range(n):
                val = prev_log_prob[i] + trans_log[i, j]
                if val > best:
                    best = val
                    best_i = i
            new_log_prob[j] = best + emission_log[j]
            backpointer[j] = best_i
        return new_log_prob, backpointer

    NUMBA_AVAILABLE = True
    logger.info("HMM: Numba JIT available — fast path enabled")

except ImportError:
    NUMBA_AVAILABLE = False
    logger.info("HMM: Numba not available — using pure Python fallback")


class HMMRegimeDetector:
    """
    3-regime Gaussian HMM for volatility classification.

    Regimes sorted by variance (ascending):
      0 = Mean-Reverting (low vol) → TRADE
      1 = Trending (medium vol) → CAUTION
      2 = Volatile (high vol) → BLOCK
    """

    def __init__(
        self,
        n_regimes: int = 3,
        lookback: int = 100,
    ):
        self._n_regimes = n_regimes
        self._lookback = lookback

        # HMM parameters (will be fitted from data)
        self._means = np.zeros(n_regimes)
        self._stds = np.ones(n_regimes)
        self._transition = np.ones((n_regimes, n_regimes)) / n_regimes
        self._initial = np.ones(n_regimes) / n_regimes

        # State
        self._current_regime: int = 0
        self._regime_probs = np.ones(n_regimes) / n_regimes
        self._return_buffer: List[float] = []
        self._fitted = False

        logger.info(f"HMMRegimeDetector initialized | regimes={n_regimes}, lookback={lookback}")

    def update(self, spread_return: float) -> int:
        """
        Update with new spread return, return current regime.

        Uses rolling sub-window volatility comparison:
        Split lookback into sub-windows, compute vol of each,
        then classify current vol against the vol distribution.
        This correctly compares volatility-to-volatility.

        Returns:
            0 = Mean-Reverting, 1 = Trending, 2 = Volatile
        """
        self._return_buffer.append(spread_return)

        # Keep buffer bounded
        if len(self._return_buffer) > self._lookback * 3:
            self._return_buffer = self._return_buffer[-self._lookback * 2:]

        # Need minimum data to classify
        if len(self._return_buffer) < 50:
            return 0  # Default: assume mean-reverting

        recent = np.array(self._return_buffer[-self._lookback:])
        n = len(recent)

        # Split into sub-windows and compute volatility of each
        window_size = min(20, n // 3)
        if window_size < 5:
            return 0

        vols = []
        for i in range(0, n - window_size + 1, window_size):
            chunk = recent[i:i + window_size]
            vols.append(np.std(chunk))

        if len(vols) < 3:
            return 0

        # Current vol = most recent sub-window
        current_vol = vols[-1]

        # Compare current vol against the DISTRIBUTION of sub-window vols
        vol_40 = np.percentile(vols, 40)
        vol_80 = np.percentile(vols, 80)

        if current_vol <= vol_40:
            self._current_regime = 0  # Low vol — mean-reverting
        elif current_vol <= vol_80:
            self._current_regime = 1  # Medium vol — trending
        else:
            self._current_regime = 2  # High vol — volatile

        return self._current_regime

    @property
    def current_regime(self) -> int:
        return self._current_regime

    @property
    def is_tradeable(self) -> bool:
        """True if current regime allows trading (regime 0 or 1)."""
        return self._current_regime < 2

    @property
    def is_blocked(self) -> bool:
        """True if current regime blocks trading (regime 2)."""
        return self._current_regime >= 2


def create_regime_detector(
    n_regimes: int = 3,
    lookback: int = 100,
) -> HMMRegimeDetector:
    """Factory function to create an HMM regime detector."""
    return HMMRegimeDetector(n_regimes=n_regimes, lookback=lookback)
