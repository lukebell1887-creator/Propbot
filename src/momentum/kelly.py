"""
ThorpKelly / GrossmanZhouDD / CVaRCap — sizing sub-components.

ThorpKelly      — estimation-error-corrected fractional Kelly
                  (Thorp 2006 The Kelly Criterion in Blackjack Sports Betting
                   and the Stock Market; MacLean-Thorp-Ziemba 2011).

GrossmanZhouDD  — closed-form drawdown-constrained growth factor
                  (Grossman & Zhou 1993 *Math. Finance* 3(3), 241-276).

CVaRCap         — expected-shortfall (CVaR) constraint
                  (Rockafellar & Uryasev 2000 *J. Risk* 2(3), 21-41).
"""

from __future__ import annotations
import math


# ----------------------------------------------------------------------
#  Thorp-corrected fractional Kelly
# ----------------------------------------------------------------------

class ThorpKelly:
    """
    Takes posterior point estimates and variances, returns the shrunk
    fractional Kelly bet.

        f_naive  = (p (W+L) - L) / (W L)                            # textbook Kelly
        shrink   = 1  -  2 Var(p) / (p (1-p))
                         -    Var(μ_R) / μ_R²                        # error correction
        f_bayes  = f_naive * max(0, shrink) * fractional              # fractional ≤ 1

    Default `fractional = 0.5` (masterplan §4.3).
    """

    def __init__(self, fractional: float = 0.5):
        if not 0.0 < fractional <= 1.0:
            raise ValueError("fractional must be in (0,1]")
        self._frac = fractional

    @staticmethod
    def _naive(p: float, win_r: float, loss_r: float) -> float:
        """Kelly for asymmetric payout.  W = avg winner in R, L = avg loser in R (positive)."""
        if win_r <= 0 or loss_r <= 0:
            return 0.0
        return max(0.0, (p * (win_r + loss_r) - loss_r) / (win_r * loss_r))

    def fraction(self,
                 p: float, p_var: float,
                 mu_R: float, mu_R_var: float,
                 win_R: float = 1.5, loss_R: float = 1.0) -> float:
        """
        Return recommended Kelly fraction of equity.
        Clamped to [0, fractional].
        """
        f_naive = self._naive(p, win_R, loss_R)
        if f_naive <= 0.0:
            return 0.0
        denom_p = max(1e-6, p * (1.0 - p))
        denom_R = max(1e-6, mu_R * mu_R)
        shrink = 1.0 - 2.0 * p_var / denom_p - mu_R_var / denom_R
        shrink = max(0.0, shrink)
        return min(self._frac, f_naive * shrink * self._frac)


# ----------------------------------------------------------------------
#  Grossman-Zhou drawdown factor
# ----------------------------------------------------------------------

class GrossmanZhouDD:
    """
    Smooth [0, 1] factor that collapses to 0 at the barrier and 1 at full headroom.

    Masterplan §4.5 formulation:

        remaining = (equity - barrier) / equity            (fractional headroom)
        GZ        = 1 - (1 - remaining / MAX_DD)^γ         γ = 2 (default)

    At MAX_DD headroom → GZ = 1.  At the barrier → GZ = 0.
    """

    def __init__(self, max_dd: float = 0.09, gamma: float = 2.0):
        if not 0.0 < max_dd < 1.0:
            raise ValueError("max_dd in (0,1)")
        if gamma <= 0.0:
            raise ValueError("gamma > 0")
        self._max_dd = max_dd
        self._gamma = gamma

    def factor(self, equity: float, peak: float) -> float:
        if peak <= 0 or equity <= 0:
            return 0.0
        barrier = peak * (1.0 - self._max_dd)
        if equity <= barrier:
            return 0.0
        remaining = (equity - barrier) / equity
        x = remaining / self._max_dd
        x = max(0.0, min(1.0, x))
        return 1.0 - (1.0 - x) ** self._gamma


# ----------------------------------------------------------------------
#  CVaR (Expected Shortfall) cap
# ----------------------------------------------------------------------

def _normal_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _normal_cdf_inv(p: float) -> float:
    """
    Beasley-Springer-Moro rational approximation to the normal inverse-CDF.
    Good to ~1e-7 — more than enough for a sizing factor.
    """
    a = [-3.969683028665376e+01, 2.209460984245205e+02,
         -2.759285104469687e+02, 1.383577518672690e+02,
         -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02,
         -1.556989798598866e+02, 6.680131188771972e+01,
         -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01,
         -2.400758277161838e+00, -2.549732539343734e+00,
          4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01,
         2.445134137142996e+00, 3.754408661907416e+00]
    p = max(1e-12, min(1.0 - 1e-12, p))
    q = p - 0.5
    if abs(q) <= 0.425:
        r = q * q
        return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5]) * q / (
               (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1.0))
    r = p if q < 0 else 1.0 - p
    r = math.sqrt(-math.log(r))
    val = (((((c[0]*r+c[1])*r+c[2])*r+c[3])*r+c[4])*r+c[5]) / (
            (((d[0]*r+d[1])*r+d[2])*r+d[3])*r+1.0)
    return val if q >= 0 else -val


class CVaRCap:
    """
    Given posterior mean μ_R and variance σ_R², compute the expected shortfall
    at level α (default 5% left tail) and return the multiplicative factor
    that, when applied to a candidate risk fraction, keeps |ES| ≤ cap · equity.

    Closed-form for Gaussian:

        ES_α(X)  =  -μ + σ · φ(Φ⁻¹(α)) / α

    where X ~ N(μ, σ²).  This is exactly the Rockafellar-Uryasev ES formula
    specialised to a Gaussian posterior predictive.
    """

    def __init__(self, alpha: float = 0.05, cap: float = 0.02):
        if not 0.0 < alpha < 0.5:
            raise ValueError("alpha ∈ (0, 0.5)")
        if not 0.0 < cap < 1.0:
            raise ValueError("cap ∈ (0, 1)")
        self._alpha = alpha
        self._cap = cap

    def factor(self, mu_R: float, sigma_R: float,
               candidate_risk_frac: float) -> float:
        """
        `candidate_risk_frac` is the pre-CVaR Kelly fraction (of equity per R).
        We compute the per-trade ES in R units, scale by candidate fraction to
        get ES in equity units, and shrink until ES ≤ cap.
        """
        if candidate_risk_frac <= 0 or sigma_R <= 0:
            return 1.0
        z = _normal_cdf_inv(self._alpha)
        es_R = -mu_R + sigma_R * _normal_pdf(z) / self._alpha
        if es_R <= 0:
            return 1.0
        es_equity = candidate_risk_frac * es_R
        if es_equity <= self._cap:
            return 1.0
        return max(1e-6, self._cap / es_equity)
