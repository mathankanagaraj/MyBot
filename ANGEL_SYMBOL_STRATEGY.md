# Angel One Symbol Strategy - Futures vs Spot

## Overview
Updated Angel One bot to use a hybrid strategy for ORB trading:
- **Indices (NIFTY/BANKNIFTY)**: Use futures for ORB strategy, spot for option selection
- **Stocks**: Use spot price for both ORB and options

---

## Symbol Configuration

### Source: `config.py`
```python
# Angel One Symbols (Indian Market)
ANGEL_INDEX_FUTURES = ["NIFTY", "BANKNIFTY"]
ANGEL_STOCK_SYMBOLS = [
    "RELIANCE",
    "INFY",
    "TCS",
    "ICICIBANK",
    "HDFCBANK",
    "SBIN",
    "AXISBANK",
    "BHARTIARTL",
]
ANGEL_SYMBOLS = ANGEL_INDEX_FUTURES + ANGEL_STOCK_SYMBOLS
```

### Removed
- `ORB_ANGEL_SYMBOLS` - No longer needed, using `ANGEL_SYMBOLS` instead
- No `.env` cleanup needed (ORB_ANGEL_SYMBOLS was never in .env)

---

## Trading Strategy

### For NIFTY and BANKNIFTY (Indices)

#### 1. **ORB Range Building & Breakout Detection**
- **Contract**: Front-month FUTURES (NFO)
- **Why**: Futures have better liquidity and tighter spreads
- **Process**:
  - Fetch futures contract token (e.g., NIFTY30DEC25FUT)
  - Load historical 1m data from futures
  - Subscribe to WebSocket for real-time futures data
  - Build ORB range from futures price
  - Detect breakouts using futures candles

#### 2. **Option Strike Selection**
- **Price Source**: SPOT index price (NSE)
- **Why**: Options are priced based on spot index, not futures
- **Process**:
  ```python
  if symbol in ANGEL_INDEX_FUTURES:
      # Use spot index price (NSE) for option selection
      ltp = await angel_client.get_last_price(symbol, exchange="NSE")
      logger.debug(f"[{symbol}] Using SPOT price for option selection: {ltp}")
  ```
- **Strike Selection**: 1-2 levels ITM from ATM (based on spot price)

### For Stocks (RELIANCE, INFY, etc.)

#### 1. **ORB Range Building & Breakout Detection**
- **Contract**: SPOT equity (NSE)
- **Why**: Stock futures less liquid than index futures
- **Process**:
  - Fetch spot stock price
  - Load historical 1m data from NSE
  - Subscribe to WebSocket for real-time spot data
  - Build ORB range from spot price
  - Detect breakouts using spot candles

#### 2. **Option Strike Selection**
- **Price Source**: Same SPOT price used for ORB
- **Process**: Standard option selection using current spot price

---

## Code Changes

### Files Modified

1. **`src/core/angelone/orb_worker_angel.py`**
   - Replaced all `ORB_ANGEL_SYMBOLS` with `ANGEL_SYMBOLS`
   - Added `ANGEL_INDEX_FUTURES` import
   - Updated futures contract resolution to only run for indices:
     ```python
     # Resolve futures contracts (NFO) for indices only
     fut_contracts = {}
     for symbol in ANGEL_INDEX_FUTURES:
         contract = await angel_client.get_current_futures_contract(symbol)
     ```
   - Enhanced option selection to use spot for indices:
     ```python
     if symbol in ANGEL_INDEX_FUTURES:
         # Use spot index price (NSE) for option selection
         ltp = await angel_client.get_last_price(symbol, exchange="NSE")
     else:
         # For stocks, use direct price
         ltp = await angel_client.get_last_price(symbol)
     ```

2. **`src/core/config.py`**
   - Removed `ORB_ANGEL_SYMBOLS` list
   - Added documentation explaining futures vs spot strategy
   - Now uses `ANGEL_SYMBOLS` for ORB trading

---

## Flow Diagram

### NIFTY/BANKNIFTY (Index)
```
Market Open
    ↓
[1] Resolve Front-Month Futures (e.g., NIFTY30DEC25FUT)
    ↓
[2] Fetch Historical Data → FUTURES (NFO)
    ↓
[3] Subscribe WebSocket → FUTURES (NFO)
    ↓
[4] Build ORB Range → Using FUTURES price
    ↓
[5] Detect Breakout → Using FUTURES candles
    ↓
    Breakout Confirmed ✅
    ↓
[6] Get SPOT Index Price (NSE) ← Different from futures!
    ↓
[7] Select Option Strike → Based on SPOT price
    ↓
[8] Place Option Order
```

### Stocks (RELIANCE, INFY, etc.)
```
Market Open
    ↓
[1] Use Spot Symbol (NSE)
    ↓
[2] Fetch Historical Data → SPOT (NSE)
    ↓
[3] Subscribe WebSocket → SPOT (NSE)
    ↓
[4] Build ORB Range → Using SPOT price
    ↓
[5] Detect Breakout → Using SPOT candles
    ↓
    Breakout Confirmed ✅
    ↓
[6] Use Same SPOT Price
    ↓
[7] Select Option Strike → Based on SPOT price
    ↓
[8] Place Option Order
```

---

## Why This Approach?

### Futures for ORB (Indices Only)
**Advantages:**
- ✅ Better liquidity and volume
- ✅ Tighter spreads
- ✅ More reliable price action
- ✅ Lower slippage on breakouts

**Disadvantages:**
- ❌ Futures price != spot price (carry cost difference)
- ❌ Options priced on spot, not futures

### Spot for Option Selection (Always)
**Why:**
- ✅ Options derive value from SPOT index, not futures
- ✅ Strike selection must be based on spot price
- ✅ Prevents incorrect ITM/OTM classification

### Example Scenario
```
9:45 AM - ORB Complete
├─ NIFTY Futures: 26,250 (used for breakout detection)
├─ NIFTY Spot: 26,230 (used for option selection)
└─ Difference: 20 points (carry cost)

If we used futures price (26,250) for option selection:
❌ Wrong strike: 26,250 CE (ATM on futures, but OTM on spot)
✅ Correct strike: 26,230 CE (ATM on spot)
```

---

## Testing Checklist

Tomorrow during market hours, verify:

### NIFTY/BANKNIFTY
- [ ] Bot loads futures contract (e.g., `NIFTY30DEC25FUT`)
- [ ] Historical data fetched from NFO exchange
- [ ] WebSocket subscribed to futures token
- [ ] ORB range calculated from futures price
- [ ] **On breakout**: Spot price fetched from NSE
- [ ] Log shows: `"Using SPOT price for option selection: {price}"`
- [ ] Option strike selected based on spot price
- [ ] Option order placed successfully

### Stocks (RELIANCE, etc.)
- [ ] Bot uses NSE exchange (no futures lookup)
- [ ] Historical data fetched from NSE
- [ ] WebSocket subscribed to spot token
- [ ] ORB range calculated from spot price
- [ ] Option strike selected based on same spot price
- [ ] Option order placed successfully

### Force Exit (All Symbols)
- [ ] At 15:15 IST, all positions closed
- [ ] Works even if position not in local tracking
- [ ] Checks broker for actual positions
- [ ] Cancels all open orders (SL + Target)

---

## Rollback Plan

If issues occur, revert to previous behavior:
```bash
git checkout HEAD~1 src/core/angelone/orb_worker_angel.py src/core/config.py
docker compose --profile angel build
docker compose --profile angel up -d
```

---

**Date:** December 24, 2025  
**Status:** ✅ Deployed and ready for testing  
**Next Review:** During next market session (December 25, 2025)
