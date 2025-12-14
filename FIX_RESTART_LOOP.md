# Fix: Bot Restart Loop During Non-Market Hours

## Problem
The Angel One bot was continuously restarting when started outside market hours (after 15:30 IST or before 09:00 IST), causing:
- Docker restart loop every few seconds
- Excessive log spam
- Unnecessary resource usage
- Container showing "exited with code 0 (restarting)" repeatedly

## Root Cause
The `market_state_watcher()` function in `angelone/worker.py` was immediately setting `_STOP_EVENT` whenever the current time was >= 15:30 IST, regardless of whether the bot had been trading during that session.

**Flow causing the issue:**
1. Bot starts at 18:00 IST (after market close)
2. Market watcher checks: `now_ist.time() >= time(15, 30)` → True
3. Sets `_STOP_EVENT` immediately
4. Bot exits cleanly with code 0
5. Docker restart policy (`restart: unless-stopped`) restarts container
6. Loop repeats infinitely

## Solution
Modified `market_state_watcher()` to track whether the bot was trading during the current session using a `was_open_today` flag:

### Key Changes

1. **Added session tracking:**
   ```python
   # Track if we were open during this session (to distinguish hard close from startup after hours)
   was_open_today = MARKET_OPEN_STATE
   ```

2. **Conditional stop event:**
   ```python
   if now_ist.time() >= time(15, 30):
       if was_open_today:
           # Only set stop event if we were trading today (hard close scenario)
           _STOP_EVENT.set()
           break
       else:
           # Started after 15:30, just keep MARKET_OPEN_STATE = False
           # Let run_angel_workers handle the sleep logic
           MARKET_OPEN_STATE = False
   ```

3. **Update flag when market opens:**
   ```python
   if is_open_now != MARKET_OPEN_STATE:
       MARKET_OPEN_STATE = is_open_now
       
       # Track if market opened during this session
       if is_open_now:
           was_open_today = True
   ```

## Behavior After Fix

### Scenario 1: Bot starts BEFORE market hours (e.g., 07:00 IST)
- Market watcher: `was_open_today = False`
- Does NOT set `_STOP_EVENT`
- `run_angel_workers()` calculates wait time until 09:00 IST
- Bot sleeps until market open
- When market opens at 09:00, watcher sets `was_open_today = True`
- Trading begins normally

### Scenario 2: Bot starts DURING market hours (e.g., 11:00 IST)
- Market watcher: `was_open_today = True` (market is open)
- Trading begins immediately
- At 15:30 IST, watcher sets `_STOP_EVENT`
- Bot exits cleanly for the day

### Scenario 3: Bot starts AFTER market hours (e.g., 18:00 IST)
- Market watcher: `was_open_today = False`
- Does NOT set `_STOP_EVENT`
- `run_angel_workers()` calculates wait time until next trading day 09:00 IST
- Bot sleeps through the night
- When market opens, watcher sets `was_open_today = True`
- Trading begins normally

## IBKR Bot Status
The IBKR bot (`ibkr/worker.py`) already had this logic implemented correctly using the `was_trading_today` flag (lines 40-68). No changes were needed for IBKR.

## Files Modified
- `/Users/mathan/Documents/GitHub/MyBot/src/core/angelone/worker.py`
  - Function: `market_state_watcher()` (lines 133-257)
  - Added `was_open_today` tracking
  - Conditional `_STOP_EVENT` setting

## Testing
To verify the fix works:
1. Stop the bot: `docker compose down`
2. Start after market hours: `docker compose up -d`
3. Check logs: `docker compose logs -f angel_bot`
4. Expected: Bot should log sleep duration and wait for next market open
5. No restart loop should occur

## Summary
The bot now correctly distinguishes between:
- **Hard close**: Market was open, reached 15:30 → Set `_STOP_EVENT` to end the day
- **Startup after hours**: Bot started when market already closed → Sleep until next open

This prevents the restart loop while maintaining proper daily shutdown behavior.
