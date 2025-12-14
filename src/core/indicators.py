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
    
    logger.debug(f"[add_indicators] Input: {len(df)} bars, date range: {df.index[0]} to {df.index[-1]}")

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
    supertrend_result = ta.supertrend(df["high"], df["low"], df["close"], length=10, multiplier=3.0)
    
    if supertrend_result is not None and len(supertrend_result.columns) >= 3:
        # Direction: 1 = bullish (price above ST), -1 = bearish (price below ST)
        df["supertrend"] = supertrend_result.iloc[:, 0] == 1  # Convert to boolean (True=BULL)
        df["st_lower"] = supertrend_result.iloc[:, 1]  # Lower band
        df["st_upper"] = supertrend_result.iloc[:, 2]  # Upper band
    else:
        # Fallback if not enough data
        df["supertrend"] = True
        df["st_lower"] = df["close"] * 0.95
        df["st_upper"] = df["close"] * 1.05
    
    # Debug logging for last values
    logger.debug(f"[add_indicators] Output: Last close={df['close'].iloc[-1]:.2f}, Last RSI={df['rsi'].iloc[-1]:.2f}, Last ST={df['supertrend'].iloc[-1]}")

    return df
