# Enabling Debug Logs

## Quick Start

### 1. Add to `.env` file

```bash
# Add this line to your .env file
LOG_LEVEL=DEBUG
```

### 2. Rebuild and Restart

```bash
docker-compose down
docker-compose build
docker-compose up -d
```

### 3. View Debug Logs

```bash
# Watch all logs including DEBUG
docker-compose logs -f tradingbot

# Or filter for specific debug messages
docker-compose logs -f tradingbot | grep "DEBUG"
```

---

## Log Levels

You can set `LOG_LEVEL` to any of these values:

| Level | What You'll See | Use Case |
|-------|----------------|----------|
| `DEBUG` | Everything (very verbose) | Troubleshooting, development |
| `INFO` | Normal operations (default) | Production monitoring |
| `WARNING` | Warnings and errors only | Quiet production |
| `ERROR` | Errors only | Critical issues only |

---

## What Debug Logs Show

With `LOG_LEVEL=DEBUG`, you'll see additional messages like:

### Market Hours Check
```
2025-12-04 04:25:00 — DEBUG — [NIFTY] 💤 Market closed, data fetcher sleeping until 04:30:00 IST
2025-12-04 04:30:00 — DEBUG — [RELIANCE] 💤 Market closed, data fetcher sleeping until 04:35:00 IST
```

### Data Fetch Timing
```
2025-12-04 09:25:00 — DEBUG — [NIFTY] ⏰ Next data fetch at 09:30:00 IST (sleeping 300s)
2025-12-04 09:30:00 — DEBUG — [RELIANCE] ⏰ Next data fetch at 09:35:00 IST (sleeping 300s)
```

### Rate Limiting
```
2025-12-04 09:25:05 — DEBUG — ⏳ Rate limit: waiting 0.3s for getCandleData
2025-12-04 09:25:06 — DEBUG — [HDFCBANK] Requesting 1m data: 2025-12-04 09:10 to 2025-12-04 09:25 (0.0104 days)
```

### API Requests
```
2025-12-04 09:25:00 — DEBUG — [NIFTY] Requesting 1m data: 2025-12-04 09:10 to 2025-12-04 09:25 (0.0104 days)
2025-12-04 09:25:00 — DEBUG — [NIFTY] Successfully fetched 15 1m candles
```

---

## Temporary Debug Mode (Without Rebuild)

If you want to enable debug logs temporarily without rebuilding:

### Option 1: Docker Exec
```bash
# Get into the running container
docker exec -it intraday_options_bot_angel /bin/bash

# Modify logger.py temporarily
sed -i 's/logging.INFO/logging.DEBUG/g' /app/src/core/logger.py

# Restart the container
exit
docker-compose restart tradingbot
```

### Option 2: Environment Override
```bash
# Stop the bot
docker-compose down

# Start with LOG_LEVEL override
LOG_LEVEL=DEBUG docker-compose up -d

# View logs
docker-compose logs -f tradingbot
```

---

## Filtering Debug Logs

### See Only Market Hours Checks
```bash
docker-compose logs -f tradingbot | grep "Market closed"
```

### See Only Data Fetching
```bash
docker-compose logs -f tradingbot | grep "Next data fetch"
```

### See Only Rate Limiting
```bash
docker-compose logs -f tradingbot | grep "Rate limit"
```

### See Only API Requests
```bash
docker-compose logs -f tradingbot | grep "Requesting 1m data"
```

---

## Reverting to INFO Level

### 1. Edit `.env`
```bash
# Change from DEBUG to INFO
LOG_LEVEL=INFO
```

### 2. Restart
```bash
docker-compose restart tradingbot
```

No rebuild needed - the change takes effect on restart!

---

## Production Recommendation

**For Production**: Use `LOG_LEVEL=INFO` (default)
- Less verbose
- Easier to spot important events
- Lower disk usage

**For Debugging**: Use `LOG_LEVEL=DEBUG`
- See everything
- Understand timing issues
- Troubleshoot problems

**For Quiet Production**: Use `LOG_LEVEL=WARNING`
- Only warnings and errors
- Minimal log noise
