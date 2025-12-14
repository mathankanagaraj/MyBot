# Live Market Monitoring Checklist

## Quick Reference for Dec 12, 2025 Market Open

### Before Market Open
- [ ] Verify docker containers running: `docker compose ps`
- [ ] Check last commit includes signal validation updates
- [ ] Tail logs ready: `docker compose logs -f angel_bot` and `ibkr_bot`

---

## What to Watch in Logs

### ✅ Good Signs (Working Correctly)

**15m Bias Detection:**
```
🔍 Checking 15m bias at HH:MM:SS IST (total bars accumulated: 5m=XX, 15m=YY)...
🎯 NEW 15m signal: BULL at HH:MM:SS IST - Starting 5m entry search...
```

**5m Entry Confirmation:**
```
✅ ENTRY: 5m entry confirmed for BULL - {'type': 'BULL', 'price': XXXXX}
```

**Rejection with Reason (Good - filtering works):**
```
⏸️ ENTRY 5m check #1: Entry rejected - macd_not_clear
⏸️ ENTRY 5m check #2: Entry rejected - no_recent_ema_crossover
⏸️ ENTRY 5m check #3: Entry rejected - last_candle_not_bullish
```

---

### ⚠️ Watch Out For (Potential Issues)

**Too Many Rejections:**
If you see valid-looking trends but constant rejections:
```
⏸️ ENTRY 5m check #1-10: All rejected
```
→ Signal logic may be too strict

**False Signals:**
If orders placed in flat/choppy markets:
```
🚀 NIFTY ENTRY order placed!  [But chart is flat]
```
→ Signal logic may be too loose

**Order Failures:**
```
❌ Failed to get option premium
❌ No options available in 2-7 DTE range
```
→ Option selection issue (already fixed)

---

## Manual Chart Validation

### When Signal Detected, Check:

**15m Chart:**
1. Last 3 candles: Are 2+ of them matching the bias?
   - BULL: Should be mostly GREEN
   - BEAR: Should be mostly RED

2. MACD Histogram:
   - BULL: Should be clearly GREEN and increasing
   - BEAR: Should be clearly RED and decreasing
   - Not acceptable: Near zero or weakening

3. Price vs EMA50:
   - BULL: Price should be above EMA50
   - BEAR: Price should be below EMA50

**5m Chart:**
1. Last candle color:
   - BULL: Must be GREEN
   - BEAR: Must be RED

2. EMA Crossover:
   - BULL: EMA9 just crossed above EMA21 (within last 3 candles)
   - BEAR: EMA9 just crossed below EMA21

3. MACD:
   - BULL: Green histogram and getting BIGGER
   - BEAR: Red histogram and getting BIGGER (more negative)

4. Volume: Should show a spike vs recent average

---

## Log Analysis Commands

### See All Signals:
```bash
docker compose logs angel_bot | grep "15m signal"
docker compose logs ibkr_bot | grep "15m signal"
```

### See Rejections:
```bash
docker compose logs angel_bot | grep "rejected"
docker compose logs ibkr_bot | grep "rejected"
```

### Count Signals by Type:
```bash
docker compose logs angel_bot | grep -c "BULL"
docker compose logs angel_bot | grep -c "BEAR"
```

### See Order Placements:
```bash
docker compose logs angel_bot | grep "order placed"
docker compose logs ibkr_bot | grep "order placed"
```

---

## Adjustment Guidelines

### If Too Strict (Missing Valid Signals)

**Symptoms:**
- Strong trends visible but no signals
- Constant `core_confirmations_fail` rejections
- All rejections during clear trending periods

**Fix Options:**
1. Lower MACD thresholds in `config.py`:
   ```python
   # From
   if bias == "BULL":
       macd_ok = last["macd_hist"] > 0.02
   # To
   if bias == "BULL":
       macd_ok = last["macd_hist"] > 0.01
   ```

2. Reduce volume requirement:
   ```python
   # From
   volume_ok = last["volume"] > avg_volume * 1.2
   # To
   volume_ok = last["volume"] > avg_volume * 1.1
   ```

3. Reduce confirmation requirement:
   ```python
   # From
   if confirmations < 3:  # Need 3 of 4
   # To
   if confirmations < 2:  # Need 2 of 4
   ```

---

### If Too Loose (False Signals in Flat Markets)

**Symptoms:**
- Signals in sideways/choppy markets
- Orders placed when chart looks flat
- Green candles triggering BEAR signals (shouldn't happen now)

**Fix Options:**
1. Increase MACD thresholds:
   ```python
   # From
   macd_clearly_bullish = last["macd_hist"] > 0.05
   # To
   macd_clearly_bullish = last["macd_hist"] > 0.07
   ```

2. Require more candles matching:
   ```python
   # From
   if bullish_candles >= 2:  # 2 of 3
   # To  
   if bullish_candles >= 3:  # 3 of 3 (all must match)
   ```

3. Increase volume requirement:
   ```python
   # From
   volume_ok = last["volume"] > avg_volume * 1.2
   # To
   volume_ok = last["volume"] > avg_volume * 1.5
   ```

---

## Emergency Stop

If bot is placing bad orders:

```bash
# Stop immediately
docker compose stop

# Check what happened
docker compose logs angel_bot | tail -100
docker compose logs ibkr_bot | tail -100

# Fix and restart
docker compose up -d
```

---

## Success Criteria for Today

### Minimum Goals:
- [ ] No signals in flat/sideways markets
- [ ] No orders when candle color contradicts bias
- [ ] Rejection reasons logged clearly

### Optimal Goals:
- [ ] At least 1-2 valid signals detected correctly
- [ ] Orders placed match chart patterns
- [ ] No false signals throughout the day

### Red Flags:
- ❌ Orders in choppy markets (like GOOGL Dec 11)
- ❌ GREEN candles generating BEAR orders (like NIFTY Dec 9)
- ❌ MACD near zero but signal detected
- ❌ No signals detected even in strong trends

---

## Contact Points

### Log Files to Share:
```bash
# Save logs for analysis
docker compose logs angel_bot > angel_logs_dec12.txt
docker compose logs ibkr_bot > ibkr_logs_dec12.txt
```

### Key Metrics to Track:
- Total 15m signals detected: _____
- Total 5m entries confirmed: _____
- Total orders placed: _____
- Total rejections: _____
- False signals (manual review): _____

---

**Remember:** First day with updated logic. Expect some tweaking may be needed based on live market behavior.

**Good luck! 🚀**
