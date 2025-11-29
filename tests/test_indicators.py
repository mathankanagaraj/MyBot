import unittest
import pandas as pd
import numpy as np
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.indicators import add_indicators

class TestIndicators(unittest.TestCase):
    def setUp(self):
        # Create sample data
        dates = pd.date_range(start='2023-01-01', periods=100, freq='5T')
        self.df = pd.DataFrame({
            'open': np.random.randn(100) + 100,
            'high': np.random.randn(100) + 105,
            'low': np.random.randn(100) + 95,
            'close': np.random.randn(100) + 100,
            'volume': np.random.randint(100, 1000, 100)
        }, index=dates)
        
        # Ensure high >= low, high >= close, high >= open, low <= close, low <= open
        self.df['high'] = self.df[['open', 'close', 'high']].max(axis=1)
        self.df['low'] = self.df[['open', 'close', 'low']].min(axis=1)

    def test_indicators_exist(self):
        df_ind = add_indicators(self.df)
        expected_cols = ['ema9', 'ema21', 'ema50', 'vwap', 'macd', 'macd_sig', 'macd_hist', 'obv', 'atr14', 'rsi', 'supertrend']
        for col in expected_cols:
            self.assertIn(col, df_ind.columns, f"{col} missing from indicators")
            
    def test_rsi_range(self):
        df_ind = add_indicators(self.df)
        # RSI should be between 0 and 100 (ignoring NaNs at start)
        rsi = df_ind['rsi'].dropna()
        self.assertTrue(((rsi >= 0) & (rsi <= 100)).all())

    def test_supertrend_values(self):
        df_ind = add_indicators(self.df)
        # SuperTrend should be boolean
        self.assertTrue(df_ind['supertrend'].dtype == bool)

if __name__ == '__main__':
    unittest.main()
