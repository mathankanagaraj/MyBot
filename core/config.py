# core/config.py
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Angel Broker API Configuration
ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE", "")
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD", "")
ANGEL_PIN = os.getenv("ANGEL_PIN", "")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "")

BASE_DIR = Path(__file__).resolve().parent.parent

# Trading Symbols Configuration
# Index Futures: Used for signal generation, then trade INDEX OPTIONS
INDEX_FUTURES = ["NIFTY", "BANKNIFTY"]

# Stock Symbols: Used for signal generation, then trade STOCK OPTIONS
STOCK_SYMBOLS = ["RELIANCE", "INFY", "TCS", "ICICIBANK", "HDFCBANK"]

# All symbols to monitor (combined list)
SYMBOLS = INDEX_FUTURES + STOCK_SYMBOLS

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Trading Configuration (LIVE ONLY)
MODE = "LIVE"  # Angel Broker doesn't support paper trading

# Risk Management
MAX_CONTRACTS_PER_TRADE = int(os.getenv("MAX_CONTRACTS_PER_TRADE", "1"))
RISK_PER_CONTRACT = float(os.getenv("RISK_PER_CONTRACT", "0.0"))
RISK_PCT_OF_PREMIUM = float(os.getenv("RISK_PCT_OF_PREMIUM", "0.20"))
RR_RATIO = float(os.getenv("RR_RATIO", "2.0"))
MIN_PREMIUM = float(os.getenv("MIN_PREMIUM", "5.0"))  # ₹5 minimum for Indian market

# Position & Risk Limits
MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", "5000"))  # ₹5,000 daily loss limit
MAX_POSITION_SIZE = float(os.getenv("MAX_POSITION_SIZE", "50000"))  # ₹50,000 max per position
ALLOC_PCT = float(os.getenv("ALLOC_PCT", "0.70"))  # 70% of available funds

# Option Selection Parameters
OPTION_MIN_DTE = int(os.getenv("OPTION_MIN_DTE", "2"))
OPTION_MAX_DTE = int(os.getenv("OPTION_MAX_DTE", "7"))
OPTION_TARGET_DELTA = float(os.getenv("OPTION_TARGET_DELTA", "0.40"))
OPTION_MAX_IV_PCT = float(os.getenv("OPTION_MAX_IV_PCT", "80"))
OPTION_MIN_OPEN_INTEREST = int(os.getenv("OPTION_MIN_OPEN_INTEREST", "100"))  # Higher for Indian market
OPTION_MAX_MID_SPREAD_PCT = float(os.getenv("OPTION_MAX_MID_SPREAD_PCT", "0.05"))

# Monitoring & Timing
MONITOR_INTERVAL = float(os.getenv("MONITOR_INTERVAL", "2.0"))
MAX_5M_CHECKS = int(os.getenv("MAX_5M_CHECKS", "6"))
UNDERLYING_ATR_MULTIPLIER = float(os.getenv("UNDERLYING_ATR_MULTIPLIER", "2.0"))
MAX_HOLD_MINUTES = int(os.getenv("MAX_HOLD_MINUTES", "120"))

# Indicator Configurations
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
SUPERTREND_PERIOD = int(os.getenv("SUPERTREND_PERIOD", "10"))
SUPERTREND_MULTIPLIER = float(os.getenv("SUPERTREND_MULTIPLIER", "3.0"))

# Logging & Audit
LOG_DIR = BASE_DIR.joinpath("logs")
AUDIT_DIR = BASE_DIR.joinpath("audit")
LOG_FILE = str(LOG_DIR.joinpath("bot.log"))
AUDIT_CSV = str(AUDIT_DIR.joinpath("trade_audit.csv"))

# Indian Market Configuration
TIMEZONE = os.getenv("TIMEZONE", "Asia/Kolkata")
MARKET_HOURS_ONLY = os.getenv("MARKET_HOURS_ONLY", "true").lower() in ("1", "true", "yes")

# NSE Market Hours: 9:15 AM - 3:30 PM IST
NSE_MARKET_OPEN_HOUR = 9
NSE_MARKET_OPEN_MINUTE = 15
NSE_MARKET_CLOSE_HOUR = 15
NSE_MARKET_CLOSE_MINUTE = 30

# Angel Broker API URLs
SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# Order exit parameters
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "2.0"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "1.0"))

# Trade polling interval
TRADE_POLL_INTERVAL = 2  # seconds
