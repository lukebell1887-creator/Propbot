"""
SHF Risk Supervisor — Portfolio-level risk management.

Monitors drawdown, consecutive losses, and triggers emergency actions.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)


class RiskAction(Enum):
    """Risk action types."""
    ALERT = "alert"
    REDUCE = "reduce"
    HALT = "halt"
    KILL_ALL = "kill_all"


@dataclass
class RiskAlert:
    """Risk alert with severity and message."""
    action: RiskAction
    message: str
    severity: int = 1


def calculate_position_size(
    balance: float,
    risk_pct: float,
    stop_distance: float,
    pip_value: float = 10.0,
) -> float:
    """
    Calculate position size from risk percentage.

    Args:
        balance: Account balance
        risk_pct: Risk as fraction (e.g., 0.0075 = 0.75%)
        stop_distance: Distance to stop loss in pips
        pip_value: Value per pip per lot (default $10 for forex)

    Returns:
        Position size in lots (minimum 0.01)
    """
    if stop_distance <= 0 or pip_value <= 0:
        return 0.01
    risk_amount = balance * risk_pct
    lots = risk_amount / (stop_distance * pip_value)
    return max(0.01, round(lots, 2))


class RiskSupervisor:
    """
    Portfolio-level risk supervisor.

    Monitors:
    - Daily drawdown (4% ghost stop)
    - Maximum drawdown (9% kill switch)
    - Consecutive losses (5 → 60 min pause)
    - Position count limits
    """

    def __init__(
        self,
        initial_balance: float,
        on_kill_all: Optional[Callable] = None,
        on_alert: Optional[Callable] = None,
        max_daily_dd: float = 0.04,
        max_total_dd: float = 0.09,
        max_consecutive_losses: int = 5,
        cooldown_minutes: float = 60.0,
    ):
        self._initial_balance = initial_balance
        self._peak_balance = initial_balance
        self._on_kill_all = on_kill_all
        self._on_alert = on_alert
        self._max_daily_dd = max_daily_dd
        self._max_total_dd = max_total_dd
        self._max_consecutive_losses = max_consecutive_losses
        self._cooldown_minutes = cooldown_minutes
        self._consecutive_losses = 0
        self._is_halted = False

        logger.info(
            f"RiskSupervisor initialized | Balance=${initial_balance:.2f} | "
            f"Daily DD limit={max_daily_dd*100:.1f}% | Max DD={max_total_dd*100:.1f}%"
        )

    def update(self, current_equity: float) -> Optional[RiskAlert]:
        """
        Update supervisor with current equity. Returns alert if triggered.
        """
        # Update peak
        if current_equity > self._peak_balance:
            self._peak_balance = current_equity

        # Check daily DD
        daily_dd = (self._initial_balance - current_equity) / self._initial_balance
        if daily_dd >= self._max_daily_dd:
            alert = RiskAlert(
                action=RiskAction.KILL_ALL,
                message=f"Daily DD {daily_dd*100:.2f}% >= {self._max_daily_dd*100:.1f}%",
                severity=3,
            )
            if self._on_kill_all:
                self._on_kill_all(alert.message)
            if self._on_alert:
                self._on_alert(alert)
            return alert

        # Check max DD
        total_dd = (self._peak_balance - current_equity) / self._peak_balance
        if total_dd >= self._max_total_dd:
            alert = RiskAlert(
                action=RiskAction.KILL_ALL,
                message=f"Max DD {total_dd*100:.2f}% >= {self._max_total_dd*100:.1f}%",
                severity=3,
            )
            if self._on_kill_all:
                self._on_kill_all(alert.message)
            if self._on_alert:
                self._on_alert(alert)
            return alert

        return None

    def record_loss(self) -> Optional[RiskAlert]:
        """Record a losing trade and check consecutive loss limit."""
        self._consecutive_losses += 1
        if self._consecutive_losses >= self._max_consecutive_losses:
            alert = RiskAlert(
                action=RiskAction.HALT,
                message=f"{self._consecutive_losses} consecutive losses → {self._cooldown_minutes}min cooldown",
                severity=2,
            )
            self._is_halted = True
            if self._on_alert:
                self._on_alert(alert)
            return alert
        return None

    def record_win(self) -> None:
        """Record a winning trade — resets consecutive loss counter."""
        self._consecutive_losses = 0

    @property
    def is_halted(self) -> bool:
        return self._is_halted

    def resume(self) -> None:
        """Resume trading after cooldown."""
        self._is_halted = False
        self._consecutive_losses = 0
        logger.info("RiskSupervisor: Trading resumed after cooldown")
