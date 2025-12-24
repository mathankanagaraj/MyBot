# core/ibkr_utils.py
"""
Utility functions specific to IBKR/US market trading.
Handles US market hours checking and timezone conversions.
"""
from datetime import datetime, time as dtime
import pytz

from core.config import (
    US_MARKET_OPEN_HOUR,
    US_MARKET_OPEN_MINUTE,
    US_MARKET_CLOSE_HOUR,
    US_MARKET_CLOSE_MINUTE,
    IBKR_TIMEZONE,
)
from core.logger import logger


# US Eastern timezone
US_ET = pytz.timezone(IBKR_TIMEZONE)


def get_us_et_now():
    """Get current time in US Eastern timezone"""
    return datetime.now(US_ET)


def is_us_market_open(now_utc=None):
    """
    Check if US market is open.
    US Trading Hours: Monday-Friday, 9:30 AM - 4:00 PM ET
    Also checks for US market holidays (Christmas, New Year, Thanksgiving, etc.)

    Args:
        now_utc: UTC datetime (optional, defaults to current time)

    Returns:
        Boolean indicating if market is open
    """
    if not now_utc:
        now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    now_et = now_utc.astimezone(US_ET)

    # Check if weekend
    if now_et.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False

    # Check if US market holiday
    try:
        from core.holiday_checker import is_us_trading_day
        if not is_us_trading_day(now_et):
            logger.debug(
                "US Market Closed: Holiday detected on %s",
                now_et.strftime("%Y-%m-%d %A")
            )
            return False
    except Exception as e:
        logger.warning(f"Holiday check failed, assuming trading day: {e}")

    # US market hours: 9:30 AM - 4:00 PM ET
    open_t = dtime(US_MARKET_OPEN_HOUR, US_MARKET_OPEN_MINUTE)
    close_t = dtime(US_MARKET_CLOSE_HOUR, US_MARKET_CLOSE_MINUTE)

    # Strict check: Open if time is >= 9:30 AND < 16:00
    is_open = open_t <= now_et.time() < close_t

    if not is_open:
        logger.debug(
            "US Market Closed Check: ET=%s (Weekday=%s) Open=%s Close=%s",
            now_et,
            now_et.weekday(),
            open_t,
            close_t,
        )

    return is_open


def get_us_market_close_time(now_et=None):
    """
    Get today's market close time in ET.

    Args:
        now_et: Current time in ET (optional)

    Returns:
        Market close datetime in ET
    """
    if not now_et:
        now_et = get_us_et_now()

    close_time = now_et.replace(
        hour=US_MARKET_CLOSE_HOUR,
        minute=US_MARKET_CLOSE_MINUTE,
        second=0,
        microsecond=0,
    )

    return close_time


def get_us_market_open_time(now_et=None):
    """
    Get today's market open time in ET.

    Args:
        now_et: Current time in ET (optional)

    Returns:
        Market open datetime in ET
    """
    if not now_et:
        now_et = get_us_et_now()

    open_time = now_et.replace(
        hour=US_MARKET_OPEN_HOUR, minute=US_MARKET_OPEN_MINUTE, second=0, microsecond=0
    )

    return open_time


def round_to_tick_size(price: float, min_tick: float) -> float:
    """
    Round price to the nearest valid tick size for the contract.
    
    Args:
        price: The price to round
        min_tick: The minimum tick increment for the contract (e.g., 0.05, 0.25)
    
    Returns:
        Price rounded to nearest valid tick
    
    Examples:
        >>> round_to_tick_size(18.67, 0.25)  # ES options
        18.75
        >>> round_to_tick_size(81.13, 0.05)  # NQ options
        81.15
        >>> round_to_tick_size(4.953, 0.01)  # Stock options
        4.95
    """
    if min_tick <= 0:
        return round(price, 2)  # Fallback to 2 decimals
    
    # Round to nearest tick and fix floating-point precision
    # Determine decimal places from tick size
    if min_tick >= 1:
        decimals = 0
    elif min_tick >= 0.1:
        decimals = 1
    elif min_tick >= 0.01:
        decimals = 2
    else:
        decimals = 3
    
    return round(round(price / min_tick) * min_tick, decimals)
