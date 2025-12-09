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
    Background task that monitors US market hours and sets _STOP_EVENT at market close.
    Runs continuously and provides clear logging of market state.
    IMPORTANT: Only sets _STOP_EVENT when market closes DURING active trading.
    Does not stop on startup if already after hours.
    """
    logger.info("🕒 Market hours watcher started (IBKR - US Markets)")
    
    last_market_state = None
    was_trading_today = False  # Track if we started trading today
    
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
                    send_telegram("✅ [IBKR] US Market is OPEN")
                    last_market_state = "OPEN"
                    was_trading_today = True  # Mark that we're trading
            
            # Market close detection (16:00 ET) - ONLY stop if we were trading
            elif current_time >= time(16, 0) and is_weekday:
                if was_trading_today and last_market_state != "CLOSED":
                    # We were trading and now market closed - stop for the day
                    logger.info("🛑 US Market closed (16:00 ET) - Stopping all trading")
                    send_telegram("🛑 [IBKR] Trading stopped - Market closed at 16:00 ET")
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
    """Place entry order with bracket SL/Target. Uses global lock to prevent simultaneous trades."""
    from core.ibkr.option_selector import find_ibkr_option_contract

    # Acquire global lock to prevent simultaneous trades
    async with _TRADE_ENTRY_LOCK:
        logger.info("[%s] 🔒 Acquired trade entry lock", symbol)
        
        # Check account balance before trade
        try:
            account_summary = await ibkr_client.get_account_summary()
            available_funds = float(account_summary.get("AvailableFunds", 0))
            logger.info(
                "[%s] 💰 Pre-trade check: Available funds: $%.2f",
                symbol,
                available_funds
            )
        except Exception as e:
            logger.error("[%s] ❌ Failed to get account summary: %s", symbol, e)
            available_funds = 0.0
        
        stock_price = await ibkr_client.get_last_price(symbol, "STOCK")
        if not stock_price:
            logger.error("[%s] ❌ Failed to get stock price", symbol)
            return False

        option_info, reason = await find_ibkr_option_contract(
            ibkr_client, symbol, bias, stock_price
        )
        if not option_info:
            logger.warning("[%s] ⚠️ %s: No option found: %s", symbol, context, reason)
            return False

        premium = option_info.premium
        if premium <= 0:
            logger.error("[%s] ❌ Invalid premium: $%.2f", symbol, premium)
            return False
        
        # Calculate position cost
        position_cost = premium * IBKR_QUANTITY * 100  # Options are in lots of 100
        
        # Check if sufficient funds (require at least 2x position cost for margin)
        if available_funds < (position_cost * 2):
            logger.error(
                "[%s] ❌ Insufficient funds. Required: $%.2f (2x), Available: $%.2f",
                symbol,
                position_cost * 2,
                available_funds
            )
            send_telegram(
                f"❌ [IBKR] [{symbol}] Trade blocked\n"
                f"Required: ${position_cost * 2:,.2f} (2x margin)\n"
                f"Available: ${available_funds:,.2f}"
            )
            return False

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
            f"🎯 [IBKR] {symbol} {bias} ({context})\nEntry: ${premium:.2f}\nSL: ${stop_loss:.2f}\nTarget: ${target:.2f}"
        )

        try:
            order_ids = await ibkr_client.place_bracket_order(
                option_info.contract, IBKR_QUANTITY, stop_loss, target
            )
        except Exception as e:
            logger.exception("[%s] ❌ Order placement failed: %s", symbol, e)
            send_telegram(f"🚨 [IBKR] [{symbol}] Order exception: {e}")
            return False

        if not order_ids or not order_ids.get("entry_order_id"):
            logger.error("[%s] ❌ Failed to place order", symbol)
            return False

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
            account_summary_post = await ibkr_client.get_account_summary()
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
            )
        except Exception as e:
            logger.error("[%s] Failed to get post-trade summary: %s", symbol, e)
            send_telegram(f"🚀 [IBKR] {symbol} {context} order placed successfully!")
        
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
                logger.info("[%s] 📊 Fetched %d 1m candles", symbol, len(df_new))
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
    from core.signal_engine import (
        detect_15m_bias,
        detect_5m_entry,
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
        df5_new, df15_new = await bar_manager.get_resampled()
        if df5_new.empty or df15_new.empty:
            continue

        bias_now = detect_15m_bias(df15_new)
        if bias_now != bias:
            send_telegram(
                f"⚠️ [IBKR] [{symbol}] {context}: 15m bias changed {bias} → {bias_now}"
            )
            return False

        entry_ok, _ = detect_5m_entry(df5_new, bias)
        if entry_ok:
            return await execute_entry_order(symbol, bias, ibkr_client, context)

    return False


# -----------------------------
# Startup 15m Signal Detection
# -----------------------------
async def handle_startup_signal(symbol, ibkr_client, bar_manager):
    """Check for recent 15m signal on startup and search for 5m entry."""
    from core.signal_engine import detect_15m_bias

    try:

        # Get current data
        df5_startup, df15_startup = await bar_manager.get_resampled()
        if df5_startup.empty or df15_startup.empty:
            return

        # Detect 15m bias
        startup_bias = detect_15m_bias(df15_startup)
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
        )

        # Search for 5m entry
        await search_5m_entry(symbol, startup_bias, ibkr_client, bar_manager, "STARTUP")

    except Exception as e:
        logger.exception("[%s] Error in startup signal detection: %s", symbol, e)


async def ibkr_signal_monitor(symbol, ibkr_client, bar_manager):
    """
    Monitor for trading signals on a symbol.

    Args:
        symbol: Symbol to monitor
        ibkr_client: IBKR API client
        bar_manager: Bar manager for this symbol
    """
    from core.signal_engine import (
        detect_15m_bias,
        get_next_candle_close_time,
        get_seconds_until_next_close,
    )

    logger.info("[%s] 👀 Signal monitor started", symbol)

    # Helper function to check if we already have a position for this symbol
    async def has_position(sym):
        try:
            positions = await ibkr_client.get_positions()
            found = False
            for p in positions:
                if p["symbol"] == sym and p["position"] != 0:
                    found = True
                    break
                if sym in p["symbol"] and p["position"] != 0:
                    found = True
                    break

            if found:
                logger.info(f"[{sym}] Position check: FOUND ✅")
                return True
            else:
                logger.info(
                    f"[{sym}] Position check: NOT FOUND ❌ (Checked {len(positions)} positions)"
                )
                return False
        except Exception as ex:
            logger.error(f"[{sym}] Error checking positions: {ex}")
            return False

    # STARTUP: Check for recent 15m signal and search for entry
    await handle_startup_signal(symbol, ibkr_client, bar_manager, has_position)

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

            # Check if we already have a position for this symbol
            if await has_position(symbol):
                logger.info("[%s] ⏸️ Position exists, pausing signal monitoring", symbol)
                await asyncio.sleep(60)
                continue

            # Wait for next 15m candle close
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

            df5m, df15m = await bar_manager.get_resampled()
            if df5m.empty or df15m.empty:
                logger.debug("[%s] ⚠️ Empty dataframe, skipping this 15m check", symbol)
                continue

            # Detect 15m bias
            logger.info(
                "[%s] 🕒 Checking 15m bias at %s ET (bars: 5m=%d, 15m=%d)...",
                symbol,
                now_et.strftime("%H:%M:%S"),
                len(df5m),
                len(df15m),
            )
            bias = detect_15m_bias(df15m)
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
            )

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
            send_telegram("🌅 [IBKR] Bot waking up for trading day...")

            # Initialize IBKR client
            ibkr_client = IBKRClient()

            # Connect to IBKR
            await ibkr_client.connect_async()

            if not ibkr_client.connected:
                logger.error("❌ Failed to connect to IBKR. Retrying in 1 minute...")
                await asyncio.sleep(60)
                continue

            logger.info("✅ Connected to IBKR")
            send_telegram("✅ Connected to IBKR")

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

            send_telegram("🚀 [IBKR] Bot Started (Session Active)")

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
            send_telegram(f"🚨 CRITICAL: IBKR Bot daily loop error: {str(e)[:100]}")
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
