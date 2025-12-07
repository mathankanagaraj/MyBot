# core/indicators.py
import pandas as pd
import numpy as np


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add technical indicators to intraday OHLCV dataframe:
    EMA9, EMA21, EMA50, VWAP, MACD, MACD Signal, MACD Histogram,
    OBV, ATR14, RSI, SuperTrend (boolean), SuperTrend Upper/Lower.
    """

    df = df.copy().sort_index()

    # --- EMAs ---
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    # --- VWAP ---
    if "volume" in df.columns and df["volume"].sum() > 0:
        tp = (df["high"] + df["low"] + df["close"]) / 3
        pv = tp * df["volume"]
        cum_vol = df["volume"].groupby(df.index.date).cumsum()
        cum_pv = pv.groupby(df.index.date).cumsum()
        df["vwap"] = cum_pv / cum_vol
    else:
        df["vwap"] = df["close"]

    # --- MACD ---
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_sig"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_sig"]

    # --- OBV ---
    if "volume" in df.columns and df["volume"].sum() > 0:
        direction = np.sign(df["close"].diff().fillna(0))
        df["obv"] = (direction * df["volume"]).cumsum()
    else:
        df["obv"] = df["close"].diff().cumsum()

    # --- ATR14 ---
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr = pd.concat(
        [(high - low), (high - close.shift()).abs(), (low - close.shift()).abs()],
        axis=1,
    ).max(axis=1)
    df["atr14"] = tr.ewm(span=14, adjust=False).mean()

    # --- RSI14 ---
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = -delta.clip(upper=0).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / (loss + 1e-9)
    df["rsi"] = 100 - (100 / (1 + rs))

    # --- SuperTrend (vectorized-ish) ---
    atr10 = tr.ewm(alpha=1 / 10, adjust=False).mean()
    hl2 = (high + low) / 2
    multiplier = 3.0
    upper_band = hl2 + multiplier * atr10
    lower_band = hl2 - multiplier * atr10

    st_bool = np.full(len(df), True, dtype=bool)
    final_upper = upper_band.copy().values
    final_lower = lower_band.copy().values

    close_vals = close.values
    for i in range(1, len(df)):
        # Maintain previous trend bands
        final_upper[i] = max(
            upper_band.iat[i],
            final_upper[i - 1] if st_bool[i - 1] else upper_band.iat[i],
        )
        final_lower[i] = min(
            lower_band.iat[i],
            final_lower[i - 1] if not st_bool[i - 1] else lower_band.iat[i],
        )

        # Determine trend
        if close_vals[i] > final_upper[i - 1]:
            st_bool[i] = True
        elif close_vals[i] < final_lower[i - 1]:
            st_bool[i] = False
        else:
            st_bool[i] = st_bool[i - 1]
            if st_bool[i]:
                final_lower[i] = final_lower[i - 1]
            else:
                final_upper[i] = final_upper[i - 1]

    df["supertrend"] = st_bool
    df["st_upper"] = final_upper
    df["st_lower"] = final_lower

    return df
