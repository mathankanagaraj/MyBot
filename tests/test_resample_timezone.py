"""
Test resample_from_1m timezone handling.
Run with: PYTHONPATH=src python3 tests/test_resample_timezone.py
"""
from datetime import datetime, timedelta, timezone
import pandas as pd
from core.signal_engine import resample_from_1m


def make_df1m(end_time_naive, minutes=10):
    idx = [end_time_naive - timedelta(minutes=i) for i in reversed(range(minutes))]
    df = pd.DataFrame({
        'open': [100 + i for i in range(minutes)],
        'high': [101 + i for i in range(minutes)],
        'low': [99 + i for i in range(minutes)],
        'close': [100 + i for i in range(minutes)],
        'volume': [1000 + i * 10 for i in range(minutes)],
    }, index=pd.DatetimeIndex(idx))
    return df


def run_test():
    # Naive 1m index
    now_naive = datetime.now().replace(second=0, microsecond=0)
    df1m = make_df1m(now_naive, minutes=12)

    # Case A: current_time is timezone-aware (UTC)
    now_aware = datetime.now(timezone.utc)
    print('Current times:')
    print('  now_naive:', now_naive, type(now_naive))
    print('  now_aware:', now_aware, type(now_aware))

    print('\nCalling resample_from_1m with timezone-aware current_time...')
    df5_a, df15_a = resample_from_1m(df1m, current_time=now_aware)
    print('Result shapes (aware):', df5_a.shape, df15_a.shape)

    # Case B: current_time is naive
    now_naive2 = datetime.now().replace(second=0, microsecond=0)
    print('\nCalling resample_from_1m with naive current_time...')
    df5_b, df15_b = resample_from_1m(df1m, current_time=now_naive2)
    print('Result shapes (naive):', df5_b.shape, df15_b.shape)

    # Show last indexes
    print('\nLast indexes:')
    print('  1m last:', df1m.index[-1])
    print('  5m last aware:', df5_a.index[-1] if not df5_a.empty else 'empty')
    print('  15m last aware:', df15_a.index[-1] if not df15_a.empty else 'empty')


if __name__ == '__main__':
    run_test()
