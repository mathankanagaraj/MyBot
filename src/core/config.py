# core/config.py
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# ============================================================================
# BROKER SELECTION
# ============================================================================
# With separate Docker containers, each container runs ONE broker:
# - angel_bot container: BROKER=ANGEL
# - ibkr_bot container: BROKER=IBKR
# Note: BROKER=BOTH is deprecated (use separate containers instead)
BROKER = os.getenv("BROKER", "ANGEL").upper()  # Options: ANGEL or IBKR

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
ANGEL_STOCK_SYMBOLS = [
    "RELIANCE",
    "INFY",
    "TCS",
    "ICICIBANK",
    "HDFCBANK",
    "SBIN",
    "AXISBANK",
    "BHARTIARTL",
]
ANGEL_SYMBOLS = ANGEL_INDEX_FUTURES + ANGEL_STOCK_SYMBOLS

# ============================================================================
# IBKR CONFIGURATION
# ============================================================================
IB_HOST = os.getenv("IB_HOST", "host.docker.internal")
IB_PORT = int(os.getenv("IB_PORT", "7497"))  # 7497=paper, 7496=live
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "109"))
IBKR_PAPER_BALANCE = float(
    os.getenv("IBKR_PAPER_BALANCE", "10000")
)  # Starting balance for paper

# IBKR Symbols (US Market - Stock Options)
IBKR_SYMBOLS_STR = os.getenv(
    "IBKR_SYMBOLS", "SPY,QQQ,TSLA,NVDA,MSFT,GOOGL,AAPL,AMZN,META"
)
IBKR_SYMBOLS = [s.strip() for s in IBKR_SYMBOLS_STR.split(",")]
IBKR_QUANTITY = int(os.getenv("IBKR_QUANTITY", "1"))  # Number of contracts per trade

# ============================================================================
# LEGACY COMPATIBILITY (for Angel-only code paths)
# ============================================================================
INDEX_FUTURES = ANGEL_INDEX_FUTURES
STOCK_SYMBOLS = ANGEL_STOCK_SYMBOLS

BASE_DIR = Path(__file__).resolve().parent.parent

# ============================================================================
# TELEGRAM NOTIFICATIONS
# ============================================================================
# Angel One Bot Telegram (for NSE/Indian market notifications)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# IBKR Bot Telegram (for US market notifications)
IBKR_TELEGRAM_TOKEN = os.getenv("IBKR_TELEGRAM_TOKEN", "")
IBKR_TELEGRAM_CHAT_ID = os.getenv("IBKR_TELEGRAM_CHAT_ID", "")

# ============================================================================
# TRADING MODE
# ============================================================================
# Each broker has its own mode
ANGEL_MODE = os.getenv(
    "ANGEL_MODE", "LIVE"
).upper()  # Angel One: Always LIVE (no paper)
IBKR_MODE = os.getenv("IBKR_MODE", "PAPER").upper()  # IBKR: PAPER or LIVE

# Legacy MODE for backward compatibility
MODE = ANGEL_MODE  # For existing Angel One code

# ============================================================================
# RISK MANAGEMENT (Common for both brokers)
# ============================================================================
MAX_CONTRACTS_PER_TRADE = int(os.getenv("MAX_CONTRACTS_PER_TRADE", "1"))
RISK_PER_CONTRACT = float(os.getenv("RISK_PER_CONTRACT", "0.0"))
RISK_PCT_OF_PREMIUM = float(os.getenv("RISK_PCT_OF_PREMIUM", "0.10"))
RR_RATIO = float(os.getenv("RR_RATIO", "2.0"))
MIN_PREMIUM = float(os.getenv("MIN_PREMIUM", "5.0"))  # ₹5 for Indian, $5 for US

# Position & Risk Limits (Percentage-based for scalability)
MAX_DAILY_LOSS_PCT = float(
    os.getenv("MAX_DAILY_LOSS_PCT", "0.05")
)  # 5% of account balance
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.70"))  # 70% max per position
ALLOC_PCT = float(
    os.getenv("ALLOC_PCT", "0.70")
)  # 70% of available funds for all positions

# ============================================================================
# OPTION SELECTION PARAMETERS (Common for both brokers)
# ============================================================================
# Stock options - use nearest monthly expiry
OPTION_MIN_DTE = int(os.getenv("OPTION_MIN_DTE", "7"))    # Minimum 7 days to avoid weekly decay
OPTION_MAX_DTE = int(os.getenv("OPTION_MAX_DTE", "45"))   # Max ~6 weeks (nearest monthly)

# Futures Options (FOP) specific parameters - 0 DTE strategy for max gamma
FUTURES_OPTION_MIN_DTE = int(os.getenv("FUTURES_OPTION_MIN_DTE", "0"))   # 0 DTE (same day expiry)
FUTURES_OPTION_MAX_DTE = int(os.getenv("FUTURES_OPTION_MAX_DTE", "2"))   # Max 2 days (0 DTE strategy)

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
# EMA crossover confirmation window (number of 5m candles to look back)
EMA_CROSSOVER_WINDOW = int(os.getenv("EMA_CROSSOVER_WINDOW", "3"))

# ============================================================================
# OPTIMIZED STRATEGY PARAMETERS (SuperTrend/VWAP/RSI Strategy)
# ============================================================================

# 5-Minute Entry Parameters
EMA_PERIOD = int(os.getenv("EMA_PERIOD", "20"))  # EMA for price structure
RSI_5M_PERIOD = int(os.getenv("RSI_5M_PERIOD", "5"))  # Fast RSI for pullback detection

# Volume Confirmation
VOLUME_MA_PERIOD = int(
    os.getenv("VOLUME_MA_PERIOD", "20")
)  # Volume moving average period

# Volume Confirmation
VOLUME_MA_PERIOD = int(
    os.getenv("VOLUME_MA_PERIOD", "20")
)  # Volume moving average period

# ============================================================================
# ENHANCED FILTERS
# ============================================================================

# ATM Strike Distance Filter
ATM_STRIKE_MAX_DISTANCE_PCT = float(
    os.getenv("ATM_STRIKE_MAX_DISTANCE_PCT", "0.05")
)  # 5% max distance from underlying

# Time Gap Between Entries
MIN_TIME_BETWEEN_ENTRIES_MINUTES = int(
    os.getenv("MIN_TIME_BETWEEN_ENTRIES_MINUTES", "15")
)  # Minimum 15 minutes between trades

# EMA Flatness Detection (Ranging Market Filter)
EMA_FLATNESS_THRESHOLD_PCT = float(
    os.getenv("EMA_FLATNESS_THRESHOLD_PCT", "0.001")
)  # 0.1% minimum slope

# Force Exit Before Expiry
FORCE_EXIT_BEFORE_EXPIRY_MINUTES = int(
    os.getenv("FORCE_EXIT_BEFORE_EXPIRY_MINUTES", "30")
)  # Force exit 30min before expiry

# ============================================================================
# NO-TRADE ZONES
# ============================================================================

# No entries during first N minutes after market open
NO_TRADE_FIRST_MINUTES = int(os.getenv("NO_TRADE_FIRST_MINUTES", "5"))

# No entries during last N minutes before expiry (on expiry day)
NO_TRADE_LAST_MINUTES_EXPIRY = int(os.getenv("NO_TRADE_LAST_MINUTES_EXPIRY", "15"))

# ============================================================================
# ORB (Opening Range Breakout) STRATEGY CONFIG
# ============================================================================
# Strategy selection: ORB or MACD_EMA (default existing strategy)
STRATEGY = os.getenv("STRATEGY", "MACD_EMA").upper()

# ORB Symbols per broker
ORB_ANGEL_SYMBOLS = [
    "NIFTY",
    "BANKNIFTY",
    "RELIANCE",
    "ICICIBANK",
    "SBIN",
    "HDFCBANK",
    "INFY",
]

# ORB Strategy Symbols (can be overridden via .env)
ORB_SYMBOLS_STR = os.getenv(
    "ORB_SYMBOLS", "ES,NQ,NVDA,TSLA,AAPL,AMD,MSFT"  # Default includes futures
)
ORB_IBKR_SYMBOLS = [s.strip() for s in ORB_SYMBOLS_STR.split(",")]

# IBKR Future Exchanges mapping
IBKR_FUTURES_EXCHANGES = {
    "ES": "CME",
    "NQ": "CME",
}

# ORB Parameters
ORB_DURATION_MINUTES = int(
    os.getenv("ORB_DURATION_MINUTES", "30")
)  # ORB building period
ORB_ATR_LENGTH = int(os.getenv("ORB_ATR_LENGTH", "14"))
ORB_ATR_MULTIPLIER = float(os.getenv("ORB_ATR_MULTIPLIER", "1.2"))
ORB_RISK_REWARD = float(os.getenv("ORB_RISK_REWARD", "1.5"))  # 1:1.5 risk-reward

# Breakout confirmation timeframe (30 = 30-min candles for higher conviction)
ORB_BREAKOUT_TIMEFRAME = int(os.getenv("ORB_BREAKOUT_TIMEFRAME", "30"))

# ORB Entry Limits (stop taking entries after this time)
# Separate limits for different markets
# Format: "HH.MM" (e.g., "15.15" = 3:15 PM)

def _parse_time_string(time_str: str) -> tuple[int, int]:
    """Parse time string like '15.15' into (hour, minute) tuple."""
    parts = time_str.split(".")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    return hour, minute

ORB_MAX_ENTRY_TIME_IBKR = os.getenv("ORB_MAX_ENTRY_TIME_IBKR", "15.15")  # 3:15 PM ET for US futures/stocks
ORB_MAX_ENTRY_HOUR_IBKR, ORB_MAX_ENTRY_MINUTE_IBKR = _parse_time_string(ORB_MAX_ENTRY_TIME_IBKR)

ORB_MAX_ENTRY_TIME_ANGEL = os.getenv("ORB_MAX_ENTRY_TIME_ANGEL", "14.15")  # 2:15 PM IST for Indian markets
ORB_MAX_ENTRY_HOUR_ANGEL, ORB_MAX_ENTRY_MINUTE_ANGEL = _parse_time_string(ORB_MAX_ENTRY_TIME_ANGEL)

# Backward compatibility: use generic setting if broker-specific not set
ORB_MAX_ENTRY_HOUR = ORB_MAX_ENTRY_HOUR_IBKR if BROKER == "IBKR" else ORB_MAX_ENTRY_HOUR_ANGEL
ORB_MAX_ENTRY_MINUTE = ORB_MAX_ENTRY_MINUTE_IBKR if BROKER == "IBKR" else ORB_MAX_ENTRY_MINUTE_ANGEL

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
MARKET_HOURS_ONLY = os.getenv("MARKET_HOURS_ONLY", "true").lower() in (
    "1",
    "true",
    "yes",
)

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
