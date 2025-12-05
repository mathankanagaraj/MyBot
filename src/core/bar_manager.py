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
