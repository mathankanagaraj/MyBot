# core/logger.py
import logging
import os
from datetime import datetime
import pytz
from core.config import LOG_FILE


class ISTFormatter(logging.Formatter):
    """Custom formatter that displays timestamps in Indian Standard Time (IST)"""
    
    def formatTime(self, record, datefmt=None):
        """Convert timestamp to IST (UTC+5:30)"""
        dt = datetime.fromtimestamp(record.created, tz=pytz.UTC)
        ist = dt.astimezone(pytz.timezone('Asia/Kolkata'))
        return ist.strftime(datefmt or '%Y-%m-%d %H:%M:%S')


def setup_logging():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    logger = logging.getLogger("intraday_bot")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(LOG_FILE)
        sh = logging.StreamHandler()
        # Use custom IST formatter instead of default
        fmt = ISTFormatter("%(asctime)s — %(levelname)s — %(name)s — %(message)s")
        fh.setFormatter(fmt)
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger

# create module-level logger
logger = setup_logging()