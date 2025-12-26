# State Persistence Fix - December 26, 2025

## Issue Summary

**Problem**: Trade state file (`ibkr_trades_2025-12-26.json`) was being reset to empty on bot restart, losing all traded symbols and position information.

**Root Cause**: When bot restarted, `sync_with_broker()` was called with an empty positions list (due to timing/connection issues), which overwrote the existing state with empty data.

## Solution Implemented

### 1. **Defensive State Preservation** ✅
Added logic to prevent state loss when broker returns empty positions:

**IBKR** (`src/core/ibkr/trade_state.py`):
```python
# If broker returns empty positions but we have existing state, don't wipe it
if not positions and (self.traded_symbols or self.open_positions):
    logger.warning("⚠️  Broker returned 0 positions but state has data. Preserving existing state.")
    return  # Skip sync to preserve state
```

**Angel One** (`src/core/angelone/trade_state.py`):
```python
# Same defensive check added for consistency
if not positions and (self.traded_symbols or self.open_positions):
    logger.warning("⚠️  Broker returned 0 positions but state has data. Preserving existing state.")
    return
```

### 2. **Enhanced Debug Logging** ✅
Added comprehensive logging to diagnose state sync issues:

- Log state BEFORE sync (traded symbols, open positions)
- Log each position being processed
- Log symbol extraction results
- Log state AFTER sync

**IBKR Example**:
```python
logger.debug("Processing position: symbol='%s', size=%s", symbol, position_size)
logger.debug("Extracted underlying: '%s' -> '%s'", symbol, underlying)
```

### 3. **Improved Symbol Extraction** ✅
Enhanced regex pattern to handle all IBKR contract formats:

```python
# Handles: "NQ 20251226C20000", "TSLA20251226C350", "ES"
match = re.match(r'^([A-Z]+)', contract_symbol)
```

### 4. **Cleanup Task Fix** ✅
Modified cleanup to only monitor `ORB_ACTIVE_POSITIONS` (not closed symbols):

**Before**:
```python
all_symbols_to_check = tracked_symbols + trade_taken_symbols
# Monitored both active AND closed positions
```

**After**:
```python
tracked_symbols = list(ORB_ACTIVE_POSITIONS.keys())
# Only monitors active positions
```

### 5. **ONE_TRADE_PER_SYMBOL Logic** ✅
Cleanup now respects the one-trade-per-symbol setting:

**IBKR**:
```python
if not config.IBKR_ONE_TRADE_PER_SYMBOL:
    del ORB_TRADE_TAKEN_TODAY[symbol]  # Clear flag
else:
    # Keep flag to block re-entry for entire day
```

**Angel One**:
```python
if not config.ANGEL_ONE_TRADE_PER_DAY:
    del ORB_TRADE_TAKEN_TODAY[symbol]
else:
    # Keep flag to prevent re-entry
```

## Test Results

### IBKR State Persistence Test ✅
```
TEST 1: State Persistence with Empty Broker Response
✅ TEST PASSED: State preserved despite empty broker response

TEST 2: State Sync with Actual Broker Positions  
✅ TEST PASSED: Symbol extraction and sync working correctly

🎉 ALL TESTS PASSED! State persistence is working correctly.
```

### Angel One State Persistence Test ✅
```
TEST: Angel One State Persistence with Empty Broker Response
✅ TEST PASSED: Angel One state preserved correctly

TEST: Angel One Sync with Broker Positions
✅ TEST PASSED: Symbol matching working correctly

🎉 ALL ANGEL ONE TESTS PASSED!
```

## Expected Behavior After Fix

### Scenario 1: Normal Operation
1. Trade placed → State saved with TSLA
2. Bot restart → State loaded (TSLA present)
3. Broker sync returns TSLA position
4. State synced correctly ✅

### Scenario 2: Empty Broker Response (Fixed!)
1. Trade placed → State saved with TSLA
2. Bot restart → State loaded (TSLA present)
3. Broker sync returns empty list (timing issue)
4. **NEW**: State preserved, sync skipped ⚠️
5. State file still contains TSLA ✅

### Scenario 3: Position Closed
1. Trade placed → State saved with TSLA
2. Position closed manually
3. Cleanup detects position gone
4. Clears from `ORB_ACTIVE_POSITIONS` ✅
5. **ONE_TRADE_PER_SYMBOL=true**: Keeps in `ORB_TRADE_TAKEN_TODAY` (blocks re-entry) 🔒
6. **ONE_TRADE_PER_SYMBOL=false**: Clears flag (allows re-entry) 🔓

## Files Modified

### IBKR
- ✅ `src/core/ibkr/trade_state.py` - Defensive sync + logging
- ✅ `src/core/ibkr/orb_worker_ibkr.py` - Cleanup fix + position logging

### Angel One  
- ✅ `src/core/angelone/trade_state.py` - Defensive sync + logging
- ✅ `src/core/angelone/orb_worker_angel.py` - Cleanup fix + flag logic

### Tests Created
- ✅ `tests/test_state_extraction.py` - Symbol extraction validation
- ✅ `tests/test_state_persistence.py` - IBKR state persistence tests
- ✅ `tests/test_angel_state_persistence.py` - Angel One state tests

## Verification Steps

1. **Check logs on startup**:
   ```
   📋 Retrieved X position(s) from IBKR broker for sync
   🔄 Syncing trade state with IBKR broker...
   Current state before sync: X traded symbols [...], X open positions [...]
   ✅ Broker sync complete: X open positions [...], X traded symbols [...]
   ```

2. **Verify state file persists**:
   ```bash
   cat /app/data/trade_state/ibkr_trades_2025-12-26.json
   # Should show traded symbols even after restart
   ```

3. **Check cleanup behavior**:
   ```
   🧹 Cleanup: Monitoring X active position(s): [symbols]
   # Should NOT show closed positions repeatedly
   ```

## Consistency Across Brokers

Both IBKR and Angel One now have:
- ✅ Defensive state preservation
- ✅ Comprehensive debug logging
- ✅ Smart cleanup (only active positions)
- ✅ ONE_TRADE_PER_SYMBOL/DAY enforcement
- ✅ Symbol extraction with logging
- ✅ Tested and verified behavior

## Notes

- State files are daily: `ibkr_trades_YYYY-MM-DD.json`, `angel_trades_YYYY-MM-DD.json`
- Old state files auto-cleaned after 7 days
- `traded_symbols` persists entire day (for one-trade-per-symbol)
- `open_positions` syncs with broker (reflects actual positions)
- Empty broker response now triggers warning but preserves state
