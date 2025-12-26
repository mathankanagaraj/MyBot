# Angel One Bot - Deployment Checklist

## ✅ Pre-Deployment Verification

- [x] **Code Changes Complete**
  - [x] config.py updated with ANGEL_MAX_LOTS, ANGEL_ONE_TRADE_PER_DAY, TRADE_STATE_DIR
  - [x] trade_state.py created with TradeStateManager class
  - [x] worker.py updated with all 4 fixes
  - [x] No syntax errors

- [ ] **Configuration Review**
  - [ ] Review ANGEL_MAX_LOTS setting (default: 2)
  - [ ] Review ANGEL_ONE_TRADE_PER_DAY setting (default: true)
  - [ ] Verify TRADE_STATE_DIR path is correct

- [ ] **Docker Setup**
  - [ ] Ensure /app/data/trade_state volume is mounted
  - [ ] Verify .env file has new parameters (optional)

## 🚀 Deployment Steps

### 1. Backup Current State
```bash
# Backup current docker-compose and .env
cp docker-compose.yml docker-compose.yml.backup
cp .env .env.backup

# Check current running containers
docker-compose ps
```

### 2. Update Docker Compose (if needed)
```yaml
# docker-compose.yml
services:
  angel_bot:
    volumes:
      - ./data/trade_state:/app/data/trade_state  # Add this line
    environment:
      - ANGEL_MAX_LOTS=2
      - ANGEL_ONE_TRADE_PER_DAY=true
```

### 3. Deploy Changes
```bash
# Pull latest code
git pull

# Restart Angel bot
docker-compose restart angel_bot

# Watch logs
docker-compose logs -f angel_bot
```

### 4. Verify Startup
```bash
# Check logs for these messages:
# ✅ Initialized TradeStateManager
# 📊 Trade state synced: X traded symbols, Y open positions
# ✅ Connected to Angel

# Check state file was created
ls -la ./data/trade_state/
# Should see: angel_trades_YYYY-MM-DD.json
```

## 🧪 Post-Deployment Testing

### Test 1: State File Creation
- [ ] Verify state file exists: `./data/trade_state/angel_trades_YYYY-MM-DD.json`
- [ ] Check file contents are valid JSON
- [ ] Verify structure: `{"date": "...", "traded_symbols": [], "open_positions": []}`

### Test 2: Lot Size Configuration
- [ ] Check logs for: `📊 Fixed lot size from config: 2 lots`
- [ ] Verify orders use max 2 lots (when ANGEL_MAX_LOTS=2)
- [ ] Confirm total quantity = 2 × lot_size

### Test 3: One-Trade-Per-Day
- [ ] Place a trade for any symbol
- [ ] Verify state file shows symbol in traded_symbols
- [ ] Verify second entry attempt is blocked
- [ ] Check logs for: `⛔ Already traded today (one-trade-per-day mode)`

### Test 4: Robo Order Verification
- [ ] Place order and check Telegram/logs
- [ ] Verify message shows Entry, SL, and Target order IDs
- [ ] Check logs for: `✅ ROBO bracket complete: Entry=X, SL=Y, Target=Z`
- [ ] Login to Angel One web/mobile and verify 3 orders exist

### Test 5: State Persistence After Restart
- [ ] Place trade (symbol should be in state file)
- [ ] Restart bot: `docker-compose restart angel_bot`
- [ ] Check logs for state sync message
- [ ] Verify symbol is still marked as traded
- [ ] Attempt to trade same symbol (should be blocked)

### Test 6: Cash Insufficient Handling
- [ ] Trigger cash insufficient error (try to trade with low balance)
- [ ] Verify symbol is marked as traded
- [ ] Check logs for: `⚠️ Marking as traded to prevent retry`
- [ ] Verify no retries occur for that symbol

## 📊 Monitoring (First Trading Day)

### Morning (Market Open)
- [ ] Check state sync on startup
- [ ] Verify no duplicate trades from previous day
- [ ] Monitor first entry order placement
- [ ] Confirm lot sizing is correct (2 lots)
- [ ] Verify robo orders have SL/Target

### During Trading
- [ ] Watch for any cash insufficient errors
- [ ] Verify failed orders don't retry
- [ ] Check capital utilization (should not exceed limits)
- [ ] Monitor position count vs traded symbols

### Evening (Market Close)
- [ ] Review daily state file
- [ ] Check total trades placed
- [ ] Verify all positions closed or tracked
- [ ] Review any warnings/errors in logs

## 🔧 Troubleshooting

### Issue: State File Not Created
```bash
# Check directory exists and permissions
ls -la ./data/
mkdir -p ./data/trade_state
chmod 755 ./data/trade_state

# Check logs for errors
docker-compose logs angel_bot | grep -i "state"
```

### Issue: Lot Size Still High
```bash
# Verify environment variable
docker-compose exec angel_bot env | grep ANGEL_MAX_LOTS

# Check config loading
docker-compose logs angel_bot | grep "Fixed lot size"

# If not set, add to docker-compose.yml and restart
```

### Issue: Re-Entry Still Happening
```bash
# Check state file for symbol
cat ./data/trade_state/angel_trades_$(date +%Y-%m-%d).json

# Verify ANGEL_ONE_TRADE_PER_DAY is true
docker-compose exec angel_bot env | grep ANGEL_ONE_TRADE_PER_DAY

# Check logs for trade blocks
docker-compose logs angel_bot | grep "Already traded today"
```

### Issue: Robo Orders Missing SL/Target
```bash
# Check logs for warning
docker-compose logs angel_bot | grep "ROBO order missing"

# Verify RoboOrderManager is being used
docker-compose logs angel_bot | grep "Placing ROBO bracket order"

# Login to Angel One and manually check orders
```

## 📞 Rollback (if needed)

If issues occur, rollback:

```bash
# Stop containers
docker-compose down

# Restore backups
mv docker-compose.yml.backup docker-compose.yml
mv .env.backup .env

# Restart with old configuration
docker-compose up -d

# Verify bot is running
docker-compose logs -f angel_bot
```

## ✅ Success Criteria

Deployment is successful when:

- [ ] ✅ State file created and updating correctly
- [ ] ✅ Lot sizing limited to 2 lots per trade
- [ ] ✅ One-trade-per-day blocking re-entries
- [ ] ✅ Robo orders creating SL and Target
- [ ] ✅ Cash insufficient errors don't retry
- [ ] ✅ State persists across restarts
- [ ] ✅ No syntax errors or crashes
- [ ] ✅ Telegram alerts working correctly

## 📈 Expected Log Output

**Startup**:
```
🤖 Bot process started
✅ Connected to Angel
✅ Initialized TradeStateManager
📊 Trade state synced: 0 traded symbols, 0 open positions
```

**First Trade**:
```
[NIFTY] 🔒 Acquired trade entry lock
[NIFTY] 🔍 Checking live positions from Angel One API...
[NIFTY] ✅ No existing positions found in broker
[NIFTY] 📊 Fixed lot size from config: 2 lots
[NIFTY] 📤 Placing ROBO bracket order: CALL Entry=₹150.00, SL=₹142.50, TP=₹165.00
[NIFTY] ✅ ROBO bracket complete: Entry=123456, SL=123457, Target=123458
[NIFTY] 📝 Marked symbol as traded and position opened
[NIFTY] ✅ Order placed successfully! (Trade #1)
```

**Blocked Re-Entry**:
```
[NIFTY] 🔒 Acquired trade entry lock
[NIFTY] ⛔ Already traded today (one-trade-per-day mode)
[NIFTY] 🔓 Released trade entry lock
```

**After Restart**:
```
✅ Initialized TradeStateManager
📊 Trade state synced: 1 traded symbols, 1 open positions
📊 **Trade State Summary**
Traded Today: NIFTY
Open Positions: 1
```

---

**Deployment Date**: _____________
**Deployed By**: _____________
**Status**: ⬜ Not Started | ⬜ In Progress | ⬜ Complete
**Issues**: _____________
