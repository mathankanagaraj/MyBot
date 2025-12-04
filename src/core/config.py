# core/config.py
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# BROKER SELECTION
# ============================================================================
BROKER = os.getenv("BROKER", "ANGEL").upper()  # Options: ANGEL, IBKR, BOTH

# ============================================================================
# ANGEL ONE CONFIGURATION
# ============================================================================
ANGEL_API_KEY = os.getenv("ANGEL_API_KEY", "")
ANGEL_CLIENT_CODE = os.getenv("ANGEL_CLIENT_CODE", "")
ANGEL_PASSWORD = os.getenv("ANGEL_PASSWORD", "")
ANGEL_PIN = os.getenv("ANGEL_PIN", "")
ANGEL_TOTP_SECRET = os.getenv("ANGEL_TOTP_SECRET", "")

# Angel One Symbols (Indian Market)
ANGEL_INDEX_FUTURES = ["NIFTY", "BANKNIFTY"]
ANGEL_STOCK_SYMBOLS = ["RELIANCE", "INFY", "TCS", "ICICIBANK", "HDFCBANK", "SBIN", "AXISBANK", "BHARTIARTL"]
ANGEL_SYMBOLS = ANGEL_INDEX_FUTURES + ANGEL_STOCK_SYMBOLS

# ============================================================================
# IBKR CONFIGURATION
# ============================================================================
IB_HOST = os.getenv("IB_HOST", "host.docker.internal")
IB_PORT = int(os.getenv("IB_PORT", "7497"))  # 7497=paper, 7496=live
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "109"))
IBKR_PAPER_BALANCE = float(os.getenv("IBKR_PAPER_BALANCE", "10000"))  # Starting balance for paper

# IBKR Symbols (US Market - Stock Options)
IBKR_SYMBOLS_STR = os.getenv("IBKR_SYMBOLS", "SPY,QQQ,TSLA,NVDA,MSFT,GOOGL,AAPL,AMZN,META")
IBKR_SYMBOLS = [s.strip() for s in IBKR_SYMBOLS_STR.split(",")]

# ============================================================================
# LEGACY COMPATIBILITY (for Angel-only code paths)
# ============================================================================
INDEX_FUTURES = ANGEL_INDEX_FUTURES
STOCK_SYMBOLS = ANGEL_STOCK_SYMBOLS
SYMBOLS = ANGEL_SYMBOLS

BASE_DIR = Path(__file__).resolve().parent.parent

# ============================================================================
# TELEGRAM NOTIFICATIONS
# ============================================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ============================================================================
# TRADING MODE
# ============================================================================
# Each broker has its own mode
ANGEL_MODE = os.getenv("ANGEL_MODE", "LIVE").upper()  # Angel One: Always LIVE (no paper)
IBKR_MODE = os.getenv("IBKR_MODE", "PAPER").upper()    # IBKR: PAPER or LIVE

# Legacy MODE for backward compatibility
MODE = ANGEL_MODE  # For existing Angel One code

# ============================================================================
# RISK MANAGEMENT (Common for both brokers)
# ============================================================================
MAX_CONTRACTS_PER_TRADE = int(os.getenv("MAX_CONTRACTS_PER_TRADE", "1"))
RISK_PER_CONTRACT = float(os.getenv("RISK_PER_CONTRACT", "0.0"))
RISK_PCT_OF_PREMIUM = float(os.getenv("RISK_PCT_OF_PREMIUM", "0.20"))
RR_RATIO = float(os.getenv("RR_RATIO", "2.0"))
MIN_PREMIUM = float(os.getenv("MIN_PREMIUM", "5.0"))  # ₹5 for Indian, $5 for US

# Position & Risk Limits (Percentage-based for scalability)
MAX_DAILY_LOSS_PCT = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))  # 5% of account balance
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.70"))  # 70% max per position
ALLOC_PCT = float(os.getenv("ALLOC_PCT", "0.70"))  # 70% of available funds for all positions

# ============================================================================
# OPTION SELECTION PARAMETERS (Common for both brokers)
# ============================================================================
OPTION_MIN_DTE = int(os.getenv("OPTION_MIN_DTE", "2"))
OPTION_MAX_DTE = int(os.getenv("OPTION_MAX_DTE", "7"))
OPTION_TARGET_DELTA = float(os.getenv("OPTION_TARGET_DELTA", "0.40"))
OPTION_MAX_IV_PCT = float(os.getenv("OPTION_MAX_IV_PCT", "80"))
OPTION_MIN_OPEN_INTEREST = int(os.getenv("OPTION_MIN_OPEN_INTEREST", "100"))
OPTION_MAX_MID_SPREAD_PCT = float(os.getenv("OPTION_MAX_MID_SPREAD_PCT", "0.05"))

# ============================================================================
# MONITORING & TIMING (Common for both brokers)
# ============================================================================
MONITOR_INTERVAL = float(os.getenv("MONITOR_INTERVAL", "2.0"))
MAX_5M_CHECKS = int(os.getenv("MAX_5M_CHECKS", "6"))
UNDERLYING_ATR_MULTIPLIER = float(os.getenv("UNDERLYING_ATR_MULTIPLIER", "2.0"))
MAX_HOLD_MINUTES = int(os.getenv("MAX_HOLD_MINUTES", "120"))

# ============================================================================
# INDICATOR CONFIGURATIONS (Common for both brokers)
# ============================================================================
RSI_PERIOD = int(os.getenv("RSI_PERIOD", "14"))
SUPERTREND_PERIOD = int(os.getenv("SUPERTREND_PERIOD", "10"))
SUPERTREND_MULTIPLIER = float(os.getenv("SUPERTREND_MULTIPLIER", "3.0"))

# ============================================================================
# LOGGING & AUDIT
# ============================================================================
LOG_DIR = BASE_DIR.joinpath("logs")
AUDIT_DIR = BASE_DIR.joinpath("audit")

# Separate log files for each broker
ANGEL_LOG_FILE = str(LOG_DIR.joinpath("angel_bot.log"))
IBKR_LOG_FILE = str(LOG_DIR.joinpath("ibkr_bot.log"))
ANGEL_AUDIT_CSV = str(AUDIT_DIR.joinpath("angel_trades.csv"))
IBKR_AUDIT_CSV = str(AUDIT_DIR.joinpath("ibkr_trades.csv"))

# Legacy compatibility
LOG_FILE = ANGEL_LOG_FILE
AUDIT_CSV = ANGEL_AUDIT_CSV

# ============================================================================
# MARKET HOURS & TIMEZONE
# ============================================================================
MARKET_HOURS_ONLY = os.getenv("MARKET_HOURS_ONLY", "true").lower() in ("1", "true", "yes")

# Indian Market (Angel One)
ANGEL_TIMEZONE = "Asia/Kolkata"
NSE_MARKET_OPEN_HOUR = 9
NSE_MARKET_OPEN_MINUTE = 15
NSE_MARKET_CLOSE_HOUR = 15
NSE_MARKET_CLOSE_MINUTE = 30

# US Market (IBKR)
IBKR_TIMEZONE = "America/New_York"
US_MARKET_OPEN_HOUR = int(os.getenv("US_MARKET_OPEN_HOUR", "9"))
US_MARKET_OPEN_MINUTE = int(os.getenv("US_MARKET_OPEN_MINUTE", "30"))
US_MARKET_CLOSE_HOUR = int(os.getenv("US_MARKET_CLOSE_HOUR", "16"))
US_MARKET_CLOSE_MINUTE = int(os.getenv("US_MARKET_CLOSE_MINUTE", "0"))

# Legacy compatibility
TIMEZONE = ANGEL_TIMEZONE

# ============================================================================
# ANGEL ONE API URLS
# ============================================================================
SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"

# ============================================================================
# ORDER EXIT PARAMETERS (Common for both brokers)
# ============================================================================
TAKE_PROFIT_PCT = float(os.getenv("TAKE_PROFIT_PCT", "2.0"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "1.0"))

# Trade polling interval
TRADE_POLL_INTERVAL = 2  # seconds
