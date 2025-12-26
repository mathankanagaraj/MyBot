"""
Test IBKR State Persistence - Verify state file preservation
"""
import json
import sys
from pathlib import Path
from unittest.mock import Mock

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

from core.ibkr.trade_state import IBKRTradeStateManager
from core.config import TRADE_STATE_DIR

def test_state_persistence():
    """Test that state persists correctly across restarts"""
    print("=" * 70)
    print("TEST 1: State Persistence with Empty Broker Response")
    print("=" * 70)
    
    # Create state manager (simulates first run)
    manager = IBKRTradeStateManager()
    
    print(f"\n1. Initial state:")
    print(f"   Traded symbols: {list(manager.traded_symbols)}")
    print(f"   Open positions: {list(manager.open_positions)}")
    print(f"   Total trades: {manager.total_trades}")
    
    # Simulate a trade being placed
    print(f"\n2. Simulating TSLA trade...")
    manager.mark_symbol_traded("TSLA")
    manager.mark_position_opened("TSLA")
    manager.increment_trade_count()
    
    print(f"   Traded symbols: {list(manager.traded_symbols)}")
    print(f"   Open positions: {list(manager.open_positions)}")
    print(f"   Total trades: {manager.total_trades}")
    
    # Verify file was saved
    state_file = manager.state_file
    with open(state_file, 'r') as f:
        saved_data = json.load(f)
    print(f"\n3. State file content:")
    print(f"   {json.dumps(saved_data, indent=2)}")
    
    # Simulate restart - create new manager (loads from file)
    print(f"\n4. Simulating bot restart (new manager loads from file)...")
    manager2 = IBKRTradeStateManager()
    
    print(f"   Traded symbols: {list(manager2.traded_symbols)}")
    print(f"   Open positions: {list(manager2.open_positions)}")
    print(f"   Total trades: {manager2.total_trades}")
    
    # Critical test: sync with EMPTY broker response (simulates slow connection)
    print(f"\n5. Syncing with broker (EMPTY response - simulating timing issue)...")
    manager2.sync_with_broker([])  # Empty positions list!
    
    print(f"   Traded symbols after sync: {list(manager2.traded_symbols)}")
    print(f"   Open positions after sync: {list(manager2.open_positions)}")
    print(f"   Total trades after sync: {manager2.total_trades}")
    
    # Verify state was preserved
    assert "TSLA" in manager2.traded_symbols, "❌ TSLA should still be in traded_symbols"
    assert "TSLA" in manager2.open_positions, "❌ TSLA should still be in open_positions"
    assert manager2.total_trades == 1, "❌ Total trades should still be 1"
    
    print(f"\n✅ TEST PASSED: State preserved despite empty broker response")
    
    # Verify file still has data
    with open(state_file, 'r') as f:
        final_data = json.load(f)
    print(f"\n6. Final state file content:")
    print(f"   {json.dumps(final_data, indent=2)}")
    
    assert "TSLA" in final_data["traded_symbols"], "❌ File should contain TSLA in traded_symbols"
    assert "TSLA" in final_data["open_positions"], "❌ File should contain TSLA in open_positions"
    
    print(f"\n✅ ALL TESTS PASSED!")
    print("=" * 70)


def test_sync_with_actual_positions():
    """Test sync when broker actually returns positions"""
    print("\n" + "=" * 70)
    print("TEST 2: State Sync with Actual Broker Positions")
    print("=" * 70)
    
    # Create manager and add a trade
    manager = IBKRTradeStateManager()
    manager.mark_symbol_traded("NQ")
    manager.mark_position_opened("NQ")
    manager.increment_trade_count()
    
    print(f"\n1. Initial state:")
    print(f"   Traded: {list(manager.traded_symbols)}")
    print(f"   Open: {list(manager.open_positions)}")
    
    # Mock position object (what IBKR returns)
    mock_position = Mock()
    mock_position.symbol = "NQ 20251226C20000"  # Full contract name
    mock_position.position = 1.0
    
    print(f"\n2. Syncing with broker position: {mock_position.symbol}")
    manager.sync_with_broker([mock_position])
    
    print(f"   Traded after sync: {list(manager.traded_symbols)}")
    print(f"   Open after sync: {list(manager.open_positions)}")
    
    assert "NQ" in manager.traded_symbols, "❌ NQ should be in traded_symbols"
    assert "NQ" in manager.open_positions, "❌ NQ should be in open_positions"
    
    print(f"\n✅ TEST PASSED: Symbol extraction and sync working correctly")
    print("=" * 70)


if __name__ == "__main__":
    try:
        test_state_persistence()
        test_sync_with_actual_positions()
        print("\n🎉 ALL TESTS PASSED! State persistence is working correctly.")
    except AssertionError as e:
        print(f"\n❌ TEST FAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ TEST ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
