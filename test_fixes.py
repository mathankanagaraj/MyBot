#!/usr/bin/env python3
"""Test script to verify strike price parsing fix"""

import re

def test_strike_parsing():
    """Test the regex-based strike price extraction"""
    
    test_cases = [
        ("RELIANCE30DEC251200PE", 1200),
        ("NIFTY26DEC2421000CE", 21000),
        ("BANKNIFTY26DEC2448000PE", 48000),
        ("INFY27DEC241500CE", 1500),
        ("TCS28DEC243500PE", 3500),
    ]
    
    print("Testing Strike Price Parsing")
    print("=" * 60)
    
    all_passed = True
    
    for symbol_name, expected_strike in test_cases:
        # Use the CORRECTED regex pattern that separates date from strike
        # NSE format: SYMBOLDDMMMYYSTRIKECE/PE
        match = re.search(r'[A-Z]+(\d{2}[A-Z]{3}\d{2})(\d+)(CE|PE)$', symbol_name)
        
        if match:
            date_part = match.group(1)  # e.g., "30DEC25"
            strike_str = match.group(2)  # e.g., "1200"
            option_type = match.group(3)  # CE or PE
            
            # NSE strikes are always in rupees, no conversion needed
            strike = float(strike_str)
            
            passed = (strike == expected_strike)
            status = "✅ PASS" if passed else "❌ FAIL"
            
            print(f"{status} | {symbol_name:30s} → Strike: {strike:8.0f} (expected: {expected_strike})")
            if passed:
                print(f"       | Date: {date_part}, Type: {option_type}")
            
            if not passed:
                all_passed = False
        else:
            print(f"❌ FAIL | {symbol_name:30s} → No match found")
            all_passed = False
    
    print("=" * 60)
    if all_passed:
        print("✅ All tests PASSED!")
    else:
        print("❌ Some tests FAILED!")
    
    return all_passed


def test_timezone_info():
    """Show timezone information"""
    from datetime import datetime, timezone, timedelta
    
    print("\n\nTesting IST Timezone Conversion")
    print("=" * 60)
    
    # IST is UTC+5:30
    ist_offset = timedelta(hours=5, minutes=30)
    ist_tz = timezone(ist_offset)
    
    # Current UTC time
    utc_now = datetime.now(timezone.utc)
    
    # Convert to IST
    ist_now = utc_now.astimezone(ist_tz)
    
    print(f"Current UTC time: {utc_now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Current IST time: {ist_now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"Offset: UTC+5:30")
    print("\nNote: The logger will use pytz for proper timezone handling")
    print("=" * 60)


if __name__ == "__main__":
    # Run tests
    parsing_passed = test_strike_parsing()
    test_timezone_info()
    
    # Exit with appropriate code
    exit(0 if parsing_passed else 1)
