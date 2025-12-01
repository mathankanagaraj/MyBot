# Angel Broker Options Trading Bot

Professional-grade algorithmic trading bot for Indian NSE market using Angel Broker SmartAPI.

## ⚠️ IMPORTANT WARNINGS

> [!CAUTION]
> **LIVE TRADING ONLY**
> 
> Angel Broker does not support paper trading. All trades execute on your live account with real money. Use at your own risk.

> [!WARNING]
> **Risk Management**
> 
> - Maximum daily loss: ₹5,000 (configurable)
> - Maximum position size: ₹50,000 (configurable)
> - One position per symbol at a time
> - 70% maximum capital allocation

## Features

- **Indian Market Focus**: NSE stocks and NIFTY options
- **Technical Strategy**: MACD + EMA + RSI + SuperTrend + OBV
   - API Key
   - Client Code
   - Password
   - TOTP Secret (for 2FA)

## Setup Instructions

### 1. Get Angel Broker API Credentials

1. Log in to [Angel Broker SmartAPI Portal](https://smartapi.angelbroking.com/)
2. Create a new API app
3. Note down your:
   - API Key
   - Client Code (your Angel Broker username)
   - Password
   - TOTP Secret (QR code value for 2FA)

### 2. Install Dependencies

```bash
cd /Users/mathan/Documents/MyBotProjects/MACD_EMA_Bot_Angel
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure Environment

Edit `.env` file with your credentials:

```env
# Angel Broker API Credentials
ANGEL_API_KEY=your_api_key_here
ANGEL_CLIENT_CODE=your_client_code_here
ANGEL_PASSWORD=your_password_here
```

### 4. Test Connection

```bash
python3 -c "from core.angel_client import AngelClient; import asyncio; asyncio.run(AngelClient().connect_async())"
```

### 5. Run the Bot

```bash
python3 main.py
```

## Configuration

### Risk Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `MAX_CONTRACTS_PER_TRADE` | 1 | Maximum lots per trade |
| `ALLOC_PCT` | 0.70 | Maximum capital allocation (70%) |
| `MAX_DAILY_LOSS` | 5000 | Daily loss limit in ₹ |
| `MAX_POSITION_SIZE` | 50000 | Maximum position size in ₹ |

### Market Hours

- **NSE Trading Hours**: 9:15 AM - 3:30 PM IST
- **Timezone**: Asia/Kolkata
- **Market Days**: Monday - Friday

### Option Selection

- **Min DTE**: 2 days
- **Max DTE**: 7 days
- **Target Delta**: 0.40 (slightly OTM)
- **Min Open Interest**: 100 contracts
- **Max Spread**: 5%

## Strategy

### 15-Minute Trend Detection

**BULL Conditions:**
- Close > VWAP
- Close > EMA(50)
- SuperTrend = Bullish
- MACD > 0 and Histogram > 0
- OBV increasing
- RSI > 55

**BEAR Conditions:**
- Close < VWAP
- Close < EMA(50)
- SuperTrend = Bearish
- MACD < 0 and Histogram < 0
- OBV decreasing
- RSI < 45

### 5-Minute Entry Confirmation

**BULL Entry:**
- Close > VWAP
- EMA(9) > EMA(21)
- Close > SMA(20)
- MACD Histogram increasing

**BEAR Entry:**
- Close < VWAP
- EMA(9) < EMA(21)
- Close < SMA(20)
- MACD Histogram decreasing

## Monitoring

### Telegram Notifications

- Market open/close alerts
- 15m trend detection
- Trade entries with details
- Position closures
- Error alerts
- Daily P&L summary

### Log Files

- **Location**: `logs/bot.log`
- **Audit Trail**: `audit/trade_audit.csv`

## Safety Features

1. **Daily Loss Limit**: Bot stops trading if daily loss exceeds limit
2. **Position Size Limit**: Maximum ₹50,000 per position
3. **One Position Per Symbol**: Prevents over-exposure
4. **Market Hours Only**: Trades only during NSE hours
5. **Comprehensive Logging**: Full audit trail of all actions

## Troubleshooting

### Connection Issues

```bash
# Check Angel Broker API status
curl https://apiconnect.angelbroking.com/rest/secure/angelbroking/user/v1/getProfile

# Verify TOTP generation
python3 -c "import pyotp; print(pyotp.TOTP('YOUR_TOTP_SECRET').now())"
```

### Common Errors

| Error | Solution |
|-------|----------|
| "Invalid TOTP" | Verify TOTP secret is correct |
| "Symbol token not found" | Check symbol name spelling |
| "Insufficient funds" | Increase available margin |
| "Daily loss limit reached" | Wait for next trading day |

## Disclaimer

> [!CAUTION]
> **Trading Risk Disclaimer**
> 
> - Trading involves substantial risk of loss
> - Past performance does not guarantee future results
> - This bot is provided as-is without warranties
> - Use at your own risk
> - The authors are not responsible for any losses
> - Always test with minimum position sizes first

## Support

For issues or questions:
1. Check logs in `logs/bot.log`
2. Review audit trail in `audit/trade_audit.csv`
3. Verify configuration in `.env`

## License

MIT License - Use at your own risk
## 📚 Documentation

For detailed technical guides, see the [`docs/`](docs/) directory:
- [Data Flow Guide](docs/DATA_FLOW_GUIDE.md) - How 1m bars flow through the system
- [Logging Guide](docs/LOGGING_GUIDE.md) - Understanding bot logging output

