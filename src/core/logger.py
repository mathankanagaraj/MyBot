# core/logger.py
import logging
import os
from datetime import datetime
import pytz
from core.config import ANGEL_LOG_FILE, IBKR_LOG_FILE, BROKER


class TimezoneFormatter(logging.Formatter):
    """Custom formatter that displays timestamps in a specified timezone"""

    def __init__(self, fmt=None, datefmt=None, tz="UTC"):
        super().__init__(fmt, datefmt)
        self.tz = pytz.timezone(tz)

    def formatTime(self, record, datefmt=None):
        """Convert timestamp to specified timezone"""
        dt = datetime.fromtimestamp(record.created, tz=pytz.UTC)
        localized = dt.astimezone(self.tz)
        return localized.strftime(datefmt or "%Y-%m-%d %H:%M:%S")


def setup_logging():
    """
    Setup logging with broker-specific log file.
    With separate containers, each container writes ONLY to its own log file:
    - Angel container → angel_bot.log (IST timezone)
    - IBKR container → ibkr_bot.log (ET timezone)
    """
    # Get log level from environment variable (default: INFO)
    log_level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    log_level_map = {
        "DEBUG": logging.DEBUG,
        "INFO": logging.INFO,
        "WARNING": logging.WARNING,
        "ERROR": logging.ERROR,
        "CRITICAL": logging.CRITICAL,
    }
    log_level = log_level_map.get(log_level_str, logging.INFO)

    # Create main logger
    logger = logging.getLogger("intraday_bot")
    logger.setLevel(log_level)

    if not logger.handlers:
        # Determine which broker and timezone based on BROKER env var
        if BROKER == "ANGEL":
            log_file = ANGEL_LOG_FILE
            timezone = "Asia/Kolkata"
        elif BROKER == "IBKR":
            log_file = IBKR_LOG_FILE
            timezone = "America/New_York"
        else:
            # Fallback to Angel
            log_file = ANGEL_LOG_FILE
            timezone = "Asia/Kolkata"

        # Console handler (same timezone as file)
        console_handler = logging.StreamHandler()
        console_fmt = TimezoneFormatter(
            "%(asctime)s — %(levelname)s — %(name)s — %(message)s", tz=timezone
        )
        console_handler.setFormatter(console_fmt)
        logger.addHandler(console_handler)

        # File handler (broker-specific log file with appropriate timezone)
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = logging.FileHandler(log_file)
        file_fmt = TimezoneFormatter(
            "%(asctime)s — %(levelname)s — %(name)s — %(message)s", tz=timezone
        )
        file_handler.setFormatter(file_fmt)
        logger.addHandler(file_handler)

        logger.info(f"Logging initialized for {BROKER} broker (timezone: {timezone})")

    return logger


# create module-level logger
logger = setup_logging()
