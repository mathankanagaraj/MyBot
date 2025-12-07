# core/worker.py
import asyncio
from datetime import datetime, timedelta, time
import pytz

from core.angelone.client import AngelClient
from core.bar_manager import BarManager
from core.cash_manager import create_cash_manager
from core.config import (
    ALLOC_PCT,
    MAX_5M_CHECKS,
    MAX_CONTRACTS_PER_TRADE,
    MAX_DAILY_LOSS_PCT,
    MAX_POSITION_PCT,
    MIN_PREMIUM,
    MONITOR_INTERVAL,
    RISK_PCT_OF_PREMIUM,
    RISK_PER_CONTRACT,
    RR_RATIO,
    SYMBOLS,
)
from core.logger import logger
from core.angelone.option_selector import find_option_contract_async
from core.signal_engine import (
    detect_5m_entry,
    detect_15m_bias,
    get_next_candle_close_time,
    get_seconds_until_next_close,
)
from core.utils import (
    init_audit_file,
    send_telegram,
    write_audit_row,
)
from core.angelone.utils import (
    is_market_open,
    get_ist_now,
)

_STOP = False
_LAST_MARKET_OPEN_STATE = None


def compute_stop_target(entry_price):
    """Calculate stop loss and target prices based on risk parameters"""
    if RISK_PER_CONTRACT and float(RISK_PER_CONTRACT) > 0:
        risk = float(RISK_PER_CONTRACT)
    else:
        risk = float(RISK_PCT_OF_PREMIUM) * float(entry_price)

    stop = max(1.0, float(entry_price) - risk)  # Minimum ₹1 for Indian market
    target = float(entry_price) + RR_RATIO * risk
    return stop, target, risk


async def heartbeat_task():
    """
    Continuous heartbeat to show the bot is alive.
    Runs 24/7, even when markets are closed.
    Uses UTC for global monitoring.
    """
    logger.info("💓 Heartbeat task started")
    while not _STOP:
        try:
            # Use UTC for global heartbeat
            now_utc = datetime.utcnow()
            logger.info(f"💓 Heartbeat: {now_utc.strftime('%H:%M:%S')} UTC")
            await asyncio.sleep(60)  # Every minute
        except Exception as e:
            logger.exception(f"Heartbeat error: {e}")
            await asyncio.sleep(60)
    logger.info("💓 Heartbeat task stopped")


async def execute_angel_entry_order(
    symbol, bias, angel_client, cash_mgr, underlying_price
):
    """
    Execute entry order with option selection and bracket order placement for Angel One.

    Args:
        symbol: Stock symbol
        bias: BULL or BEAR
        angel_client: Angel One API client
        cash_mgr: Cash manager instance
        underlying_price: Current underlying price

    Returns:
        True if order placed successfully, False otherwise
    """
    from core.config import INDEX_FUTURES

    # Select option contract
    logger.info("[%s] 🔍 Selecting option contract...", symbol)
    opt_contract, reason = await find_option_contract_async(
        angel_client, symbol, bias, underlying_price
    )
    if not opt_contract:
        logger.error("[%s] ❌ Option selection failed: %s", symbol, reason)
        send_telegram(f"❌ {symbol} option selection failed: {reason}")
        return False

    logger.info("[%s] ✅ Selected option: %s", symbol, opt_contract["symbol"])

    # Get option premium
    logger.info("[%s] 💰 Fetching option premium...", symbol)
    prem = await angel_client.get_last_price(opt_contract["symbol"], exchange="NFO")
    if prem is None or prem < MIN_PREMIUM:
        logger.error(
            "[%s] ❌ Premium too low: ₹%s (min: ₹%.2f)", symbol, prem, MIN_PREMIUM
        )
        send_telegram(f"❌ {symbol} premium too low: ₹{prem}")
        return False

    logger.info("[%s] 💰 Option premium: ₹%.2f", symbol, prem)

    # Calculate position size
    lot_size = opt_contract.get("lot_size", 1)
    per_lot_cost = float(prem) * float(lot_size)
    qty = MAX_CONTRACTS_PER_TRADE
    est_cost = per_lot_cost * qty

    logger.info(
        "[%s] 📊 Position sizing: %d lots × %d qty × ₹%.2f = ₹%.2f",
        symbol,
        qty,
        lot_size,
        prem,
        est_cost,
    )

    # Check if we can open position
    can_open = await cash_mgr.can_open_position(symbol, est_cost)
    if not can_open:
        logger.error("[%s] ❌ Insufficient funds or risk limit reached", symbol)
        send_telegram(f"❌ {symbol} insufficient funds or risk limit reached")
        return False

    # Register position
    cash_mgr.register_open(symbol, est_cost)

    # Calculate stop loss and target
    risk_amt = prem * RISK_PCT_OF_PREMIUM
    stop_price = prem - risk_amt
    target_price = prem + (risk_amt * RR_RATIO)

    if stop_price < 1.0:
        stop_price = 1.0

    # Place bracket order
    logger.info(
        f"[{symbol}] 📤 Placing bracket order: {bias} "
        f"Entry=₹{prem:.2f}, SL=₹{stop_price:.2f}, TP=₹{target_price:.2f}"
    )

    bracket = await angel_client.place_bracket_order(
        option_symbol=opt_contract["symbol"],
        option_token=opt_contract["token"],
        quantity=qty * lot_size,
        stop_loss_price=stop_price,
        target_price=target_price,
        exchange="NFO",
    )

    if bracket is None:
        logger.error("[%s] ❌ Order placement failed", symbol)
        send_telegram(f"❌ {symbol} order placement failed")
        cash_mgr.force_release(symbol)
        return False

    logger.info("[%s] ✅ Order placed successfully!", symbol)
    send_telegram(
        f"✅ Entered {symbol} {bias}\n"
        f"Option: {opt_contract['symbol']}\n"
        f"Entry: ₹{prem:.2f} | SL: ₹{stop_price:.2f} | TP: ₹{target_price:.2f}"
    )

    # Write audit
    write_audit_row(
        timestamp=get_ist_now().isoformat(),
        symbol=symbol,
        bias=bias,
        option=opt_contract["symbol"],
        entry_price=prem,
        stop=stop_price,
        target=target_price,
        exit_price=0,
        outcome="OPEN",
        holding_seconds=0,
        details="Entry confirmed",
    )

    return True


async def search_angel_5m_entry(
    symbol, bias, angel_client, cash_mgr, bar_manager, context="ENTRY"
):
    """
    Search for 5m entry confirmation over multiple candles for Angel One.

    Args:
        symbol: Stock symbol
        bias: BULL or BEAR from 15m detection
        angel_client: Angel One API client
        cash_mgr: Cash manager instance
        bar_manager: Bar manager for this symbol
        context: "STARTUP" or "ENTRY" for logging

    Returns:
        True if entry executed, False otherwise
    """
    from core.config import INDEX_FUTURES

    global _STOP
    checks = 0

    while checks < MAX_5M_CHECKS and not _STOP:
        checks += 1
        now_ist = get_ist_now()

        # Market close check
        if now_ist.time() >= time(15, 30):
            logger.info(
                "[%s] 🛑 Market closed, stopping %s entry search", symbol, context
            )
            return False

        # Wait for next 5m candle close
        next_5m_close = get_next_candle_close_time(now_ist, "5min")
        sleep_seconds = get_seconds_until_next_close(now_ist, "5min")

        logger.info(
            "[%s] ⏰ %s 5m check #%d - waiting for %s IST (sleeping %ds)",
            symbol,
            context,
            checks,
            next_5m_close.strftime("%H:%M:%S"),
            sleep_seconds,
        )
        await asyncio.sleep(sleep_seconds)

        now_ist = get_ist_now()
        if now_ist.time() >= time(15, 30):
            return False

        # Get fresh data
        now_utc = now_ist.astimezone(pytz.UTC).replace(tzinfo=None)
        df5_new, df15_new = await bar_manager.get_resampled(current_time=now_utc)
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
                f"⚠️ [{symbol}] {context}: 15m bias changed {bias} → {bias_now}, aborting"
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

        # Get underlying price
        if symbol in INDEX_FUTURES:
            logger.info("[%s] 📊 Fetching futures price for index...", symbol)
            underlying = await angel_client.get_futures_price(symbol)
        else:
            logger.info("[%s] 📊 Fetching stock price...", symbol)
            underlying = await angel_client.get_last_price(symbol, exchange="NSE")

        if not underlying:
            logger.error("[%s] ❌ Failed to get underlying price", symbol)
            send_telegram(f"❌ {symbol} failed to get underlying price")
            return False

        logger.info("[%s] 💰 Underlying price: ₹%.2f", symbol, underlying)

        # Execute order
        success = await execute_angel_entry_order(
            symbol, bias, angel_client, cash_mgr, underlying
        )
        return success

    logger.info("[%s] ⛔ No %s entry after %d checks", symbol, context, checks)
    return False


async def handle_angel_startup_signal(symbol, angel_client, cash_mgr, bar_manager):
    """
    Check for recent 15m signal on startup and search for entry if found.

    Args:
        symbol: Stock symbol
        angel_client: Angel One API client
        cash_mgr: Cash manager instance
        bar_manager: Bar manager for this symbol
    """
    try:
        now_ist = get_ist_now()
        if not is_market_open():
            return

        # Get current data
        df5_startup, df15_startup = await bar_manager.get_resampled()
        if df5_startup.empty or df15_startup.empty:
            return

        # Detect 15m bias
        startup_bias = detect_15m_bias(df15_startup)
        if not startup_bias:
            return

        # Calculate how old this signal is
        latest_15m_time = df15_startup.index[-1]
        time_since_signal = (
            now_ist.replace(tzinfo=None) - latest_15m_time
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
                f"🔍 [{symbol}] Startup detected recent 15m {startup_bias} signal "
                f"({int(time_since_signal)}m ago). Searching for entry..."
            )

            # Search for 5m entry
            await search_angel_5m_entry(
                symbol, startup_bias, angel_client, cash_mgr, bar_manager, "STARTUP"
            )
        else:
            logger.debug(
                "[%s] Recent 15m signal is %d mins old (too old)",
                symbol,
                int(time_since_signal),
            )
    except Exception as e:
        logger.exception("[%s] Error in startup signal detection: %s", symbol, e)


async def worker_loop(symbol, angel_client, cash_mgr, bar_manager):
    """
    Main worker loop for each symbol.
    Monitors market, detects signals, and executes trades.
    """
    global _STOP

    logger.info("[%s] 🚀 Worker started", symbol)

    # STARTUP: Check for recent 15m signal and search for entry
    await handle_angel_startup_signal(symbol, angel_client, cash_mgr, bar_manager)

    # MAIN LOOP: Monitor for new 15m signals
    while not _STOP:
        try:
            now_ist = get_ist_now()

            # Strict Market Close Check
            if now_ist.time() >= time(15, 30):
                logger.info(
                    "[%s] 🛑 Market closed (15:30 reached), stopping worker", symbol
                )
                break

            # Market hours guard
            is_open = is_market_open()
            await notify_market_state(is_open)

            from core.config import MARKET_HOURS_ONLY

            if MARKET_HOURS_ONLY and not is_open:
                logger.debug("[%s] 💤 Market closed, sleeping 1 minute...", symbol)
                await asyncio.sleep(60)
                continue

            # Check if we already have an open position for this symbol
            has_position = symbol in cash_mgr.open_positions

            # Also check API directly (to handle restarts or out-of-sync state)
            if not has_position:
                try:
                    positions = await angel_client.get_positions()
                    for p in positions:
                        if (
                            symbol in p.get("tradingsymbol", "")
                            and int(p.get("netqty", 0)) != 0
                        ):
                            has_position = True
                            logger.warning(
                                "[%s] ⚠️ Found existing position in API not in cache. Syncing...",
                                symbol,
                            )
                            # Register in cash manager
                            cash_mgr.open_positions[symbol] = {
                                "symbol": p.get("tradingsymbol"),
                                "quantity": int(p.get("netqty", 0)),
                                "entry_price": float(p.get("avgnetprice", 0)),
                                "entry_time": datetime.now(),
                            }
                            break
                except Exception as e:
                    logger.error("[%s] Error checking positions: %s", symbol, e)

            if has_position:
                # Poll for position closure
                positions = await angel_client.get_positions()
                has_pos = False

                for p in positions:
                    if (
                        symbol in p.get("tradingsymbol", "")
                        and int(p.get("netqty", 0)) != 0
                    ):
                        has_pos = True
                        break

                if not has_pos:
                    logger.info("[%s] ✅ Position closed", symbol)
                    cash_mgr.force_release(symbol)
                    send_telegram(f"✅ {symbol} position closed")
                else:
                    logger.info(
                        "[%s] ⚠️ Position still open, skipping signal check...", symbol
                    )

                await asyncio.sleep(MONITOR_INTERVAL)
                continue

            # Wait until next 15m candle close before checking bias
            next_15m_close = get_next_candle_close_time(now_ist, "15min")
            sleep_seconds = get_seconds_until_next_close(now_ist, "15min")

            logger.info(
                "[%s] ⏰ Waiting for 15m close at %s IST (sleeping %ds)",
                symbol,
                next_15m_close.strftime("%H:%M:%S"),
                sleep_seconds,
            )
            await asyncio.sleep(sleep_seconds)

            # Get latest bars at 15m boundary
            now_ist = get_ist_now()

            # Double check market close after waking up
            if now_ist.time() >= time(15, 30):
                logger.info(
                    "[%s] 🛑 Market closed (15:30 reached), stopping worker", symbol
                )
                break

            # Convert IST to UTC for bar_manager
            now_utc = now_ist.astimezone(pytz.UTC).replace(tzinfo=None)
            df5, df15 = await bar_manager.get_resampled(current_time=now_utc)

            if df15.empty:
                logger.warning("[%s] ⚠️ No 15m data available, waiting...", symbol)
                await asyncio.sleep(60)
                continue

            # Detect 15m bias at candle close
            logger.info(
                "[%s] 🔍 Checking 15m bias at %s IST (bars: 5m=%d, 15m=%d)...",
                symbol,
                now_ist.strftime("%H:%M:%S"),
                len(df5),
                len(df15),
            )
            bias = detect_15m_bias(df15)

            if not bias:
                continue

            logger.info(
                "[%s] ✅ 15m bias detected: %s at %s IST",
                symbol,
                bias,
                now_ist.strftime("%H:%M:%S"),
            )

            # Notify 15m bias found
            logger.info(
                "[%s] 🎯 NEW 15m signal: %s at %s IST - Starting 5m entry search...",
                symbol,
                bias,
                now_ist.strftime("%H:%M:%S"),
            )
            send_telegram(
                f"📊 [{symbol}] 15m Trend: {bias} at {now_ist.strftime('%H:%M')} IST. Looking for 5m entry..."
            )

            # Search for 5m entry confirmation
            await search_angel_5m_entry(
                symbol, bias, angel_client, cash_mgr, bar_manager, "ENTRY"
            )

        except Exception as e:
            logger.exception("[%s] ❌ Worker exception: %s", symbol, e)
            send_telegram(f"⚠️ Error in {symbol} worker: {str(e)[:100]}")
            await asyncio.sleep(2)

    logger.info("[%s] 🛑 Worker exiting", symbol)


async def pre_market_check(cash_mgr):
    """
    Perform pre-market balance check and notification.
    Called once when bot starts or when market opens.
    """
    logger.info("🔍 Performing pre-market balance check...")
    await cash_mgr.check_and_log_start_balance()


async def end_of_day_report(cash_mgr, angel_client):
    """
    Generate and send end-of-day trading report.
    Includes balance, P&L, trade count, and position status.
    """
    logger.info("📊 Generating end-of-day report...")

    try:
        # Get daily statistics
        stats = await cash_mgr.get_daily_statistics()

        # Get open positions from Angel API
        positions = await angel_client.get_positions()
        open_positions = [p for p in positions if p.get("netqty", "0") != "0"]

        # Calculate P&L percentage
        start_bal = stats["start_balance"]
        pnl = stats["daily_pnl"]
        pnl_pct = (pnl / start_bal * 100) if start_bal > 0 else 0.0

        # Build report message
        msg = (
            f"📊 **End of Day Report**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Start Balance: ₹{start_bal:,.2f}\n"
            f"💰 End Balance: ₹{stats['current_balance']:,.2f}\n"
            f"📈 Daily P&L: ₹{pnl:,.2f} ({pnl_pct:+.2f}%)\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Total Trades: {stats['total_trades']}\n"
            f"📂 Open Positions: {len(open_positions)}\n"
        )

        # Add open position details if any
        if open_positions:
            msg += "\n🔓 Open Positions:\n"
            for pos in open_positions:
                symbol = pos.get("tradingsymbol", "Unknown")
                qty = pos.get("netqty", "0")
                pnl_pos = float(pos.get("pnl", 0))
                msg += f"  • {symbol}: Qty {qty} | P&L ₹{pnl_pos:,.2f}\n"
        else:
            msg += "\n✅ All positions closed\n"

        msg += "━━━━━━━━━━━━━━━━━━━━"

        logger.info(msg.replace("**", "").replace("━", "-"))
        send_telegram(msg)

    except Exception as e:
        logger.exception("Error generating end-of-day report: %s", e)
        send_telegram(f"⚠️ Error generating end-of-day report: {str(e)[:100]}")


async def schedule_end_of_day_report(cash_mgr, angel_client):
    """
    Background task that schedules end-of-day report at market close.
    Runs continuously and triggers report at 3:30 PM IST each trading day.
    """
    from core.angelone.utils import get_seconds_until_market_close

    logger.info("📅 End-of-day report scheduler started")

    while not _STOP:
        try:
            # Calculate wait time until market close
            wait_seconds = get_seconds_until_market_close()

            logger.info(
                f"⏰ End-of-day report scheduled in {wait_seconds/3600:.1f} hours"
            )

            # Wait until market close
            await asyncio.sleep(wait_seconds)

            # Generate report
            if not _STOP:
                await end_of_day_report(cash_mgr, angel_client)

            # Wait a bit before scheduling next report (avoid duplicate reports)
            await asyncio.sleep(300)  # 5 minutes

        except Exception as e:
            logger.exception("Error in end-of-day scheduler: %s", e)
            await asyncio.sleep(60)

    logger.info("📅 End-of-day report scheduler exiting")


async def run_angel_workers():
    """Initialize and run all Angel One worker tasks in a daily loop"""
    global _STOP

    logger.info("🤖 Bot process started")
    init_audit_file()

    # Start heartbeat task immediately and continuously
    # This ensures we have logs even when the bot is sleeping overnight
    heartbeat = asyncio.create_task(heartbeat_task())

    while not _STOP:
        try:
            now_ist = get_ist_now()
            current_time = now_ist.time()

            # Define active window: 09:00 to 15:30
            # We start at 09:00 to allow 15 mins for pre-market checks and data loading
            start_time = time(9, 0)
            end_time = time(15, 30)

            # Check if we are in the active window (Mon-Fri, 09:00-15:30)
            is_weekday = now_ist.weekday() <= 4  # 0=Mon, 4=Fri
            is_active_window = is_weekday and (start_time <= current_time < end_time)

            if not is_active_window:
                # Calculate wait time until next start (09:00 AM)
                if current_time >= end_time or not is_weekday:
                    # Wait until tomorrow 09:00 (start point)
                    next_start = datetime.combine(
                        now_ist.date() + timedelta(days=1), start_time
                    )
                else:
                    # Wait until today 09:00 (if started before market open)
                    next_start = datetime.combine(now_ist.date(), start_time)

                # Skip weekends
                while next_start.weekday() > 4:  # If Sat(5) or Sun(6)
                    next_start += timedelta(days=1)

                # Make next_start timezone aware
                tz = pytz.timezone("Asia/Kolkata")
                if next_start.tzinfo is None:
                    next_start = tz.localize(next_start)

                wait_seconds = (next_start - now_ist).total_seconds()
                wait_hours = wait_seconds / 3600

                logger.info(
                    f"[ANGEL] 💤 Market closed. Sleeping {wait_hours:.1f} hours until "
                    f"{next_start.strftime('%Y-%m-%d %H:%M')} IST (09:00 market open)"
                )

                # Sleep in chunks to allow for graceful shutdown
                while wait_seconds > 0 and not _STOP:
                    sleep_chunk = min(wait_seconds, 60)
                    await asyncio.sleep(sleep_chunk)
                    wait_seconds -= sleep_chunk

                if _STOP:
                    break

            # 🌅 Start Daily Cycle
            logger.info("🌅 Starting daily trading cycle...")
            send_telegram("🌅 Bot waking up for trading day...")

            # Initialize Angel Broker client
            angel_client = AngelClient()

            # Connect to Angel Broker
            await angel_client.connect_async()

            if not angel_client.connected:
                logger.error(
                    "❌ Failed to connect to Angel Broker. Retrying in 1 minute..."
                )
                await asyncio.sleep(60)
                continue

            # Create cash manager
            cash_mgr = create_cash_manager(
                angel_client=angel_client,
                max_alloc_pct=ALLOC_PCT,
                max_daily_loss_pct=MAX_DAILY_LOSS_PCT,
                max_position_pct=MAX_POSITION_PCT,
            )

            # Initialize BarManagers for each symbol
            bar_managers = {}

            logger.info("Initializing BarManagers and loading historical data...")

            for symbol in SYMBOLS:
                # Create BarManager
                bar_mgr = BarManager(symbol, max_bars=2880)  # 2 days of 1m bars
                bar_managers[symbol] = bar_mgr

                # Load initial historical data
                logger.info("[%s] Loading historical data...", symbol)
                df_hist = await angel_client.req_historic_1m(symbol, duration_days=2)

                if df_hist is not None and not df_hist.empty:
                    await bar_mgr.initialize_from_historical(df_hist)
                    logger.info("[%s] Loaded %d historical bars", symbol, len(df_hist))
                else:
                    logger.warning("[%s] Failed to load historical data", symbol)

            # Perform pre-market balance check
            logger.info("🔍 Checking account balance...")
            await pre_market_check(cash_mgr)

            # Start worker tasks
            tasks = []

            # Initialize WebSocket
            from core.angelone.client import AngelWebSocket

            logger.info("🚀 Starting Angel WebSocket...")
            ws_client = AngelWebSocket(
                auth_token=angel_client.auth_token,
                api_key=angel_client.api_key,
                client_code=angel_client.client_code,
                feed_token=angel_client.feed_token,
                bar_managers=bar_managers,
                loop=asyncio.get_running_loop(),
            )

            # Add symbols to WebSocket
            for symbol in SYMBOLS:
                token = angel_client.get_symbol_token(symbol, "NSE")
                if token:
                    ws_client.add_symbol(symbol, token, "NSE")
                else:
                    logger.error(f"Could not find token for {symbol} to subscribe")

            # Start WebSocket in a separate thread (it's blocking)
            import threading

            ws_thread = threading.Thread(target=ws_client.connect, daemon=True)
            ws_thread.start()

            # Start worker loop for each symbol
            logger.info("🚀 Starting worker loops...")
            for symbol in SYMBOLS:
                bar_mgr = bar_managers.get(symbol)
                tasks.append(worker_loop(symbol, angel_client, cash_mgr, bar_mgr))
                logger.info("[%s] 🔄 Worker thread started", symbol)

            send_telegram("🚀 Angel Broker Bot Started (LIVE TRADING)")

            # Wait for all tasks to complete
            # The workers and data fetchers are designed to exit at 15:30
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                logger.info("Tasks cancelled")
            except Exception as e:
                logger.exception("Error in daily task group: %s", e)

            # 🏁 End of Day Cleanup
            logger.info("🏁 Daily trading session ended (15:30 reached)")

            # Finalize any pending bars (crucial for capturing the 15:29 minute)
            logger.info("💾 Finalizing last minute bars...")
            for symbol, bar_mgr in bar_managers.items():
                await bar_mgr.finalize_bar()

            # Generate End of Day Report
            await end_of_day_report(cash_mgr, angel_client)

            # Disconnect
            angel_client.disconnect()
            logger.info("👋 Disconnected from Angel Broker. Waiting for next day...")

        except Exception as e:
            logger.exception("CRITICAL: Error in main daily loop: %s", e)
            send_telegram(f"🚨 CRITICAL: Bot daily loop error: {str(e)[:100]}")
            await asyncio.sleep(60)  # Prevent tight loop on error

    # Wait for heartbeat to finish
    if not heartbeat.done():
        await heartbeat


def stop_angel_workers():
    """Stop all Angel One worker tasks"""
    global _STOP
    _STOP = True


async def notify_market_state(is_open: bool):
    """Notify user when market opens/closes"""
    global _LAST_MARKET_OPEN_STATE

    first_run = _LAST_MARKET_OPEN_STATE is None
    if first_run:
        _LAST_MARKET_OPEN_STATE = is_open

    if first_run:
        if not is_open:
            logger.warning("🔔 BOT started outside NSE market hours")
            send_telegram("🔔 BOT started outside NSE market hours")
        else:
            logger.info("🔔 BOT started during NSE market hours")
            send_telegram("🔔 BOT started during NSE market hours")
        return

    if is_open != _LAST_MARKET_OPEN_STATE:
        if is_open:
            send_telegram("🔔 NSE Market is OPEN")
        else:
            send_telegram("🛑 NSE Market is CLOSED")
        _LAST_MARKET_OPEN_STATE = is_open
