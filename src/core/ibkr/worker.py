# core/ibkr/worker.py
"""
Fully refactored IBKR broker worker implementation.
Handles US market trading with options via Interactive Brokers.
Async-safe, cancellation-aware, with heartbeat, data fetchers, signal monitors, and startup checks.
"""
import asyncio
from datetime import datetime, time

from core.config import IBKR_SYMBOLS, MAX_5M_CHECKS, RR_RATIO, IBKR_QUANTITY
from core.logger import logger
from core.utils import send_telegram
from core.ibkr.utils import is_us_market_open, get_us_et_now

_STOP_EVENT = asyncio.Event()  # Global stop event
_TRADE_ENTRY_LOCK = asyncio.Lock()  # Global trade entry lock to prevent simultaneous order placement


# -----------------------------
# Market Hours Watcher
# -----------------------------
async def market_hours_watcher():
    """
    Monitor US market hours and update global state.
    Runs continuously and provides clear logging of market state.
    IMPORTANT: Only sets _STOP_EVENT when market closes DURING active trading.
    Does not stop on startup if already after hours.
    """
    logger.info("🕒 Market hours watcher started (IBKR - US Markets)")
    
    last_market_state = None
    
    # Initialize was_trading_today based on current market state
    # If market is currently open, we're trading today
    try:
        is_currently_open = is_us_market_open()
        was_trading_today = is_currently_open
    except Exception:
        was_trading_today = False
    
    while not _STOP_EVENT.is_set():
        try:
            now_et = get_us_et_now()
            current_time = now_et.time()
            
            # Check if market is open (09:30 - 16:00 ET, Mon-Fri)
            is_weekday = now_et.weekday() <= 4
            is_market_hours = is_us_market_open()
            
            # Market open detection (09:30 - 16:00 ET)
            if is_market_hours and is_weekday:
                if last_market_state != "OPEN":
                    logger.info("✅ US Market is OPEN (09:30-16:00 ET)")
                    send_telegram("✅ [IBKR] US Market is OPEN", broker="IBKR")
                    last_market_state = "OPEN"
                    was_trading_today = True  # Mark that we're trading
            
            # Market close detection (16:00 ET) - ONLY stop if we were trading
            elif current_time >= time(16, 0) and is_weekday:
                if was_trading_today and last_market_state != "CLOSED":
                    # We were trading and now market closed - stop for the day
                    logger.info("🛑 US Market closed (16:00 ET) - Stopping all trading")
                    send_telegram("🛑 [IBKR] Trading stopped - Market closed at 16:00 ET", broker="IBKR")
                    last_market_state = "CLOSED"
                    _STOP_EVENT.set()
                    break
                elif not was_trading_today and last_market_state != "AFTER_HOURS":
                    # Started after hours - just log, don't stop
                    logger.info("🚫 US Market closed (after hours) - Waiting for next session")
                    last_market_state = "AFTER_HOURS"
            
            # Before market open or weekend
            else:
                if last_market_state != "WAITING":
                    logger.info("🚫 US Market is CLOSED - Waiting for market hours")
                    last_market_state = "WAITING"
            
            # Check every 30 seconds
            await asyncio.sleep(30)
            
        except asyncio.CancelledError:
            logger.info("Market hours watcher cancelled")
            break
        except Exception as e:
            logger.exception("Market hours watcher error: %s", e)
            await asyncio.sleep(60)
    
    logger.info("🕒 Market hours watcher stopped")


# -----------------------------
# Helper Functions
# -----------------------------
def market_closed(now_et=None):
    """Check if market is closed or past 16:00 ET."""
    now_et = now_et or get_us_et_now()
    return not is_us_market_open() or now_et.time() >= time(16, 0)


async def sleep_until_next(seconds):
    """Sleep for a period but allow cancellation."""
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return


# -----------------------------
# Heartbeat
# -----------------------------
async def heartbeat_task(interval=60):
    """Continuous heartbeat to show bot is alive."""
    logger.info("� Heartbeat task started")
    heartbeat_count = 0
    
    while not _STOP_EVENT.is_set():
        try:
            heartbeat_count += 1
            now_utc = datetime.utcnow()
            logger.info(f"� Heartbeat #{heartbeat_count}: {now_utc.strftime('%H:%M:%S')} UTC")
            
            # Sleep for interval, but check for cancellation
            await sleep_until_next(interval)
            
        except asyncio.CancelledError:
            logger.info("� Heartbeat task cancelled")
            break
        except Exception as e:
            logger.error(f"� Heartbeat task error: {e}")
            await sleep_until_next(10)  # Retry sooner on error
    
    logger.info("� Heartbeat task stopped")


# -----------------------------
# Position Check
# -----------------------------
async def has_position(ibkr_client, symbol):
    """Check if position exists for a symbol."""
    try:
        positions = await ibkr_client.get_positions()
        found = any(p["position"] != 0 and symbol in p["symbol"] for p in positions)
        logger.info(
            f"[{symbol}] Position check: {'FOUND ✅' if found else 'NOT FOUND ❌'}"
        )
        return found
    except Exception as e:
        logger.error(f"[{symbol}] Error checking positions: {e}")
        return False


# -----------------------------
# Execute Order
# -----------------------------
async def execute_entry_order(symbol, bias, ibkr_client, context="ENTRY"):
    """
    Place entry order with bracket SL/Target for IBKR.
    Uses global lock to prevent simultaneous trades.
    
    Pre-Trade Validation Flow (using IBKR API as single source of truth):
    1. Lock Acquisition - Acquire global trade lock
    2. Live Position Check - Query broker API for existing positions (CRITICAL)
    3. Balance Verification - Check available funds from broker
    4. Stock Price Fetch - Get current underlying price
    5. Option Selection - Find appropriate contract
    6. Premium Validation - Ensure premium is valid
    7. Position Sizing - Calculate cost and margin requirement
    8. Final Funds Check - Verify 2x margin available
    9. Order Placement - Execute bracket order with retries
    
    Note: Does NOT rely on local cache for position verification.
          Only IBKR API is the source of truth.
    """
    from core.ibkr.option_selector import find_ibkr_option_contract

    # Acquire global lock to prevent simultaneous trades
    async with _TRADE_ENTRY_LOCK:
        logger.info("[%s] 🔒 Acquired trade entry lock", symbol)
        
        # 1. Check real-time positions from broker API (SINGLE SOURCE OF TRUTH)
        # Retry up to 3 times for API reliability
        logger.info("[%s] 🔍 Checking live positions from IBKR API...", symbol)
        live_positions = None
        for attempt in range(3):
            try:
                live_positions = await ibkr_client.get_positions()
                break  # Success, exit retry loop
            except Exception as e:
                logger.warning("[%s] ⚠️ Position check attempt %d/3 failed: %s", symbol, attempt + 1, e)
                if attempt < 2:  # Don't sleep on last attempt
                    await asyncio.sleep(1)  # Wait 1 second before retry
        
        # If all retries failed, block trade for safety
        if live_positions is None:
            logger.error("[%s] ❌ CRITICAL: Failed to verify positions after 3 attempts", symbol)
            send_telegram(
                f"❌ [IBKR] [{symbol}] Trade blocked\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Failed to verify positions from broker\n"
                f"Retried 3 times - blocking for safety"
            , broker="IBKR")
            logger.info("[%s] 🔓 Released trade entry lock", symbol)
            return False
        
        # Check for existing positions in the same underlying
        has_position = False
        for pos in live_positions:
            position_qty = pos.get("position", 0)
            if position_qty != 0:
                pos_symbol = pos.get("symbol", "")
                # Check if this position matches our symbol
                # For options, check if underlying matches (e.g., AAPL in AAPL250117C00150000)
                if symbol == pos_symbol or symbol in pos_symbol:
                    has_position = True
                    logger.error(
                        "[%s] ❌ Live position exists in broker: %s (Qty: %d)",
                        symbol,
                        pos_symbol,
                        position_qty
                    )
                    send_telegram(
                        f"❌ [IBKR] [{symbol}] Trade blocked\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📊 Live Position Found:\n"
                        f"Symbol: {pos_symbol}\n"
                        f"Quantity: {position_qty}\n"
                        f"Market Value: ${pos.get('marketValue', 0):,.2f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"❌ Cannot open duplicate position"
                    , broker="IBKR")
                    logger.info("[%s] 🔓 Released trade entry lock", symbol)
                    return False
        
        if not has_position:
            logger.info("[%s] ✅ No existing positions found in broker", symbol)
        
        # 2. Check account balance before trade
        try:
            account_summary = await ibkr_client.get_account_summary_async()
            available_funds = float(account_summary.get("AvailableFunds", 0))
            logger.info(
                "[%s] 💰 Balance check: Available funds: $%.2f",
                symbol,
                available_funds
            )
        except Exception as e:
            logger.error("[%s] ❌ Failed to get account summary: %s", symbol, e)
            send_telegram(
                f"❌ [IBKR] [{symbol}] Trade blocked\n"
                f"Failed to get account balance"
            , broker="IBKR")
            logger.info("[%s] 🔓 Released trade entry lock", symbol)
            return False
        
        # 3. Get stock price
        stock_price = await ibkr_client.get_last_price(symbol, "STOCK")
        if not stock_price:
            logger.error("[%s] ❌ Failed to get stock price", symbol)
            send_telegram(f"❌ [IBKR] [{symbol}] Failed to get stock price", broker="IBKR")
            logger.info("[%s] 🔓 Released trade entry lock", symbol)
            return False

        # 4. Select option contract
        option_info, reason = await find_ibkr_option_contract(
            ibkr_client, symbol, bias, stock_price
        )
        if not option_info:
            logger.warning("[%s] ⚠️ %s: No option found: %s", symbol, context, reason)
            send_telegram(f"❌ [IBKR] [{symbol}] No option found: {reason}", broker="IBKR")
            logger.info("[%s] 🔓 Released trade entry lock", symbol)
            return False

        # 5. Validate premium
        premium = option_info.premium
        if premium <= 0:
            logger.error("[%s] ❌ Invalid premium: $%.2f", symbol, premium)
            send_telegram(f"❌ [IBKR] [{symbol}] Invalid premium: ${premium:.2f}", broker="IBKR")
            logger.info("[%s] 🔓 Released trade entry lock", symbol)
            return False
        
        # 6. Calculate position cost
        position_cost = premium * IBKR_QUANTITY * 100  # Options are in lots of 100
        
        # 7. Check if sufficient funds (require at least 2x position cost for margin)
        if available_funds < (position_cost * 2):
            logger.error(
                "[%s] ❌ Insufficient funds. Required: $%.2f (2x), Available: $%.2f",
                symbol,
                position_cost * 2,
                available_funds
            )
            send_telegram(
                f"❌ [IBKR] [{symbol}] Trade blocked\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Required: ${position_cost * 2:,.2f} (2x margin)\n"
                f"Available: ${available_funds:,.2f}"
            , broker="IBKR")
            logger.info("[%s] 🔓 Released trade entry lock", symbol)
            return False

        # 8. Calculate SL and Target
        stop_loss = premium * 0.8
        target = premium * (1 + 0.2 * RR_RATIO)

        logger.info(
            "[%s] 📈 %s Entry: $%.2f | SL: $%.2f | Target: $%.2f",
            symbol,
            context,
            premium,
            stop_loss,
            target,
        )
        send_telegram(
            f"🎯 [IBKR] {symbol} {bias} ({context})\n"
            f"Entry: ${premium:.2f}\n"
            f"SL: ${stop_loss:.2f}\n"
            f"Target: ${target:.2f}"
        , broker="IBKR")

        # 9. Place bracket order with retry logic
        order_ids = None
        for attempt in range(3):
            try:
                order_ids = await ibkr_client.place_bracket_order(
                    option_info.contract, IBKR_QUANTITY, stop_loss, target
                )
                if order_ids and order_ids.get("entry_order_id"):
                    break  # Success, exit retry loop
            except Exception as e:
                logger.warning("[%s] ⚠️ Order placement attempt %d/3 failed: %s", symbol, attempt + 1, e)
                if attempt < 2:  # Don't sleep on last attempt
                    await asyncio.sleep(2)  # Wait 2 seconds before retry
        
        # Check if order placement succeeded
        if not order_ids or not order_ids.get("entry_order_id"):
            logger.error("[%s] ❌ Failed to place order after 3 attempts", symbol)
            send_telegram(
                f"❌ [IBKR] [{symbol}] Order placement failed\n"
                f"Retried 3 times - unable to place order"
            , broker="IBKR")
            logger.info("[%s] 🔓 Released trade entry lock", symbol)
            return False

        # 10. Order placed successfully
        logger.info(
            "[%s] ✅ Order placed: Entry=%s | SL=%s | Target=%s | OCA=%s",
            symbol,
            order_ids.get("entry_order_id"),
            order_ids.get("sl_order_id"),
            order_ids.get("target_order_id"),
            order_ids.get("oca_group", "N/A"),
        )
        
        # Get post-trade balance summary
        try:
            account_summary_post = await ibkr_client.get_account_summary_async()
            available_funds_post = float(account_summary_post.get("AvailableFunds", 0))
            net_liquidation = float(account_summary_post.get("NetLiquidation", 0))
            
            # Get position count
            positions = await ibkr_client.get_positions()
            open_positions_count = len([p for p in positions if p["position"] != 0])
            
            send_telegram(
                f"🚀 [IBKR] {symbol} {context} order placed!\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Cash Summary:\n"
                f"Position Cost: ${position_cost:,.2f}\n"
                f"Available Funds: ${available_funds_post:,.2f}\n"
                f"Net Liquidation: ${net_liquidation:,.2f}\n"
                f"Open Positions: {open_positions_count}"
            , broker="IBKR")
        except Exception as e:
            logger.error("[%s] Failed to get post-trade summary: %s", symbol, e)
            send_telegram(f"🚀 [IBKR] {symbol} {context} order placed successfully!", broker="IBKR")
        
        return True


# -----------------------------
# Data Fetcher
# -----------------------------
async def ibkr_data_fetcher(symbol, ibkr_client, bar_manager, symbol_index=0):
    """Continuously fetch 1-minute data for a symbol and update BarManager."""
    from core.signal_engine import get_seconds_until_next_close

    await sleep_until_next(symbol_index * 0.4)
    logger.info("[%s] 📡 Data fetcher started", symbol)
    retry_count = 0

    while not _STOP_EVENT.is_set():
        try:
            now_et = get_us_et_now()
            if market_closed(now_et):
                logger.info("[%s] 🛑 Market closed, data fetcher exiting", symbol)
                break

            if not is_us_market_open():
                sleep_seconds = get_seconds_until_next_close(now_et, "5min")
                logger.debug(
                    "[%s] 💤 Market closed, sleeping %ds", symbol, sleep_seconds
                )
                await sleep_until_next(sleep_seconds)
                continue

            # Fetch only 15 minutes of 1m candles (15 bars) for 5min resampling
            df_new = await ibkr_client.req_historic_1m(symbol, duration_days=0.0104)
            if df_new is not None and not df_new.empty:
                for idx, row in df_new.iterrows():
                    await bar_manager.add_bar(
                        {
                            "datetime": idx,
                            "open": row["open"],
                            "high": row["high"],
                            "low": row["low"],
                            "close": row["close"],
                            "volume": row["volume"],
                        }
                    )
                logger.debug("[%s] 📊 Fetched %d 1m candles", symbol, len(df_new))
                retry_count = 0
            else:
                logger.warning("[%s] ⚠️ No data returned from API", symbol)
                retry_count += 1
                if retry_count > 5:
                    logger.error("[%s] ❌ Too many failures, pausing fetcher", symbol)
                    await sleep_until_next(60)
                    retry_count = 0

            sleep_seconds = get_seconds_until_next_close(now_et, "5min")
            await sleep_until_next(sleep_seconds)

        except Exception as e:
            logger.exception("[%s] ❌ Data fetcher exception: %s", symbol, e)
            await sleep_until_next(60)


# -----------------------------
# 5m Entry Search
# -----------------------------
async def search_5m_entry(symbol, bias, ibkr_client, bar_manager, context="ENTRY"):
    """
    Search for 5m entry confirmation.
    Uses DIRECT 5m bar fetching to ensure accurate price detection.
    """
    from core.signal_engine import (
        detect_15m_bias,
        detect_5m_entry,
        prepare_bars_with_indicators,
        get_next_candle_close_time,
        get_seconds_until_next_close,
    )

    checks = 0
    while checks < MAX_5M_CHECKS and not _STOP_EVENT.is_set():
        checks += 1
        now_et = get_us_et_now()
        if market_closed(now_et):
            return False

        next_5m_close = get_next_candle_close_time(now_et, "5min")
        sleep_seconds = get_seconds_until_next_close(now_et, "5min")
        logger.info(
            "[%s] ⏰ %s 5m check #%d waiting %s ET (%ds)",
            symbol,
            context,
            checks,
            next_5m_close.strftime("%H:%M:%S"),
            sleep_seconds,
        )
        await sleep_until_next(sleep_seconds)

        now_et = get_us_et_now()
        
        # Fetch DIRECT 15m and 5m bars from IBKR
        # Use 5 days to properly warm up indicators (EMA50 needs 50+ bars, MACD needs 26+, plus warm-up)
        logger.debug(f"[{symbol}] 📥 Fetching direct 15m/5m bars for entry check #{checks}...")
        df15_raw = await ibkr_client.get_historical_bars_direct(symbol, bar_size="15 mins", duration_str="5 D")
        df5_raw = await ibkr_client.get_historical_bars_direct(symbol, bar_size="5 mins", duration_str="5 D")
        
        if df15_raw is None or df15_raw.empty or df5_raw is None or df5_raw.empty:
            logger.warning(f"[{symbol}] ⚠️ No data available for 5m check #{checks}")
            continue
        
        # Prepare bars with indicators
        df15_new = prepare_bars_with_indicators(df15_raw, timeframe="15min", current_time=now_et)
        df5_new = prepare_bars_with_indicators(df5_raw, timeframe="5min", current_time=now_et)
        
        if df5_new.empty or df15_new.empty:
            continue

        # Re-check 15m bias (ensure it hasn't changed)
        bias_now = detect_15m_bias(df15_new, symbol=symbol)
        if bias_now != bias:
            send_telegram(
                f"⚠️ [IBKR] [{symbol}] {context}: 15m bias changed {bias} → {bias_now}"
            , broker="IBKR")
            return False

        # Check 5m entry with detailed logging
        entry_ok, details = detect_5m_entry(df5_new, bias, symbol=symbol)
        if not entry_ok:
            reason = details.get("reason", "unknown")
            logger.debug(
                "[%s] ⏸️ %s 5m check #%d: Entry rejected - %s",
                symbol,
                context,
                checks,
                reason,
            )
            continue
            
        # Entry confirmed!
        logger.info(
            "[%s] ✅ %s: 5m entry confirmed for %s",
            symbol,
            context,
            bias
        )
        return await execute_entry_order(symbol, bias, ibkr_client, context)

    return False


# -----------------------------
# Startup 15m Signal Detection
# -----------------------------
async def handle_startup_signal(symbol, ibkr_client, bar_manager):
    """Check for recent 15m signal on startup and search for 5m entry."""
    from core.signal_engine import detect_15m_bias, prepare_bars_with_indicators

    try:
        now_et = get_us_et_now()
        
        # Fetch 15m bars directly from IBKR (last 5 days to properly warm up EMA50, MACD, RSI and all indicators)
        logger.info(f"[{symbol}] 📥 STARTUP: Fetching direct 15m bars from IBKR...")
        df15_raw = await ibkr_client.get_historical_bars_direct(symbol, bar_size="15 mins", duration_str="5 D")
        if df15_raw is None or df15_raw.empty:
            logger.warning(f"[{symbol}] ⚠️ STARTUP: No 15m data available")
            return
        
        # Add indicators and filter incomplete candles
        df15_startup = prepare_bars_with_indicators(df15_raw, timeframe="15min", current_time=now_et)
        if df15_startup.empty:
            logger.warning(f"[{symbol}] ⚠️ STARTUP: No complete 15m bars after filtering")
            return

        # Detect 15m bias
        startup_bias = detect_15m_bias(df15_startup, symbol=symbol)
        if not startup_bias:
            return

        # We have a valid 15m bias - search for 5m entry
        logger.info(
            "[%s] 🔍 STARTUP: Detected 15m %s bias - Starting 5m entry search",
            symbol,
            startup_bias,
        )
        send_telegram(
            f"🔍 [IBKR] [{symbol}] Startup detected 15m {startup_bias} bias. Searching for entry..."
        , broker="IBKR")

        # Search for 5m entry
        await search_5m_entry(symbol, startup_bias, ibkr_client, bar_manager, "STARTUP")

    except Exception as e:
        logger.exception("[%s] Error in startup signal detection: %s", symbol, e)


async def ibkr_signal_monitor(symbol, ibkr_client, bar_manager):
    """
    Monitor for trading signals on a symbol.
    Uses DIRECT 15m bar fetching instead of resampling to ensure accurate price detection.

    Args:
        symbol: Symbol to monitor
        ibkr_client: IBKR API client
        bar_manager: Bar manager for this symbol (kept for 5m entry searches)
    """
    from core.signal_engine import (
        detect_15m_bias,
        prepare_bars_with_indicators,
        get_next_candle_close_time,
        get_seconds_until_next_close,
    )

    logger.info("[%s] 👀 Signal monitor started (DIRECT 15m fetch mode)", symbol)

    # STARTUP: Check for recent 15m signal and search for entry
    # Note: Position check is done inside execute_entry_order, not here
    await handle_startup_signal(symbol, ibkr_client, bar_manager)

    # MAIN LOOP: Monitor for new 15m signals
    while not _STOP_EVENT.is_set():

        try:
            now_et = get_us_et_now()

            # Strict Market Close Check
            if now_et.time() >= time(16, 0):
                logger.info(
                    "[%s] 🛑 Market closed (16:00 reached), stopping signal monitor",
                    symbol,
                )
                break

            # Market hours guard
            if not is_us_market_open():
                logger.debug("[%s] 💤 Market closed, signal monitor sleeping", symbol)
                await asyncio.sleep(300)
                continue

            # Wait for next 15m candle close
            # Note: Position check happens inside execute_entry_order, not here
            next_15m_close = get_next_candle_close_time(now_et, "15min")
            sleep_seconds = get_seconds_until_next_close(now_et, "15min")

            logger.debug(
                "[%s] ⏰ Waiting for next 15m close at %s ET (sleeping %ds)",
                symbol,
                next_15m_close.strftime("%H:%M:%S"),
                sleep_seconds,
            )
            await asyncio.sleep(sleep_seconds)

            # Get fresh data at 15m boundary
            now_et = get_us_et_now()

            # Double check market still open after sleep
            if now_et.time() >= time(16, 0) or not is_us_market_open():
                logger.info("[%s] 🛑 Market closed after sleep", symbol)
                break

            # Fetch DIRECT 15m bars from IBKR (ensures we get exact candle close prices)
            logger.info(f"[{symbol}] 📥 Fetching direct 15m bars from IBKR...")
            # Fetch 5 days to properly warm up EMA50, MACD(26), RSI(14) and other indicators
            df15_raw = await ibkr_client.get_historical_bars_direct(symbol, bar_size="15 mins", duration_str="5 D")
            if df15_raw is None or df15_raw.empty:
                logger.warning(f"[{symbol}] ⚠️ No 15m data available, skipping")
                continue
            
            # Add indicators and filter incomplete candles
            df15m = prepare_bars_with_indicators(df15_raw, timeframe="15min", current_time=now_et)
            if df15m.empty:
                logger.debug("[%s] ⚠️ Empty dataframe after filtering, skipping this 15m check", symbol)
                continue

            # Detect 15m bias
            logger.info(
                "[%s] 🕒 Checking 15m bias at %s ET (bars: %d, latest close: $%.2f)...",
                symbol,
                now_et.strftime("%H:%M:%S"),
                len(df15m),
                df15m['close'].iloc[-1]
            )
            bias = detect_15m_bias(df15m, symbol=symbol)
            if not bias:
                logger.debug("[%s] No clear 15m bias", symbol)
                continue

            # Notify 15m bias found
            logger.info(
                "[%s] 🎯 NEW 15m signal: %s at %s ET - Starting 5m entry search...",
                symbol,
                bias,
                now_et.strftime("%H:%M:%S"),
            )
            send_telegram(
                f"📊 [IBKR] [{symbol}] 15m Trend: {bias} at {now_et.strftime('%H:%M')} ET. Looking for 5m entry..."
            , broker="IBKR")

            # Search for 5m entry confirmation
            await search_5m_entry(symbol, bias, ibkr_client, bar_manager, "ENTRY")

        except Exception as e:
            logger.exception("[%s] ❌ Signal monitor exception: %s", symbol, e)
            if "Not connected" in str(e) or "Peer closed" in str(e):
                logger.error("[%s] Connection lost, signal monitor exiting", symbol)
                break
            await asyncio.sleep(60)


async def calculate_wait_time(current_time, start_time, end_time, is_weekday, now_et):
    from datetime import timedelta
    import pytz

    if current_time >= end_time or not is_weekday:
        # Wait until tomorrow 09:00 (start point)
        next_start = datetime.combine(now_et.date() + timedelta(days=1), start_time)
    else:
        # Wait until today 09:00 (if started before market open)
        next_start = datetime.combine(now_et.date(), start_time)

    # Skip weekends
    while next_start.weekday() > 4:  # If Sat(5) or Sun(6)
        next_start += timedelta(days=1)

    # Make next_start timezone aware
    tz = pytz.timezone("America/New_York")
    if next_start.tzinfo is None:
        next_start = tz.localize(next_start)

    wait_seconds = (next_start - now_et).total_seconds()
    wait_hours = wait_seconds / 3600

    logger.info(
        f"💤 Market closed. Sleeping {wait_hours:.1f} hours until "
        f"{next_start.strftime('%Y-%m-%d %H:%M')} ET (09:00 market open)"
    )
    return wait_seconds


async def run_ibkr_workers():
    """
    Run IBKR worker for US market with full trading logic.
    Features:
    - Daily loop (starts fresh each day)
    - Smart sleep (waits for market open)
    - Heartbeat (keeps container alive)
    - Market hours watcher (monitors market open/close)
    """
    from core.ibkr.client import IBKRClient
    from core.bar_manager import BarManager

    logger.info("🤖 IBKR Bot process started")

    # Start heartbeat task immediately and continuously
    heartbeat = asyncio.create_task(heartbeat_task())
    
    # Start market hours watcher immediately
    market_watcher = asyncio.create_task(market_hours_watcher())

    while not _STOP_EVENT.is_set():

        try:
            # --- 1. Check Market Hours & Sleep Logic ---
            now_et = get_us_et_now()
            current_time = now_et.time()

            # Define active window: 09:00 ET to 16:00 ET
            # We start at 09:00 to allow 30 mins for pre-market checks/sync
            start_time = time(9, 0)
            end_time = time(16, 0)

            # Check if we are in the active window (Mon-Fri)
            is_weekday = now_et.weekday() <= 4  # 0=Mon, 4=Fri
            is_active_window = is_weekday and (start_time <= current_time < end_time)

            if not is_active_window:
                # Calculate wait time until next start (09:00 ET)
                wait_seconds = await calculate_wait_time(
                    current_time, start_time, end_time, is_weekday, now_et
                )

                # Sleep in chunks to allow for graceful shutdown
                while wait_seconds > 0 and not _STOP_EVENT.is_set():
                    sleep_chunk = min(wait_seconds, 60)
                    await asyncio.sleep(sleep_chunk)
                    wait_seconds -= sleep_chunk

                if _STOP_EVENT.is_set():
                    break

            # --- 2. Start Daily Trading Session ---
            logger.info("🌅 Starting daily trading cycle...")
            send_telegram("🌅 [IBKR] Bot waking up for trading day...", broker="IBKR")

            # Initialize IBKR client
            ibkr_client = IBKRClient()

            # Connect to IBKR
            await ibkr_client.connect_async()

            if not ibkr_client.connected:
                logger.error("❌ Failed to connect to IBKR. Retrying in 1 minute...")
                await asyncio.sleep(60)
                continue

            logger.info("✅ Connected to IBKR")
            send_telegram("✅ Connected to IBKR", broker="IBKR")

            # Wait for portfolio sync
            logger.info("⏳ Waiting 5s for portfolio sync...")
            await asyncio.sleep(5)

            # Initialize BarManagers for each symbol
            bar_managers = {}
            logger.info("Initializing BarManagers and loading historical data...")

            for symbol in IBKR_SYMBOLS:
                # Create BarManager (fresh instance each day)
                bar_mgr = BarManager(symbol, max_bars=2880)  # 2 days of 1m bars
                bar_managers[symbol] = bar_mgr

                # Load initial historical data
                logger.info("[%s] Loading historical data...", symbol)
                df_hist = await ibkr_client.req_historic_1m(symbol, duration_days=2)

                if df_hist is not None and not df_hist.empty:
                    await bar_mgr.initialize_from_historical(df_hist)
                    logger.info("[%s] Loaded %d historical bars", symbol, len(df_hist))
                else:
                    logger.warning("[%s] Failed to load historical data", symbol)

            # Start worker tasks
            tasks = []

            # Start data fetchers and signal monitors for each symbol
            logger.info("🚀 Starting data fetchers and signal monitors...")
            for idx, symbol in enumerate(IBKR_SYMBOLS):
                bar_mgr = bar_managers.get(symbol)

                # Start data fetcher
                logger.info("Starting data fetcher for %s", symbol)
                tasks.append(ibkr_data_fetcher(symbol, ibkr_client, bar_mgr, idx))

                # Start signal monitor
                logger.info("Starting signal monitor for %s", symbol)
                tasks.append(ibkr_signal_monitor(symbol, ibkr_client, bar_mgr))

            send_telegram("🚀 [IBKR] Bot Started (Session Active)", broker="IBKR")

            # Wait for all tasks to complete
            # The workers are designed to exit at 16:00 ET
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                logger.info("Tasks cancelled")
            except Exception as e:
                logger.exception("Error in task group: %s", e)

            # Cleanup after session ends
            logger.info("🏁 Trading session ended (16:00 ET reached)")
            ibkr_client.disconnect()
            logger.info("👋 Disconnected from IBKR")

        except Exception as e:
            logger.exception("CRITICAL: Error in main daily loop: %s", e)
            send_telegram(f"🚨 CRITICAL: IBKR Bot daily loop error: {str(e)[:100]}", broker="IBKR")
            await asyncio.sleep(60)  # Prevent tight loop on error

    # Main loop exited - this is normal end of day
    logger.info("🏁 IBKR main loop completed for the day")
    
    # Cleanup: Wait for background tasks to finish
    logger.info("Shutting down IBKR workers...")
    
    # Cancel market watcher if still running
    if not market_watcher.done():
        market_watcher.cancel()
        try:
            await market_watcher
        except Exception:
            pass
    
    # Wait for heartbeat to finish if stopped
    if not heartbeat.done():
        heartbeat.cancel()
        try:
            await heartbeat
        except Exception:
            pass
    
    logger.info("IBKR workers shutdown complete")
    
    # Don't exit main process - let Docker handle restart if needed
    # This prevents immediate restart loop


def stop_ibkr_workers():
    """Stop all IBKR workers"""
    _STOP_EVENT.set()
    logger.info("🛑 Stop signal sent to all workers")
