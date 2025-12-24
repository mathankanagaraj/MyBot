# core/orb_signal_engine.py
"""
ORB (Opening Range Breakout) Strategy Engine

Implements the ORB strategy logic based on Pine Script:
- Calculates ORB high/low from first N minutes of trading
- Detects valid breakouts (entire candle outside ORB range)
- Calculates ATR-based risk with ORB structural risk
- Provides stop loss and take profit levels

Used by both AngelOne and IBKR ORB workers.
"""

import pandas as pd
from datetime import datetime, timedelta, time
from typing import Dict, Optional, Tuple
from core.logger import logger
from core.config import (
    ORB_DURATION_MINUTES,
    ORB_ATR_MULTIPLIER,
    ORB_RISK_REWARD,
)


def resample_to_timeframe(
    df: pd.DataFrame, timeframe_minutes: int = 30
) -> pd.DataFrame:
    """
    Resample 1-minute or smaller bars to a larger timeframe.

    Args:
        df: DataFrame with OHLCV data indexed by datetime
        timeframe_minutes: Target timeframe in minutes (default: 30)

    Returns:
        Resampled DataFrame with OHLCV data
    """
    if df is None or df.empty:
        return pd.DataFrame()

    rule = f"{timeframe_minutes}min"
    df_resampled = (
        df.resample(rule, label="right", closed="right")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }
        )
        .dropna()
    )

    return df_resampled


def get_seconds_until_next_candle(now: datetime, timeframe_minutes: int = 30) -> int:
    """
    Calculate seconds until the next candle closes for a given timeframe.

    Args:
        now: Current datetime
        timeframe_minutes: Candle timeframe in minutes (e.g., 5, 15, 30, 60)

    Returns:
        Seconds until next candle close
    """
    current_minute = now.minute
    current_second = now.second

    # Calculate next close minute
    # e.g., if now is 10:12 and timeframe is 15:
    # 12 // 15 = 0. (0+1)*15 = 15.
    # if now is 10:15:01. 15 // 15 = 1. (1+1)*15 = 30.

    # Logic for finding next multiple of timeframe
    next_close_minute = ((current_minute // timeframe_minutes) + 1) * timeframe_minutes

    minutes_remaining = next_close_minute - current_minute

    # Calculate seconds
    seconds_remaining = (minutes_remaining * 60) - current_second

    # If next_close_minute exceeds 60 (e.g., 60, 90), it means next hour(s).
    # The formula remains valid in terms of total seconds difference.
    # But clean minute handling:
    # (next_close_minute - current_minute) handles the minute diff correctly even if > 60
    # e.g. current 45, interval 30. next = 60. diff = 15.
    # current 12, interval 30. next = 30. diff = 18.

    # Add buffer
    return max(10, seconds_remaining + 5)


def get_seconds_until_next_30m_close(now: datetime) -> int:
    """
    Calculate seconds until the next 30-minute candle closes.
    Wrapper for get_seconds_until_next_candle.
    """
    return get_seconds_until_next_candle(now, timeframe_minutes=30)


def calculate_atr(df: pd.DataFrame, period: int = 14) -> Optional[float]:
    """
    Calculate Average True Range (ATR) for the given DataFrame.

    Args:
        df: DataFrame with 'high', 'low', 'close' columns
        period: ATR period (default: 14)

    Returns:
        ATR value (float) or None if insufficient data
    """
    if df is None or len(df) < period + 1:
        return None

    high = df["high"]
    low = df["low"]
    close = df["close"].shift(1)

    tr1 = high - low
    tr2 = abs(high - close)
    tr3 = abs(low - close)

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean().iloc[-1]

    return float(atr) if not pd.isna(atr) else None


def calculate_orb_range(
    df: pd.DataFrame,
    market_open_time: time,
    orb_duration_minutes: int = ORB_DURATION_MINUTES,
    symbol: str = "UNKNOWN",
) -> Dict:
    """
    Calculate ORB (Opening Range Breakout) high and low.

    The ORB range is built from the first N minutes of trading after market open.

    Args:
        df: DataFrame with OHLCV data indexed by datetime
        market_open_time: Market open time (e.g., time(9, 15) for NSE)
        orb_duration_minutes: Duration in minutes to build ORB (default: 30)
        symbol: Symbol name for logging

    Returns:
        dict with keys:
            - orb_high: ORB high price
            - orb_low: ORB low price
            - orb_range: ORB high - ORB low
            - orb_complete: True if ORB period is complete
            - orb_end_time: Timestamp when ORB period ended
    """
    if df is None or df.empty:
        logger.debug(f"[{symbol}] ORB Range: No data available")
        return {"orb_high": None, "orb_low": None, "orb_complete": False}

    # Get today's date from the last bar
    last_bar_time = df.index[-1]
    if isinstance(last_bar_time, pd.Timestamp):
        today = last_bar_time.date()
    else:
        today = datetime.now().date()

    # Calculate ORB start and end times
    orb_start = datetime.combine(today, market_open_time)
    orb_end = orb_start + timedelta(minutes=orb_duration_minutes)

    # Make timezone-aware if DataFrame index is timezone-aware
    if df.index.tz is not None:
        orb_start = orb_start.replace(tzinfo=df.index.tz)
        orb_end = orb_end.replace(tzinfo=df.index.tz)

    # Filter bars within ORB period
    orb_bars = df[(df.index >= orb_start) & (df.index < orb_end)]

    if orb_bars.empty:
        logger.debug(
            f"[{symbol}] ORB Range: No bars in ORB period ({orb_start} to {orb_end}). "
            f"Data range in DF: {df.index[0]} to {df.index[-1]}"
        )
        return {"orb_high": None, "orb_low": None, "orb_complete": False}

    orb_high = float(orb_bars["high"].max())
    orb_low = float(orb_bars["low"].min())
    orb_range = orb_high - orb_low

    # Check if ORB period is complete
    orb_complete = last_bar_time >= orb_end

    logger.info(f"[{symbol}] 📊 ORB RANGE CALCULATED")
    logger.info(
        f"[{symbol}]   ORB Period: {orb_start.strftime('%H:%M')} - {orb_end.strftime('%H:%M')}"
    )
    logger.info(f"[{symbol}]   ORB High: {orb_high:.2f}")
    logger.info(f"[{symbol}]   ORB Low: {orb_low:.2f}")
    logger.info(f"[{symbol}]   ORB Range: {orb_range:.2f}")
    logger.info(f"[{symbol}]   ORB Complete: {'✅' if orb_complete else '⏳'}")

    return {
        "orb_high": orb_high,
        "orb_low": orb_low,
        "orb_range": orb_range,
        "orb_complete": orb_complete,
        "orb_end_time": orb_end,
    }


def calculate_orb_risk(
    atr: float,
    orb_range: float,
    atr_multiplier: float = ORB_ATR_MULTIPLIER,
    symbol: str = "UNKNOWN",
) -> float:
    """
    Calculate risk points for ORB strategy.

    Risk = max(ATR * multiplier, ORB_range * 0.5)

    This ensures risk is at least based on ATR volatility OR half the ORB range,
    whichever is larger, providing better structural protection.

    Args:
        atr: Current ATR value
        orb_range: ORB high - ORB low
        atr_multiplier: ATR multiplier (default: 1.2)
        symbol: Symbol for logging

    Returns:
        Risk in price points
    """
    atr_risk = atr * atr_multiplier
    orb_risk = orb_range * 0.5
    risk_pts = max(atr_risk, orb_risk)

    logger.debug(
        f"[{symbol}] ORB Risk: ATR-based={atr_risk:.2f}, ORB-based={orb_risk:.2f} → Using {risk_pts:.2f}"
    )

    return risk_pts


def detect_orb_breakout(
    df: pd.DataFrame, orb_high: float, orb_low: float, symbol: str = "UNKNOWN"
) -> Dict:
    """
    Detect valid ORB breakout with fake breakout filter.

    Valid breakout rules (from Pine Script):
    - LONG: close > orb_high AND low > orb_high (entire candle above ORB)
    - SHORT: close < orb_low AND high < orb_low (entire candle below ORB)

    Args:
        df: DataFrame with OHLCV data (uses last bar)
        orb_high: ORB high price
        orb_low: ORB low price
        symbol: Symbol for logging

    Returns:
        dict with keys:
            - breakout: "LONG", "SHORT", or None
            - price: Entry price (close of breakout candle)
            - candle_time: Timestamp of breakout candle
    """
    if df is None or df.empty:
        return {"breakout": None, "reason": "no_data"}

    if orb_high is None or orb_low is None:
        return {"breakout": None, "reason": "orb_not_set"}

    last = df.iloc[-1]
    close = float(last["close"])
    high = float(last["high"])
    low = float(last["low"])
    open_price = float(last["open"])
    candle_time = df.index[-1]

    logger.info(f"[{symbol}] 🔷 ORB BREAKOUT CHECK")
    logger.info(f"[{symbol}]   Time: {candle_time}")
    logger.info(
        f"[{symbol}]   O: {open_price:.2f} H: {high:.2f} L: {low:.2f} C: {close:.2f}"
    )
    logger.info(f"[{symbol}]   ORB High: {orb_high:.2f}, ORB Low: {orb_low:.2f}")

    # LONG breakout: entire candle above ORB high
    valid_long = close > orb_high and low > orb_high

    # SHORT breakout: entire candle below ORB low
    valid_short = close < orb_low and high < orb_low

    logger.info(
        f"[{symbol}]   LONG Check (Close > ORB_H AND Low > ORB_H): {'✅' if valid_long else '❌'}"
    )
    if not valid_long and close > orb_high:
        logger.info(
            f"[{symbol}]   ⚠️ Partial LONG: Close is above ORB High, but Low ({low:.2f}) is still within range."
        )

    logger.info(
        f"[{symbol}]   SHORT Check (Close < ORB_L AND High < ORB_L): {'✅' if valid_short else '❌'}"
    )
    if not valid_short and close < orb_low:
        logger.info(
            f"[{symbol}]   ⚠️ Partial SHORT: Close is below ORB Low, but High ({high:.2f}) is still within range."
        )

    if valid_long:
        logger.info(f"[{symbol}]   ✅ VALID LONG BREAKOUT DETECTED")
        return {
            "breakout": "LONG",
            "price": close,
            "candle_time": candle_time,
        }

    if valid_short:
        logger.info(f"[{symbol}]   ✅ VALID SHORT BREAKOUT DETECTED")
        return {
            "breakout": "SHORT",
            "price": close,
            "candle_time": candle_time,
        }

    logger.info(f"[{symbol}]   ❌ No valid breakout")
    return {"breakout": None, "reason": "no_breakout"}


def get_orb_sl_tp(
    entry_price: float,
    direction: str,
    risk_pts: float,
    rr_ratio: float = ORB_RISK_REWARD,
    symbol: str = "UNKNOWN",
) -> Tuple[float, float]:
    """
    Calculate stop loss and take profit prices for ORB trade.

    Args:
        entry_price: Entry price
        direction: "LONG" or "SHORT"
        risk_pts: Risk in price points
        rr_ratio: Risk:Reward ratio (default: 1.5)
        symbol: Symbol for logging

    Returns:
        Tuple of (stop_loss, take_profit)
    """
    if direction == "LONG":
        stop_loss = entry_price - risk_pts
        take_profit = entry_price + (risk_pts * rr_ratio)
    else:  # SHORT
        stop_loss = entry_price + risk_pts
        take_profit = entry_price - (risk_pts * rr_ratio)

    logger.info(f"[{symbol}] 📋 ORB SL/TP CALCULATED ({direction})")
    logger.info(f"[{symbol}]   Entry: {entry_price:.2f}")
    logger.info(f"[{symbol}]   Risk: {risk_pts:.2f} pts")
    logger.info(f"[{symbol}]   Stop Loss: {stop_loss:.2f}")
    logger.info(f"[{symbol}]   Take Profit: {take_profit:.2f} (RR: 1:{rr_ratio})")

    return stop_loss, take_profit


def check_orb_trade_allowed(
    current_hour: int,
    max_entry_hour: int,
    trade_taken_today: bool,
    symbol: str = "UNKNOWN",
    current_minute: int = 0,
    max_entry_minute: int = 0,
) -> Tuple[bool, str]:
    """
    Check if ORB trade is allowed based on time and daily limit.

    Args:
        current_hour: Current hour (0-23)
        max_entry_hour: Maximum hour for entries (e.g., 14 = no entries after 2 PM)
        trade_taken_today: Whether a trade has already been taken today
        symbol: Symbol for logging
        current_minute: Current minute (0-59)
        max_entry_minute: Maximum minute for entries (e.g., 15 = no entries after :15)

    Returns:
        Tuple of (is_allowed, reason)
    """
    if trade_taken_today:
        logger.debug(f"[{symbol}] ORB: Trade already taken today")
        return False, "trade_taken_today"

    # Compare time: (hour, minute) tuple
    current_time = (current_hour, current_minute)
    max_time = (max_entry_hour, max_entry_minute)
    
    if current_time >= max_time:
        logger.debug(
            f"[{symbol}] ORB: Past max entry time ({current_hour:02d}:{current_minute:02d} >= {max_entry_hour:02d}:{max_entry_minute:02d})"
        )
        return False, "past_max_entry_hour"

    return True, "allowed"


def should_force_exit(
    current_time: datetime,
    market_close_time: time,
    exit_before_minutes: int = 15,
    symbol: str = "UNKNOWN",
) -> bool:
    """
    Check if position should be force-closed before market close.

    Args:
        current_time: Current datetime
        market_close_time: Market close time
        exit_before_minutes: Minutes before close to exit (default: 15)
        symbol: Symbol for logging

    Returns:
        True if position should be closed
    """
    today = current_time.date()
    close_dt = datetime.combine(today, market_close_time)

    # Make timezone-aware if current_time is timezone-aware
    if current_time.tzinfo is not None:
        close_dt = close_dt.replace(tzinfo=current_time.tzinfo)

    exit_time = close_dt - timedelta(minutes=exit_before_minutes)

    if current_time >= exit_time:
        logger.warning(
            f"[{symbol}] ⚠️ FORCE EXIT: {exit_before_minutes} min before market close"
        )
        return True

    return False
