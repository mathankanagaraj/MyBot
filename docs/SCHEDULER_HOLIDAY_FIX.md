# Scheduler Holiday Fix

## Issue
The bot scheduler was calculating next wake-up times without checking for holidays, causing the bot to wake up on non-trading days.

### Observed Behavior (Before Fix)
```
2025-12-24 21:35:18 — INFO — ⏳ Current time (21:35) is past market close. Skipping daily session.
2025-12-24 21:35:18 — INFO — 💤 Daily cycle complete. Sleeping 11.16h until 25-Dec 08:45
```
❌ December 25 is Christmas (NSE Holiday)

## Root Cause
The scheduler (`src/core/scheduler.py`) had two sleep calculation points:

1. **Pre-market wait** (lines 85-103): Calculated sleep time to today's market open without checking if today is a trading day
2. **End-of-day sleep** (lines 159-193): Only skipped weekends, did not check holidays

## Solution

### 1. Added Holiday Check at Loop Start (Lines 60-134)
Before every daily cycle, the scheduler now:
1. Checks if today is a trading day using broker-specific holiday checker
2. If today is a holiday, logs "Today is a holiday. Skipping to next trading day..."
3. Calculates next trading day (skipping weekends AND holidays)
4. Sleeps until next trading day's pre-market time
5. Continues to next iteration

```python
# Holiday Check (Skip Non-Trading Days)
try:
    is_trading_day = True
    if broker_name == "ANGEL":
        from core.holiday_checker import is_nse_trading_day
        is_trading_day = is_nse_trading_day(now)
    elif broker_name == "IBKR":
        from core.holiday_checker import is_us_trading_day
        is_trading_day = is_us_trading_day(now)
    
    if not is_trading_day:
        logger.info(f"📅 Today ({now.strftime('%Y-%m-%d %A')}) is a holiday. Skipping to next trading day...")
        # Calculate next trading day...
        logger.info(f"💤 Holiday detected. Sleeping {wait_hours:.2f}h until {next_start_time.strftime('%d-%b %H:%M')}...")
        await asyncio.wait_for(asyncio.shield(stop_event.wait()), timeout=wait_seconds)
        continue  # Skip to next iteration
```

### 2. Enhanced End-of-Day Sleep (Lines 229-264)
After market close, the scheduler now:
1. Skips weekends (existing logic)
2. Checks if next_start is a trading day
3. If holiday, logs "📅 Skipping holiday: YYYY-MM-DD Day"
4. Advances to next day and re-checks for weekends
5. Repeats until finding a valid trading day

```python
# Skip weekends
while next_start.weekday() > 4:
    next_start += timedelta(days=1)

# Skip holidays based on broker
try:
    if broker_name == "ANGEL":
        from core.holiday_checker import is_nse_trading_day
        while not is_nse_trading_day(next_start):
            logger.info(f"🚫 NSE Holiday detected (manual override): {next_start.strftime('%Y-%m-%d %A')}")
            logger.info(f"📅 Skipping holiday: {next_start.strftime('%Y-%m-%d %A')}")
            next_start += timedelta(days=1)
            while next_start.weekday() > 4:
                next_start += timedelta(days=1)
    elif broker_name == "IBKR":
        from core.holiday_checker import is_us_trading_day
        while not is_us_trading_day(next_start):
            logger.info(f"📅 Skipping holiday: {next_start.strftime('%Y-%m-%d %A')}")
            next_start += timedelta(days=1)
            while next_start.weekday() > 4:
                next_start += timedelta(days=1)
except Exception as e:
    logger.warning(f"⚠️ Could not check holiday status: {e}")
```

## Verified Behavior (After Fix)

### Angel Bot (NSE)
```
2025-12-24 21:40:19 — INFO — ⏳ Current time (21:40) is past market close. Skipping daily session.
2025-12-24 21:40:19 — INFO — 🚫 NSE Holiday detected (manual override): 2025-12-25 Thursday
2025-12-24 21:40:19 — INFO — 📅 Skipping holiday: 2025-12-25 Thursday
2025-12-24 21:40:19 — INFO — 💤 Daily cycle complete. Sleeping 35.08h until 26-Dec 08:45
```
✅ Correctly skips Christmas, wakes on December 26

### IBKR Bot (NYSE)
```
2025-12-24 11:10:19 — INFO — ✅ NYSE holiday calendar loaded
2025-12-24 11:10:19 — INFO — 📅 Upcoming US holidays (next 30 days): 3 holiday(s)
2025-12-24 11:10:19 — INFO —   • 2025-12-25 Thursday: US Market Holiday
2025-12-24 11:10:19 — INFO —   • 2026-01-01 Thursday: US Market Holiday
2025-12-24 11:10:19 — INFO —   • 2026-01-19 Monday: US Market Holiday
```
✅ Loaded US holidays correctly (will skip at market close)

## Expected Future Behavior

### Scenario 1: Bot Restart on Holiday
If bot restarts on December 25 (Christmas):
1. Scheduler loop starts
2. Checks: `is_nse_trading_day(2025-12-25)` → `False`
3. Logs: "📅 Today (2025-12-25 Thursday) is a holiday. Skipping to next trading day..."
4. Calculates next trading day: December 26
5. Logs: "💤 Holiday detected. Sleeping 11.25h until 26-Dec 08:45"
6. Bot sleeps until December 26

### Scenario 2: Normal End-of-Day on December 24
At market close on December 24:
1. Scheduler calculates next_start = December 25 08:45
2. Skips weekends (none)
3. Checks: `is_nse_trading_day(2025-12-25)` → `False`
4. Logs: "📅 Skipping holiday: 2025-12-25 Thursday"
5. Advances to December 26
6. Checks: `is_nse_trading_day(2025-12-26)` → `True`
7. Logs: "💤 Daily cycle complete. Sleeping 35.08h until 26-Dec 08:45"

### Scenario 3: Multi-Day Holiday (e.g., Weekend + Holiday)
December 27-28-29 (Sat-Sun-Mon if Mon is holiday):
1. Skips Saturday (weekend)
2. Skips Sunday (weekend)
3. Checks Monday: `is_trading_day(Mon)` → `False`
4. Logs: "📅 Skipping holiday: YYYY-MM-DD Monday"
5. Advances to Tuesday
6. Wakes on Tuesday pre-market

## Files Modified
1. **src/core/scheduler.py** (Lines 60-134, 229-264)
   - Added holiday check at loop start
   - Added holiday skip in end-of-day calculation
   - Broker-specific holiday functions (ANGEL: NSE, IBKR: US)

## Dependencies
- **src/core/holiday_checker.py**
  - `is_nse_trading_day(date)`: Returns True if NSE is open
  - `is_us_trading_day(date)`: Returns True if US markets are open
  - Uses manual overrides + pandas-market-calendars

## Testing
- ✅ Verified Dec 25 (Christmas) detected as NSE holiday
- ✅ Angel bot sleep calculation: 35.08h to Dec 26 (not 11.16h to Dec 25)
- ✅ IBKR bot loaded 3 US holidays correctly
- ⏳ Pending: Live test on Dec 25-26 (bot should not wake on Dec 25)

## Deployment
```bash
# Deploy to containers
docker cp src/core/scheduler.py angel_bot:/app/core/scheduler.py
docker cp src/core/scheduler.py ibkr_bot:/app/core/scheduler.py

# Restart containers
docker restart angel_bot ibkr_bot
```

## Rollback Plan
If issues occur:
1. Stop containers: `docker stop angel_bot ibkr_bot`
2. Restore previous scheduler.py from git
3. Copy to containers and restart
4. Check logs for correct behavior

## Related Documentation
- [HOLIDAY_DETECTION.md](HOLIDAY_DETECTION.md) - Holiday detection module details
- [IMPROVEMENTS_SUMMARY.md](../IMPROVEMENTS_SUMMARY.md) - General improvements
- [QUICK_REFERENCE.md](../QUICK_REFERENCE.md) - Bot operations reference
