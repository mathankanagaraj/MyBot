"""
Test Angel One State Persistence
"""
import json
import sys
from pathlib import Path
from unittest.mock import Mock

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from core.angelone.trade_state import TradeStateManager as AngelTradeStateManager

def test_angel_state_persistence():
    """Test that Angel One state persists correctly"""
    print("=" * 70)
    print("TEST: Angel One State Persistence with Empty Broker Response")
    print("=" * 70)
    
    # Create state manager
    manager = AngelTradeStateManager()
    
    print(f"\n1. Initial state:")
    print(f"   Traded symbols: {list(manager.traded_symbols)}")
    print(f"   Open positions: {list(manager.open_positions)}")
    
    # Simulate a trade
    print(f"\n2. Simulating NIFTY trade...")
    manager.mark_symbol_traded("NIFTY")
    manager.mark_position_opened("NIFTY")
    
    print(f"   Traded symbols: {list(manager.traded_symbols)}")
    print(f"   Open positions: {list(manager.open_positions)}")
    
    # Simulate restart
    print(f"\n3. Simulating bot restart...")
    manager2 = AngelTradeStateManager()
    
    print(f"   Traded symbols: {list(manager2.traded_symbols)}")
    print(f"   Open positions: {list(manager2.open_positions)}")
    
    # Sync with empty broker response
    print(f"\n4. Syncing with broker (EMPTY response)...")
    manager2.sync_with_broker([])
    
    print(f"   Traded symbols after sync: {list(manager2.traded_symbols)}")
    print(f"   Open positions after sync: {list(manager2.open_positions)}")
    
    # Verify state preserved
    assert "NIFTY" in manager2.traded_symbols, "❌ NIFTY should be in traded_symbols"
    assert "NIFTY" in manager2.open_positions, "❌ NIFTY should be in open_positions"
    
    print(f"\n✅ TEST PASSED: Angel One state preserved correctly")
    print("=" * 70)


def test_angel_sync_with_positions():
    """Test sync with actual broker positions"""
    print("\n" + "=" * 70)
    print("TEST: Angel One Sync with Broker Positions")
    print("=" * 70)
    
    manager = AngelTradeStateManager()
    manager.mark_symbol_traded("BANKNIFTY")
    manager.mark_position_opened("BANKNIFTY")
    
    print(f"\n1. Initial state:")
    print(f"   Traded: {list(manager.traded_symbols)}")
    print(f"   Open: {list(manager.open_positions)}")
    
    # Mock broker position
    mock_position = {
        "tradingsymbol": "BANKNIFTY30DEC2559300PE",
        "netqty": "50"
    }
    
    print(f"\n2. Syncing with broker position: {mock_position['tradingsymbol']}")
    manager.sync_with_broker([mock_position])
    
    print(f"   Traded after sync: {list(manager.traded_symbols)}")
    print(f"   Open after sync: {list(manager.open_positions)}")
    
    assert "BANKNIFTY" in manager.traded_symbols, "❌ BANKNIFTY should be in traded_symbols"
    assert "BANKNIFTY" in manager.open_positions, "❌ BANKNIFTY should be in open_positions"
    
    print(f"\n✅ TEST PASSED: Symbol matching working correctly")
    print("=" * 70)


if __name__ == "__main__":
    try:
        test_angel_state_persistence()
        test_angel_sync_with_positions()
        print("\n🎉 ALL ANGEL ONE TESTS PASSED!")
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ TEST ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
