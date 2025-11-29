# core/cash_manager.py
from dataclasses import dataclass, field
from typing import Dict

from core.logger import logger


@dataclass
class LiveCashManager:
    """
    Live cash manager for Angel Broker.
    Tracks positions, enforces risk limits, and monitors daily P&L.
    """

    max_alloc_pct: float = 0.70
    max_daily_loss: float = 5000.0  # ₹5,000 daily loss limit
    max_position_size: float = 50000.0  # ₹50,000 max per position
    open_positions: Dict[str, float] = field(default_factory=dict)
    daily_pnl: float = 0.0

    def __init__(self, angel_client, max_alloc_pct=0.70, max_daily_loss=5000.0, max_position_size=50000.0):
        self.angel_client = angel_client
        self.max_alloc_pct = max_alloc_pct
        self.max_daily_loss = max_daily_loss
        self.max_position_size = max_position_size
        self.open_positions = {}
        self.daily_pnl = 0.0

    async def available_exposure(self):
        """
        Calculate available exposure based on account funds and risk limits.

        Returns:
            Available exposure in ₹
        """
        try:
            summary = await self.angel_client.get_account_summary_async()
            avail_funds = float(summary.get("AvailableFunds", 0))

            # Maximum we can allocate
            max_allocation = avail_funds * self.max_alloc_pct

            # Subtract currently used exposure
            current_exposure = sum(self.open_positions.values())

            # Check daily loss limit
            if abs(self.daily_pnl) >= self.max_daily_loss:
                logger.warning(f"Daily loss limit reached: ₹{self.daily_pnl:.2f}")
                return 0.0

            available = max(0.0, max_allocation - current_exposure)

            return available

        except Exception as e:
            logger.exception(f"Error calculating available exposure: {e}")
            return 0.0

    async def can_open_position(self, symbol: str, cost: float) -> bool:
        """
        Check if we can open a new position.

        Args:
            symbol: Trading symbol
            cost: Estimated cost of position

        Returns:
            True if position can be opened
        """
        # Check if position already exists
        if symbol in self.open_positions:
            logger.warning(f"Position already exists for {symbol}")
            return False

        # Check if cost is valid
        if cost <= 0:
            logger.warning(f"Invalid cost: ₹{cost}")
            return False

        # Check position size limit
        if cost > self.max_position_size:
            logger.warning(f"Position size ₹{cost:.2f} exceeds limit ₹{self.max_position_size:.2f}")
            return False

        # Check daily loss limit
        if abs(self.daily_pnl) >= self.max_daily_loss:
            logger.warning(f"Daily loss limit reached: ₹{self.daily_pnl:.2f}")
            return False

        # Check available exposure
        available = await self.available_exposure()

        if cost > available:
            logger.warning(f"Insufficient exposure: need ₹{cost:.2f}, available ₹{available:.2f}")
            return False

        return True

    def register_open(self, symbol: str, cost: float) -> bool:
        """
        Register a new open position.

        Args:
            symbol: Trading symbol
            cost: Position cost

        Returns:
            True if registered successfully
        """
        if symbol in self.open_positions:
            logger.error(f"Position already exists for {symbol}")
            return False

        self.open_positions[symbol] = cost
        logger.info(f"Registered open position: {symbol} @ ₹{cost:.2f}")
        return True

    def register_close(self, symbol: str, exit_value: float) -> float:
        """
        Register position closure and calculate P&L.

        Args:
            symbol: Trading symbol
            exit_value: Exit value of position

        Returns:
            P&L for this trade
        """
        entry_cost = self.open_positions.pop(symbol, 0.0)
        pnl = float(exit_value) - float(entry_cost)

        # Update daily P&L
        self.daily_pnl += pnl

        logger.info(f"Closed position: {symbol} | Entry: ₹{entry_cost:.2f} | Exit: ₹{exit_value:.2f} | P&L: ₹{pnl:.2f}")
        logger.info(f"Daily P&L: ₹{self.daily_pnl:.2f}")

        return pnl

    def force_release(self, symbol: str):
        """
        Force release a position (e.g., if position closed externally).

        Args:
            symbol: Trading symbol
        """
        if symbol in self.open_positions:
            cost = self.open_positions.pop(symbol)
            logger.info(f"Force released position: {symbol} @ ₹{cost:.2f}")

    def reset_daily_pnl(self):
        """Reset daily P&L counter (call at start of each trading day)"""
        logger.info(f"Resetting daily P&L. Previous: ₹{self.daily_pnl:.2f}")
        self.daily_pnl = 0.0

    def get_daily_pnl(self) -> float:
        """Get current daily P&L"""
        return self.daily_pnl


def create_cash_manager(angel_client, max_alloc_pct=0.70, max_daily_loss=5000.0, max_position_size=50000.0):
    """
    Create cash manager for Angel Broker (LIVE only).

    Args:
        angel_client: AngelClient instance
        max_alloc_pct: Maximum allocation percentage
        max_daily_loss: Maximum daily loss in ₹
        max_position_size: Maximum position size in ₹

    Returns:
        LiveCashManager instance
    """
    return LiveCashManager(
        angel_client=angel_client,
        max_alloc_pct=max_alloc_pct,
        max_daily_loss=max_daily_loss,
        max_position_size=max_position_size,
    )
