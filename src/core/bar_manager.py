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
        
    async def add_bar(self, bar_dict):
        """
        Add a new 1-minute bar to the buffer.
        
        Args:
            bar_dict: Dict with keys: datetime, open, high, low, close, volume
        """
        async with self.lock:
            bar_time = bar_dict['datetime']
            
            # Avoid duplicates
            if self.last_bar_time and bar_time <= self.last_bar_time:
                logger.debug("[%s] Skipping duplicate/old bar: %s", self.symbol, bar_time)
                return
            
            self.bars.append(bar_dict)
            self.last_bar_time = bar_time
            logger.debug("[%s] Added bar: %s (total: %d)", self.symbol, bar_time, len(self.bars))
    
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
                bars_list = [b for b in bars_list if b['datetime'] >= cutoff_time]
            
            if not bars_list:
                return pd.DataFrame()
            
            df = pd.DataFrame(bars_list)
            df = df.set_index('datetime')[['open', 'high', 'low', 'close', 'volume']]
            return df
    
    async def get_resampled(self, lookback_minutes=None):
        """
        Get 5m and 15m resampled bars with indicators.
        
        Args:
            lookback_minutes: If specified, only use last N minutes of 1m data
            
        Returns:
            Tuple of (df5m, df15m) DataFrames with indicators
        """
        df1m = await self.get_bars_df(lookback_minutes)
        
        if df1m.empty:
            return pd.DataFrame(), pd.DataFrame()
        
        df5, df15 = resample_from_1m(df1m)
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
                    'datetime': idx,
                    'open': row['open'],
                    'high': row['high'],
                    'low': row['low'],
                    'close': row['close'],
                    'volume': row['volume']
                }
                self.bars.append(bar_dict)
            
            if self.bars:
                self.last_bar_time = self.bars[-1]['datetime']
                logger.info("[%s] Initialized with %d historical bars", self.symbol, len(self.bars))
    
    def get_bar_count(self):
        """Get current number of bars in buffer."""
        return len(self.bars)
