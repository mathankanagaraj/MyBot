# Enhanced Logging for Bot Monitoring

## Summary

Added comprehensive logging to make background worker activity fully visible, addressing the issue where only one startup message was visible per symbol.

## Changes Made

### 1. **Background Data Fetcher Task** (NEW - Critical Fix)

Previously, the bot only loaded historical data once at startup and never updated it. This meant the BarManager would become stale over time.

**Added**: `data_fetcher_loop()` - A background task for each symbol that:
- Runs every **5 minutes**
- Fetches the latest 1-minute bars from Angel Broker API
- Updates the BarManager with fresh data
- Logs each fetch iteration with timestamp and bar count

**Logging Output**:
```
[NIFTY] 📡 Data fetch #1 at 12:30:45 - Fetching latest 1m bars...
[NIFTY] ✅ Data fetch #1: Added 15 new bars (total: 2880)
[NIFTY] 💤 Data fetcher sleeping 5 minutes...
```

### 2. **Worker Loop Monitoring**

Enhanced the main `worker_loop()` with detailed logging at every step:

#### Loop Iteration Tracking
```
[NIFTY] 🔄 Loop #1 at 12:30:00 (bars: 2880)
[NIFTY] 🔄 Loop #2 at 12:31:00 (bars: 2881)
```

#### Market Status
```
[NIFTY] 💤 Market closed, sleeping 5 minutes...
```

#### Position Monitoring
```
[NIFTY] 📊 Has open position, checking status...
[NIFTY] ✅ Position still open, monitoring...
[NIFTY] ⏳ Sleeping 60 seconds (position monitoring)...
```

#### Data Availability
```
[NIFTY] 📈 Fetching resampled bars (5m, 15m)...
[NIFTY] 📊 Data available: 5m bars=100, 15m bars=33
```

#### 15-Minute Trend Detection
```
[NIFTY] 🔍 Detecting 15m trend bias...
[NIFTY] ✅ 15m bias detected: BULLISH
[NIFTY] 🎯 NEW 15m signal: BULLISH - Starting 5m entry search...
```

Or when no trend:
```
[NIFTY] ⛔ No 15m bias detected, sleeping 60s...
```

#### Duplicate Signal Prevention
```
[NIFTY] ⏭️ Skipping duplicate signal (3.5 min since last), sleeping 60s...
```

### 3. **5-Minute Entry Confirmation Loop**

Detailed logging for each check in the 5-minute confirmation process:

```
[NIFTY] 🔎 Starting 5m entry confirmation loop (max 10 checks)...
[NIFTY] 🔍 5m check #1/10 - waiting 60s for next bar...
[NIFTY] 🔄 5m check #1: 15m bias revalidation: BULLISH (was: BULLISH)
[NIFTY] 🎯 5m check #1: Checking entry conditions...
[NIFTY] ⛔ 5m check #1: No entry signal yet
[NIFTY] 🔍 5m check #2/10 - waiting 60s for next bar...
...
[NIFTY] ✅ 5m ENTRY SIGNAL CONFIRMED: BULLISH - RSI oversold bounce
```

### 4. **Order Execution Flow**

Complete visibility into the order placement process:

```
[NIFTY] 📊 Fetching futures price for index...
[NIFTY] 💰 Underlying price: ₹19,450.50
[NIFTY] 🔍 Selecting option contract...
[NIFTY] ✅ Selected option: NIFTY24DEC19500CE
[NIFTY] 💰 Fetching option premium...
[NIFTY] 💰 Option premium: ₹125.50
[NIFTY] 📊 Position sizing: 1 lots × 50 qty × ₹125.50 = ₹6,275.00
[NIFTY] 📤 Placing bracket order: BULLISH Entry=₹125.50, SL=₹100.40, TP=₹175.70
[NIFTY] ✅ Order placed successfully!
```

### 5. **Error Handling**

All errors are now logged with context:

```
[NIFTY] ❌ Failed to get underlying price
[NIFTY] ❌ Option selection failed: No liquid strikes available
[NIFTY] ❌ Premium too low: ₹5.0 (min: ₹10.00)
[NIFTY] ❌ Insufficient funds or risk limit reached
[NIFTY] ❌ Order placement failed
```

## Logging Frequency

### Data Fetcher (per symbol)
- **Every 5 minutes**: Data fetch attempt
- Logs: Fetch iteration number, timestamp, bars added, total bar count

### Worker Loop (per symbol)
- **Every 60 seconds**: Loop iteration (when no position)
- **Every 60 seconds**: 15m bias check
- **Every 60 seconds during 5m confirmation**: Entry signal check
- **Every MONITOR_INTERVAL seconds**: Position monitoring (when position open)

## Emoji Legend

- 🚀 Startup
- 🔄 Loop iteration / Revalidation
- 📡 Data fetching
- 📊 Data/Position status
- 📈 Fetching bars
- 🔍 Searching/Detecting
- 🎯 Signal found
- ✅ Success
- ⛔ No signal/blocked
- ❌ Error
- ⚠️ Warning
- 💤 Sleeping
- ⏳ Waiting
- 💰 Price/Premium
- 📤 Sending order
- 🛑 Shutdown

## Benefits

1. **Full visibility** into what each worker thread is doing at all times
2. **Easy debugging** - can see exactly where the bot is in its decision flow
3. **Performance monitoring** - can track how often data is fetched and signals are checked
4. **Issue detection** - quickly identify if data fetching fails or signals aren't being detected
5. **Progress tracking** - see the 5m entry confirmation loop progress in real-time

## Example Log Output

```
[NIFTY] 🚀 Worker task started
[NIFTY] 📡 Data fetcher task started
[NIFTY] 🔄 Loop #1 at 09:15:30 (bars: 2880)
[NIFTY] 📈 Fetching resampled bars (5m, 15m)...
[NIFTY] 📊 Data available: 5m bars=288, 15m bars=96
[NIFTY] 🔍 Detecting 15m trend bias...
[NIFTY] ⛔ No 15m bias detected, sleeping 60s...
[NIFTY] 📡 Data fetch #1 at 09:15:35 - Fetching latest 1m bars...
[NIFTY] ✅ Data fetch #1: Added 5 new bars (total: 2885)
[NIFTY] 💤 Data fetcher sleeping 5 minutes...
[NIFTY] 🔄 Loop #2 at 09:16:30 (bars: 2885)
...
```

## Implementation Details

### Files Modified
- `src/core/worker.py`: Added comprehensive logging throughout worker loop and new data fetcher task

### New Functions
- `data_fetcher_loop()`: Background task that fetches 1m bars every 5 minutes

### Configuration
No configuration changes needed. Logging uses the existing logger configuration.

## Related Documentation
- See [DATA_FLOW_GUIDE.md](./DATA_FLOW_GUIDE.md) for details on how data flows through the system
