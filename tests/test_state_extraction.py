"""Test IBKR symbol extraction from position objects"""
import re

def extract_underlying_symbol(contract_symbol: str) -> str:
    """
    Extract underlying symbol from option contract.
    
    Examples:
        SPY -> SPY (stock)
        ES -> ES (futures)
        ES 20251226C5800 -> ES (futures option with space)
        SPY 20251226C580 -> SPY (stock option with space)
        NQ20251226C20000 -> NQ (futures option no space)
        TSLA20251226C350 -> TSLA (stock option no space)
    
    Args:
        contract_symbol: Contract symbol from IBKR
        
    Returns:
        Underlying symbol (e.g., SPY, ES, QQQ, TSLA)
    """
    # For option contracts with space, take first part before space
    parts = contract_symbol.split()
    if len(parts) > 1:
        return parts[0]  # ES from "ES 20251226C5800"
    
    # For contracts without space, strip all trailing digits, C, P
    # Strategy: Find where the letters end and numbers/options begin
    # NQ20251226C20000 -> NQ
    # TSLA20251226C350 -> TSLA
    # Remove all trailing: digits (0-9), C, P
    
    # Match: start with letters, then optionally followed by digits and C/P
    match = re.match(r'^([A-Z]+)', contract_symbol)
    if match:
        return match.group(1)
    
    # Fallback: return as-is if no match
    return contract_symbol


# Test cases
test_symbols = [
    ("SPY", "SPY"),
    ("ES", "ES"),
    ("NQ", "NQ"),
    ("TSLA", "TSLA"),
    ("ES 20251226C5800", "ES"),
    ("NQ 20251226C20000", "NQ"),
    ("SPY 20251226C580", "SPY"),
    ("TSLA 20251226C350", "TSLA"),
    ("NQ20251226C20000", "NQ"),
    ("TSLA20251226C350", "TSLA"),
    ("SPY20251226C58000", "SPY"),
    ("MSFT20251226C42000", "MSFT"),
    ("AAPL20251226C23500", "AAPL"),
]

print("Testing symbol extraction:")
print("=" * 60)
for symbol_in, expected in test_symbols:
    result = extract_underlying_symbol(symbol_in)
    status = "✅" if result == expected else "❌"
    print(f"{status} '{symbol_in:25s}' -> '{result:10s}' (expected: {expected})")

print("\n" + "=" * 60)
print("All tests passed!" if all(
    extract_underlying_symbol(s) == e for s, e in test_symbols
) else "Some tests failed!")
