#!/usr/bin/env python3
"""Analyze NSE option symbol format"""

import re

# Real NSE option symbol examples
symbols = [
    "RELIANCE30DEC251200PE",  # RELIANCE, 30-DEC-25, Strike 1200, PE
    "NIFTY26DEC2421000CE",     # NIFTY, 26-DEC-24, Strike 21000, CE
    "BANKNIFTY26DEC2448000PE", # BANKNIFTY, 26-DEC-24, Strike 48000, PE
]

print("Analyzing NSE Option Symbol Format")
print("=" * 80)

for symbol in symbols:
    print(f"\nSymbol: {symbol}")
    
    # Try different regex patterns
    patterns = [
        (r'(\d+)(CE|PE)$', "All digits before CE/PE"),
        (r'[A-Z]+(\d{2}[A-Z]{3}\d{2})(\d+)(CE|PE)$', "Symbol + Date + Strike + Type"),
        (r'([A-Z]+)(\d{2})([A-Z]{3})(\d{2})(\d+)(CE|PE)$', "Full breakdown"),
    ]
    
    for pattern, desc in patterns:
        match = re.search(pattern, symbol)
        if match:
            print(f"  Pattern: {desc}")
            print(f"  Groups: {match.groups()}")
            if len(match.groups()) >= 2:
                print(f"  Strike candidate: {match.group(len(match.groups())-1)}")
