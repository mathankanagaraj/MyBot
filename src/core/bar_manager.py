# core/bar_manager.py
import asyncio
from collections import deque
from datetime import datetime, timedelta
import pandas as pd
from core.logger import logger
from core.signal_engine import resample_from_1m


class BarManager:
    """
    Manages a rolling window of 1-minute bars for a symbol.
    Thread-safe for async operations.
    """

    def __init__(self, symbol, max_bars=2880):
        """
        Args:
            symbol: Trading symbol
            max_bars: Maximum number of 1m bars to keep (default: 2880 = 2 days)
        """
        self.symbol = symbol
        self.max_bars = max_bars
        self.bars = deque(maxlen=max_bars)
        self.lock = asyncio.Lock()
        self.last_bar_time = None

        # Real-time bar construction
        self.current_bar = None
        self.current_bar_start = None

        # ========================================================================
        # INTELLIGENT INDICATOR CACHING (Optimized Strategy)
        # ========================================================================
        # Cache for 15m indicators (bias detection)
        self.cache_15m = {
            "supertrend": None,  # SuperTrend direction (bool)
            "vwap": None,  # VWAP price
            "rsi14": None,  # RSI(14)
            "price": None,  # Close price
            "last_candle_time": None,  # Timestamp for cache invalidation
        }

        # Cache for 5m indicators (entry detection)
        self.cache_5m = {
            "ema20": None,  # EMA(20) for structure
            "ema20_series": None,  # Last 6 EMA values for flatness check
            "rsi5": None,  # RSI(5) current value
            "rsi5_recent": None,  # Last 3 RSI(5) values for pullback confirm
            "volume_ma": None,  # Volume MA(20)
            "current_volume": None,  # Current candle volume
            "last_candle_time": None,  # Timestamp for cache invalidation
        }

    async def process_tick(self, price: float, timestamp: datetime, volume: int = 0):
        """
        Process a real-time tick and update the current bar.

        Args:
            price: Last Traded Price (LTP)
            timestamp: Tick timestamp (datetime object)
            volume: Tick volume (optional)
        """
        async with self.lock:
            # Round down to nearest minute to get bar start time
            bar_start = timestamp.replace(second=0, microsecond=0)

            # If we have a current bar but this tick belongs to a new minute
            if (
                self.current_bar
                and self.current_bar_start
                and bar_start > self.current_bar_start
            ):
                # Finalize the previous bar
                logger.debug(
                    "[%s] 🕯️ Finalizing bar: %s | O:%.2f H:%.2f L:%.2f C:%.2f",
                    self.symbol,
                    self.current_bar_start.strftime("%H:%M"),
                    self.current_bar["open"],
                    self.current_bar["high"],
                    self.current_bar["low"],
                    self.current_bar["close"],
                )
                self.bars.append(self.current_bar)
                self.last_bar_time = self.current_bar_start

                # Reset for new bar
                self.current_bar = None
                self.current_bar_start = None

            # Initialize new bar if needed
            if self.current_bar is None:
                self.current_bar_start = bar_start
                self.current_bar = {
                    "datetime": bar_start,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": volume,
                }
                logger.debug(
                    "[%s] 🆕 New bar started at %s",
                    self.symbol,
                    bar_start.strftime("%H:%M:%S"),
                )
            else:
                # Update existing bar
                self.current_bar["high"] = max(self.current_bar["high"], price)
                self.current_bar["low"] = min(self.current_bar["low"], price)
                self.current_bar["close"] = price
                self.current_bar["volume"] += volume  # Accumulate volume if available

    async def add_bar(self, bar_dict):
        """
        Add a new 1-minute bar to the buffer.

        Args:
            bar_dict: Dict with keys: datetime, open, high, low, close, volume
        """
        async with self.lock:
            bar_time = bar_dict["datetime"]

            # Avoid duplicates
            if self.last_bar_time and bar_time <= self.last_bar_time:
                logger.debug(
                    "[%s] Skipping duplicate/old bar: %s", self.symbol, bar_time
                )
                return

            self.bars.append(bar_dict)
            self.last_bar_time = bar_time
            logger.debug(
                "[%s] Added bar: %s (total: %d)", self.symbol, bar_time, len(self.bars)
            )

    async def get_bars_df(self, lookback_minutes=None):
        """
        Get bars as a pandas DataFrame.

        Args:
            lookback_minutes: If specified, only return last N minutes of data

        Returns:
            DataFrame indexed by datetime with columns: open, high, low, close, volume
        """
        async with self.lock:
            if not self.bars:
                return pd.DataFrame()

            bars_list = list(self.bars)

            if lookback_minutes:
                cutoff_time = datetime.utcnow() - timedelta(minutes=lookback_minutes)
                bars_list = [b for b in bars_list if b["datetime"] >= cutoff_time]

            if not bars_list:
                return pd.DataFrame()

            df = pd.DataFrame(bars_list)
            df = df.set_index("datetime")[["open", "high", "low", "close", "volume"]]
            return df

    async def finalize_bar(self):
        """
        Force finalize the current bar if it exists.
        Useful for end-of-day processing to ensure the last minute is captured
        even if no new tick arrives to trigger the closure.
        """
        async with self.lock:
            if self.current_bar and self.current_bar_start:
                logger.debug(
                    "[%s] 🕯️ Force finalizing last bar: %s",
                    self.symbol,
                    self.current_bar_start.strftime("%H:%M:%S"),
                )
                self.bars.append(self.current_bar)
                self.last_bar_time = self.current_bar_start
                self.current_bar = None
                self.current_bar_start = None

    async def get_resampled(self, lookback_minutes=None, current_time=None):
        """
        Get 5m and 15m resampled bars with indicators.

        Args:
            lookback_minutes: If specified, only use last N minutes of 1m data
            current_time: If specified, filter out incomplete candles

        Returns:
            Tuple of (df5m, df15m) DataFrames with indicators
        """
        df1m = await self.get_bars_df(lookback_minutes)

        if df1m.empty:
            return pd.DataFrame(), pd.DataFrame()

        df5, df15 = resample_from_1m(df1m, current_time=current_time)
        return df5, df15

    async def initialize_from_historical(self, historical_df):
        """
        Initialize the bar buffer from historical data.

        Args:
            historical_df: DataFrame indexed by datetime with OHLCV columns
        """
        async with self.lock:
            self.bars.clear()

            for idx, row in historical_df.iterrows():
                bar_dict = {
                    "datetime": idx,
                    "open": row["open"],
                    "high": row["high"],
                    "low": row["low"],
                    "close": row["close"],
                    "volume": row["volume"],
                }
                self.bars.append(bar_dict)

            if self.bars:
                self.last_bar_time = self.bars[-1]["datetime"]
                logger.info(
                    "[%s] Initialized with %d historical bars",
                    self.symbol,
                    len(self.bars),
                )

    def get_bar_count(self):
        """Get current number of bars in buffer."""
        return len(self.bars)

    # ========================================================================
    # INTELLIGENT CACHING METHODS (Optimized Strategy)
    # ========================================================================

    async def get_cached_15m_indicators(self, df15):
        """
        Get cached 15m indicators, recomputing only if stale.

        Args:
            df15: DataFrame with 15m bars and indicators already computed

        Returns:
            dict: Cached indicator values
        """
        async with self.lock:
            if df15.empty:
                return None

            last_candle_time = df15.index[-1]

            # Check if cache is stale
            if self.cache_15m["last_candle_time"] != last_candle_time:
                # Recompute indicators
                logger.debug(
                    f"[{self.symbol}] 🔄 Updating 15m cache (new candle: {last_candle_time})"
                )

                last_row = df15.iloc[-1]

                self.cache_15m["supertrend"] = last_row.get("supertrend", None)
                self.cache_15m["vwap"] = last_row.get("vwap", None)
                self.cache_15m["rsi14"] = last_row.get("rsi", None)
                self.cache_15m["price"] = last_row.get("close", None)
                self.cache_15m["last_candle_time"] = last_candle_time

                logger.debug(
                    f"[{self.symbol}] ✅ 15m cache updated - "
                    f"ST: {self.cache_15m['supertrend']}, "
                    f"VWAP: {self.cache_15m['vwap']:.2f if self.cache_15m['vwap'] else 'N/A'}, "
                    f"RSI: {self.cache_15m['rsi14']:.2f if self.cache_15m['rsi14'] else 'N/A'}"
                )
            else:
                logger.debug(f"[{self.symbol}] ♻️ Using cached 15m indicators")

            return self.cache_15m.copy()

    async def get_cached_5m_indicators(self, df5):
        """
        Get cached 5m indicators, recomputing only if stale.

        Args:
            df5: DataFrame with 5m bars and indicators already computed

        Returns:
            dict: Cached indicator values
        """
        async with self.lock:
            if df5.empty:
                return None

            last_candle_time = df5.index[-1]

            # Check if cache is stale
            if self.cache_5m["last_candle_time"] != last_candle_time:
                # Recompute indicators
                logger.debug(
                    f"[{self.symbol}] 🔄 Updating 5m cache (new candle: {last_candle_time})"
                )

                last_row = df5.iloc[-1]

                # Get EMA(20) current value
                self.cache_5m["ema20"] = last_row.get(
                    "sma20", None
                )  # Using sma20 column from indicators.py

                # Get last 6 EMA values for flatness check
                if "sma20" in df5.columns and len(df5) >= 6:
                    self.cache_5m["ema20_series"] = df5["sma20"].tail(6).tolist()
                else:
                    self.cache_5m["ema20_series"] = None

                # Get RSI(5) - need to calculate from close prices
                if len(df5) >= 6:  # Need at least 6 candles for RSI(5)
                    from core.indicators import calculate_rsi

                    self.cache_5m["rsi5"] = calculate_rsi(df5["close"], period=5)
                    # Get last 3 RSI(5) values for pullback confirmation
                    rsi5_series = []
                    for i in range(min(3, len(df5))):
                        if len(df5) >= 6 + i:
                            rsi_val = calculate_rsi(
                                df5["close"].iloc[: -(i) if i > 0 else len(df5)],
                                period=5,
                            )
                            if rsi_val is not None:
                                rsi5_series.insert(0, rsi_val)
                    self.cache_5m["rsi5_recent"] = rsi5_series if rsi5_series else None
                else:
                    self.cache_5m["rsi5"] = None
                    self.cache_5m["rsi5_recent"] = None

                # Volume indicators
                self.cache_5m["current_volume"] = last_row.get("volume", None)
                if "volume" in df5.columns and len(df5) >= 20:
                    self.cache_5m["volume_ma"] = df5["volume"].tail(20).mean()
                else:
                    self.cache_5m["volume_ma"] = None

                self.cache_5m["last_candle_time"] = last_candle_time

                logger.debug(
                    f"[{self.symbol}] ✅ 5m cache updated - "
                    f"EMA20: {self.cache_5m['ema20']:.2f if self.cache_5m['ema20'] else 'N/A'}, "
                    f"RSI5: {self.cache_5m['rsi5']:.2f if self.cache_5m['rsi5'] else 'N/A'}, "
                    f"Vol: {self.cache_5m['current_volume']}, VolMA: {self.cache_5m['volume_ma']:.0f if self.cache_5m['volume_ma'] else 'N/A'}"
                )
            else:
                logger.debug(f"[{self.symbol}] ♻️ Using cached 5m indicators")

            return self.cache_5m.copy()

    def clear_cache(self):
        """Clear all cached indicators (useful for testing or reset)."""
        logger.info(f"[{self.symbol}] 🗑️ Clearing indicator caches")
        self.cache_15m = {k: None for k in self.cache_15m.keys()}
        self.cache_5m = {k: None for k in self.cache_5m.keys()}
