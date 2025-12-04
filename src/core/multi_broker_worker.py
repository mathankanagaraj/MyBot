# core/multi_broker_worker.py
"""
Multi-broker worker that runs Angel One and IBKR traders in parallel.
Handles timezone detection and market hours for both brokers.
"""
import asyncio
from datetime import time, timedelta

from core.config import BROKER, IBKR_SYMBOLS
from core.logger import logger
from core.utils import send_telegram
from core.ibkr_utils import is_us_market_open, get_us_et_now
from core.worker import run_all_workers as run_angel_workers


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


async def ibkr_signal_monitor(symbol, ibkr_client, bar_manager):
    """
    Monitor for trading signals on a symbol (matching Angel One's logic).

    Args:
        symbol: Symbol to monitor
        ibkr_client: IBKR API client
        bar_manager: Bar manager for this symbol
    """
    from core.signal_engine import (
        detect_15m_bias,
        detect_5m_entry,
        get_next_candle_close_time,
        get_seconds_until_next_close,
    )
    from core.ibkr_option_selector import find_ibkr_option_contract
    from core.config import RR_RATIO, MAX_5M_CHECKS

    global _STOP

    logger.info("[%s] 👀 Signal monitor started", symbol)
    last_15m_signal_time = None

    # STARTUP SIGNAL DETECTION: Check if there's a recent 15m signal we should act on
    try:
        now_et = get_us_et_now()
        if is_us_market_open():
            df5_startup, df15_startup = await bar_manager.get_resampled()
            if not df5_startup.empty and not df15_startup.empty:
                startup_bias = detect_15m_bias(df15_startup)
                if startup_bias:
                    # Calculate how old this signal is
                    latest_15m_time = df15_startup.index[-1]
                    time_since_signal = (
                        now_et.replace(tzinfo=None) - latest_15m_time
                    ).total_seconds() / 60

                    # If signal is recent (within last 30 minutes), act on it
                    if time_since_signal <= 30:
                        logger.info(
                            "[%s] 🔍 STARTUP: Found recent 15m %s signal from %d mins ago - Starting 5m entry search",
                            symbol,
                            startup_bias,
                            int(time_since_signal),
                        )
                        send_telegram(
                            f"🔍 [IBKR] [{symbol}] Startup detected recent 15m {startup_bias} signal "
                            f"({int(time_since_signal)}m ago). Searching for entry..."
                        )
                        last_15m_signal_time = now_et

                        # Jump directly into 5m entry search
                        checks = 0
                        entered = False

                        while checks < MAX_5M_CHECKS and not entered and not _STOP:
                            checks += 1
                            now_et = get_us_et_now()

                            if now_et.time() >= time(16, 0):
                                break

                            next_5m_close = get_next_candle_close_time(now_et, "5min")
                            sleep_seconds = get_seconds_until_next_close(now_et, "5min")

                            logger.info(
                                "[%s] ⏰ STARTUP 5m check #%d - waiting for %s ET (sleeping %ds)",
                                symbol,
                                checks,
                                next_5m_close.strftime("%H:%M:%S"),
                                sleep_seconds,
                            )
                            await asyncio.sleep(sleep_seconds)

                            now_et = get_us_et_now()
                            if now_et.time() >= time(16, 0):
                                break

                            df5_new, df15_new = await bar_manager.get_resampled()
                            if df5_new.empty or df15_new.empty:
                                continue

                            # Revalidate 15m bias
                            bias_now = detect_15m_bias(df15_new)
                            if bias_now != startup_bias:
                                logger.warning(
                                    "[%s] ⚠️ 15m bias changed %s → %s, aborting startup entry search",
                                    symbol,
                                    startup_bias,
                                    bias_now,
                                )
                                send_telegram(
                                    f"⚠️ [IBKR] [{symbol}] Startup: 15m bias changed {startup_bias} → {bias_now}, aborting entry search"
                                )
                                break

                            # Check 5m entry
                            entry_ok, details = detect_5m_entry(df5_new, startup_bias)
                            if entry_ok:
                                # Entry found!
                                logger.info(
                                    "[%s] ✅ STARTUP: 5m entry confirmed for %s - %s",
                                    symbol,
                                    startup_bias,
                                    details,
                                )

                                # Get stock price and find option (same as main loop)
                                stock_price = await ibkr_client.get_last_price(
                                    symbol, "STOCK"
                                )
                                if stock_price:
                                    option_info = await find_ibkr_option_contract(
                                        ibkr_client, symbol, startup_bias, stock_price
                                    )

                                    if option_info:
                                        premium = option_info.get("premium", 0)
                                        if premium > 0:
                                            stop_loss = premium * 0.8
                                            target = premium * (1 + (0.2 * RR_RATIO))

                                            logger.info(
                                                f"[IBKR] [{symbol}] 📈 STARTUP Entry: ${premium:.2f}, "
                                                f"SL: ${stop_loss:.2f}, Target: ${target:.2f}"
                                            )

                                            send_telegram(
                                                f"🎯 [IBKR] {symbol} {startup_bias} (Startup)\n"
                                                f"Entry: ${premium:.2f}\n"
                                                f"SL: ${stop_loss:.2f}\n"
                                                f"Target: ${target:.2f}"
                                            )
                                entered = True
                    else:
                        logger.debug(
                            "[%s] Recent 15m signal is %d mins old (too old)",
                            symbol,
                            int(time_since_signal),
                        )
    except Exception as e:
        logger.exception("[%s] Error in startup signal detection: %s", symbol, e)

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
            last_15m_signal_time = now_et

            checks = 0
            entered = False

            # Look for 5m entry confirmation at 5m candle closes
            logger.info(
                "[%s] 🔎 Monitoring 5m entries (max %d checks)...",
                symbol,
                MAX_5M_CHECKS,
            )
            while checks < MAX_5M_CHECKS and not entered and not _STOP:
                checks += 1

                # Wait for next 5m candle close
                now_et = get_us_et_now()

                # Strict Market Close Check inside inner loop
                if now_et.time() >= time(16, 0):
                    logger.info(
                        "[%s] 🛑 Market closed (16:00 reached), stopping entry search",
                        symbol,
                    )
                    break

                next_5m_close = get_next_candle_close_time(now_et, "5min")
                sleep_seconds = get_seconds_until_next_close(now_et, "5min")

                logger.info(
                    "[%s] ⏰ 5m check #%d - waiting for %s ET (sleeping %ds)",
                    symbol,
                    checks,
                    next_5m_close.strftime("%H:%M:%S"),
                    sleep_seconds,
                )
                await asyncio.sleep(sleep_seconds)

                # Get fresh data at 5m boundary
                now_et = get_us_et_now()

                # Double check market close after waking up
                if now_et.time() >= time(16, 0):
                    logger.info(
                        "[%s] 🛑 Market closed (16:00 reached), stopping entry search",
                        symbol,
                    )
                    break

                df5_new, df15_new = await bar_manager.get_resampled()
                if df5_new.empty or df15_new.empty:
                    logger.debug(
                        "[%s] ⚠️ Empty dataframe at 5m check #%d", symbol, checks
                    )
                    continue

                # Revalidate 15m bias hasn't flipped
                bias_now = detect_15m_bias(df15_new)

                if bias_now != bias:
                    logger.warning(
                        "[%s] ⚠️ 15m bias changed %s → %s, aborting entry search",
                        symbol,
                        bias,
                        bias_now,
                    )
                    send_telegram(
                        f"⚠️ [IBKR] {symbol} 15m bias changed {bias} → {bias_now}, aborting"
                    )
                    break

                # Check 5m entry conditions at candle close
                logger.info(
                    "[%s] 🔎 Checking 5m entry conditions for %s bias...", symbol, bias
                )
                entry_ok, details = detect_5m_entry(df5_new, bias)

                if not entry_ok:
                    continue  # No entry yet

                # Entry signal confirmed!
                logger.info(
                    f"[{symbol}] ✅ 5m ENTRY SIGNAL CONFIRMED: {bias} - {details}"
                )

                # Get underlying price
                stock_price = await ibkr_client.get_last_price(symbol, "STOCK")

                if not stock_price:
                    logger.error("[%s] ❌ Failed to get stock price", symbol)
                    continue

                # Find option
                option_info = await find_ibkr_option_contract(
                    ibkr_client, symbol, bias, stock_price
                )

                if not option_info:
                    logger.error("[%s] ❌ No suitable option found", symbol)
                    continue

                premium = option_info.get("premium", 0)

                if premium <= 0:
                    logger.error("[%s] ❌ Invalid premium: $%.2f", symbol, premium)
                    continue

                # Calculate bracket levels (1:2 RR)
                stop_loss = premium * 0.8  # 20% SL
                target = premium * (1 + (0.2 * RR_RATIO))  # 40% target for 1:2 RR

                logger.info(
                    f"[IBKR] [{symbol}] 📈 Entry: ${premium:.2f}, "
                    f"SL: ${stop_loss:.2f}, Target: ${target:.2f}"
                )

                send_telegram(
                    f"🎯 [IBKR] {symbol} {bias}\n"
                    f"Entry: ${premium:.2f}\n"
                    f"SL: ${stop_loss:.2f}\n"
                    f"Target: ${target:.2f}"
                )

                entered = True
                # Note: Actual order placement logic would go here

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
    from core.config import IBKR_MODE

    global _STOP

    # Check if we're within 15 minutes of market open
    now_et = get_us_et_now()
    market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    time_until_open = (market_open - now_et).total_seconds() / 60

    if time_until_open > 15:
        sleep_minutes = time_until_open - 15
        sleep_hours = sleep_minutes / 60
        logger.info(
            f"[IBKR] ⏰ Too early. Sleeping {sleep_hours:.1f} hours "
            f"until {(now_et + timedelta(minutes=sleep_minutes)).strftime('%H:%M')} ET "
            f"(15 min before market)"
        )
        await asyncio.sleep(sleep_minutes * 60)

    logger.info("[IBKR] 🌅 Ready to connect (within 15 min of market open)")

    # Initialize IBKR client
    ibkr_client = IBKRClient()

    # Initialize BarManagers
    bar_managers = {}
    for symbol in IBKR_SYMBOLS:
        bar_managers[symbol] = BarManager(symbol, max_bars=2880)

    send_telegram(f"🚀 [IBKR] Bot Started ({IBKR_MODE} Trading)")

    # Start heartbeat task (same as Angel worker)
    from core.worker import heartbeat_task

    asyncio.create_task(heartbeat_task())

    # Outer loop for connection management
    while not _STOP:
        try:
            # Check for Market Close / Sleep Time FIRST
            # This ensures we sleep even if disconnected (e.g. Gateway restart at close)
            now_et = get_us_et_now()
            if now_et.time() >= time(16, 0):
                logger.info("[IBKR] 🌙 Market closed. Calculating sleep time...")

                next_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)

                # If it's already past 9:30 AM today, next open is tomorrow
                if now_et >= next_open:
                    next_open += timedelta(days=1)

                # Handle weekends (if today is Friday, next open is Monday)
                while next_open.weekday() > 4:  # If Sat or Sun
                    next_open += timedelta(days=1)

                sleep_seconds = (next_open - now_et).total_seconds()
                sleep_hours = sleep_seconds / 3600

                logger.info(
                    f"[IBKR] 💤 Sleeping {sleep_hours:.1f} hours until next market open: "
                    f"{next_open.strftime('%Y-%m-%d %H:%M:%S')} ET"
                )
                send_telegram(
                    f"🌙 [IBKR] Market Closed. Sleeping {sleep_hours:.1f}h until {next_open.strftime('%H:%M')} ET"
                )

                # Sleep until 15 mins before open to allow for connection/warmup
                warmup_seconds = 15 * 60
                if sleep_seconds > warmup_seconds:
                    await asyncio.sleep(sleep_seconds - warmup_seconds)
                else:
                    await asyncio.sleep(60)

                logger.info("[IBKR] 🌅 Waking up for new trading day!")
                continue

            # Connect to IB
            if not ibkr_client.connected:
                logger.info("[IBKR] 🔄 Connecting to IB Gateway...")
                await ibkr_client.connect_async()

                if not ibkr_client.connected:
                    logger.error("[IBKR] ❌ Failed to connect. Retrying in 60s...")
                    await asyncio.sleep(60)
                    continue

                send_telegram(f"✅ [IBKR] Connected ({IBKR_MODE})")

                # Load historical data after successful connection
                for symbol in IBKR_SYMBOLS:
                    try:
                        bar_mgr = bar_managers[symbol]
                        logger.info(f"[IBKR] [{symbol}] Loading historical data...")
                        df_hist = await ibkr_client.req_historic_1m(
                            symbol, duration_days=2
                        )

                        if df_hist is not None and not df_hist.empty:
                            await bar_mgr.initialize_from_historical(df_hist)
                            logger.info(
                                f"[IBKR] [{symbol}] Loaded {len(df_hist)} historical bars"
                            )
                        else:
                            logger.warning(
                                f"[IBKR] [{symbol}] Failed to load historical data"
                            )
                    except Exception as e:
                        logger.error(f"[IBKR] [{symbol}] Error loading history: {e}")

            # Start per-symbol tasks (data fetchers + signal monitors)
            tasks = []
            for i, symbol in enumerate(IBKR_SYMBOLS):
                bar_mgr = bar_managers[symbol]

                # Data fetcher task
                logger.info(f"[IBKR] Starting data fetcher for {symbol}")
                fetcher_task = asyncio.create_task(
                    ibkr_data_fetcher(symbol, ibkr_client, bar_mgr, i)
                )
                tasks.append(fetcher_task)

                # Signal monitor task
                logger.info(f"[IBKR] Starting signal monitor for {symbol}")
                monitor_task = asyncio.create_task(
                    ibkr_signal_monitor(symbol, ibkr_client, bar_mgr)
                )
                tasks.append(monitor_task)

            # Wait for all tasks to complete (or until stop)
            try:
                await asyncio.gather(*tasks)
            except Exception as e:
                logger.error(f"[IBKR] Task error: {e}")

            # If we get here, check if it's due to connection loss
            if not ibkr_client.connected or not ibkr_client.ib.isConnected():
                logger.warning("[IBKR] ⚠️ Connection lost! Attempting reconnect...")
                send_telegram("⚠️ [IBKR] Connection lost! Reconnecting...")
                ibkr_client.connected = False
                continue

            # Market closed - Sleep until next market open
            # Loop continues to reconnect and start fresh

        except Exception as e:
            logger.exception(f"[IBKR] ❌ Fatal error in outer loop: {e}")
            await asyncio.sleep(60)

    # Cleanup
    ibkr_client.disconnect()
    logger.info("[IBKR] 🛑 Worker exiting")


async def run_multi_broker():
    """
    Run the broker worker based on BROKER configuration.
    With separate containers, each container runs ONE broker:
    - angel_bot container: Runs Angel One worker
    - ibkr_bot container: Runs IBKR worker
    """
    global _STOP

    logger.info(f"🚀 Starting broker worker: {BROKER}")

    # With separate containers, run only the configured broker
    if BROKER == "ANGEL":
        logger.info("[ANGEL] 🇮🇳 Starting Angel One worker...")
        try:
            await run_angel_workers()
        except asyncio.CancelledError:
            logger.info("Angel worker cancelled")
        except Exception as e:
            logger.exception(f"Error in Angel worker: {e}")
            send_telegram(f"🚨 Angel worker error: {str(e)[:100]}")
        finally:
            logger.info("👋 Angel worker shutdown complete")

    elif BROKER == "IBKR":
        logger.info("[IBKR] 🇺🇸 Starting IBKR worker...")
        try:
            await run_ibkr_workers()
        except asyncio.CancelledError:
            logger.info("IBKR worker cancelled")
        except Exception as e:
            logger.exception(f"Error in IBKR worker: {e}")
            send_telegram(f"🚨 IBKR worker error: {str(e)[:100]}")
        finally:
            logger.info("👋 IBKR worker shutdown complete")

    else:
        error_msg = f"❌ Invalid BROKER configuration: {BROKER}. Must be ANGEL or IBKR"
        logger.error(error_msg)
        send_telegram(error_msg)


def stop_all_workers():
    """Stop all broker workers"""
    global _STOP
    _STOP = True

    # Also stop Angel workers if applicable
    from core.worker import stop_all_workers as stop_angel_workers

    stop_angel_workers()
