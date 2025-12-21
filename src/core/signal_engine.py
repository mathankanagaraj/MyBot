# core/signal_engine.py
import pandas as pd
from datetime import timedelta
from core.indicators import add_indicators
from core.logger import logger


def is_candle_complete(candle_time, timeframe, current_time, buffer_sec=2):
    """
    Check if a candle is complete based on its timeframe and current time.
    Adds a small buffer to account for feed delays.
    """
    # Compare using POSIX timestamps to avoid offset-naive vs offset-aware issues
    try:
        candle_ts = pd.Timestamp(candle_time).timestamp()
        current_ts = pd.Timestamp(current_time).timestamp()
    except Exception:
        # Fallback to original comparison if conversion fails
        if isinstance(candle_time, pd.Timestamp):
            candle_time = candle_time.to_pydatetime()
        return current_time >= (candle_time + timedelta(seconds=buffer_sec))

    return current_ts >= (candle_ts + float(buffer_sec))


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


def prepare_bars_with_indicators(
    df: pd.DataFrame, timeframe: str = "15min", current_time=None
):
    """
    Prepare bars with indicators, removing incomplete candles.
    Used for direct-fetched 15m/5m bars (no resampling needed).

    Args:
        df: DataFrame with OHLCV bars at the specified timeframe
        timeframe: "5min" or "15min"
        current_time: Optional current time to filter incomplete candles

    Returns:
        DataFrame with indicators added and incomplete candles removed
    """
    if df is None or df.empty:
        return pd.DataFrame()

    df_prepared = df.copy()

    # Remove last candle if incomplete
    if current_time is not None and not df_prepared.empty:
        last_time = df_prepared.index[-1]
        if not is_candle_complete(last_time, timeframe, current_time):
            logger.debug(
                f"[prepare_bars] Dropping incomplete {timeframe} candle: {last_time}"
            )
            df_prepared = df_prepared.iloc[:-1]

    if not df_prepared.empty:
        logger.debug(
            f"[prepare_bars] Before indicators: {len(df_prepared)} bars, index range: {df_prepared.index[0]} to {df_prepared.index[-1]}"
        )
        logger.debug(
            f"[prepare_bars] Last 3 closes before indicators: {df_prepared['close'].tail(3).tolist()}"
        )

        df_prepared = add_indicators(df_prepared)

        # Validate indicator calculation
        if "rsi" in df_prepared.columns and len(df_prepared) > 0:
            logger.debug(
                f"[prepare_bars] After indicators - Last 3 RSI values: {df_prepared['rsi'].tail(3).tolist()}"
            )
            logger.debug(
                f"[prepare_bars] After indicators - Last 3 SuperTrend: {df_prepared['supertrend'].tail(3).tolist()}"
            )
            logger.debug(
                f"[prepare_bars] After indicators - Last close: {df_prepared['close'].iloc[-1]:.2f}, Last RSI: {df_prepared['rsi'].iloc[-1]:.2f}"
            )

    return df_prepared


def resample_from_1m(df1m: pd.DataFrame, current_time=None):
    """
    Resample 1-minute bars to 5-minute and 15-minute, removing incomplete candles.
    Calculates standard indicators (EMA, SMA, VWAP, MACD, RSI, OBV) for complete bars only.

    NOTE: This function is DEPRECATED for IBKR - use direct 15m/5m fetching instead.
    Still used for Angel One NSE markets.
    """
    df = df1m.copy()

    # Remove last 1m candle if incomplete — compare via epoch seconds to avoid tz mismatches
    if current_time is not None and not df.empty:
        last_1m = df.index[-1]
        try:
            last_ts = pd.Timestamp(last_1m).timestamp()
            curr_ts = pd.Timestamp(current_time).timestamp()
            if last_ts > (curr_ts - 60):
                logger.debug(
                    "[resample] Dropping incomplete 1m candle: %s (current_time: %s)",
                    last_1m,
                    current_time,
                )
                df = df.iloc[:-1]
        except Exception:
            # Fallback to datetime comparison if timestamp conversion fails
            try:
                if last_1m > current_time - timedelta(seconds=60):
                    logger.debug(
                        "[resample] Dropping incomplete 1m candle: %s (current_time: %s)",
                        last_1m,
                        current_time,
                    )
                    df = df.iloc[:-1]
            except Exception:
                # Give up silently; we'll handle incomplete candles later
                logger.debug(
                    "[resample] Could not determine completeness of last 1m candle"
                )

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

    # Diagnostic logs: show recent index timestamps and tzinfo
    try:
        logger.debug(
            "[resample] last 1m indexes: %s", df.index[-5:].astype(str).tolist()
        )
    except Exception:
        logger.debug("[resample] last 1m indexes: (insufficient)")
    try:
        logger.debug("[resample] 5m indexes: %s", df5m.index[-5:].astype(str).tolist())
    except Exception:
        logger.debug("[resample] 5m indexes: (insufficient)")
    try:
        logger.debug(
            "[resample] 15m indexes: %s", df15m.index[-5:].astype(str).tolist()
        )
    except Exception:
        logger.debug("[resample] 15m indexes: (insufficient)")

    if not df5m.empty:
        df5m = add_indicators(df5m)
    if not df15m.empty:
        df15m = add_indicators(df15m)

    return df5m, df15m


# ============================================================================
# OPTIMIZED STRATEGY FUNCTIONS (SuperTrend/VWAP/RSI Strategy)
# ============================================================================


def detect_15m_bias_optimized(df15, symbol="UNKNOWN"):
    """
    Detect 15-minute bias using optimized SuperTrend + VWAP + RSI strategy.

    Flow:
    - BULLISH: Price > SuperTrend AND Price > VWAP AND RSI(14) > 45
    - BEARISH: Price < SuperTrend AND Price < VWAP AND RSI(14) < 55

    Args:
        df15: DataFrame with 15-minute bars and indicators
        symbol: Symbol name for logging

    Returns:
        dict with keys: bias ("BULL"/"BEAR"/None), price, details
    """
    if df15 is None or len(df15) < 20:
        logger.debug(f"[{symbol}] 15m bias (optimized): Insufficient data")
        return {"bias": None, "reason": "insufficient_data"}

    last = df15.iloc[-1]

    # Extract values
    price = last.get("close")
    supertrend = last.get("supertrend")  # True = bullish (price > ST)
    vwap = last.get("vwap")
    rsi14 = last.get("rsi")

    # Logging
    logger.info(f"[{symbol}] 🔷 15m BIAS CHECK (Optimized Strategy)")
    logger.info(f"[{symbol}]   Time: {last.name}")
    logger.info(f"[{symbol}]   Price: {price:.2f}")
    logger.info(f"[{symbol}]   SuperTrend: {'BULLISH' if supertrend else 'BEARISH'}")
    logger.info(f"[{symbol}]   VWAP: {f'{vwap:.2f}' if vwap else 'N/A'}")
    logger.info(
        f"[{symbol}]   RSI(14): {f'{rsi14:.2f}' if rsi14 is not None else 'N/A'}"
    )

    # Check for missing indicators
    if None in [price, vwap, rsi14] or supertrend is None:
        logger.warning(f"[{symbol}]   ⚠️ Missing indicators")
        return {"bias": None, "reason": "missing_indicators"}

    # BULLISH BIAS CHECK
    check_bull_st = supertrend  # Price > SuperTrend
    check_bull_vwap = price > vwap

    logger.info(f"[{symbol}]   BULL Checks:")
    logger.info(f"[{symbol}]     Price > SuperTrend: {'✅' if check_bull_st else '❌'}")
    logger.info(
        f"[{symbol}]     Price > VWAP: {'✅' if check_bull_vwap else '❌'} ({price:.2f} > {vwap:.2f})"
    )

    if check_bull_st and check_bull_vwap:
        logger.info(f"[{symbol}]   ✅ BULLISH BIAS DETECTED")
        return {
            "bias": "BULL",
            "price": price,
            "details": {"supertrend": "bullish", "vwap": vwap},
        }

    # BEARISH BIAS CHECK
    check_bear_st = not supertrend  # Price < SuperTrend
    check_bear_vwap = price < vwap

    logger.info(f"[{symbol}]   BEAR Checks:")
    logger.info(f"[{symbol}]     Price < SuperTrend: {'✅' if check_bear_st else '❌'}")
    logger.info(
        f"[{symbol}]     Price < VWAP: {'✅' if check_bear_vwap else '❌'} ({price:.2f} < {vwap:.2f})"
    )

    if check_bear_st and check_bear_vwap:
        logger.info(f"[{symbol}]   ✅ BEARISH BIAS DETECTED")
        return {
            "bias": "BEAR",
            "price": price,
            "details": {"supertrend": "bearish", "vwap": vwap},
        }

    # No bias
    logger.info(f"[{symbol}]   ❌ NO BIAS DETECTED")
    return {"bias": None, "reason": "conditions_not_met"}


def detect_5m_entry_optimized(df5, bias, symbol="UNKNOWN", last_entry_time=None):
    """
    Detect 5-minute entry using optimized RSI pullback strategy.

    CALL Entry Flow:
    1. Price > EMA(20)
    2. RSI(5) touched below 35 recently (pullback confirmation)
    3. RSI(5) crossed above 40 (ignition)
    4. Volume > Volume MA(20)
    5. Candle is GREEN
    6. EMA not flat
    7. Min time since last entry > 15 min

    PUT Entry Flow (mirror):
    1. Price < EMA(20)
    2. RSI(5) touched above 65 recently
    3. RSI(5) crossed below 60
    4. Volume > Volume MA(20)
    5. Candle is RED
    6. EMA not flat
    7. Min time since last entry > 15 min

    Args:
        df5: DataFrame with 5m bars and indicators
        bias: "BULL" or "BEAR"
        symbol: Symbol name
        last_entry_time: Timestamp of last entry (for minimum gap check)

    Returns:
        dict with keys: signal ("CALL"/"PUT"/None), price, filters_passed, filters_failed
    """
    from core.config import (
        EMA_PERIOD,
        RSI_5M_PERIOD,
        VOLUME_MA_PERIOD,
        MIN_TIME_BETWEEN_ENTRIES_MINUTES,
        EMA_FLATNESS_THRESHOLD_PCT,
    )
    from core.indicators import (
        calculate_rsi,
        calculate_ema,
        calculate_volume_ma,
        check_ema_flatness,
        check_candle_color,
    )

    if df5 is None or len(df5) < 30:
        logger.debug(f"[{symbol}] 5m entry (optimized): Insufficient data")
        return {"signal": None, "reason": "insufficient_data"}

    last = df5.iloc[-1]
    price = last.get("close")

    logger.info(f"[{symbol}] 🔷 5m ENTRY CHECK (Optimized Strategy)")
    logger.info(f"[{symbol}]   Time: {last.name}")
    logger.info(f"[{symbol}]   Bias: {bias}")
    logger.info(f"[{symbol}]   Price: {price:.2f}")

    filters_passed = {}
    filters_failed = {}

    # Filter 1: Bias Alignment
    if bias not in ["BULL", "BEAR"]:
        logger.info(f"[{symbol}]   ❌ No active bias")
        return {"signal": None, "reason": "no_bias"}
    filters_passed["bias_alignment"] = True

    # Calculate indicators
    ema20 = calculate_ema(df5["close"], period=EMA_PERIOD)
    rsi5_current = calculate_rsi(df5["close"], period=RSI_5M_PERIOD)

    # Get RSI(5) for last 3 candles for pullback confirmation
    rsi5_recent = []
    for i in range(min(3, len(df5))):
        idx = -(i + 1)
        rsi_val = calculate_rsi(
            df5["close"].iloc[:idx] if idx < -1 else df5["close"], period=RSI_5M_PERIOD
        )
        if rsi_val is not None:
            rsi5_recent.insert(0, rsi_val)

    volume_ma = calculate_volume_ma(df5["volume"], period=VOLUME_MA_PERIOD)
    current_volume = last.get("volume")

    # Get last 6 EMA values for flatness check
    ema20_series = []
    for i in range(min(6, len(df5))):
        idx = -(i + 1)
        ema_val = calculate_ema(
            df5["close"].iloc[:idx] if idx < -1 else df5["close"], period=EMA_PERIOD
        )
        if ema_val is not None:
            ema20_series.insert(0, ema_val)

    logger.info(f"[{symbol}]   EMA(20): {f'{ema20:.2f}' if ema20 else 'N/A'}")
    logger.info(
        f"[{symbol}]   RSI(5): {f'{rsi5_current:.2f}' if rsi5_current else 'N/A'}"
    )
    logger.info(f"[{symbol}]   RSI(5) recent: {rsi5_recent}")
    logger.info(
        f"[{symbol}]   Volume: {current_volume}, MA: {f'{volume_ma:.0f}' if volume_ma else 'N/A'}"
    )

    # === CALL ENTRY FLOW ===
    if bias == "BULL":
        # Filter 2: Price > EMA(20)
        if ema20 is None or price <= ema20:
            filters_failed["price_vs_ema"] = (
                f"Price {price:.2f} <= EMA20 {f'{ema20:.2f}' if ema20 else 'N/A'}"
            )
            logger.info(f"[{symbol}]   ❌ {filters_failed['price_vs_ema']}")
            return {
                "signal": None,
                "filters_passed": filters_passed,
                "filters_failed": filters_failed,
            }
        filters_passed["price_vs_ema"] = f"Price > EMA20 ({price:.2f} > {ema20:.2f})"
        logger.info(f"[{symbol}]   ✅ {filters_passed['price_vs_ema']}")

        # Filter 3: RSI(5) Pullback (recent < 50) - REMOVED STRICT DEAD ZONE
        rsi_was_low = any(r < 50 for r in rsi5_recent) if rsi5_recent else False
        if not rsi_was_low:
            filters_failed["rsi_pullback"] = (
                f"RSI never dipped < 50 recently (no pullback)"
            )
            logger.info(f"[{symbol}]   ❌ {filters_failed['rsi_pullback']}")
            return {
                "signal": None,
                "filters_passed": filters_passed,
                "filters_failed": filters_failed,
            }
        filters_passed["rsi_pullback"] = f"RSI dipped < 50 recently"
        logger.info(f"[{symbol}]   ✅ {filters_passed['rsi_pullback']}")

        # Filter 4: RSI(5) crossed above 40 (Ignition)
        if rsi5_current is None or rsi5_current <= 40:
            filters_failed["rsi_ignition"] = (
                f"RSI {f'{rsi5_current:.2f}' if rsi5_current else 'N/A'} <= 40"
            )
            logger.info(f"[{symbol}]   ❌ {filters_failed['rsi_ignition']}")
            return {
                "signal": None,
                "filters_passed": filters_passed,
                "filters_failed": filters_failed,
            }
        filters_passed["rsi_ignition"] = f"RSI crossed above 40 ({rsi5_current:.2f})"
        logger.info(f"[{symbol}]   ✅ {filters_passed['rsi_ignition']}")

        expected_direction = "BULL"
        signal_type = "CALL"

    # === PUT ENTRY FLOW ===
    else:  # BEAR
        # Filter 2: Price < EMA(20)
        if ema20 is None or price >= ema20:
            filters_failed["price_vs_ema"] = (
                f"Price {price:.2f} >= EMA20 {f'{ema20:.2f}' if ema20 else 'N/A'}"
            )
            logger.info(f"[{symbol}]   ❌ {filters_failed['price_vs_ema']}")
            return {
                "signal": None,
                "filters_passed": filters_passed,
                "filters_failed": filters_failed,
            }
        filters_passed["price_vs_ema"] = f"Price < EMA20 ({price:.2f} < {ema20:.2f})"
        logger.info(f"[{symbol}]   ✅ {filters_passed['price_vs_ema']}")

        # Filter 3: RSI(5) Pullback (recent > 50) - REMOVED STRICT DEAD ZONE
        rsi_was_high = any(r > 50 for r in rsi5_recent) if rsi5_recent else False
        if not rsi_was_high:
            filters_failed["rsi_pullback"] = (
                f"RSI never rose > 50 recently (no pullback)"
            )
            logger.info(f"[{symbol}]   ❌ {filters_failed['rsi_pullback']}")
            return {
                "signal": None,
                "filters_passed": filters_passed,
                "filters_failed": filters_failed,
            }
        filters_passed["rsi_pullback"] = f"RSI rose > 50 recently"
        logger.info(f"[{symbol}]   ✅ {filters_passed['rsi_pullback']}")

        # Filter 4: RSI(5) crossed below 60
        if rsi5_current is None or rsi5_current >= 60:
            filters_failed["rsi_ignition"] = (
                f"RSI {f'{rsi5_current:.2f}' if rsi5_current else 'N/A'} >= 60"
            )
            logger.info(f"[{symbol}]   ❌ {filters_failed['rsi_ignition']}")
            return {
                "signal": None,
                "filters_passed": filters_passed,
                "filters_failed": filters_failed,
            }
        filters_passed["rsi_ignition"] = f"RSI crossed below 60 ({rsi5_current:.2f})"
        logger.info(f"[{symbol}]   ✅ {filters_passed['rsi_ignition']}")

        expected_direction = "BEAR"
        signal_type = "PUT"

    # Filter 5: Volume > Volume MA (SKIP FOR INDICES)
    is_index = "NIFTY" in symbol or "BANKNIFTY" in symbol
    if not is_index:
        if volume_ma is None or current_volume is None or current_volume <= volume_ma:
            filters_failed["volume"] = (
                f"Volume {current_volume} <= MA {f'{volume_ma:.0f}' if volume_ma else 'N/A'}"
            )
            logger.info(f"[{symbol}]   ❌ {filters_failed['volume']}")
            return {
                "signal": None,
                "filters_passed": filters_passed,
                "filters_failed": filters_failed,
            }
        filters_passed["volume"] = (
            f"Volume > MA ({current_volume:.0f} > {volume_ma:.0f})"
        )
        logger.info(f"[{symbol}]   ✅ {filters_passed['volume']}")
    else:
        filters_passed["volume"] = "Skipped (Index)"
        logger.info(f"[{symbol}]   ✅ Volume check skipped for index")

    # Filter 6: Candle Color
    candle_color_ok = check_candle_color(last, expected_direction)
    if not candle_color_ok:
        filters_failed["candle_color"] = (
            f"Candle not {'GREEN' if expected_direction == 'BULL' else 'RED'}"
        )
        logger.info(f"[{symbol}]   ❌ {filters_failed['candle_color']}")
        return {
            "signal": None,
            "filters_passed": filters_passed,
            "filters_failed": filters_failed,
        }
    filters_passed["candle_color"] = (
        f"Candle is {'GREEN' if expected_direction == 'BULL' else 'RED'}"
    )
    logger.info(f"[{symbol}]   ✅ {filters_passed['candle_color']}")

    # Filter 7: EMA Not Flat
    if ema20_series and len(ema20_series) >= 2:
        ema_is_flat = check_ema_flatness(
            ema20_series, price, EMA_FLATNESS_THRESHOLD_PCT
        )
        if ema_is_flat:
            filters_failed["ema_flatness"] = "EMA is flat (ranging market)"
            logger.info(f"[{symbol}]   ❌ {filters_failed['ema_flatness']}")
            return {
                "signal": None,
                "filters_passed": filters_passed,
                "filters_failed": filters_failed,
            }
        filters_passed["ema_flatness"] = "EMA has slope (trending market)"
        logger.info(f"[{symbol}]   ✅ {filters_passed['ema_flatness']}")

    # Filter 8: Minimum Time Between Entries
    if last_entry_time is not None:
        time_diff = (
            pd.Timestamp(last.name) - pd.Timestamp(last_entry_time)
        ).total_seconds() / 60
        if time_diff < MIN_TIME_BETWEEN_ENTRIES_MINUTES:
            filters_failed["time_gap"] = (
                f"Only {time_diff:.1f}min since last entry (min: {MIN_TIME_BETWEEN_ENTRIES_MINUTES})"
            )
            logger.info(f"[{symbol}]   ❌ {filters_failed['time_gap']}")
            return {
                "signal": None,
                "filters_passed": filters_passed,
                "filters_failed": filters_failed,
            }
        filters_passed["time_gap"] = f"{time_diff:.1f}min since last entry"
        logger.info(f"[{symbol}]   ✅ {filters_passed['time_gap']}")

    # ALL FILTERS PASSED
    logger.info(f"[{symbol}]   ✅ {signal_type} ENTRY SIGNAL CONFIRMED at {price:.2f}")
    logger.info(
        f"[{symbol}]   Filters passed: {len(filters_passed)}, failed: {len(filters_failed)}"
    )

    return {
        "signal": signal_type,
        "price": price,
        "filters_passed": filters_passed,
        "filters_failed": filters_failed,
    }
