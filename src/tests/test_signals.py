import unittest
import pandas as pd
import numpy as np
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.indicators import add_indicators
from core.signal_engine import detect_15m_bias, detect_5m_entry

class TestSignals(unittest.TestCase):
    def create_bullish_df(self):
        dates = pd.date_range(start='2023-01-01', periods=100, freq='15T')
        df = pd.DataFrame({
            'open': np.linspace(100, 110, 100),
            'high': np.linspace(101, 111, 100),
            'low': np.linspace(99, 109, 100),
            'close': np.linspace(100.5, 110.5, 100), # Up trend
            'volume': np.linspace(1000, 2000, 100)
        }, index=dates)
        return add_indicators(df)

    def test_detect_15m_bias_bull(self):
        df = self.create_bullish_df()
        # Force indicators to match BULL conditions for the last rows
        # cond_bull_vwap = last['close'] > last['vwap']
        # cond_bull_ema = last['close'] > last['ema50']
        # cond_bull_st = last['supertrend'] == True
        # cond_bull_macd = (last['macd'] > 0) and (last['macd_hist'] > 0)
        # cond_bull_obv = last['obv'] > prev['obv']
        # cond_bull_rsi = last['rsi'] > 55
        
        # We need to manually tweak the last row to ensure all conditions are met if the random/linear data isn't perfect
        # But linear up trend should mostly satisfy.
        
        # Let's mock the values directly to be sure logic works
        last_idx = df.index[-2]
        prev_idx = df.index[-3]
        
        df.loc[last_idx, 'close'] = 150
        df.loc[last_idx, 'vwap'] = 140
        df.loc[last_idx, 'ema50'] = 130
        df.loc[last_idx, 'supertrend'] = True
        df.loc[last_idx, 'macd'] = 1.0
        df.loc[last_idx, 'macd_hist'] = 0.5
        df.loc[last_idx, 'obv'] = 10000
        df.loc[last_idx, 'rsi'] = 60
        
        df.loc[prev_idx, 'obv'] = 9000
        
        bias = detect_15m_bias(df)
        self.assertEqual(bias, 'BULL')

if __name__ == '__main__':
    unittest.main()
