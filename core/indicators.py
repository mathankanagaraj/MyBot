# core/indicators.py
import numpy as np
import pandas as pd


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy().sort_index()
    # EMAs
    df["ema9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()

    # VWAP per day (skip for indices without volume)
    has_volume = "volume" in df.columns and df["volume"].sum() > 0

    if has_volume:
        df["date"] = df.index.date
        tp = (df["high"] + df["low"] + df["close"]) / 3.0
        pv = tp * df["volume"]
        df["vwap"] = pv.groupby(df["date"]).cumsum() / df["volume"].groupby(df["date"]).cumsum()
    else:
        # For indices without volume, use close price as VWAP substitute
        df["vwap"] = df["close"]

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_sig"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_sig"]

    # OBV (skip for indices without volume)
    if has_volume:
        obv = [0]
        for i in range(1, len(df)):
            if df["close"].iat[i] > df["close"].iat[i - 1]:
                obv.append(obv[-1] + df["volume"].iat[i])
            elif df["close"].iat[i] < df["close"].iat[i - 1]:
                obv.append(obv[-1] - df["volume"].iat[i])
            else:
                obv.append(obv[-1])
        df["obv"] = obv
    else:
        # For indices, use price momentum as OBV substitute
        df["obv"] = df["close"].diff().cumsum()

    # ATR 14
    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = (df["high"] - df["close"].shift()).abs()
    df["tr3"] = (df["low"] - df["close"].shift()).abs()
    df["tr"] = df[["tr1", "tr2", "tr3"]].max(axis=1)
    df["atr14"] = df["tr"].ewm(span=14, adjust=False).mean()

    # RSI
    delta = df["close"].diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss
    df["rsi"] = 100 - (100 / (1 + rs))

    # SuperTrend
    # Basic implementation of SuperTrend
    # ATR for SuperTrend (usually 10)
    high = df["high"]
    low = df["low"]
    close = df["close"]

    # Calculate TR for SuperTrend
    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 10, adjust=False).mean()  # Default period 10

    multiplier = 3.0
    hl2 = (high + low) / 2
    final_upperband = hl2 + (multiplier * atr)
    final_lowerband = hl2 - (multiplier * atr)

    # Initialize columns
    # Iterative calculation for SuperTrend (requires previous values)
    # Note: This is slow in Python loops but necessary for SuperTrend logic unless vectorized carefully
    # We will use a simple loop for correctness as dataframes are small (intraday)

    # Convert to numpy for speed
    c_np = close.values
    fu_np = final_upperband.values
    fl_np = final_lowerband.values
    st_np = np.zeros(len(df), dtype=bool)
    st_np[0] = True  # default

    # We need to maintain state of bands
    upper_band = fu_np.copy()
    lower_band = fl_np.copy()

    for i in range(1, len(df)):
        # Upper Band
        if c_np[i - 1] > upper_band[i - 1]:
            upper_band[i] = max(upper_band[i], upper_band[i - 1])
        else:
            upper_band[i] = fu_np[i]

        # Lower Band
        if c_np[i - 1] < lower_band[i - 1]:
            lower_band[i] = min(lower_band[i], lower_band[i - 1])
        else:
            lower_band[i] = fl_np[i]

        # Trend
        if c_np[i] > upper_band[i - 1]:
            st_np[i] = True
        elif c_np[i] < lower_band[i - 1]:
            st_np[i] = False
        else:
            st_np[i] = st_np[i - 1]
            # Adjust bands based on trend
            if st_np[i]:
                lower_band[i] = lower_band[i - 1]  # Keep lower band if bullish
            else:
                upper_band[i] = upper_band[i - 1]  # Keep upper band if bearish

    df["supertrend"] = st_np
    # For debugging/visualization
    df["st_upper"] = upper_band
    df["st_lower"] = lower_band

    df.drop(columns=["tr1", "tr2", "tr3", "tr"], inplace=True, errors="ignore")
    if "date" in df.columns:
        df.drop(columns=["date"], inplace=True)
    return df
