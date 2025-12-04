# core/logger.py
import logging
import os
from datetime import datetime
import pytz
from core.config import ANGEL_LOG_FILE, IBKR_LOG_FILE, BROKER, LOG_DIR


class TimezoneFormatter(logging.Formatter):
    """Custom formatter that displays timestamps in a specified timezone"""
    
    def __init__(self, fmt=None, datefmt=None, tz='UTC'):
        super().__init__(fmt, datefmt)
        self.tz = pytz.timezone(tz)
    
    def formatTime(self, record, datefmt=None):
        """Convert timestamp to specified timezone"""
        dt = datetime.fromtimestamp(record.created, tz=pytz.UTC)
        localized = dt.astimezone(self.tz)
        return localized.strftime(datefmt or '%Y-%m-%d %H:%M:%S')


def setup_logging():
    """
    Setup logging with separate log files for each broker.
    - Angel One logs: IST timezone
    - IBKR logs: US Eastern timezone
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
        # Console handler (uses IST for backward compatibility)
        console_handler = logging.StreamHandler()
        console_fmt = TimezoneFormatter(
            "%(asctime)s — %(levelname)s — %(name)s — %(message)s",
            tz='Asia/Kolkata'
        )
        console_handler.setFormatter(console_fmt)
        logger.addHandler(console_handler)
        
        # Common log file (UTC timezone for general messages)
        os.makedirs(os.path.dirname(ANGEL_LOG_FILE), exist_ok=True)
        common_log_file = str(LOG_DIR.joinpath("bot.log"))
        common_handler = logging.FileHandler(common_log_file)
        common_fmt = TimezoneFormatter(
            "%(asctime)s — %(levelname)s — %(name)s — %(message)s",
            tz='UTC'
        )
        common_handler.setFormatter(common_fmt)
        common_handler.addFilter(CommonLogFilter())  # Only general messages
        logger.addHandler(common_handler)
        
        # Add broker-specific file handlers based on BROKER config
        if BROKER in ['ANGEL', 'BOTH']:
            # Angel One log file (IST timezone)
            os.makedirs(os.path.dirname(ANGEL_LOG_FILE), exist_ok=True)
            angel_handler = logging.FileHandler(ANGEL_LOG_FILE)
            angel_fmt = TimezoneFormatter(
                "%(asctime)s — %(levelname)s — %(name)s — %(message)s",
                tz='Asia/Kolkata'
            )
            angel_handler.setFormatter(angel_fmt)
            angel_handler.addFilter(BrokerFilter('ANGEL'))
            logger.addHandler(angel_handler)
        
        if BROKER in ['IBKR', 'BOTH']:
            # IBKR log file (US Eastern timezone)
            os.makedirs(os.path.dirname(IBKR_LOG_FILE), exist_ok=True)
            ibkr_handler = logging.FileHandler(IBKR_LOG_FILE)
            ibkr_fmt = TimezoneFormatter(
                "%(asctime)s — %(levelname)s — %(name)s — %(message)s",
                tz='America/New_York'
            )
            ibkr_handler.setFormatter(ibkr_fmt)
            ibkr_handler.addFilter(BrokerFilter('IBKR'))
            logger.addHandler(ibkr_handler)
    
    return logger


class BrokerFilter(logging.Filter):
    """Filter log records to route to broker-specific log files"""
    
    def __init__(self, broker):
        super().__init__()
        self.broker = broker
        
        # IBKR symbols for filtering
        self.ibkr_symbols = ['SPY', 'QQQ', 'TSLA', 'NVDA', 'MSFT', 'GOOGL', 'AAPL', 'AMZN', 'META']
        # Angel symbols for filtering
        self.angel_symbols = ['NIFTY', 'BANKNIFTY', 'RELIANCE', 'INFY', 'TCS', 'ICICIBANK', 
                              'HDFCBANK', 'SBIN', 'AXISBANK', 'BHARTIARTL']
    
    def filter(self, record):
        """
        Filter log records based on broker prefix in message.
        - If message contains [ANGEL] or Angel symbols, route to Angel log
        - If message contains [IBKR] or IBKR symbols, route to IBKR log
        - General messages (no prefix) go to both logs
        """
        message = record.getMessage()
        
        # Explicit broker tags
        if '[ANGEL]' in message:
            return self.broker == 'ANGEL'
        elif '[IBKR]' in message:
            return self.broker == 'IBKR'
        
        # Check for broker-specific symbols in brackets [SYMBOL]
        for symbol in self.ibkr_symbols:
            if f'[{symbol}]' in message:
                return self.broker == 'IBKR'
        
        for symbol in self.angel_symbols:
            if f'[{symbol}]' in message:
                return self.broker == 'ANGEL'
        
        # General messages go to both logs
        return True


class CommonLogFilter(logging.Filter):
    """Filter to keep only general system messages in bot.log (no broker-specific)"""
    
    def __init__(self):
        super().__init__()
        self.ibkr_symbols = ['SPY', 'QQQ', 'TSLA', 'NVDA', 'MSFT', 'GOOGL', 'AAPL', 'AMZN', 'META']
        self.angel_symbols = ['NIFTY', 'BANKNIFTY', 'RELIANCE', 'INFY', 'TCS', 'ICICIBANK', 
                              'HDFCBANK', 'SBIN', 'AXISBANK', 'BHARTIARTL']
    
    def filter(self, record):
        """
        Only allow general messages in common log.
        Block messages with broker tags or symbols.
        """
        message = record.getMessage()
        
        # Block broker-specific tags
        if '[ANGEL]' in message or '[IBKR]' in message:
            return False
        
        # Block broker-specific symbols
        for symbol in self.ibkr_symbols + self.angel_symbols:
            if f'[{symbol}]' in message:
                return False
        
        # Allow general messages
        return True


# create module-level logger
logger = setup_logging()