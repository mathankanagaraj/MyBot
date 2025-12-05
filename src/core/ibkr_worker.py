# core/ibkr_worker.py
"""
IBKR broker worker implementation.
Handles US market trading with options via Interactive Brokers.
"""
import asyncio
from datetime import time

from core.config import IBKR_SYMBOLS
from core.logger import logger
from core.utils import send_telegram
from core.ibkr_utils import is_us_market_open, get_us_et_now


# Global stop flag
_STOP = False


async def ibkr_data_fetcher(symbol, ibkr_client, bar_manager, symbol_index):
    """
    Continuously fetch 1-minute data for a symbol and update BarManager.
    Aligned to 5-minute boundaries like Angel One.

    Args:
        symbol: Symbol to fetch data for
        ibkr_client: IBKR API client
        bar_manager: Bar manager for this symbol
        symbol_index: Index of symbol in list (for staggered startup)
    """
    from core.signal_engine import (
        get_next_candle_close_time,
        get_seconds_until_next_close,
    )

    global _STOP

    # Stagger initial startup to prevent hitting rate limits
    startup_delay = symbol_index * 0.4
    if startup_delay > 0:
        logger.info(
            "[%s] 📡 Data fetcher starting in %.1fs (staggered)", symbol, startup_delay
        )
        await asyncio.sleep(startup_delay)

    logger.info("[%s] 📡 Data fetcher started", symbol)
    retry_count = 0

    while not _STOP:
        try:
            now_et = get_us_et_now()

            # Strict Market Close Check
            if now_et.time() >= time(16, 0):
                logger.info(
                    "[%s] 🛑 Market closed (16:00 reached), stopping data fetcher",
                    symbol,
                )
                break

            # Check if market is open
            if not is_us_market_open():
                # Market is closed, sleep until next 5-minute boundary
                next_check = get_next_candle_close_time(now_et, "5min")
                sleep_seconds = get_seconds_until_next_close(now_et, "5min")

                logger.debug(
                    "[%s] 💤 Market closed, data fetcher sleeping until %s ET",
                    symbol,
                    next_check.strftime("%H:%M:%S"),
                )
                await asyncio.sleep(sleep_seconds)
                continue

            # Market is open - fetch data at 5-minute boundaries
            # Fetch last 15 minutes of data to ensure we don't miss any bars (matching Angel One)
            df_new = await ibkr_client.req_historic_1m(
                symbol, duration_days=0.0104
            )  # ~15 minutes

            if df_new is not None and not df_new.empty:
                # Add new bars to BarManager
                for idx, row in df_new.iterrows():
                    bar_dict = {
                        "datetime": idx,
                        "open": row["open"],
                        "high": row["high"],
                        "low": row["low"],
                        "close": row["close"],
                        "volume": row["volume"],
                    }
                    await bar_manager.add_bar(bar_dict)

                logger.info(
                    "[%s] 📊 Fetched %d 1m candles at %s ET",
                    symbol,
                    len(df_new),
                    now_et.strftime("%H:%M:%S"),
                )
                retry_count = 0  # Reset retry counter on success
            else:
                logger.warning(
                    "[%s] ⚠️ No data returned from API at %s ET (market open)",
                    symbol,
                    now_et.strftime("%H:%M:%S"),
                )
                retry_count += 1
                if retry_count > 5:
                    logger.error(
                        "[%s] ❌ Too many consecutive failures, pausing fetcher", symbol
                    )
                    await asyncio.sleep(60)
                    retry_count = 0

            # Sleep until next 5-minute boundary (matching Angel One pattern)
            sleep_seconds = get_seconds_until_next_close(now_et, "5min")
            await asyncio.sleep(sleep_seconds)

        except Exception as e:
            logger.exception("[%s] ❌ Data fetcher exception: %s", symbol, e)
            if "Not connected" in str(e) or "Peer closed" in str(e):
                logger.error("[%s] Connection lost, data fetcher exiting", symbol)
                break
            await asyncio.sleep(60)


async def execute_entry_order(symbol, bias, ibkr_client, context="ENTRY"):
    """
    Execute entry order with option selection and bracket order placement.

    Args:
        symbol: Stock symbol
        bias: BULL or BEAR
        ibkr_client: IBKR API client
        context: "STARTUP" or "ENTRY" for logging

    Returns:
        True if order placed successfully, False otherwise
    """
    from core.ibkr_option_selector import find_ibkr_option_contract
    from core.config import RR_RATIO, IBKR_QUANTITY

    # Get stock price
    stock_price = await ibkr_client.get_last_price(symbol, "STOCK")
    if not stock_price:
        logger.error("[%s] ❌ Failed to get stock price", symbol)
        return False

    # Find option
    option_info, reason = await find_ibkr_option_contract(
        ibkr_client, symbol, bias, stock_price
    )

    if not option_info:
        logger.warning("[%s] ⚠️ %s: No option found: %s", symbol, context, reason)
        return False

    premium = option_info.get("premium", 0)
    if premium <= 0:
        logger.error("[%s] ❌ Invalid premium: $%.2f", symbol, premium)
        return False

    # Calculate bracket levels
    stop_loss = premium * 0.8
    target = premium * (1 + (0.2 * RR_RATIO))

    logger.info(
        f"[IBKR] [{symbol}] 📈 {context} Entry: ${premium:.2f}, "
        f"SL: ${stop_loss:.2f}, Target: ${target:.2f}"
    )

    send_telegram(
        f"🎯 [IBKR] {symbol} {bias} ({context})\n"
        f"Entry: ${premium:.2f}\n"
        f"SL: ${stop_loss:.2f}\n"
        f"Target: ${target:.2f}"
    )

    # Place bracket order
    logger.info(f"[IBKR] [{symbol}] 🚀 Placing {context} Bracket Order...")
    order_ids = await ibkr_client.place_bracket_order(
        option_info["contract"],
        IBKR_QUANTITY,
        stop_loss,
        target,
    )

    if not order_ids:
        logger.error(f"[IBKR] ❌ Failed to place {context} order for {symbol}")
        send_telegram(f"🚨 [IBKR] {context} Order Placement Failed for {symbol}!")
        return False

    logger.info(f"[IBKR] ✅ Order placed! Entry ID: {order_ids.get('entry_order_id')}")
    send_telegram(
        f"🚀 [IBKR] {context} ORDER PLACED!\n"
        f"Symbol: {symbol}\n"
        f"Contract: {option_info['symbol']}\n"
        f"Entry ID: {order_ids.get('entry_order_id')}\n"
        f"SL ID: {order_ids.get('sl_order_id')}\n"
        f"Target ID: {order_ids.get('target_order_id')}"
    )

    # Report balance
    try:
        summary = await ibkr_client.get_account_summary_async()
        funds = summary.get("AvailableFunds", 0.0)
        net_liq = summary.get("NetLiquidation", 0.0)
        logger.info(f"[IBKR] Cash Balance: ${funds:,.2f} | Net Liq: ${net_liq:,.2f}")
        send_telegram(
            f"💰 [IBKR] Balance Update:\nCash: ${funds:,.2f}\nNet Liq: ${net_liq:,.2f}"
        )
    except Exception as exc:
        logger.error(f"[IBKR] Failed to fetch balance: {exc}")

    return True


async def search_5m_entry(symbol, bias, ibkr_client, bar_manager, context="ENTRY"):
    """
    Search for 5m entry confirmation over multiple candles.

    Args:
        symbol: Stock symbol
        bias: BULL or BEAR from 15m detection
        ibkr_client: IBKR API client
        bar_manager: Bar manager for this symbol
        context: "STARTUP" or "ENTRY" for logging

    Returns:
        True if entry executed, False otherwise
    """
    from core.signal_engine import (
        detect_15m_bias,
        detect_5m_entry,
        get_next_candle_close_time,
        get_seconds_until_next_close,
    )
    from core.config import MAX_5M_CHECKS

    global _STOP
    checks = 0

    while checks < MAX_5M_CHECKS and not _STOP:
        checks += 1
        now_et = get_us_et_now()

        # Market close check
        if now_et.time() >= time(16, 0):
            logger.info(
                "[%s] 🛑 Market closed, stopping %s entry search", symbol, context
            )
            return False

        # Wait for next 5m candle close
        next_5m_close = get_next_candle_close_time(now_et, "5min")
        sleep_seconds = get_seconds_until_next_close(now_et, "5min")

        logger.info(
            "[%s] ⏰ %s 5m check #%d - waiting for %s ET (sleeping %ds)",
            symbol,
            context,
            checks,
            next_5m_close.strftime("%H:%M:%S"),
            sleep_seconds,
        )
        await asyncio.sleep(sleep_seconds)

        now_et = get_us_et_now()
        if now_et.time() >= time(16, 0):
            return False

        # Get fresh data
        df5_new, df15_new = await bar_manager.get_resampled()
        if df5_new.empty or df15_new.empty:
            logger.debug("[%s] ⚠️ Empty dataframe at 5m check #%d", symbol, checks)
            continue

        # Revalidate 15m bias
        bias_now = detect_15m_bias(df15_new)
        if bias_now != bias:
            logger.warning(
                "[%s] ⚠️ 15m bias changed %s → %s, aborting %s entry search",
                symbol,
                bias,
                bias_now,
                context,
            )
            send_telegram(
                f"⚠️ [IBKR] [{symbol}] {context}: 15m bias changed {bias} → {bias_now}, aborting"
            )
            return False

        # Check 5m entry
        entry_ok, details = detect_5m_entry(df5_new, bias)
        if not entry_ok:
            continue

        # Entry confirmed!
        logger.info(
            "[%s] ✅ %s: 5m entry confirmed for %s - %s",
            symbol,
            context,
            bias,
            details,
        )

        # Execute order
        success = await execute_entry_order(symbol, bias, ibkr_client, context)
        return success

    logger.info("[%s] ⛔ No %s entry after %d checks", symbol, context, checks)
    return False


async def handle_startup_signal(symbol, ibkr_client, bar_manager, has_position_fn):
    """
    Check for recent 15m signal on startup and search for entry if found.

    Args:
        symbol: Stock symbol
        ibkr_client: IBKR API client
        bar_manager: Bar manager for this symbol
        has_position_fn: Async function to check for existing position
    """
    from core.signal_engine import detect_15m_bias

    try:
        # Check for existing position
        if await has_position_fn(symbol):
            logger.info(
                "[%s] ⏸️ Position already exists at startup, skipping startup entry search",
                symbol,
            )
            return

        # Check if market is open
        now_et = get_us_et_now()
        if not is_us_market_open():
            return

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

    global _STOP

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
                logger.debug(f"[{sym}] Position check: FOUND ✅")
                return True
            else:
                logger.debug(
                    f"[{sym}] Position check: NOT FOUND ❌ (Checked {len(positions)} positions)"
                )
                return False
        except Exception as ex:
            logger.error(f"[{sym}] Error checking positions: {ex}")
            return False

    # STARTUP: Check for recent 15m signal and search for entry
    await handle_startup_signal(symbol, ibkr_client, bar_manager, has_position)

    # MAIN LOOP: Monitor for new 15m signals
    while not _STOP:
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


async def run_ibkr_workers():
    """
    Run IBKR worker for US market with full trading logic.
    """
    from core.ibkr_client import IBKRClient
    from core.bar_manager import BarManager

    global _STOP

    logger.info("[IBKR] 🚀 Starting IBKR workers...")
    send_telegram("🚀 [IBKR] Starting IBKR workers...")

    # Initialize IBKR client
    ibkr_client = IBKRClient()

    # Connect to IBKR
    await ibkr_client.connect_async()

    if not ibkr_client.connected:
        logger.error("[IBKR] ❌ Failed to connect to IBKR")
        send_telegram("❌ [IBKR] Failed to connect to IBKR")
        return

    logger.info("[IBKR] ✅ Connected to IBKR")
    send_telegram("✅ [IBKR] Connected to IBKR")

    # Wait for portfolio sync
    logger.info("[IBKR] ⏳ Waiting 5s for portfolio sync...")
    await asyncio.sleep(5)

    # Initialize BarManagers for each symbol
    bar_managers = {}

    logger.info("[IBKR] Initializing BarManagers and loading historical data...")

    for symbol in IBKR_SYMBOLS:
        # Create BarManager
        bar_mgr = BarManager(symbol, max_bars=2880)  # 2 days of 1m bars
        bar_managers[symbol] = bar_mgr

        # Load initial historical data
        logger.info("[IBKR] [%s] Loading historical data...", symbol)
        df_hist = await ibkr_client.req_historic_1m(symbol, duration_days=2)

        if df_hist is not None and not df_hist.empty:
            await bar_mgr.initialize_from_historical(df_hist)
            logger.info("[IBKR] [%s] Loaded %d historical bars", symbol, len(df_hist))
        else:
            logger.warning("[IBKR] [%s] Failed to load historical data", symbol)

    # Start worker tasks
    tasks = []

    # Start data fetchers and signal monitors for each symbol
    logger.info("[IBKR] 🚀 Starting data fetchers and signal monitors...")
    for idx, symbol in enumerate(IBKR_SYMBOLS):
        bar_mgr = bar_managers.get(symbol)

        # Start data fetcher
        logger.info("[IBKR] Starting data fetcher for %s", symbol)
        tasks.append(ibkr_data_fetcher(symbol, ibkr_client, bar_mgr, idx))

        # Start signal monitor
        logger.info("[IBKR] Starting signal monitor for %s", symbol)
        tasks.append(ibkr_signal_monitor(symbol, ibkr_client, bar_mgr))

    send_telegram("🚀 [IBKR] Bot Started")

    # Wait for all tasks to complete
    # The workers are designed to exit at 16:00 ET
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("[IBKR] Tasks cancelled")
    except Exception as e:
        logger.exception("[IBKR] Error in task group: %s", e)

    # Cleanup
    logger.info("[IBKR] 🏁 Trading session ended (16:00 ET reached)")
    ibkr_client.disconnect()
    logger.info("[IBKR] 👋 Disconnected from IBKR")


def stop_ibkr_workers():
    """Stop all IBKR workers"""
    global _STOP
    _STOP = True
    logger.info("[IBKR] 🛑 Stop signal sent to all workers")
