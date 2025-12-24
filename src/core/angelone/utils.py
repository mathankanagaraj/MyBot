# core/angelone/utils.py
"""
Utility functions specific to Angel One/NSE market trading.
Handles NSE market hours checking and timezone conversions.
"""
from datetime import datetime, time as dtime
import pytz

from core.config import (
    NSE_MARKET_OPEN_HOUR,
    NSE_MARKET_OPEN_MINUTE,
    NSE_MARKET_CLOSE_HOUR,
    NSE_MARKET_CLOSE_MINUTE,
)
from core.logger import logger

# Timezone constants
IST = pytz.timezone("Asia/Kolkata")


def get_ist_now():
    """Get current time in IST timezone"""
    return datetime.now(IST)


def utc_to_ist(utc_dt):
    """
    Convert UTC datetime to IST datetime for display.

    Args:
        utc_dt: datetime object (naive or UTC-aware)

    Returns:
        IST-aware datetime
    """
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=pytz.utc)
    return utc_dt.astimezone(IST)


def is_market_open(now_utc=None):
    """
    Check if NSE market is open.
    NSE Trading Hours: Monday-Friday, 9:15 AM - 3:30 PM IST
    Also checks for NSE holidays (Republic Day, Independence Day, Diwali, etc.)
    """
    if not now_utc:
        now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    now_ist = now_utc.astimezone(IST)

    # Check if weekend
    if now_ist.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False

    # Check if NSE holiday
    try:
        from core.holiday_checker import is_nse_trading_day
        if not is_nse_trading_day(now_ist):
            logger.debug(
                "NSE Market Closed: Holiday detected on %s",
                now_ist.strftime("%Y-%m-%d %A")
            )
            return False
    except Exception as e:
        logger.warning(f"Holiday check failed, assuming trading day: {e}")

    # NSE market hours: 9:15 AM - 3:30 PM IST
    open_t = dtime(NSE_MARKET_OPEN_HOUR, NSE_MARKET_OPEN_MINUTE)
    close_t = dtime(NSE_MARKET_CLOSE_HOUR, NSE_MARKET_CLOSE_MINUTE)

    # Strict check: Open if time is >= 9:15 AND < 15:30
    # We use < 15:30 because at 15:30:00 market is technically closed for new candle formation
    is_open = open_t <= now_ist.time() < close_t

    if not is_open:
        logger.debug(
            "NSE Market Closed Check: IST=%s (Weekday=%s) Open=%s Close=%s",
            now_ist,
            now_ist.weekday(),
            open_t,
            close_t,
        )

    return is_open


def get_seconds_until_market_close(now_utc=None):
    """
    Calculate seconds until NSE market close (3:30 PM IST).
    If already past market close, returns seconds until next trading day's close.
    Skips weekends and NSE holidays.

    Returns:
        Number of seconds until market close
    """
    from datetime import timedelta

    if not now_utc:
        now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    now_ist = now_utc.astimezone(IST)

    # Create market close time for today
    close_time = now_ist.replace(
        hour=NSE_MARKET_CLOSE_HOUR,
        minute=NSE_MARKET_CLOSE_MINUTE,
        second=0,
        microsecond=0,
    )

    # If we're past market close today, target next trading day
    if now_ist >= close_time:
        try:
            from core.holiday_checker import get_next_nse_trading_day
            # Get next trading day (skips weekends and holidays)
            next_day = get_next_nse_trading_day(now_ist)
            close_time = next_day.replace(
                hour=NSE_MARKET_CLOSE_HOUR,
                minute=NSE_MARKET_CLOSE_MINUTE,
                second=0,
                microsecond=0,
            )
        except Exception as e:
            logger.warning(f"Holiday check failed, using simple date logic: {e}")
            # Fallback: Move to next day
            close_time += timedelta(days=1)
            # Skip weekends
            while close_time.weekday() >= 5:
                close_time += timedelta(days=1)

    # Calculate seconds difference
    seconds = int((close_time - now_ist).total_seconds())
    return max(seconds, 0)

    # If we're past market close today, target next trading day
    if now_ist >= close_time:
        # Move to next day
        close_time += timedelta(days=1)

        # Skip weekends
        while close_time.weekday() >= 5:  # Saturday = 5, Sunday = 6
            close_time += timedelta(days=1)

    # Calculate seconds difference
    seconds = (close_time - now_ist).total_seconds()
    return max(0, int(seconds))
