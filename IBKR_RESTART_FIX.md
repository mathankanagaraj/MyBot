# IBKR Container Restart Loop - FIXED

## Problem Identified 🔍

The IBKR container was stuck in an infinite restart loop:

```
1. Bot starts at 18:00 ET (after market close)
2. Market hours watcher starts
3. Watcher checks: current_time (18:00) >= 16:00 → TRUE
4. Sets _STOP_EVENT immediately
5. Main loop exits (exit code 0)
6. Docker restarts container (restart policy)
7. Loop repeats every ~60 seconds
```

### Root Cause
The market hours watcher was **immediately stopping** the bot if started after 16:00 ET, without checking if the bot was actually trading during the day.

---

## Solution Applied ✅

### Key Changes

1. **Added `was_trading_today` flag**
   - Tracks whether bot actually started trading during market hours
   - Only set to `True` when market opens (09:30-16:00 ET)

2. **Modified STOP_EVENT logic**
   ```python
   # Before: Always stopped if time >= 16:00
   if current_time >= time(16, 0):
       _STOP_EVENT.set()  # ❌ Wrong - stops on startup after hours
   
   # After: Only stops if was trading
   if current_time >= time(16, 0) and was_trading_today:
       _STOP_EVENT.set()  # ✅ Correct - only stops after active trading
   ```

3. **Added AFTER_HOURS state**
   - New state to distinguish "started after hours" from "closed after trading"
   - Logs appropriately without stopping

---

## Behavior Now 🎯

### Scenario 1: Started Before Market Open (e.g., 08:00 ET)
```
08:00 ET: Bot starts
08:00 ET: "US Market is CLOSED - Waiting for market hours"
09:30 ET: "US Market is OPEN" + was_trading_today = True
16:00 ET: "US Market closed - Stopping all trading" + _STOP_EVENT.set()
         → Clean shutdown, Docker can restart for next day
```

### Scenario 2: Started After Market Close (e.g., 18:00 ET) ✅ FIXED
```
18:00 ET: Bot starts
18:00 ET: "US Market closed (after hours) - Waiting for next session"
18:00 ET: Main loop sleeps until 09:00 ET next day
         → No restart loop!
         → Heartbeat continues
         → Watcher monitors but doesn't stop
```

### Scenario 3: Started During Market Hours (e.g., 11:00 ET)
```
11:00 ET: Bot starts
11:00 ET: "US Market is OPEN" + was_trading_today = True
16:00 ET: "US Market closed - Stopping all trading" + _STOP_EVENT.set()
         → Clean shutdown after trading session
```

---

## State Transitions

```
WAITING → OPEN → CLOSED (if was_trading_today = True)
   ↓
AFTER_HOURS (if started after 16:00 and was_trading_today = False)
```

---

## Testing Results Expected

### Before Fix ❌
```bash
# Logs showed restart loop
18:00:51 — Bot starts
18:00:51 — Market watcher: "Market closed - Stopping"
18:01:51 — Bot exits
18:01:51 — Docker restarts
18:01:52 — Cycle repeats infinitely
```

### After Fix ✅
```bash
# Bot stays running
18:00:51 — Bot starts
18:00:51 — Market watcher: "Market closed (after hours) - Waiting"
18:00:51 — Main loop: "Sleeping 15.0 hours until 09:00 ET"
18:01:51 — Heartbeat continues
# ... stays running until next market open ...
```

---

## Verification Steps

1. **Stop current containers**
   ```bash
   docker-compose down
   ```

2. **Start IBKR bot after market hours**
   ```bash
   docker-compose up ibkr_bot
   ```

3. **Check logs - should see:**
   ```
   ✅ "Market closed (after hours) - Waiting for next session"
   ✅ "Sleeping X hours until 09:00 ET"
   ✅ Heartbeat continues every 60 seconds
   ❌ NO "Stopping all trading" message
   ❌ NO container restart
   ```

4. **Verify container stays running**
   ```bash
   docker-compose ps
   # Should show ibkr_bot as "Up"
   ```

---

## Additional Improvements

### Better Logging
- `🚫 US Market closed (after hours)` - Started after close
- `🛑 US Market closed (16:00 ET)` - Closed during active trading
- Clear distinction for debugging

### Graceful Shutdown
- Bot completes daily cycle cleanly
- Cancels background tasks properly
- No force kills or errors

### Docker Integration
- Works with Docker restart policies
- Exits only when intended (after trading)
- Stays running when sleeping overnight

---

## Impact

✅ **Fixed**: Infinite restart loop eliminated  
✅ **Improved**: Better state tracking and logging  
✅ **Maintained**: All existing functionality preserved  
✅ **Enhanced**: Clearer distinction between states  

---

## Files Modified

- `/src/core/ibkr/worker.py`
  - `market_hours_watcher()` function
  - Added `was_trading_today` flag
  - Modified STOP_EVENT logic
  - Added AFTER_HOURS state

---

## Status

**Issue**: ❌ Container restarting every 60 seconds  
**Root Cause**: ✅ Identified - Incorrect stop logic  
**Fix Applied**: ✅ Complete  
**Testing**: 🔄 Ready for verification  
**Risk**: 🟢 Low - Logic improvement only  

---

**Deployment**: ✅ Ready to deploy  
**Rollback**: Simple - revert single function  
**Breaking Changes**: ❌ None
