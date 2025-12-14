# core/signal_engine.py
import pandas as pd
from datetime import timedelta
from core.indicators import add_indicators
from core.logger import logger
from core.config import EMA_CROSSOVER_WINDOW


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


def prepare_bars_with_indicators(df: pd.DataFrame, timeframe: str = "15min", current_time=None):
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
            logger.debug(f"[prepare_bars] Dropping incomplete {timeframe} candle: {last_time}")
            df_prepared = df_prepared.iloc[:-1]
    
    if not df_prepared.empty:
        logger.debug(f"[prepare_bars] Before indicators: {len(df_prepared)} bars, index range: {df_prepared.index[0]} to {df_prepared.index[-1]}")
        logger.debug(f"[prepare_bars] Last 3 closes before indicators: {df_prepared['close'].tail(3).tolist()}")
        
        df_prepared = add_indicators(df_prepared)
        
        # Validate indicator calculation
        if 'rsi' in df_prepared.columns and len(df_prepared) > 0:
            logger.debug(f"[prepare_bars] After indicators - Last 3 RSI values: {df_prepared['rsi'].tail(3).tolist()}")
            logger.debug(f"[prepare_bars] After indicators - Last 3 SuperTrend: {df_prepared['supertrend'].tail(3).tolist()}")
            logger.debug(f"[prepare_bars] After indicators - Last close: {df_prepared['close'].iloc[-1]:.2f}, Last RSI: {df_prepared['rsi'].iloc[-1]:.2f}")
    
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
                logger.debug("[resample] Dropping incomplete 1m candle: %s (current_time: %s)", last_1m, current_time)
                df = df.iloc[:-1]
        except Exception:
            # Fallback to datetime comparison if timestamp conversion fails
            try:
                if last_1m > current_time - timedelta(seconds=60):
                    logger.debug("[resample] Dropping incomplete 1m candle: %s (current_time: %s)", last_1m, current_time)
                    df = df.iloc[:-1]
            except Exception:
                # Give up silently; we'll handle incomplete candles later
                logger.debug("[resample] Could not determine completeness of last 1m candle")

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
        logger.debug("[resample] last 1m indexes: %s", df.index[-5:].astype(str).tolist())
    except Exception:
        logger.debug("[resample] last 1m indexes: (insufficient)")
    try:
        logger.debug("[resample] 5m indexes: %s", df5.index[-5:].astype(str).tolist())
    except Exception:
        logger.debug("[resample] 5m indexes: (insufficient)")
    try:
        logger.debug("[resample] 15m indexes: %s", df15.index[-5:].astype(str).tolist())
    except Exception:
        logger.debug("[resample] 15m indexes: (insufficient)")

    if not df5m.empty:
        df5m = add_indicators(df5m)
    if not df15m.empty:
        df15m = add_indicators(df15m)

    return df5m, df15m


def detect_15m_bias(df15, symbol="UNKNOWN"):
    """
    Detect 15-minute trend bias with detailed logging.
    
    Args:
        df15: DataFrame with 15-minute bars (must have incomplete candles already filtered)
        symbol: Symbol name for logging
    """
    if df15 is None or len(df15) < 10:
        logger.debug(f"[{symbol}] 15m bias: Insufficient data ({len(df15) if df15 is not None else 0} bars)")
        return None

    # Use -1 (last candle) since incomplete candles are already filtered by prepare_bars_with_indicators()
    last = df15.iloc[-1]  # last COMPLETE closed candle
    prev = df15.iloc[-2]  # previous candle
    prev2 = df15.iloc[-3]  # 2 candles ago
    prev5 = df15.iloc[-6:-1]  # for slopes (5-bar window ending at last candle)
    
    # Log current price levels with candle timestamp
    # Note: last.name is the candle's timestamp (already in UTC from IBKR data)
    try:
        import pytz
        candle_time_utc = pd.Timestamp(last.name).tz_localize('UTC')
        candle_time_et = candle_time_utc.tz_convert('America/New_York')
        time_str = candle_time_et.strftime('%Y-%m-%d %H:%M:%S ET')
    except Exception:
        time_str = str(last.name)
    
    logger.info(f"[{symbol}] 📊 15m BIAS CHECK - Candle closed at {time_str}")
    logger.info(f"[{symbol}]   ⏰ Total bars in dataset: {len(df15)} | Using last complete bar")
    logger.info(f"[{symbol}]   📈 OHLC: O={last['open']:.2f} H={last['high']:.2f} L={last['low']:.2f} C={last['close']:.2f}")
    
    # Safe value extraction (pandas-ta can return None for insufficient data)
    ema9 = last.get('ema9', None)
    ema21 = last.get('ema21', None)
    ema50 = last.get('ema50', None)
    vwap = last.get('vwap', None)
    prev_vwap = prev.get('vwap', None)
    macd_hist = last.get('macd_hist', None)
    prev_macd_hist = prev.get('macd_hist', None)
    rsi = last.get('rsi', None)
    prev_rsi = prev.get('rsi', None)
    st_upper = last.get('st_upper', None)
    st_lower = last.get('st_lower', None)
    obv = last.get('obv', 0)
    
    # Format values safely (handle None)
    ema9_str = f"{ema9:.2f}" if ema9 is not None else "N/A"
    ema21_str = f"{ema21:.2f}" if ema21 is not None else "N/A"
    ema50_str = f"{ema50:.2f}" if ema50 is not None else "N/A"
    vwap_str = f"{vwap:.2f}" if vwap is not None else "N/A"
    prev_vwap_str = f"{prev_vwap:.2f}" if prev_vwap is not None else "N/A"
    macd_str = f"{macd_hist:.4f}" if macd_hist is not None else "N/A"
    prev_macd_str = f"{prev_macd_hist:.4f}" if prev_macd_hist is not None else "N/A"
    macd_avg5 = prev5["macd_hist"].mean() if prev5["macd_hist"].notna().any() else None
    macd_avg5_str = f"{macd_avg5:.4f}" if macd_avg5 is not None else "N/A"
    rsi_str = f"{rsi:.2f}" if rsi is not None else "N/A"
    prev_rsi_str = f"{prev_rsi:.2f}" if prev_rsi is not None else "N/A"
    st_upper_str = f"{st_upper:.2f}" if st_upper is not None else "N/A"
    st_lower_str = f"{st_lower:.2f}" if st_lower is not None else "N/A"
    obv_slope = df15['obv'].iloc[-5:].diff().mean() if df15['obv'].notna().any() else None
    obv_slope_str = f"{obv_slope:.0f}" if obv_slope is not None else "N/A"
    
    # Log with formatted strings
    logger.info(f"[{symbol}]   📊 EMAs: EMA9={ema9_str} | EMA21={ema21_str} | EMA50={ema50_str}")
    logger.info(f"[{symbol}]   💰 VWAP: {vwap_str} (prev: {prev_vwap_str})")
    logger.info(f"[{symbol}]   📉 Prev 3 closes: [{prev2['close']:.2f}, {prev['close']:.2f}, {last['close']:.2f}]")
    logger.info(f"[{symbol}]   📊 MACD: {macd_str} (prev: {prev_macd_str}, avg5: {macd_avg5_str})")
    logger.info(f"[{symbol}]   📊 RSI(14): {rsi_str} | Prev RSI: {prev_rsi_str} | Candle: {'GREEN' if last['close']>last['open'] else 'RED'}")
    logger.info(f"[{symbol}]   📊 SuperTrend: {'BULLISH (price > ST)' if last['supertrend'] else 'BEARISH (price < ST)'} | ST_Upper: {st_upper_str} | ST_Lower: {st_lower_str}")
    logger.info(f"[{symbol}]   📊 OBV Slope (5-bar avg): {obv_slope_str} | Last OBV: {obv:.0f}")

    # -------------------------------
    # TIER 0: Candle Body Color Validation (CRITICAL)
    # -------------------------------
    # At least 2 of the last 3 candles must be colored correctly for the bias
    bullish_candles = sum([
        last["close"] > last["open"],
        prev["close"] > prev["open"],
        prev2["close"] > prev2["open"],
    ])
    bearish_candles = sum([
        last["close"] < last["open"],
        prev["close"] < prev["open"],
        prev2["close"] < prev2["open"],
    ])
    
    logger.info(f"[{symbol}]   Candle Colors: {bullish_candles} bullish, {bearish_candles} bearish (last 3)")
    
    # Most recent candle MUST match the bias
    last_candle_bullish = last["close"] > last["open"]
    last_candle_bearish = last["close"] < last["open"]
    
    logger.info(f"[{symbol}]   Last Candle: {'GREEN ✅' if last_candle_bullish else 'RED ✅' if last_candle_bearish else 'DOJI'}")

    # -------------------------------
    # TIER 1: Trend Structure (must match)
    # -------------------------------
    # Check if indicators are available (pandas-ta returns None for insufficient data)
    if ema50 is None or rsi is None or macd_hist is None:
        logger.warning(f"[{symbol}]   ⚠️ Insufficient data for indicators (need 50+ bars for EMA50)")
        logger.warning(f"[{symbol}]   EMA50: {'✅' if ema50 is not None else '❌'}, RSI: {'✅' if rsi is not None else '❌'}, MACD: {'✅' if macd_hist is not None else '❌'}")
        return None
    
    bull_structure = last["close"] > ema50
    bear_structure = last["close"] < ema50
    
    logger.info(f"[{symbol}]   Structure: BULL={bull_structure}, BEAR={bear_structure}")

    # If structure is ambiguous, no trend
    if not (bull_structure or bear_structure):
        logger.info(f"[{symbol}]   ❌ No clear structure (price near EMA50)")
        return None

    # -------------------------------
    # TIER 2: Confirmation (need 2 out of 3)
    # -------------------------------
    # CRITICAL: MACD histogram must be CLEARLY positive/negative (not just trending)
    # AND momentum must be INCREASING (bull) or DECREASING (bear)
    macd_avg5_val = macd_avg5 if macd_avg5 is not None else 0
    macd_increasing = macd_hist > macd_avg5_val if macd_hist is not None else False
    macd_decreasing = macd_hist < macd_avg5_val if macd_hist is not None else False
    
    macd_clearly_bullish = macd_hist is not None and macd_hist > 0.05 and macd_increasing
    macd_clearly_bearish = macd_hist is not None and macd_hist < -0.05 and macd_decreasing
    
    logger.info(f"[{symbol}]   MACD Analysis:")
    logger.info(f"[{symbol}]     Clearly Bullish: {macd_clearly_bullish} (>{0.05} & increasing)")
    logger.info(f"[{symbol}]     Clearly Bearish: {macd_clearly_bearish} (<{-0.05} & decreasing)")
    
    bull_confirms = sum(
        [
            last["close"] > vwap if vwap is not None else False,
            bool(last.get("supertrend", False)),  # Ensure boolean
            macd_clearly_bullish,
        ]
    )

    bear_confirms = sum(
        [
            last["close"] < vwap if vwap is not None else False,
            not bool(last.get("supertrend", True)),  # Ensure boolean
            macd_clearly_bearish,
        ]
    )
    
    logger.info(f"[{symbol}]   Confirmations: BULL={bull_confirms}/3, BEAR={bear_confirms}/3")
    vwap_cmp = ">" if vwap is not None and last['close'] > vwap else "<"
    logger.info(f"[{symbol}]     Close vs VWAP: {last['close']:.2f} {vwap_cmp} {vwap_str}")

    # -------------------------------
    # TIER 3: Momentum Filter
    # -------------------------------
    bull_momentum = rsi > 52 if rsi is not None else False
    bear_momentum = rsi < 48 if rsi is not None else False
    
    logger.info(f"[{symbol}]   Momentum: BULL={bull_momentum} (RSI>{52}), BEAR={bear_momentum} (RSI<{48})")

    # -------------------------------
    # OBV Trend (5-bar slope)
    # -------------------------------
    obv_slope_val = obv_slope if obv_slope is not None else 0

    bull_obv = obv_slope_val > 0
    bear_obv = obv_slope_val < 0
    
    logger.info(f"[{symbol}]   OBV: BULL={bull_obv}, BEAR={bear_obv} (slope: {obv_slope_str})")

    # -------------------------------
    # FINAL DECISION
    # -------------------------------
    # BULL: Structure + Confirmations + Momentum + OBV + Candle Colors
    if (bull_structure and bull_confirms >= 2 and bull_momentum and bull_obv and
        bullish_candles >= 2 and last_candle_bullish):
        logger.info(f"[{symbol}]   ✅ BULL BIAS DETECTED")
        logger.info(f"[{symbol}]     Structure ✅ | Confirms {bull_confirms}/3 ✅ | Momentum ✅ | OBV ✅ | Candles {bullish_candles}/3 ✅")
        return "BULL"

    # BEAR: Structure + Confirmations + Momentum + OBV + Candle Colors
    if (bear_structure and bear_confirms >= 2 and bear_momentum and bear_obv and
        bearish_candles >= 2 and last_candle_bearish):
        logger.info(f"[{symbol}]   ✅ BEAR BIAS DETECTED")
        logger.info(f"[{symbol}]     Structure ✅ | Confirms {bear_confirms}/3 ✅ | Momentum ✅ | OBV ✅ | Candles {bearish_candles}/3 ✅")
        return "BEAR"
    
    # Log why detection failed with detailed diagnostics
    logger.info(f"[{symbol}]   ❌ NO BIAS DETECTED")

    # Compute per-check booleans for diagnostics
    bull_candle_req = bullish_candles >= 2
    bear_candle_req = bearish_candles >= 2
    last_candle_req_bull = last_candle_bullish
    last_candle_req_bear = last_candle_bearish
    bull_structure_req = bull_structure
    bear_structure_req = bear_structure
    bull_macd_req = macd_clearly_bullish
    bear_macd_req = macd_clearly_bearish
    bull_momentum_req = bull_momentum
    bear_momentum_req = bear_momentum
    bull_obv_req = obv_slope_val > 0
    bear_obv_req = obv_slope_val < 0

    # Log diagnostics in a compact, numeric form
    logger.info(
        f"[{symbol}]   Diagnostics: "
        f"Candles(Bull/Bear)={bullish_candles}/{bearish_candles}, "
        f"Last(Green/Red)={last_candle_bullish}/{last_candle_bearish}, "
        f"EMA50(Close)={last['close']:.2f}/{ema50_str}, "
        f"MACD={macd_str} (avg5={macd_avg5_str}), "
        f"RSI={rsi_str}, OBV_slope={obv_slope_str}"
    )

    # Per-reason verbose lines to aid tuning (only printed when rejected)
    logger.info(f"[{symbol}]   Checks (Bull): CandleReq={bull_candle_req}, LastCandle={last_candle_req_bull}, Structure={bull_structure_req}, MACD={bull_macd_req}, Momentum={bull_momentum_req}, OBV={bull_obv_req}, Confirms={bull_confirms}/3")
    logger.info(f"[{symbol}]   Checks (Bear): CandleReq={bear_candle_req}, LastCandle={last_candle_req_bear}, Structure={bear_structure_req}, MACD={bear_macd_req}, Momentum={bear_momentum_req}, OBV={bear_obv_req}, Confirms={bear_confirms}/3")

    # Helpful hints for quick adjustments
    if not (bull_candle_req or bear_candle_req):
        logger.info(f"[{symbol}]   HINT: Candle color requirement failing (need 2 of 3). Consider MIN_MATCHING_CANDLES change if too strict.")
    if not (bull_macd_req or bear_macd_req):
        logger.info(f"[{symbol}]   HINT: MACD magnitude/strength failing. Consider lowering MACD thresholds for live validation.")
    if not (bull_obv_req or bear_obv_req):
        logger.info(f"[{symbol}]   HINT: OBV slope not aligned with price; synthetic data may differ from live volume patterns.")

    return None


def detect_5m_entry(df5, bias, symbol="UNKNOWN"):
    """
    Detect 5-minute entry signal with detailed logging.
    
    Args:
        df5: DataFrame with 5-minute bars (incomplete candles already filtered)
        bias: "BULL" or "BEAR" from 15m detection
        symbol: Symbol name for logging
    """
    if df5 is None or len(df5) < 30:
        logger.debug(f"[{symbol}] 5m entry: Insufficient data ({len(df5) if df5 is not None else 0} bars)")
        return False, {"reason": "insufficient_data"}

    # Use -1 (last candle) since incomplete candles are already filtered
    last = df5.iloc[-1]  # last COMPLETE closed candle
    prev = df5.iloc[-2]
    prev2 = df5.iloc[-3]

    # Compute SMA20 if missing
    if "sma20" not in df5.columns:
        df5["sma20"] = df5["close"].rolling(20).mean()

    sma20_last = df5["sma20"].iloc[-1]
    sma20_prev = df5["sma20"].iloc[-2]
    
    # Log current 5m levels
    logger.info(f"[{symbol}] 📊 5m ENTRY CHECK for {bias} at {last.name}")
    logger.info(f"[{symbol}]   Price: {last['close']:.2f} | Open: {last['open']:.2f} | High: {last['high']:.2f} | Low: {last['low']:.2f}")
    logger.info(f"[{symbol}]   EMA9: {last['ema9']:.2f} | EMA21: {last['ema21']:.2f} | SMA20: {sma20_last:.2f}")
    logger.info(f"[{symbol}]   VWAP: {last['vwap']:.2f}")
    logger.info(f"[{symbol}]   MACD: {last['macd_hist']:.4f} (prev: {prev['macd_hist']:.4f}, prev2: {prev2['macd_hist']:.4f})")
    logger.info(f"[{symbol}]   RSI: {last['rsi']:.2f}")
    logger.info(f"[{symbol}]   Volume: {last['volume']:.0f} (20-bar avg: {df5['volume'].iloc[-20:].mean():.0f})")

    # ---- STRUCTURE (must match bias) ----
    if bias == "BULL":
        structure_ok = last["close"] > sma20_last
    else:
        structure_ok = last["close"] < sma20_last
    
    logger.info(f"[{symbol}]   Structure: {'✅' if structure_ok else '❌'} (Close {last['close']:.2f} {'>' if bias=='BULL' else '<'} SMA20 {sma20_last:.2f})")

    if not structure_ok:
        logger.info(f"[{symbol}]   ❌ REJECTED: trend_structure_fail")
        return False, {"reason": "trend_structure_fail"}

    # ---- EMA CROSSOVER CONFIRMATION ----
    # Use configurable window to detect if EMA9 crossed EMA21 within the last N candles
    try:
        window = int(EMA_CROSSOVER_WINDOW)
    except Exception:
        window = 3

    # Ensure we have enough history to evaluate the window
    ema_diff = df5['ema9'] - df5['ema21']
    available = len(ema_diff)
    lookback = min(window, available)

    if lookback < 2:
        logger.info(f"[{symbol}]   EMA Crossover: ❌ (insufficient history for window={window})")
        logger.info(f"[{symbol}]   ❌ REJECTED: no_recent_ema_crossover")
        return False, {"reason": "no_recent_ema_crossover"}

    recent = ema_diff.iloc[-lookback:]
    curr_diff = recent.iloc[-1]
    prev_diff_any = recent.iloc[:-1]

    if bias == "BULL":
        # require current diff > 0 and any previous diff <= 0 within the window
        ema_crossed = (curr_diff > 0) and (prev_diff_any.le(0).any())
        logger.info(f"[{symbol}]   EMA Crossover (window={window}): {'✅' if ema_crossed else '❌'}")
        logger.info(f"[{symbol}]     Current diff: {curr_diff:.4f} (EMA9-EMA21)")
        logger.debug(f"[{symbol}]     Recent diffs: {recent.tolist()}")
    else:
        # BEAR: require current diff < 0 and any previous diff >= 0 within the window
        ema_crossed = (curr_diff < 0) and (prev_diff_any.ge(0).any())
        logger.info(f"[{symbol}]   EMA Crossover (window={window}): {'✅' if ema_crossed else '❌'}")
        logger.info(f"[{symbol}]     Current diff: {curr_diff:.4f} (EMA9-EMA21)")
        logger.debug(f"[{symbol}]     Recent diffs: {recent.tolist()}")

    if not ema_crossed:
        logger.info(f"[{symbol}]   ❌ REJECTED: no_recent_ema_crossover")
        return False, {"reason": "no_recent_ema_crossover"}

    # ---- CANDLE COLOR VALIDATION (CRITICAL) ----
    # Last candle MUST match the bias
    last_candle_bullish = last["close"] > last["open"]
    last_candle_bearish = last["close"] < last["open"]
    
    logger.info(f"[{symbol}]   Last Candle: {'GREEN ✅' if last_candle_bullish else 'RED ✅' if last_candle_bearish else 'DOJI'}")
    
    if bias == "BULL" and not last_candle_bullish:
        logger.info(f"[{symbol}]   ❌ REJECTED: last_candle_not_bullish (Close {last['close']:.2f} <= Open {last['open']:.2f})")
        return False, {"reason": "last_candle_not_bullish"}
    if bias == "BEAR" and not last_candle_bearish:
        logger.info(f"[{symbol}]   ❌ REJECTED: last_candle_not_bearish (Close {last['close']:.2f} >= Open {last['open']:.2f})")
        return False, {"reason": "last_candle_not_bearish"}

    # ---- MACD CONFIRMATION (must be clearly positive/negative AND getting stronger) ----
    # Check last 3 candles to see if MACD is strengthening in the right direction
    prev_macd = prev["macd_hist"]
    prev2_macd = prev2["macd_hist"]
    
    if bias == "BULL":
        macd_clear = last["macd_hist"] > 0.02  # Clearly positive
        macd_strengthening = last["macd_hist"] > prev_macd  # Getting MORE positive
        macd_ok = macd_clear and macd_strengthening
        logger.info(f"[{symbol}]   MACD: {'✅' if macd_ok else '❌'}")
        logger.info(f"[{symbol}]     Clear: {macd_clear} (>{0.02})")
        logger.info(f"[{symbol}]     Strengthening: {macd_strengthening} ({last['macd_hist']:.4f} > {prev_macd:.4f})")
    else:
        macd_clear = last["macd_hist"] < -0.02  # Clearly negative
        macd_strengthening = last["macd_hist"] < prev_macd  # Getting MORE negative
        macd_ok = macd_clear and macd_strengthening
        logger.info(f"[{symbol}]   MACD: {'✅' if macd_ok else '❌'}")
        logger.info(f"[{symbol}]     Clear: {macd_clear} (<{-0.02})")
        logger.info(f"[{symbol}]     Strengthening: {macd_strengthening} ({last['macd_hist']:.4f} < {prev_macd:.4f})")

    if not macd_ok:
        if bias == "BULL":
            if not macd_clear:
                logger.info(f"[{symbol}]   ❌ REJECTED: macd_not_clear")
                return False, {"reason": "macd_not_clear"}
            else:
                logger.info(f"[{symbol}]   ❌ REJECTED: macd_weakening_not_strengthening")
                return False, {"reason": "macd_weakening_not_strengthening"}
        else:
            if not macd_clear:
                logger.info(f"[{symbol}]   ❌ REJECTED: macd_not_clear")
                return False, {"reason": "macd_not_clear"}
            else:
                logger.info(f"[{symbol}]   ❌ REJECTED: macd_weakening_not_strengthening")
                return False, {"reason": "macd_weakening_not_strengthening"}

    # ---- VOLUME CONFIRMATION ----
    # Volume should be above 20-bar average
    avg_volume = df5["volume"].iloc[-20:].mean()
    volume_ok = last["volume"] > avg_volume * 1.2  # 20% above average
    
    logger.info(f"[{symbol}]   Volume: {'✅' if volume_ok else '❌'} ({last['volume']:.0f} vs {avg_volume*1.2:.0f} threshold)")

    # ---- RSI MOMENTUM ----
    if bias == "BULL":
        rsi_ok = 45 < last["rsi"] < 70  # Not overbought, has momentum
        logger.info(f"[{symbol}]   RSI: {'✅' if rsi_ok else '❌'} ({last['rsi']:.2f} in 45-70 range)")
    else:
        rsi_ok = 30 < last["rsi"] < 55  # Not oversold, has momentum
        logger.info(f"[{symbol}]   RSI: {'✅' if rsi_ok else '❌'} ({last['rsi']:.2f} in 30-55 range)")

    # ---- CORE CONFIRMATIONS (need at least 3 out of 4) ----
    if bias == "BULL":
        vwap_ok = last["close"] > last["vwap"]
        confirm_list = [vwap_ok, macd_ok, volume_ok, rsi_ok]
        logger.info(f"[{symbol}]   Core Confirmations:")
        logger.info(f"[{symbol}]     VWAP: {'✅' if vwap_ok else '❌'} ({last['close']:.2f} > {last['vwap']:.2f})")
        logger.info(f"[{symbol}]     MACD: {'✅' if macd_ok else '❌'}")
        logger.info(f"[{symbol}]     Volume: {'✅' if volume_ok else '❌'}")
        logger.info(f"[{symbol}]     RSI: {'✅' if rsi_ok else '❌'}")
    else:
        vwap_ok = last["close"] < last["vwap"]
        confirm_list = [vwap_ok, macd_ok, volume_ok, rsi_ok]
        logger.info(f"[{symbol}]   Core Confirmations:")
        logger.info(f"[{symbol}]     VWAP: {'✅' if vwap_ok else '❌'} ({last['close']:.2f} < {last['vwap']:.2f})")
        logger.info(f"[{symbol}]     MACD: {'✅' if macd_ok else '❌'}")
        logger.info(f"[{symbol}]     Volume: {'✅' if volume_ok else '❌'}")
        logger.info(f"[{symbol}]     RSI: {'✅' if rsi_ok else '❌'}")

    confirmations = sum(confirm_list)
    logger.info(f"[{symbol}]     Total: {confirmations}/4")
    
    if confirmations < 3:
        logger.info(f"[{symbol}]   ❌ REJECTED: core_confirmations_fail_{confirmations}/4")
        return False, {"reason": f"core_confirmations_fail_{confirmations}/4"}

    # ---- PRICE ACTION FILTER ----
    # Instead of requiring both candles to be green/red,
    # check for breakout or trend-continuation structure.
    if bias == "BULL":
        pa_ok = (
            last["close"] > prev["high"]  # breakout
            or last["close"] > last["open"]  # bullish body
        )
        logger.info(f"[{symbol}]   Price Action: {'✅' if pa_ok else '❌'}")
        logger.info(f"[{symbol}]     Breakout: {last['close']:.2f} > {prev['high']:.2f} = {last['close'] > prev['high']}")
        logger.info(f"[{symbol}]     Bullish Body: {last['close']:.2f} > {last['open']:.2f} = {last['close'] > last['open']}")
    else:
        pa_ok = (
            last["close"] < prev["low"]  # breakdown
            or last["close"] < last["open"]  # bearish body
        )
        logger.info(f"[{symbol}]   Price Action: {'✅' if pa_ok else '❌'}")
        logger.info(f"[{symbol}]     Breakdown: {last['close']:.2f} < {prev['low']:.2f} = {last['close'] < prev['low']}")
        logger.info(f"[{symbol}]     Bearish Body: {last['close']:.2f} < {last['open']:.2f} = {last['close'] < last['open']}")

    if not pa_ok:
        logger.info(f"[{symbol}]   ❌ REJECTED: price_action_fail")
        return False, {"reason": "price_action_fail"}

    # ---- FINAL CHECK: PREVIOUS HOLD OF TREND ----
    if bias == "BULL":
        prev_trend_ok = prev["close"] >= sma20_prev
        logger.info(f"[{symbol}]   Previous Trend: {'✅' if prev_trend_ok else '❌'} ({prev['close']:.2f} >= {sma20_prev:.2f})")
        if not prev_trend_ok:
            logger.info(f"[{symbol}]   ❌ REJECTED: previous_candle_not_trending")
            return False, {"reason": "previous_candle_not_trending"}
    else:
        prev_trend_ok = prev["close"] <= sma20_prev
        logger.info(f"[{symbol}]   Previous Trend: {'✅' if prev_trend_ok else '❌'} ({prev['close']:.2f} <= {sma20_prev:.2f})")
        if not prev_trend_ok:
            logger.info(f"[{symbol}]   ❌ REJECTED: previous_candle_not_trending")
            return False, {"reason": "previous_candle_not_trending"}
    
    logger.info(f"[{symbol}]   ✅ ENTRY SIGNAL CONFIRMED at {last['close']:.2f}")
    return True, {"type": bias, "price": last["close"]}
