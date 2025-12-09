# Quick Reference - Trade Flow & Architecture

## System Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                    MAIN PROCESS (main.py)                    │
│  ┌────────────────┐              ┌────────────────┐         │
│  │  AngelOne Bot  │              │   IBKR Bot     │         │
│  │  (NSE Market)  │              │  (US Market)   │         │
│  └────────────────┘              └────────────────┘         │
└─────────────────────────────────────────────────────────────┘
         │                                    │
         ▼                                    ▼
┌─────────────────────┐            ┌──────────────────────┐
│ AngelOne Worker     │            │ IBKR Worker          │
│ (worker.py)         │            │ (worker.py)          │
├─────────────────────┤            ├──────────────────────┤
│ 🕒 Market Watcher   │            │ 🕒 Market Watcher    │
│ 💓 Heartbeat        │            │ 💓 Heartbeat         │
│ 📊 EOD Scheduler    │            │                      │
│                     │            │                      │
│ Per Symbol:         │            │ Per Symbol:          │
│ ├─ Data Fetcher     │            │ ├─ Data Fetcher      │
│ └─ Signal Monitor   │            │ └─ Signal Monitor    │
│    (Parallel)       │            │    (Parallel)        │
└─────────────────────┘            └──────────────────────┘
         │                                    │
         └────────────────┬───────────────────┘
                          ▼
              ┌───────────────────────┐
              │ 🔒 TRADE ENTRY LOCK   │ ← NEW: Global synchronization
              │ (Sequential ordering) │
              └───────────────────────┘
```

## Trade Execution Flow

### Phase 1: Signal Detection (Parallel)
```
For each symbol (in parallel):
  ├─ Continuous 1-minute data fetching
  ├─ Wait for 15-minute candle close
  ├─ Detect 15m bias (CALL/PUT)
  └─ If bias detected → Start 5m entry search
```

### Phase 2: Entry Confirmation (Parallel with Lock)
```
For detected signal:
  ├─ Wait for up to 6x 5-minute candles
  ├─ Check 5m entry confirmation
  └─ If confirmed → Attempt trade entry ──┐
                                           │
  ┌────────────────────────────────────────┘
  │
  ▼
🔒 ACQUIRE GLOBAL LOCK (Sequential from here)
  │
  ├─ ✅ Get pre-trade balance
  ├─ ✅ Check cash availability
  ├─ ✅ Select option contract
  ├─ ✅ Get option premium
  ├─ ✅ Calculate position size
  ├─ ✅ Re-validate cash (with lock held)
  ├─ ✅ Place bracket order
  ├─ ✅ Get post-trade balance
  ├─ ✅ Send notification
  └─ ✅ Write audit log
  │
🔓 RELEASE LOCK
```

## Market Hours Management

### AngelOne (NSE Market - IST Timezone)
```
┌────────────────────────────────────────────┐
│ Market Hours: 09:15 - 15:30 IST (Mon-Fri) │
└────────────────────────────────────────────┘
         │
         ▼
  Market State Watcher (checks every 5s)
         │
         ├─ < 09:15: WAITING → Sleep until market open
         ├─ 09:15 - 15:30: OPEN ✅ → Trading active
         └─ >= 15:30: CLOSED 🛑 → Set _STOP_EVENT
                                  └─> All workers stop
```

### IBKR (US Market - ET Timezone)
```
┌────────────────────────────────────────────┐
│ Market Hours: 09:30 - 16:00 ET (Mon-Fri)  │
└────────────────────────────────────────────┘
         │
         ▼
  Market Hours Watcher (checks every 30s)
         │
         ├─ < 09:30: WAITING → Sleep until market open
         ├─ 09:30 - 16:00: OPEN ✅ → Trading active
         └─ >= 16:00: CLOSED 🛑 → Set _STOP_EVENT
                                  └─> All workers stop
```

## Cash Management Flow

### Pre-Trade Validation
```python
# 1. Acquire lock (ensures sequential checking)
async with _TRADE_ENTRY_LOCK:
    
    # 2. Get current balance
    balance = await get_account_balance()
    
    # 3. Calculate position cost
    position_cost = premium × quantity × lot_size
    
    # 4. Check available exposure
    available = await cash_mgr.available_exposure()
    
    # 5. Validate (accounts for all open positions)
    if position_cost > available:
        ❌ Block trade + Notify
    
    # 6. Register position
    cash_mgr.register_open(symbol, cost)
    
    # 7. Place order
    order = place_bracket_order(...)
    
    # 8. Get post-trade summary
    post_balance = await get_account_balance()
    
    # 9. Notify with full details
    send_notification(order_details + cash_summary)
```

## Key Global Variables

### AngelOne Worker
```python
_STOP_EVENT          # Hard stop signal (set at 15:30 IST)
_TRADE_ENTRY_LOCK    # Order placement synchronization
MARKET_OPEN_STATE    # Current market state (True/False)
MARKET_STATE_EVENT   # Wakes workers on state change
ACTIVE_OCO_ORDERS    # Tracks SL/Target orders per symbol
```

### IBKR Worker
```python
_STOP_EVENT          # Hard stop signal (set at 16:00 ET)
_TRADE_ENTRY_LOCK    # Order placement synchronization
```

## Notification Format

### 📊 Daily Start
```
🌅 [Angel/IBKR] Bot waking up for trading day...
✅ Connected to Angel/IBKR

📊 Daily Balance Check
━━━━━━━━━━━━━━━━━━━━
💰 Total Funds: ₹1,000,000.00
✅ Available: ₹800,000.00
📈 Max Allocation (70%): ₹560,000.00
🎯 Available for Trading: ₹560,000.00
━━━━━━━━━━━━━━━━━━━━
```

### 🎯 Signal Detection
```
📊 [IBKR] [TSLA] 15m Trend: CALL at 10:45 ET. Looking for 5m entry...
```

### ✅ Successful Trade
```
✅ Entered TSLA CALL
Option: TSLA241220C280
Entry: $55.00 | SL: $44.00 | TP: $77.00
━━━━━━━━━━━━━━━━━━━━
💰 Cash Summary:
Position Cost: $5,500.00
Available Funds: $94,500.00
Net Liquidation: $100,000.00
Open Positions: 2
```

### ❌ Trade Blocked
```
❌ [NIFTY] Trade blocked
Required: ₹100,000.00
Available: ₹50,000.00
Current balance: ₹500,000.00
```

### 🛑 Market Close
```
🛑 [AngelOne] Trading stopped - Market closed at 15:30 IST
```

### 📊 End of Day Report
```
📊 **End of Day Report**
━━━━━━━━━━━━━━━━━━━━
💰 Start Balance: ₹800,000.00
💰 End Balance: ₹815,000.00
📈 Daily P&L: ₹15,000.00 (+1.88%)
━━━━━━━━━━━━━━━━━━━━
📊 Total Trades: 5
📂 Open Positions: 0
━━━━━━━━━━━━━━━━━━━━
✅ All positions closed
━━━━━━━━━━━━━━━━━━━━
```

## Log Emoji Legend

| Emoji | Meaning |
|-------|---------|
| 🔒 | Trade entry lock acquired |
| 💰 | Cash/balance operations |
| 📊 | Market data / statistics |
| 🕒 | Time / scheduling events |
| ✅ | Success / confirmation |
| ❌ | Error / failure |
| ⚠️ | Warning |
| 🛑 | Stop / shutdown |
| 🔔 | Market state change |
| 🎯 | Signal detected |
| 🚀 | Order placed |
| 💓 | Heartbeat |
| 🌅 | Daily start |
| 📈 | Performance / profit |
| 📉 | Loss |
| 🏁 | Session end |

## Configuration Variables (config.py)

### Risk Management
```python
MAX_CONTRACTS_PER_TRADE = 1      # Contracts per order
RISK_PCT_OF_PREMIUM = 0.10       # 10% risk per trade
RR_RATIO = 2.0                   # Risk:Reward = 1:2
MIN_PREMIUM = 5.0                # Minimum ₹5/$ premium

MAX_DAILY_LOSS_PCT = 0.05        # 5% daily loss limit
MAX_POSITION_PCT = 0.70          # 70% max per position
ALLOC_PCT = 0.70                 # 70% allocation limit
```

### Market Hours
```python
MARKET_HOURS_ONLY = True         # Enforce market hours

# AngelOne (NSE)
NSE_MARKET_OPEN_HOUR = 9
NSE_MARKET_OPEN_MINUTE = 15
NSE_MARKET_CLOSE_HOUR = 15
NSE_MARKET_CLOSE_MINUTE = 30

# IBKR (US)
US_MARKET_OPEN_HOUR = 9
US_MARKET_OPEN_MINUTE = 30
US_MARKET_CLOSE_HOUR = 16
US_MARKET_CLOSE_MINUTE = 0
```

### Signal Detection
```python
MAX_5M_CHECKS = 6                # Max 5-min candles to check
MONITOR_INTERVAL = 2.0           # Position monitoring interval
```

## Testing Checklist

### ✅ Trade Entry Lock
- [ ] Multiple symbols detect signals simultaneously
- [ ] Only one order executes at a time
- [ ] Logs show "🔒 Acquired trade entry lock"
- [ ] Lock releases after each trade

### ✅ Cash Management
- [ ] Pre-trade balance logged correctly
- [ ] Trades blocked when insufficient funds
- [ ] Post-trade balance updated
- [ ] Notifications show correct cash summary

### ✅ Market Hours
- [ ] Bot waits when started before market open
- [ ] Trading activates at 09:15 IST / 09:30 ET
- [ ] Automatic stop at 15:30 IST / 16:00 ET
- [ ] _STOP_EVENT set correctly
- [ ] Clean shutdown of all tasks

### ✅ Parallel Processing
- [ ] Each symbol has independent data fetcher
- [ ] Signal monitors run concurrently
- [ ] No interference between symbols
- [ ] Logs show parallel activity

### ✅ Notifications
- [ ] Daily start message with balance
- [ ] Signal detection alerts
- [ ] Trade confirmations with cash summary
- [ ] Trade blocked messages with details
- [ ] Market close notifications
- [ ] End-of-day reports

## Common Operations

### Start Bot
```bash
# AngelOne container
docker-compose up angel_bot

# IBKR container
docker-compose up ibkr_bot
```

### Monitor Logs
```bash
# Real-time logs
docker-compose logs -f angel_bot
docker-compose logs -f ibkr_bot

# Log files
tail -f logs/angel_bot.log
tail -f logs/ibkr_bot.log
```

### Check Audit Files
```bash
cat audit/angel_trades.csv
cat audit/ibkr_trades.csv
```

### Manual Stop
```bash
docker-compose stop angel_bot
docker-compose stop ibkr_bot
```

## Troubleshooting

### Bot Not Trading
1. Check market hours (must be 09:15-15:30 IST or 09:30-16:00 ET)
2. Check MARKET_HOURS_ONLY setting in config
3. Verify _STOP_EVENT not already set
4. Check available cash balance

### Multiple Orders at Once
1. Verify _TRADE_ENTRY_LOCK is defined
2. Check logs for "Acquired trade entry lock"
3. Ensure execute_entry_order uses `async with _TRADE_ENTRY_LOCK:`

### Market Watcher Not Working
1. Check if watcher task started (look for "Market state/hours watcher started")
2. Verify is_market_open() function working
3. Check timezone settings (Asia/Kolkata for Angel, America/New_York for IBKR)

### Cash Check Failing
1. Verify broker API connection
2. Check get_account_balance() / get_account_summary() working
3. Ensure cash_manager initialized properly
4. Check daily loss limits not exceeded

## Performance Metrics

### Expected Behavior
- Data fetch interval: Every 1 minute per symbol
- Market state check: Every 5-30 seconds
- 15m signal check: Every 15 minutes per symbol
- 5m entry check: Up to 6 checks (30 minutes max)
- Order placement: 1-2 seconds with lock
- Balance check: <100ms

### Resource Usage
- CPU: Low (mostly I/O wait)
- Memory: ~100-200 MB per bot
- Network: Minimal (websocket + periodic API calls)
- Disk: Audit logs + standard logs

---

**Version**: 1.0  
**Last Updated**: December 2025  
**Status**: Production Ready ✅
