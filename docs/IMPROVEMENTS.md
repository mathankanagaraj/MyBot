# Trading Bot Improvements - Quick Reference

## 🎯 What Was Fixed

### 1. Timezone Display (UTC → IST)
- ✅ All logs now show IST times (14:45 instead of 09:15)
- ✅ Added "IST" suffix to avoid confusion
- ✅ Sleep durations shown in logs

### 2. 15m Boundary Detection
- ✅ Bot ALWAYS waits for proper 15m boundaries (09:15, 09:30, 09:45, etc.)
- ✅ Works correctly regardless of bot start time
- ✅ No missed signals due to timing issues

### 3. Heartbeat Logging
- ✅ Heartbeat every 15 minutes showing bot is alive
- ✅ Shows IST time, market status, position status

### 4. API Rate Limiting
- ✅ Prevents AB1004 errors
- ✅ Staggered startup (400ms delays between symbols)
- ✅ Automatic queuing when limits approached
- ✅ 90% safety margin on all limits

### 5. Enhanced Error Logging
- ✅ Detects and logs AB1004 rate limit errors
- ✅ 2-minute backoff for rate limiting
- ✅ Retry counters in logs

## 📊 Rate Limits Enforced

| API | Limit | Safety (90%) |
|-----|-------|--------------|
| getCandleData | 3/sec, 180/min | 2.7/sec, 162/min |
| ltpData | 10/sec, 500/min | 9/sec, 450/min |
| placeOrder | 20/sec, 500/min | 18/sec, 450/min |
| getPosition | 1/sec | 0.9/sec |
| getRMS | 2/sec | 1.8/sec |

## 🚀 Quick Deploy

```bash
cd /Users/mathan/Documents/GitHub/MyBot
docker-compose down
docker-compose build
docker-compose up -d
docker-compose logs -f tradingbot
```

## 🔍 What to Look For

### Good Signs ✅
```
[RELIANCE] ⏰ Waiting for 15m close at 14:45:00 IST (sleeping 395s)
[RELIANCE] 🔍 Checking 15m bias at 14:45:00 IST
[RELIANCE] 💓 Heartbeat - IST: 14:53:30, Market: OPEN, Position: NO
[HDFCBANK] 📡 Data fetcher starting in 0.4s (staggered)
⏳ Rate limit: waiting 0.3s for getCandleData
```

### Bad Signs ❌ (Should NOT see these anymore)
```
Waiting for 15m close at 09:15:00  # UTC time
AB1004: Something Went Wrong  # Rate limit error
[1.5 hour gap in logs]  # No heartbeat
```

## 📁 Files Modified

- `src/core/utils.py` - IST helper functions
- `src/core/signal_engine.py` - Timezone-aware functions
- `src/core/worker.py` - Complete timezone overhaul + heartbeat
- `src/core/angel_client.py` - Rate limiting integration
- `src/core/rate_limiter.py` - **NEW** - Rate limiter implementation

## 🧪 Test Commands

```bash
# Watch for IST timestamps
docker-compose logs -f tradingbot | grep "IST"

# Watch for heartbeat
docker-compose logs -f tradingbot | grep "Heartbeat"

# Watch for rate limiting
docker-compose logs -f tradingbot | grep -E "Rate limit|AB1004"

# Watch for signals
docker-compose logs -f tradingbot | grep -E "15m bias|5m entry"
```

## 🎓 Key Concepts

### Staggered Startup
- Symbol 0: starts immediately
- Symbol 1: starts after 0.4s
- Symbol 2: starts after 0.8s
- ...
- Symbol 9: starts after 3.6s
- **Result**: 10 requests spread over 4 seconds (2.5/sec) instead of 10/sec burst

### Rate Limiting
- Bot automatically waits when approaching limits
- No manual intervention needed
- Debug logs show wait times
- 90% safety margin prevents edge cases

### 15m Boundary Detection
- Bot calculates next 15m boundary from current time
- Always waits until boundary before checking bias
- Example: Started at 14:38 → waits until 14:45
- Ensures professional-grade timing

## 💡 Tips

1. **First Run**: Watch logs for first 10 minutes to verify staggered startup
2. **Market Hours**: Verify heartbeat messages appear every 15 minutes
3. **Signal Detection**: Compare bot signals with manual chart analysis
4. **Rate Limiting**: Should see occasional "waiting Xs" messages, but no AB1004 errors

## 🆘 Troubleshooting

### If you see UTC times (09:15 instead of 14:45)
- Check if code was properly deployed
- Rebuild docker image: `docker-compose build`

### If you see AB1004 errors
- Check rate limiter is enabled (should be by default)
- Verify staggered startup is working (check logs)
- May need to increase safety margin

### If no heartbeat messages
- Check bot is running: `docker-compose ps`
- Check logs: `docker-compose logs tradingbot`
- Verify market hours or MARKET_HOURS_ONLY setting

## ✨ Expected Behavior

**Startup**:
1. Bot loads historical data (staggered over 4 seconds)
2. Data fetchers start with delays
3. Workers wait for next 15m boundary
4. No AB1004 errors

**During Market Hours**:
1. Heartbeat every 15 minutes
2. 15m bias checks at boundaries (09:15, 09:30, 09:45, etc.)
3. 5m entry checks at 5m boundaries
4. Smooth data fetching every 5 minutes
5. All times in IST

**Signal Detection**:
1. Wait for 15m boundary
2. Check 15m bias
3. If bias found, monitor 5m entries
4. All checks happen at proper candle closes
5. No missed signals due to timing
