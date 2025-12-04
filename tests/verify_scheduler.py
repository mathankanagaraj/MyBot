import sys
import os
from datetime import datetime, time, timedelta
import pytz

# Add src to path
sys.path.append(os.path.join(os.getcwd(), "src"))

from core.utils import is_market_open, get_seconds_until_market_close
from core.signal_engine import get_next_candle_close_time, get_seconds_until_next_close

IST = pytz.timezone("Asia/Kolkata")

def test_market_open_logic():
    print("\n--- Testing Market Open Logic ---")
    
    # Test cases: (Time string, Expected Result)
    test_cases = [
        ("09:14:59", False),
        ("09:15:00", True),
        ("12:00:00", True),
        ("15:29:59", True),
        ("15:30:00", False),  # Strict close check
        ("15:31:00", False),
    ]
    
    today = datetime.now(IST).date()
    # Ensure today is a weekday for test
    if today.weekday() >= 5:
        today -= timedelta(days=today.weekday() - 4) # Move to Friday
        
    for time_str, expected in test_cases:
        t = datetime.strptime(time_str, "%H:%M:%S").time()
        dt = IST.localize(datetime.combine(today, t))
        
        # Mock UTC time for is_market_open
        dt_utc = dt.astimezone(pytz.utc)
        
        result = is_market_open(dt_utc)
        status = "✅ PASS" if result == expected else f"❌ FAIL (Expected {expected})"
        print(f"Time: {time_str} IST -> Open? {result} | {status}")

def test_scheduler_logic():
    print("\n--- Testing Scheduler Logic ---")
    
    # Test cases: (Current Time, Interval, Expected Next Close)
    test_cases = [
        ("09:16:30", "5min", "09:20:00"),
        ("09:19:59", "5min", "09:20:00"),
        ("09:20:01", "5min", "09:25:00"),
        ("15:26:00", "5min", "15:30:00"),
        ("09:16:30", "15min", "09:30:00"),
        ("09:29:59", "15min", "09:30:00"),
    ]
    
    today = datetime.now(IST).date()
    
    for time_str, interval, expected_str in test_cases:
        t = datetime.strptime(time_str, "%H:%M:%S").time()
        dt = IST.localize(datetime.combine(today, t))
        
        next_close = get_next_candle_close_time(dt, interval)
        seconds = get_seconds_until_next_close(dt, interval)
        
        expected_time = datetime.strptime(expected_str, "%H:%M:%S").time()
        
        match = next_close.time() == expected_time
        status = "✅ PASS" if match else f"❌ FAIL (Expected {expected_str}, Got {next_close.strftime('%H:%M:%S')})"
        
        print(f"Now: {time_str} | Interval: {interval} | Next: {next_close.strftime('%H:%M:%S')} (Wait {seconds}s) | {status}")

if __name__ == "__main__":
    test_market_open_logic()
    test_scheduler_logic()
