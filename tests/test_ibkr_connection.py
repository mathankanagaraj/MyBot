#!/usr/bin/env python3
"""
Test script to verify IBKR connection and basic functionality.
Run this before full integration to ensure IB Gateway is working.
"""
import asyncio
import sys

# Add src to path
sys.path.insert(0, 'src')

from core.ibkr_client import IBKRClient
from core.ibkr_utils import is_us_market_open, get_us_et_now


async def test_ibkr_connection():
    """Test IBKR connection and basic operations"""
    
    print("=" * 60)
    print("IBKR Connection Test")
    print("=" * 60)
    
    # Check market status
    now_et = get_us_et_now()
    market_open = is_us_market_open()
    
    print(f"\n📅 Current Time (ET): {now_et.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📊 US Market Status: {'🟢 OPEN' if market_open else '🔴 CLOSED'}")
    
    # Create IB client
    print("\n🔗 Connecting to IB Gateway...")
    client = IBKRClient()
    
    try:
        await client.connect_async()
        
        if not client.connected:
            print("❌ Failed to connect to IB Gateway")
            print("\nTroubleshooting:")
            print("1. Ensure TWS or IB Gateway is running")
            print("2. Check port 7497 (paper) or 7496 (live) is correct")
            print("3. Verify API connections are enabled in TWS/Gateway settings")
            return False
        
        print("✅ Connected to IB Gateway successfully!")
        
        # Test account summary
        print("\n💰 Getting account summary...")
        summary = await client.get_account_summary_async()
        
        if summary:
            print(f"✅ Available Funds: ${summary.get('AvailableFunds', 0):,.2f}")
            print(f"✅ Net Liquidation: ${summary.get('NetLiquidation', 0):,.2f}")
        
        # Test historical data fetch
        print("\n📊 Testing historical data fetch for SPY...")
        df_spy = await client.req_historic_1m("SPY", duration_days=1)
        
        if df_spy is not None and not df_spy.empty:
            print(f"✅ Fetched {len(df_spy)} 1-minute bars for SPY")
            print(f"   Latest bar: {df_spy.index[-1]}")
            print(f"   Close: ${df_spy['close'].iloc[-1]:.2f}")
        else:
            print("⚠️ Could not fetch historical data (might be closed)")
        
        # Test option chain (only if market is open)
        if market_open:
            print("\n🔍 Testing option chain retrieval for SPY...")
            spy_price = df_spy['close'].iloc[-1] if df_spy is not None else 450.0
            options = await client.get_option_chain("SPY", spy_price)
            
            if options:
                print(f"✅ Found {len(options)} option contracts")
                print(f"   Sample: {options[0]['symbol'] if options else 'N/A'}")
            else:
                print("⚠️ No options found")
        else:
            print("\n⏸️  Skipping option chain test (market closed)")
        
        # Disconnect
        client.disconnect()
        print("\n✅ All tests passed!")
        print("\n" + "=" * 60)
        print("IBKR is ready for trading!")
        print("=" * 60)
        return True
        
    except Exception as e:
        print(f"\n❌ Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    print("\n🧪 Starting IBKR Connection Test...\n")
    
    result = asyncio.run(test_ibkr_connection())
    
    sys.exit(0 if result else 1)
