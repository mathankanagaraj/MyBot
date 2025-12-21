# core/indicators.py
import pandas as pd
import numpy as np
import pandas_ta as ta


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add technical indicators to intraday OHLCV dataframe using pandas-ta library.
    This ensures calculations match TradingView, Investing.com, and other standard platforms.

    Indicators added:
    - EMA9, EMA21, EMA50
    - VWAP (custom - per-day calculation)
    - MACD(12,26,9)
    - RSI(14)
    - OBV
    - ATR(14)
    - SuperTrend(10,3)
    - SMA20 (for 5m entry)
    """
    from core.logger import logger

    df = df.copy().sort_index()

    logger.debug(
        f"[add_indicators] Input: {len(df)} bars, date range: {df.index[0]} to {df.index[-1]}"
    )

    # --- EMAs (using pandas-ta for consistency) ---
    df["ema9"] = ta.ema(df["close"], length=9)
    df["ema21"] = ta.ema(df["close"], length=21)
    df["ema50"] = ta.ema(df["close"], length=50)

    # --- SMA20 (for 5m entry checks) ---
    df["sma20"] = ta.sma(df["close"], length=20)

    # --- VWAP (custom - per-day calculation, not in pandas-ta) ---
    if "volume" in df.columns and df["volume"].sum() > 0:
        tp = (df["high"] + df["low"] + df["close"]) / 3
        pv = tp * df["volume"]
        cum_vol = df["volume"].groupby(df.index.date).cumsum()
        cum_pv = pv.groupby(df.index.date).cumsum()
        df["vwap"] = cum_pv / cum_vol
    else:
        df["vwap"] = df["close"]

    # --- MACD (12,26,9) using pandas-ta ---
    macd_result = ta.macd(df["close"], fast=12, slow=26, signal=9)
    if macd_result is not None:
        df["macd"] = macd_result["MACD_12_26_9"]
        df["macd_sig"] = macd_result["MACDs_12_26_9"]
        df["macd_hist"] = macd_result["MACDh_12_26_9"]
    else:
        # Fallback if not enough data
        df["macd"] = 0
        df["macd_sig"] = 0
        df["macd_hist"] = 0

    # --- RSI(14) using pandas-ta ---
    rsi_result = ta.rsi(df["close"], length=14)
    df["rsi"] = rsi_result if rsi_result is not None else 50

    # --- OBV using pandas-ta ---
    if "volume" in df.columns and df["volume"].sum() > 0:
        obv_result = ta.obv(df["close"], df["volume"])
        df["obv"] = obv_result if obv_result is not None else 0
    else:
        df["obv"] = 0

    # --- ATR(14) using pandas-ta ---
    atr_result = ta.atr(df["high"], df["low"], df["close"], length=14)
    df["atr14"] = atr_result if atr_result is not None else 0

    # --- SuperTrend (10,3) using pandas-ta ---
    # pandas-ta SuperTrend returns: SUPERTd (direction), SUPERTl (lower), SUPERTs (upper)
    supertrend_result = ta.supertrend(
        df["high"], df["low"], df["close"], length=10, multiplier=3.0
    )

    if supertrend_result is not None and len(supertrend_result.columns) >= 3:
        # Direction: 1 = bullish (price above ST), -1 = bearish (price below ST)
        df["supertrend"] = (
            supertrend_result.iloc[:, 0] == 1
        )  # Convert to boolean (True=BULL)
        df["st_lower"] = supertrend_result.iloc[:, 1]  # Lower band
        df["st_upper"] = supertrend_result.iloc[:, 2]  # Upper band
    else:
        # Fallback if not enough data
        df["supertrend"] = True
        df["st_lower"] = df["close"] * 0.95
        df["st_upper"] = df["close"] * 1.05

    # Debug logging for last values
    logger.debug(
        f"[add_indicators] Output: Last close={df['close'].iloc[-1]:.2f}, Last RSI={df['rsi'].iloc[-1]:.2f}, Last ST={df['supertrend'].iloc[-1]}"
    )

    return df


# ============================================================================
# STANDALONE INDICATOR UTILITIES (For Optimized Strategy)
# ============================================================================


def calculate_rsi(close_prices, period=14):
    """
    Calculate RSI for given period using pandas-ta.

    Args:
        close_prices: Series or array of close prices
        period: RSI period (default: 14)

    Returns:
        RSI value (float) or None if insufficient data
    """
    if isinstance(close_prices, (list, np.ndarray)):
        close_prices = pd.Series(close_prices)

    if len(close_prices) < period + 1:
        return None

    rsi_result = ta.rsi(close_prices, length=period)
    if rsi_result is not None and len(rsi_result) > 0:
        return rsi_result.iloc[-1]
    return None


def calculate_ema(prices, period=20):
    """
    Calculate EMA for given period using pandas-ta.

    Args:
        prices: Series or array of prices
        period: EMA period (default: 20)

    Returns:
        EMA value (float) or None if insufficient data
    """
    if isinstance(prices, (list, np.ndarray)):
        prices = pd.Series(prices)

    if len(prices) < period:
        return None

    ema_result = ta.ema(prices, length=period)
    if ema_result is not None and len(ema_result) > 0:
        return ema_result.iloc[-1]
    return None


def calculate_volume_ma(volumes, period=20):
    """
    Calculate volume moving average.

    Args:
        volumes: Series or array of volumes
        period: MA period (default: 20)

    Returns:
        Volume MA value (float) or None if insufficient data
    """
    if isinstance(volumes, (list, np.ndarray)):
        volumes = pd.Series(volumes)

    if len(volumes) < period:
        return None

    return volumes.rolling(window=period).mean().iloc[-1]


def check_ema_flatness(ema_values, current_price, threshold_pct=0.001):
    """
    Check if EMA is flat (ranging market indicator).

    A flat EMA indicates a ranging/choppy market where trend-following
    strategies perform poorly.

    Args:
        ema_values: Series or list of recent EMA values (last 5-10 values recommended)
        current_price: Current price for percentage calculation
        threshold_pct: Minimum slope threshold as percentage (default: 0.001 = 0.1%)

    Returns:
        bool: True if EMA is flat (slope below threshold), False otherwise
    """
    if isinstance(ema_values, (list, np.ndarray)):
        ema_values = pd.Series(ema_values)

    if len(ema_values) < 2:
        return True  # Not enough data, consider flat

    # Calculate slope as percentage of price
    ema_slope = (
        ema_values.iloc[-1] - ema_values.iloc[-6 if len(ema_values) >= 6 else 0]
    ) / (len(ema_values) - 1 if len(ema_values) > 1 else 1)
    slope_pct = abs(ema_slope) / current_price if current_price > 0 else 0

    is_flat = slope_pct < threshold_pct

    from core.logger import logger

    logger.debug(
        f"[EMA Flatness] Slope: {ema_slope:.4f}, Slope %: {slope_pct*100:.4f}%, "
        f"Threshold: {threshold_pct*100:.4f}%, Is Flat: {is_flat}"
    )

    return is_flat


def check_candle_color(bar, expected_direction):
    """
    Check if candle color matches expected direction.

    Args:
        bar: Dict or Series with 'open' and 'close' keys
        expected_direction: "BULL" or "BEAR"

    Returns:
        bool: True if candle is green for BULL, red for BEAR
    """
    if isinstance(bar, pd.Series):
        open_price = bar["open"]
        close_price = bar["close"]
    else:
        open_price = bar.get("open")
        close_price = bar.get("close")

    if open_price is None or close_price is None:
        return False

    is_green = close_price > open_price
    is_red = close_price < open_price

    if expected_direction == "BULL":
        return is_green
    elif expected_direction == "BEAR":
        return is_red

    return False


def check_atm_strike_distance(strike_price, underlying_price, max_pct=0.05):
    """
    Validate that an option strike is near ATM (At-The-Money).

    Options too far OTM/ITM have poor liquidity and unreliable pricing.

    Args:
        strike_price: Option strike price
        underlying_price: Current underlying asset price
        max_pct: Maximum allowed distance as percentage (default: 0.05 = 5%)

    Returns:
        tuple: (is_valid: bool, distance_pct: float)
    """
    if underlying_price <= 0:
        return False, 0.0

    distance_pct = abs(strike_price - underlying_price) / underlying_price
    is_valid = distance_pct <= max_pct

    from core.logger import logger

    logger.debug(
        f"[ATM Check] Strike: {strike_price}, Underlying: {underlying_price:.2f}, "
        f"Distance: {distance_pct*100:.2f}%, Max: {max_pct*100:.2f}%, Valid: {is_valid}"
    )

    return is_valid, distance_pct
