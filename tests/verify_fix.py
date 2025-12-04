import asyncio
import sys
import os

# Add src to path
sys.path.append(os.path.join(os.getcwd(), "src"))

from core.angel_client import AngelClient
from core.logger import logger

# Mock logger to avoid clutter
logger.setLevel("ERROR")

async def verify_fix():
    print("Verifying fix for get_symbol_token...")
    
    client = AngelClient(enable_rate_limiting=False)
    
    # Mock scrip_master loading to avoid full download if possible, 
    # but for accurate test we should probably just load it or mock the data structure
    # Let's try to load it properly to be sure
    print("Loading scrip master...")
    success = await client.load_scrip_master()
    if not success:
        print("Failed to load scrip master")
        return

    test_cases = [
        ("TCS", "NSE", "Expected Token for Underlying"),
        ("TCS30DEC253200CE", "NFO", "Expected Token for Option"),
        ("TCS30DEC253120CE", "NFO", "Expected Token for Option")
    ]
    
    for symbol, exchange, desc in test_cases:
        print(f"\nTesting {symbol} on {exchange} ({desc})...")
        token = client.get_symbol_token(symbol, exchange)
        
        if token:
            print(f"✅ SUCCESS: Found token {token} for {symbol}")
        else:
            print(f"❌ FAILED: Token not found for {symbol}")

if __name__ == "__main__":
    asyncio.run(verify_fix())
