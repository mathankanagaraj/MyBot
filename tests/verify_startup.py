import sys
import os
from datetime import datetime, time, timedelta
import pytz

IST = pytz.timezone("Asia/Kolkata")

def test_startup_logic():
    print("\n--- Testing Startup Logic ---")
    
    # Test cases: (Current Time, Expected Action)
    test_cases = [
        ("08:55:00", "SLEEP until 09:00"),
        ("09:00:00", "START"),
        ("12:00:00", "START"),
        ("15:29:59", "START"),
        ("15:30:00", "SLEEP until tomorrow 09:00"),
        ("16:00:00", "SLEEP until tomorrow 09:00"),
    ]
    
    start_time = time(9, 0)
    end_time = time(15, 30)
    
    for time_str, expected in test_cases:
        t = datetime.strptime(time_str, "%H:%M:%S").time()
        
        is_active = start_time <= t < end_time
        
        if is_active:
            action = "START"
        else:
            if t >= end_time:
                action = "SLEEP until tomorrow 09:00"
            else:
                action = "SLEEP until 09:00"
                
        status = "✅ PASS" if action == expected else f"❌ FAIL (Expected {expected}, Got {action})"
        print(f"Time: {time_str} IST -> Action: {action} | {status}")

if __name__ == "__main__":
    test_startup_logic()
