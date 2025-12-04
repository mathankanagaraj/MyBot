# core/worker.py
import asyncio
from datetime import datetime, timedelta, time
import pytz

from core.angel_client import AngelClient
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
from core.option_selector import find_option_contract_async
from core.signal_engine import (
    detect_5m_entry,
    detect_15m_bias,
    get_next_candle_close_time,
    get_seconds_until_next_close,
)
from core.utils import (
    init_audit_file,
    is_market_open,
    send_telegram,
    write_audit_row,
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
    Independent heartbeat task to confirm event loop liveness.
    Logs every minute.
    """
    logger.info("💓 Heartbeat task started")
    while not _STOP:
        try:
            now_ist = get_ist_now()
            logger.info(f"💓 Heartbeat: {now_ist.strftime('%H:%M:%S')} IST")
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Heartbeat error: {e}")
            await asyncio.sleep(60)
    logger.info("💓 Heartbeat task stopped")


async def worker_loop(symbol, angel_client, cash_mgr, bar_manager):
    """
    Main worker loop for each symbol.
    Monitors market, detects signals, and executes trades.
    Ensures 15m boundary checks happen at proper times regardless of bot start time.
    """
    logger.info("[%s] 🚀 Worker started", symbol)
    last_15m_signal_time = None

    while not _STOP:
        try:
            now_ist = get_ist_now()

            # Strict Market Close Check
            # If it's past 15:30, stop immediately
            if now_ist.time() >= time(15, 30):
                logger.info("[%s] 🛑 Market closed (15:30 reached), stopping worker", symbol)
                break

            # Market hours guard
            is_open = is_market_open()
            await notify_market_state(is_open)

            from core.config import MARKET_HOURS_ONLY

            if MARKET_HOURS_ONLY and not is_open:
                # Calculate sleep until next 9:15 AM or just sleep 1 minute
                logger.debug("[%s] 💤 Market closed, sleeping 1 minute...", symbol)
                await asyncio.sleep(60)
                continue

            # Check if we already have an open position for this symbol
            if symbol in cash_mgr.open_positions:
                # Poll for position closure via Angel API
                positions = await angel_client.get_positions()
                has_pos = False

                for p in positions:
                    if symbol in p.get("tradingsymbol", ""):
                        has_pos = True
                        break

                if not has_pos:
                    logger.info("[%s] ✅ Position closed", symbol)
                    cash_mgr.force_release(symbol)
                    send_telegram(f"✅ {symbol} position closed")

                await asyncio.sleep(MONITOR_INTERVAL)
                continue

            # CRITICAL: Wait until next 15m candle close before checking bias
            # This ensures we always check at proper 15m boundaries (09:15, 09:30, 09:45, etc.)
            next_15m_close = get_next_candle_close_time(now_ist, '15min')
            sleep_seconds = get_seconds_until_next_close(now_ist, '15min')
            
            logger.info(
                "[%s] ⏰ Waiting for 15m close at %s IST (sleeping %ds)",
                symbol,
                next_15m_close.strftime("%H:%M:%S"),
                sleep_seconds,
            )
            await asyncio.sleep(sleep_seconds)
            
            # Now we're at a 15m boundary - get latest bars with complete candles only
            now_ist = get_ist_now()
            
            # Double check market close after waking up
            if now_ist.time() >= time(15, 30):
                logger.info("[%s] 🛑 Market closed (15:30 reached), stopping worker", symbol)
                break

            # Convert IST to UTC for bar_manager (it expects UTC)
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
                continue  # No bias, loop will wait for next 15m close

            logger.info("[%s] ✅ 15m bias detected: %s at %s IST", symbol, bias, now_ist.strftime("%H:%M:%S"))

            # Avoid duplicate triggers
            now_ist = get_ist_now()
            if last_15m_signal_time and (now_ist - last_15m_signal_time) < timedelta(minutes=15):
                time_since_last = (now_ist - last_15m_signal_time).total_seconds() / 60
                logger.info(
                    "[%s] ⏭️ Skipping duplicate signal (%.1f min since last), sleeping 60s...",
                    symbol,
                    time_since_last,
                )
                await asyncio.sleep(60)
                continue

            # Notify 15m bias found
            logger.info(
                "[%s] 🎯 NEW 15m signal: %s at %s IST - Starting 5m entry search...",
                symbol,
                bias,
                now_ist.strftime("%H:%M:%S"),
            )
            send_telegram(f"📊 [{symbol}] 15m Trend: {bias} at {now_ist.strftime('%H:%M')} IST. Looking for 5m entry...")
            last_15m_signal_time = now_ist

            checks = 0
            entered = False

            # Look for 5m entry confirmation at 5m candle closes
            logger.info("[%s] 🔎 Monitoring 5m entries (max %d checks)...", symbol, MAX_5M_CHECKS)
            while checks < MAX_5M_CHECKS and not entered and not _STOP:
                checks += 1
                
                # Wait for next 5m candle close (use IST for display)
                now_ist = get_ist_now()
                
                # Strict Market Close Check inside inner loop
                if now_ist.time() >= time(15, 30):
                    logger.info("[%s] 🛑 Market closed (15:30 reached), stopping entry search", symbol)
                    break

                next_5m_close = get_next_candle_close_time(now_ist, '5min')
                sleep_seconds = get_seconds_until_next_close(now_ist, '5min')
                
                logger.info(
                    "[%s] ⏰ 5m check #%d - waiting for %s IST (sleeping %ds)",
                    symbol,
                    checks,
                    next_5m_close.strftime("%H:%M:%S"),
                    sleep_seconds,
                )
                await asyncio.sleep(sleep_seconds)

                # Get fresh data at 5m boundary with complete candles only
                now_ist = get_ist_now()
                
                # Double check market close after waking up
                if now_ist.time() >= time(15, 30):
                    logger.info("[%s] 🛑 Market closed (15:30 reached), stopping entry search", symbol)
                    break

                now_utc = now_ist.astimezone(pytz.UTC).replace(tzinfo=None)
                df5_new, df15_new = await bar_manager.get_resampled(current_time=now_utc)
                if df5_new.empty or df15_new.empty:
                    logger.debug("[%s] ⚠️ Empty dataframe at 5m check #%d", symbol, checks)
                    continue

                # Revalidate 15m bias hasn't flipped
                bias_now = detect_15m_bias(df15_new)
                
                if bias_now != bias:
                    logger.warning("[%s] ⚠️ 15m bias changed %s → %s, aborting entry search", symbol, bias, bias_now)
                    send_telegram(f"⚠️ {symbol} 15m bias changed {bias} → {bias_now}, aborting")
                    break

                # Check 5m entry conditions at candle close
                entry_ok, details = detect_5m_entry(df5_new, bias)
                
                if not entry_ok:
                    continue  # No entry yet

                # Entry signal confirmed!
                logger.info(f"[{symbol}] ✅ 5m ENTRY SIGNAL CONFIRMED: {bias} - {details}")

                # Get underlying price
                from core.config import INDEX_FUTURES

                if symbol in INDEX_FUTURES:
                    logger.info("[%s] 📊 Fetching futures price for index...", symbol)
                    underlying = await angel_client.get_futures_price(symbol)
                else:
                    logger.info("[%s] 📊 Fetching stock price...", symbol)
                    underlying = await angel_client.get_last_price(symbol, exchange="NSE")

                if not underlying:
                    logger.error("[%s] ❌ Failed to get underlying price", symbol)
                    send_telegram(f"❌ {symbol} failed to get underlying price")
                    break

                logger.info("[%s] 💰 Underlying price: ₹%.2f", symbol, underlying)

                # Select option contract
                logger.info("[%s] 🔍 Selecting option contract...", symbol)
                opt_contract, reason = await find_option_contract_async(angel_client, symbol, bias, underlying)
                if not opt_contract:
                    logger.error("[%s] ❌ Option selection failed: %s", symbol, reason)
                    send_telegram(f"❌ {symbol} option selection failed: {reason}")
                    break

                logger.info("[%s] ✅ Selected option: %s", symbol, opt_contract["symbol"])

                # Get option premium
                logger.info("[%s] 💰 Fetching option premium...", symbol)
                prem = await angel_client.get_last_price(opt_contract["symbol"], exchange="NFO")
                if prem is None or prem < MIN_PREMIUM:
                    logger.error("[%s] ❌ Premium too low: ₹%s (min: ₹%.2f)", symbol, prem, MIN_PREMIUM)
                    send_telegram(f"❌ {symbol} premium too low: ₹{prem}")
                    break

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
                    break

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
                    break

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
                    details=details,
                )

                entered = True

            if not entered:
                logger.info("[%s] ⛔ No entry after %d checks", symbol, checks)
            # Loop will continue and wait for next 15m candle close

        except Exception as e:
            logger.exception("[%s] ❌ Worker exception: %s", symbol, e)
            send_telegram(f"⚠️ Error in {symbol} worker: {str(e)[:100]}")
            await asyncio.sleep(2)

    logger.info("[%s] 🛑 Worker exiting", symbol)


async def data_fetcher_loop(symbol, angel_client, bar_manager, symbol_index=0):
    """
    Background task that fetches new 1-minute bars aligned to 5-minute boundaries.
    Only fetches during market hours to avoid wasting API calls.
    Fetches 15 minutes of data with overlap to ensure no gaps.
    
    Args:
        symbol: Symbol to fetch data for
        angel_client: Angel API client
        bar_manager: Bar manager for this symbol
        symbol_index: Index of symbol in list (for staggered startup)
    """
    from core.config import MARKET_HOURS_ONLY
    
    # Stagger initial startup to prevent hitting rate limits during initialization
    startup_delay = symbol_index * 0.4
    if startup_delay > 0:
        logger.info("[%s] 📡 Data fetcher starting in %.1fs (staggered)", symbol, startup_delay)
        await asyncio.sleep(startup_delay)
    
    logger.info("[%s] 📡 Data fetcher started", symbol)
    retry_count = 0

    while not _STOP:
        try:
            now_ist = get_ist_now()
            
            # Strict Market Close Check
            if now_ist.time() >= time(15, 30):
                logger.info("[%s] 🛑 Market closed (15:30 reached), stopping data fetcher", symbol)
                break

            # Check if market is open (only fetch during market hours)
            if MARKET_HOURS_ONLY and not is_market_open():
                # Market is closed, sleep until next 5-minute boundary
                next_check = get_next_candle_close_time(now_ist, '5min')
                sleep_seconds = get_seconds_until_next_close(now_ist, '5min')
                
                logger.debug(
                    "[%s] 💤 Market closed, data fetcher sleeping until %s IST",
                    symbol,
                    next_check.strftime("%H:%M:%S"),
                )
                await asyncio.sleep(sleep_seconds)
                continue
            
            # Market is open - fetch data
            # Fetch last 15 minutes of data to ensure we don't miss any bars
            df_new = await angel_client.req_historic_1m(symbol, duration_days=0.0104)

            if df_new is not None and not df_new.empty:
                # Add new bars to BarManager
                for idx, row in df_new.iterrows():
                    bar_dict = {
                        'datetime': idx,
                        'open': row['open'],
                        'high': row['high'],
                        'low': row['low'],
                        'close': row['close'],
                        'volume': row['volume']
                    }
                    await bar_manager.add_bar(bar_dict)
                
                logger.info(
                    "[%s] 📊 Fetched %d 1m candles at %s IST",
                    symbol,
                    len(df_new),
                    now_ist.strftime("%H:%M:%S"),
                )
                retry_count = 0  # Reset retry counter on success
            else:
                logger.warning(
                    "[%s] ⚠️ No data returned from API at %s IST (market open)",
                    symbol,
                    now_ist.strftime("%H:%M:%S"),
                )

            # Sleep until next 5-minute boundary (00, 05, 10, 15, 20, 25, etc.)
            now_ist = get_ist_now()
            
            # Strict check again before sleeping
            if now_ist.time() >= time(15, 30):
                logger.info("[%s] 🛑 Market closed (15:30 reached), stopping data fetcher", symbol)
                break

            next_fetch = get_next_candle_close_time(now_ist, '5min')
            sleep_seconds = get_seconds_until_next_close(now_ist, '5min')
            
            logger.debug(
                "[%s] ⏰ Next data fetch at %s IST (sleeping %ds)",
                symbol,
                next_fetch.strftime("%H:%M:%S"),
                sleep_seconds,
            )
            await asyncio.sleep(sleep_seconds)

        except Exception as e:
            retry_count += 1
            logger.exception(
                "[%s] ❌ Data fetcher exception (retry #%d) at %s IST: %s",
                symbol,
                retry_count,
                get_ist_now().strftime("%H:%M:%S"),
                e,
            )
            
            # Check if it's a rate limiting error
            error_str = str(e).lower()
            if "ab1004" in error_str or "try after sometime" in error_str:
                logger.warning(
                    "[%s] 🚫 API rate limit detected, waiting 2 minutes before retry...",
                    symbol,
                )
                await asyncio.sleep(120)  # Wait 2 minutes for rate limiting
            else:
                # For other errors, wait until next 5-minute boundary
                now_ist = get_ist_now()
                sleep_seconds = get_seconds_until_next_close(now_ist, '5min')
                await asyncio.sleep(sleep_seconds)

    logger.info("[%s] 🛑 Data fetcher exiting", symbol)


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
    from core.utils import get_seconds_until_market_close
    
    logger.info("📅 End-of-day report scheduler started")
    
    while not _STOP:
        try:
            # Calculate wait time until market close
            wait_seconds = get_seconds_until_market_close()
            
            logger.info(f"⏰ End-of-day report scheduled in {wait_seconds/3600:.1f} hours")
            
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


async def run_all_workers():
    """Initialize and run all worker tasks in a daily loop"""
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
            
            # Check if we are in the active window
            is_active_window = start_time <= current_time < end_time
            
            if not is_active_window:
                # Calculate wait time until next start (09:00 AM)
                if current_time >= end_time:
                    # Wait until tomorrow 09:00
                    next_start = datetime.combine(now_ist.date() + timedelta(days=1), start_time)
                else:
                    # Wait until today 09:00
                    next_start = datetime.combine(now_ist.date(), start_time)
                
                # Make next_start timezone aware
                tz = pytz.timezone("Asia/Kolkata")
                if next_start.tzinfo is None:
                    next_start = tz.localize(next_start)
                
                wait_seconds = (next_start - now_ist).total_seconds()
                
                logger.info(
                    f"💤 Market closed. Sleeping until {next_start.strftime('%Y-%m-%d %H:%M:%S')} IST "
                    f"({wait_seconds/3600:.1f} hours)"
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
                logger.error("❌ Failed to connect to Angel Broker. Retrying in 1 minute...")
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

            # Start worker tasks AND data fetcher tasks
            tasks = []
            
            # Heartbeat is already running globally
            
            # Start end-of-day report scheduler
            # Note: The scheduler itself waits for 15:30, but since we are now handling the daily loop here,
            # we might not strictly need a separate scheduler if we just run it at the end of this block.
            # However, keeping it as a task ensures it runs in parallel if we want it to.
            # But simpler is better: let's run the report explicitly after workers exit (at 15:30).
            # So we WON'T start the scheduler task, we'll just call the report function at the end.
            
            # Start data fetcher for each symbol
            logger.info("🚀 Starting background data fetchers (5-minute interval, staggered startup)...")
            for idx, symbol in enumerate(SYMBOLS):
                bar_mgr = bar_managers.get(symbol)
                tasks.append(data_fetcher_loop(symbol, angel_client, bar_mgr, symbol_index=idx))
                logger.info("[%s] 📡 Data fetcher thread queued (delay: %.1fs)", symbol, idx * 0.4)
            
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


def stop_all_workers():
    """Stop all worker tasks"""
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
