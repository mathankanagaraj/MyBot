# core/signal_engine.py
import pandas as pd
from datetime import timedelta
from core.indicators import add_indicators


def is_candle_complete(candle_time, timeframe, current_time, buffer_sec=2):
    """
    Check if a candle is complete based on its timeframe and current time.
    Adds a small buffer to account for feed delays.
    """
    if isinstance(candle_time, pd.Timestamp):
        candle_time = candle_time.to_pydatetime()
    return current_time >= (candle_time + timedelta(seconds=buffer_sec))


def get_next_candle_close_time(current_time, timeframe):
    """
    Compute next candle close time given a timeframe.
    Handles both naive and timezone-aware datetimes.
    """
    if timeframe == "5min":
        interval_minutes = 5
    elif timeframe == "15min":
        interval_minutes = 15
    else:
        raise ValueError(f"Unsupported timeframe: {timeframe}")

    # Round up to next interval
    delta_min = interval_minutes - (current_time.minute % interval_minutes)
    next_close = current_time.replace(second=0, microsecond=0) + timedelta(
        minutes=delta_min
    )
    return next_close


def get_seconds_until_next_close(current_time, timeframe):
    """
    Get seconds until next candle close, with minimum 5 seconds buffer.
    """
    next_close = get_next_candle_close_time(current_time, timeframe)
    seconds = (next_close - current_time).total_seconds()
    return max(5, int(seconds) + 2)  # small extra buffer


# --- Resampling & Indicator Pipeline --- #


def resample_from_1m(df1m: pd.DataFrame, current_time=None):
    """
    Resample 1-minute bars to 5-minute and 15-minute, removing incomplete candles.
    Calculates standard indicators (EMA, SMA, VWAP, MACD, RSI, OBV) for complete bars only.
    """
    df = df1m.copy()

    # Remove last 1m candle if incomplete
    if current_time is not None and not df.empty:
        last_1m = df.index[-1]
        if last_1m > current_time - timedelta(seconds=60):
            df = df.iloc[:-1]

    # --- Resample --- #
    def resample(df, timeframe):
        rule = "5min" if timeframe == "5min" else "15min"
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
        # Remove last candle if incomplete
        if current_time is not None and not df_resampled.empty:
            last_time = df_resampled.index[-1]
            if not is_candle_complete(last_time, timeframe, current_time):
                df_resampled = df_resampled.iloc[:-1]
        return df_resampled

    df5m = resample(df, "5min")
    df15m = resample(df, "15min")

    if not df5m.empty:
        df5m = add_indicators(df5m)
    if not df15m.empty:
        df15m = add_indicators(df15m)

    return df5m, df15m


def detect_15m_bias(df15):
    if df15 is None or len(df15) < 10:
        return None

    last = df15.iloc[-2]  # last closed candle
    prev5 = df15.iloc[-7:-2]  # for slopes (5-bar window)

    # -------------------------------
    # TIER 1: Trend Structure (must match)
    # -------------------------------
    bull_structure = last["close"] > last["ema50"]
    bear_structure = last["close"] < last["ema50"]

    # If structure is ambiguous, no trend
    if not (bull_structure or bear_structure):
        return None

    # -------------------------------
    # TIER 2: Confirmation (need 2 out of 3)
    # -------------------------------
    bull_confirms = sum(
        [
            last["close"] > last["vwap"],
            bool(last["supertrend"]),  # Ensure boolean
            last["macd_hist"] > 0 and prev5["macd_hist"].mean() < last["macd_hist"],
        ]
    )

    bear_confirms = sum(
        [
            last["close"] < last["vwap"],
            not bool(last["supertrend"]),  # Ensure boolean
            last["macd_hist"] < 0 and prev5["macd_hist"].mean() > last["macd_hist"],
        ]
    )

    # -------------------------------
    # TIER 3: Momentum Filter
    # -------------------------------
    bull_momentum = last["rsi"] > 52
    bear_momentum = last["rsi"] < 48

    # -------------------------------
    # OBV Trend (5-bar slope)
    # -------------------------------
    obv_slope = df15["obv"].iloc[-5:].diff().mean()

    bull_obv = obv_slope > 0
    bear_obv = obv_slope < 0

    # -------------------------------
    # FINAL DECISION
    # -------------------------------
    if bull_structure and bull_confirms >= 2 and bull_momentum and bull_obv:
        return "BULL"

    if bear_structure and bear_confirms >= 2 and bear_momentum and bear_obv:
        return "BEAR"

    return None


def detect_5m_entry(df5, bias):
    if df5 is None or len(df5) < 30:
        return False, {"reason": "insufficient_data"}

    last = df5.iloc[-2]
    prev = df5.iloc[-3]

    # Compute SMA20 if missing
    if "sma20" not in df5.columns:
        df5["sma20"] = df5["close"].rolling(20).mean()

    sma20_last = df5["sma20"].iloc[-2]
    sma20_prev = df5["sma20"].iloc[-3]

    # ---- STRUCTURE (must match bias) ----
    if bias == "BULL":
        structure_ok = last["close"] > sma20_last
    else:
        structure_ok = last["close"] < sma20_last

    if not structure_ok:
        return False, {"reason": "trend_structure_fail"}

    # ---- CORE CONFIRMATIONS (2 out of 3) ----
    if bias == "BULL":
        confirm_list = [
            last["close"] > last["vwap"],
            last["ema9"] > last["ema21"],
            last["macd_hist"] > 0,
        ]
    else:
        confirm_list = [
            last["close"] < last["vwap"],
            last["ema9"] < last["ema21"],
            last["macd_hist"] < 0,
        ]

    confirmations = sum(confirm_list)
    if confirmations < 2:
        return False, {"reason": "core_confirmations_fail"}

    # ---- PRICE ACTION FILTER ----
    # Instead of requiring both candles to be green/red,
    # check for breakout or trend-continuation structure.
    if bias == "BULL":
        pa_ok = (
            last["close"] > prev["high"]  # breakout
            or last["close"] > last["open"]  # bullish body
        )
    else:
        pa_ok = (
            last["close"] < prev["low"]  # breakdown
            or last["close"] < last["open"]  # bearish body
        )

    if not pa_ok:
        return False, {"reason": "price_action_fail"}

    # ---- FINAL CHECK: PREVIOUS HOLD OF TREND ----
    if bias == "BULL" and prev["close"] < sma20_prev:
        return False, {"reason": "previous_candle_not_trending"}
    if bias == "BEAR" and prev["close"] > sma20_prev:
        return False, {"reason": "previous_candle_not_trending"}

    return True, {"type": bias, "price": last["close"]}
