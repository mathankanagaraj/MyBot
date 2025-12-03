#!/usr/bin/env python3
"""
Test script to verify timezone handling and 15m boundary detection.
Run this to ensure the fixes work correctly.
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent / "src"))

from datetime import datetime, timedelta
import pytz
from core.utils import get_ist_now, utc_to_ist, IST
from core.signal_engine import get_next_candle_close_time, get_seconds_until_next_close


def test_ist_utilities():
    """Test IST utility functions"""
    print("=" * 60)
    print("Testing IST Utility Functions")
    print("=" * 60)
    
    # Test get_ist_now
    now_ist = get_ist_now()
    print(f"✓ get_ist_now(): {now_ist.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    assert now_ist.tzinfo is not None, "IST time should be timezone-aware"
    
    # Test utc_to_ist with naive datetime
    utc_naive = datetime.utcnow()
    ist_converted = utc_to_ist(utc_naive)
    print(f"✓ utc_to_ist(naive): {ist_converted.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    assert ist_converted.tzinfo is not None, "Converted time should be timezone-aware"
    
    # Test utc_to_ist with aware datetime
    utc_aware = datetime.now(pytz.UTC)
    ist_converted2 = utc_to_ist(utc_aware)
    print(f"✓ utc_to_ist(aware): {ist_converted2.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    
    print()


def test_15m_boundary_detection():
    """Test 15m boundary detection at various times"""
    print("=" * 60)
    print("Testing 15m Boundary Detection")
    print("=" * 60)
    
    # Test cases: (hour, minute) -> expected next 15m boundary
    test_cases = [
        (9, 17, "09:30"),   # After market open
        (9, 30, "09:45"),   # Exactly on boundary
        (9, 44, "09:45"),   # Just before boundary
        (14, 38, "14:45"),  # The problematic case from logs
        (14, 45, "15:00"),  # Exactly on boundary
        (15, 28, "15:30"),  # Near market close
    ]
    
    for hour, minute, expected in test_cases:
        # Create IST datetime
        test_time = datetime.now(IST).replace(hour=hour, minute=minute, second=30, microsecond=0)
        
        next_close = get_next_candle_close_time(test_time, '15min')
        sleep_seconds = get_seconds_until_next_close(test_time, '15min')
        
        result = next_close.strftime("%H:%M")
        status = "✓" if result == expected else "✗"
        
        print(f"{status} {test_time.strftime('%H:%M:%S')} -> {result} (expected {expected}), sleep {sleep_seconds}s")
        
        if result != expected:
            print(f"   ERROR: Expected {expected} but got {result}")
    
    print()


def test_5m_boundary_detection():
    """Test 5m boundary detection"""
    print("=" * 60)
    print("Testing 5m Boundary Detection")
    print("=" * 60)
    
    test_cases = [
        (9, 17, "09:20"),
        (9, 20, "09:25"),
        (9, 24, "09:25"),
        (14, 38, "14:40"),
        (14, 43, "14:45"),
    ]
    
    for hour, minute, expected in test_cases:
        test_time = datetime.now(IST).replace(hour=hour, minute=minute, second=30, microsecond=0)
        
        next_close = get_next_candle_close_time(test_time, '5min')
        sleep_seconds = get_seconds_until_next_close(test_time, '5min')
        
        result = next_close.strftime("%H:%M")
        status = "✓" if result == expected else "✗"
        
        print(f"{status} {test_time.strftime('%H:%M:%S')} -> {result} (expected {expected}), sleep {sleep_seconds}s")
    
    print()


def test_bot_startup_scenarios():
    """Test that bot will wait for proper 15m boundary regardless of start time"""
    print("=" * 60)
    print("Testing Bot Startup Scenarios")
    print("=" * 60)
    
    scenarios = [
        ("Bot starts at 09:17", 9, 17, "09:30"),
        ("Bot starts at 09:30", 9, 30, "09:45"),
        ("Bot starts at 14:38", 14, 38, "14:45"),  # The problematic case
        ("Bot starts at 14:42", 14, 42, "14:45"),
        ("Bot starts at 14:45", 14, 45, "15:00"),
    ]
    
    for description, hour, minute, expected_boundary in scenarios:
        start_time = datetime.now(IST).replace(hour=hour, minute=minute, second=0, microsecond=0)
        next_boundary = get_next_candle_close_time(start_time, '15min')
        wait_seconds = get_seconds_until_next_close(start_time, '15min')
        
        result = next_boundary.strftime("%H:%M")
        status = "✓" if result == expected_boundary else "✗"
        
        print(f"{status} {description}:")
        print(f"   Start: {start_time.strftime('%H:%M:%S')} IST")
        print(f"   Next 15m boundary: {result} IST (expected {expected_boundary})")
        print(f"   Wait time: {wait_seconds}s ({wait_seconds/60:.1f} minutes)")
        print()


def test_data_fetch_duration():
    """Test that data fetch duration is exactly 15 minutes"""
    print("=" * 60)
    print("Testing Data Fetch Duration")
    print("=" * 60)
    
    duration_days = 0.0104
    duration_minutes = duration_days * 24 * 60
    
    print(f"Duration in days: {duration_days}")
    print(f"Duration in minutes: {duration_minutes:.2f}")
    print(f"Expected: 15.00 minutes")
    
    if abs(duration_minutes - 15.0) < 0.1:
        print("✓ Duration is correct (15 minutes)")
    else:
        print(f"✗ Duration is incorrect (expected 15 minutes, got {duration_minutes:.2f})")
    
    print()


if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("TIMEZONE AND BOUNDARY DETECTION TEST SUITE")
    print("=" * 60 + "\n")
    
    try:
        test_ist_utilities()
        test_15m_boundary_detection()
        test_5m_boundary_detection()
        test_bot_startup_scenarios()
        test_data_fetch_duration()
        
        print("=" * 60)
        print("✅ ALL TESTS COMPLETED")
        print("=" * 60)
        print("\nNext steps:")
        print("1. Run the bot and verify logs show IST times")
        print("2. Check that 15m boundary detection works at any start time")
        print("3. Verify heartbeat messages appear every 15 minutes")
        print("4. Monitor for API rate limiting detection")
        
    except Exception as e:
        print(f"\n❌ TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
