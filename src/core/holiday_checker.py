# core/holiday_checker.py
"""
Holiday checker for NSE (India) and US stock markets.
Uses pandas_market_calendars for official exchange holiday data.

Supports:
- NSE (National Stock Exchange of India)
- NYSE/NASDAQ (US markets)
- Automatic timezone handling
- Caching for performance
"""

from datetime import datetime, timedelta
from typing import Optional
import pytz

from core.logger import logger

# Lazy import to avoid startup failures if library not installed
_mcal = None
_nse_calendar = None
_nyse_calendar = None

# Manual NSE holiday overrides (pandas_market_calendars NSE data is incomplete)
# Source: https://www.nseindia.com/resources/exchange-communication-holidays
NSE_HOLIDAY_OVERRIDES = {
    # 2025 NSE Holidays
    datetime(2025, 1, 26).date(),  # Republic Day
    datetime(2025, 2, 26).date(),  # Maha Shivaratri
    datetime(2025, 3, 14).date(),  # Holi
    datetime(2025, 3, 31).date(),  # Id-Ul-Fitr (Ramzan Id)
    datetime(2025, 4, 10).date(),  # Mahavir Jayanti
    datetime(2025, 4, 14).date(),  # Dr. Baba Saheb Ambedkar Jayanti
    datetime(2025, 4, 18).date(),  # Good Friday
    datetime(2025, 5, 1).date(),   # Maharashtra Day
    datetime(2025, 6, 7).date(),   # Id-Ul-Adha (Bakri Id)
    datetime(2025, 8, 15).date(),  # Independence Day
    datetime(2025, 8, 27).date(),  # Ganesh Chaturthi
    datetime(2025, 10, 2).date(),  # Mahatma Gandhi Jayanti
    datetime(2025, 10, 21).date(), # Dussehra
    datetime(2025, 10, 22).date(), # Diwali - Laxmi Pujan
    datetime(2025, 11, 5).date(),  # Diwali - Balipratipada
    datetime(2025, 11, 24).date(), # Gurunanak Jayanti
    datetime(2025, 12, 25).date(), # Christmas
    # 2026 NSE Holidays (partial - update as announced)
    datetime(2026, 1, 26).date(),  # Republic Day
    datetime(2026, 12, 25).date(), # Christmas
}


def _get_mcal():
    """Lazy load pandas_market_calendars"""
    global _mcal
    if _mcal is None:
        try:
            import pandas_market_calendars as mcal
            _mcal = mcal
        except ImportError:
            logger.error(
                "pandas_market_calendars not installed. "
                "Install with: pip install pandas-market-calendars"
            )
            raise
    return _mcal


def _get_nse_calendar():
    """Get NSE calendar instance (cached)"""
    global _nse_calendar
    if _nse_calendar is None:
        mcal = _get_mcal()
        try:
            _nse_calendar = mcal.get_calendar("NSE")
            logger.info("✅ NSE holiday calendar loaded")
        except Exception as e:
            logger.error(f"Failed to load NSE calendar: {e}")
            raise
    return _nse_calendar


def _get_nyse_calendar():
    """Get NYSE calendar instance (cached)"""
    global _nyse_calendar
    if _nyse_calendar is None:
        mcal = _get_mcal()
        try:
            _nyse_calendar = mcal.get_calendar("NYSE")
            logger.info("✅ NYSE holiday calendar loaded")
        except Exception as e:
            logger.error(f"Failed to load NYSE calendar: {e}")
            raise
    return _nyse_calendar


def is_nse_trading_day(date: Optional[datetime] = None) -> bool:
    """
    Check if NSE is open for trading on a given date.
    
    Args:
        date: Datetime to check (default: today in IST)
    
    Returns:
        True if NSE is open, False if holiday/weekend
    
    Examples:
        >>> is_nse_trading_day()  # Check today
        True
        >>> from datetime import datetime
        >>> is_nse_trading_day(datetime(2025, 1, 26))  # Republic Day
        False
    """
    if date is None:
        # Use current date in IST
        ist = pytz.timezone("Asia/Kolkata")
        date = datetime.now(ist)
    
    # Ensure timezone-aware
    if date.tzinfo is None:
        ist = pytz.timezone("Asia/Kolkata")
        date = ist.localize(date)
    
    # Convert to date for comparison
    check_date = date.date() if isinstance(date, datetime) else date
    
    # Check manual overrides first (more accurate than library)
    if check_date in NSE_HOLIDAY_OVERRIDES:
        logger.info(f"🚫 NSE Holiday detected (manual override): {check_date.strftime('%Y-%m-%d %A')}")
        return False
    
    # Weekend check
    if date.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False
    
    try:
        calendar = _get_nse_calendar()
        
        # Get schedule for this date
        schedule = calendar.schedule(
            start_date=check_date.strftime("%Y-%m-%d"),
            end_date=check_date.strftime("%Y-%m-%d")
        )
        
        # If schedule is empty, market is closed
        is_open = len(schedule) > 0
        
        if not is_open:
            logger.info(f"🚫 NSE Holiday/Weekend detected (library): {check_date.strftime('%Y-%m-%d %A')}")
        
        return is_open
        
    except Exception as e:
        logger.error(f"Error checking NSE trading day: {e}")
        # Fallback: assume open on weekdays if not in manual overrides
        return date.weekday() < 5


def is_us_trading_day(date: Optional[datetime] = None) -> bool:
    """
    Check if US markets (NYSE/NASDAQ) are open for trading on a given date.
    
    Args:
        date: Datetime to check (default: today in ET)
    
    Returns:
        True if US markets are open, False if holiday/weekend
    
    Examples:
        >>> is_us_trading_day()  # Check today
        True
        >>> from datetime import datetime
        >>> is_us_trading_day(datetime(2025, 12, 25))  # Christmas
        False
    """
    if date is None:
        # Use current date in ET
        us_et = pytz.timezone("America/New_York")
        date = datetime.now(us_et)
    
    # Ensure timezone-aware
    if date.tzinfo is None:
        us_et = pytz.timezone("America/New_York")
        date = us_et.localize(date)
    
    try:
        calendar = _get_nyse_calendar()
        
        # Get schedule for this date
        schedule = calendar.schedule(
            start_date=date.strftime("%Y-%m-%d"),
            end_date=date.strftime("%Y-%m-%d")
        )
        
        # If schedule is empty, market is closed
        is_open = len(schedule) > 0
        
        if not is_open:
            logger.info(f"🚫 US Market Holiday/Weekend detected: {date.strftime('%Y-%m-%d %A')}")
        
        return is_open
        
    except Exception as e:
        logger.error(f"Error checking US trading day: {e}")
        # Fallback: assume open on weekdays
        return date.weekday() < 5


def get_next_nse_trading_day(from_date: Optional[datetime] = None) -> datetime:
    """
    Get the next NSE trading day after from_date.
    
    Args:
        from_date: Starting date (default: today in IST)
    
    Returns:
        Next trading day as datetime in IST
    """
    if from_date is None:
        ist = pytz.timezone("Asia/Kolkata")
        from_date = datetime.now(ist)
    
    # Ensure timezone-aware
    if from_date.tzinfo is None:
        ist = pytz.timezone("Asia/Kolkata")
        from_date = ist.localize(from_date)
    
    try:
        calendar = _get_nse_calendar()
        
        # Get next 30 days of trading
        end_date = from_date + timedelta(days=30)
        schedule = calendar.schedule(
            start_date=from_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d")
        )
        
        if len(schedule) == 0:
            # No trading days in next 30 days (unlikely)
            logger.warning("No NSE trading days found in next 30 days")
            return from_date + timedelta(days=1)
        
        # Return first trading day
        next_day = schedule.index[0].to_pydatetime()
        
        # Ensure IST timezone
        ist = pytz.timezone("Asia/Kolkata")
        if next_day.tzinfo is None:
            next_day = ist.localize(next_day)
        else:
            next_day = next_day.astimezone(ist)
        
        return next_day
        
    except Exception as e:
        logger.error(f"Error getting next NSE trading day: {e}")
        # Fallback: skip weekends
        next_day = from_date + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        return next_day


def get_next_us_trading_day(from_date: Optional[datetime] = None) -> datetime:
    """
    Get the next US trading day after from_date.
    
    Args:
        from_date: Starting date (default: today in ET)
    
    Returns:
        Next trading day as datetime in ET
    """
    if from_date is None:
        us_et = pytz.timezone("America/New_York")
        from_date = datetime.now(us_et)
    
    # Ensure timezone-aware
    if from_date.tzinfo is None:
        us_et = pytz.timezone("America/New_York")
        from_date = us_et.localize(from_date)
    
    try:
        calendar = _get_nyse_calendar()
        
        # Get next 30 days of trading
        end_date = from_date + timedelta(days=30)
        schedule = calendar.schedule(
            start_date=from_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d")
        )
        
        if len(schedule) == 0:
            # No trading days in next 30 days (unlikely)
            logger.warning("No US trading days found in next 30 days")
            return from_date + timedelta(days=1)
        
        # Return first trading day
        next_day = schedule.index[0].to_pydatetime()
        
        # Ensure ET timezone
        us_et = pytz.timezone("America/New_York")
        if next_day.tzinfo is None:
            next_day = us_et.localize(next_day)
        else:
            next_day = next_day.astimezone(us_et)
        
        return next_day
        
    except Exception as e:
        logger.error(f"Error getting next US trading day: {e}")
        # Fallback: skip weekends
        next_day = from_date + timedelta(days=1)
        while next_day.weekday() >= 5:
            next_day += timedelta(days=1)
        return next_day


def get_upcoming_nse_holidays(days: int = 30) -> list:
    """
    Get list of upcoming NSE holidays.
    
    Args:
        days: Number of days to look ahead (default: 30)
    
    Returns:
        List of holiday dates with names
    """
    from datetime import date as date_type
    import pandas as pd
    
    ist = pytz.timezone("Asia/Kolkata")
    today = datetime.now(ist).date()
    end_date = today + timedelta(days=days)
    
    # Start with manual overrides
    upcoming = []
    for holiday_date in sorted(NSE_HOLIDAY_OVERRIDES):
        if today <= holiday_date <= end_date:
            upcoming.append((holiday_date, "NSE Holiday"))
    
    # Also check library for any additional holidays
    try:
        calendar = _get_nse_calendar()
        holidays = calendar.holidays()
        
        # Convert tuple of numpy datetime64 to list of datetime.date
        for holiday in holidays.holidays:
            # Convert numpy.datetime64 to pandas Timestamp to datetime.date
            holiday_date = pd.Timestamp(holiday).date()
            if today <= holiday_date <= end_date:
                # Only add if not already in manual overrides
                if holiday_date not in NSE_HOLIDAY_OVERRIDES:
                    upcoming.append((holiday_date, "NSE Holiday (library)"))
        
    except Exception as e:
        logger.error(f"Error getting NSE holidays from library: {e}")
    
    # Sort by date and remove duplicates
    upcoming = sorted(set(upcoming), key=lambda x: x[0])
    return upcoming


def get_upcoming_us_holidays(days: int = 30) -> list:
    """
    Get list of upcoming US market holidays.
    
    Args:
        days: Number of days to look ahead (default: 30)
    
    Returns:
        List of holiday dates with names
    """
    from datetime import date as date_type
    import pandas as pd
    
    us_et = pytz.timezone("America/New_York")
    today = datetime.now(us_et).date()
    end_date = today + timedelta(days=days)
    
    try:
        calendar = _get_nyse_calendar()
        holidays = calendar.holidays()
        
        # Convert tuple of numpy datetime64 to list of datetime.date
        upcoming = []
        for holiday in holidays.holidays:
            # Convert numpy.datetime64 to pandas Timestamp to datetime.date
            holiday_date = pd.Timestamp(holiday).date()
            if today <= holiday_date <= end_date:
                upcoming.append((holiday_date, "US Market Holiday"))
        
        return upcoming
        
    except Exception as e:
        logger.error(f"Error getting US holidays: {e}")
        return []


def get_us_market_close_time(date: Optional[datetime] = None) -> datetime:
    """
    Get the market close time for a given date (handles early closes).
    
    Args:
        date: Date to check (default: today in ET)
    
    Returns:
        Datetime of market close in ET timezone
    
    Examples:
        >>> from datetime import datetime
        >>> close_time = get_us_market_close_time(datetime(2025, 12, 24))
        >>> close_time.hour  # 13 (1 PM early close on Christmas Eve)
        13
    """
    if date is None:
        us_et = pytz.timezone("America/New_York")
        date = datetime.now(us_et)
    
    # Ensure timezone-aware
    if date.tzinfo is None:
        us_et = pytz.timezone("America/New_York")
        date = us_et.localize(date)
    
    try:
        calendar = _get_nyse_calendar()
        
        # Get schedule for this date
        schedule = calendar.schedule(
            start_date=date.strftime("%Y-%m-%d"),
            end_date=date.strftime("%Y-%m-%d")
        )
        
        if len(schedule) == 0:
            # Market closed - return None or default 4 PM
            logger.warning(f"Market closed on {date.strftime('%Y-%m-%d')}")
            us_et = pytz.timezone("America/New_York")
            return date.replace(hour=16, minute=0, second=0, microsecond=0, tzinfo=us_et)
        
        # Get market close time from schedule
        close_time = schedule.iloc[0]['market_close']
        
        # Convert to datetime and ensure ET timezone
        us_et = pytz.timezone("America/New_York")
        if hasattr(close_time, 'to_pydatetime'):
            close_time = close_time.to_pydatetime()
        
        if close_time.tzinfo is None:
            close_time = us_et.localize(close_time)
        else:
            close_time = close_time.astimezone(us_et)
        
        # Log if early close
        if close_time.hour < 16:
            logger.info(
                f"📅 Early Close Detected: {date.strftime('%Y-%m-%d %A')} "
                f"closes at {close_time.strftime('%I:%M %p ET')}"
            )
        
        return close_time
        
    except Exception as e:
        logger.error(f"Error getting US market close time: {e}")
        # Default to 4 PM ET
        us_et = pytz.timezone("America/New_York")
        return date.replace(hour=16, minute=0, second=0, microsecond=0, tzinfo=us_et)


def is_us_early_close_day(date: Optional[datetime] = None) -> bool:
    """
    Check if US market has an early close on a given date.
    
    Args:
        date: Date to check (default: today in ET)
    
    Returns:
        True if market closes before 4 PM ET
    """
    close_time = get_us_market_close_time(date)
    return close_time.hour < 16
