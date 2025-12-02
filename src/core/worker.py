# core/worker.py
import asyncio
from datetime import datetime, timedelta

from core.angel_client import AngelClient
from core.bar_manager import BarManager
from core.cash_manager import create_cash_manager
from core.config import (
    ALLOC_PCT,
    MAX_5M_CHECKS,
    MAX_CONTRACTS_PER_TRADE,
    MAX_DAILY_LOSS,
    MAX_POSITION_SIZE,
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
from core.utils import init_audit_file, is_market_open, send_telegram, write_audit_row

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


async def worker_loop(symbol, angel_client, cash_mgr, bar_manager):
    """
    Main worker loop for each symbol.
    Monitors market, detects signals, and executes trades.
    """
    logger.info("[%s] 🚀 Worker started", symbol)
    last_15m_signal_time = None

    while not _STOP:
        try:

            # Market hours guard
            is_open = is_market_open()
            await notify_market_state(is_open)

            from core.config import MARKET_HOURS_ONLY

            if MARKET_HOURS_ONLY and not is_open:
                await asyncio.sleep(300)  # Sleep 5 minutes if market closed
                continue

            # Check if we already have an open position for this symbol
            if symbol in cash_mgr.open_positions:
                # Poll for position closure via Angel API
                positions = angel_client.get_positions()
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

            # Get current time for candle completion checks
            now = datetime.utcnow()
            
            # Wait until next 15m candle close before checking bias
            next_15m_close = get_next_candle_close_time(now, '15min')
            sleep_seconds = get_seconds_until_next_close(now, '15min')
            
            logger.info(
                "[%s] ⏰ Waiting for 15m close at %s",
                symbol,
                next_15m_close.strftime("%H:%M:%S"),
            )
            await asyncio.sleep(sleep_seconds)
            
            # Now we're at a 15m boundary - get latest bars with complete candles only
            now = datetime.utcnow()
            df5, df15 = await bar_manager.get_resampled(current_time=now)

            if df15.empty:
                logger.warning("[%s] ⚠️ No 15m data available, waiting...", symbol)
                await asyncio.sleep(60)
                continue

            # Detect 15m bias at candle close
            logger.info("[%s] 🔍 Checking 15m bias (bars: 5m=%d, 15m=%d)...", symbol, len(df5), len(df15))
            bias = detect_15m_bias(df15)
            
            if not bias:
                continue  # No bias, loop will wait for next 15m close

            logger.info("[%s] ✅ 15m bias detected: %s", symbol, bias)

            # Avoid duplicate triggers
            now = datetime.utcnow()
            if last_15m_signal_time and (now - last_15m_signal_time) < timedelta(minutes=15):
                time_since_last = (now - last_15m_signal_time).total_seconds() / 60
                logger.info(
                    "[%s] ⏭️ Skipping duplicate signal (%.1f min since last), sleeping 60s...",
                    symbol,
                    time_since_last,
                )
                await asyncio.sleep(60)
                continue

            # Notify 15m bias found
            logger.info("[%s] 🎯 NEW 15m signal: %s - Starting 5m entry search...", symbol, bias)
            send_telegram(f"📊 [{symbol}] 15m Trend: {bias}. Looking for 5m entry...")
            last_15m_signal_time = now

            checks = 0
            entered = False

            # Look for 5m entry confirmation at 5m candle closes
            logger.info("[%s] 🔎 Monitoring 5m entries (max %d checks)...", symbol, MAX_5M_CHECKS)
            while checks < MAX_5M_CHECKS and not entered and not _STOP:
                checks += 1
                
                # Wait for next 5m candle close
                now = datetime.utcnow()
                next_5m_close = get_next_candle_close_time(now, '5min')
                sleep_seconds = get_seconds_until_next_close(now, '5min')
                
                logger.info(
                    "[%s] ⏰ 5m check #%d - waiting for %s",
                    symbol,
                    checks,
                    next_5m_close.strftime("%H:%M:%S"),
                )
                await asyncio.sleep(sleep_seconds)

                # Get fresh data at 5m boundary with complete candles only
                now = datetime.utcnow()
                df5_new, df15_new = await bar_manager.get_resampled(current_time=now)
                if df5_new.empty or df15_new.empty:
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
                # For indices: Get futures price (for signal accuracy)
                # For stocks: Get stock price
                from core.config import INDEX_FUTURES

                if symbol in INDEX_FUTURES:
                    logger.info("[%s] 📊 Fetching futures price for index...", symbol)
                    # Get current monthly futures price for indices
                    underlying = await angel_client.get_futures_price(symbol)
                else:
                    logger.info("[%s] 📊 Fetching stock price...", symbol)
                    # Get stock price
                    underlying = await angel_client.get_last_price(symbol, exchange="NSE")

                if not underlying:
                    logger.error("[%s] ❌ Failed to get underlying price", symbol)
                    send_telegram(f"❌ {symbol} failed to get underlying price")
                    break

                logger.info("[%s] 💰 Underlying price: ₹%.2f", symbol, underlying)

                # Select option contract (ALWAYS OPTIONS, never futures)
                # For indices: Uses futures price to select index option strike
                # For stocks: Uses stock price to select stock option strike
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

                bracket = angel_client.place_bracket_order(
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
                    timestamp=datetime.utcnow().isoformat(),
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


async def data_fetcher_loop(symbol, angel_client, bar_manager):
    """
    Background task that fetches new 1-minute bars every 5 minutes.
    This keeps the BarManager updated with fresh data.
    """
    logger.info("[%s] 📡 Data fetcher started", symbol)

    while not _STOP:
        try:

            # Fetch last 15 minutes of data to ensure we don't miss any bars
            df_new = await angel_client.req_historic_1m(symbol, duration_days=0.01)  # ~15 minutes

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

            # Sleep for 5 minutes before next fetch
            await asyncio.sleep(300)  # 5 minutes

        except Exception as e:
            logger.exception("[%s] ❌ Data fetcher exception: %s", symbol, e)
            await asyncio.sleep(60)  # Retry after 1 minute on error

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
        positions = angel_client.get_positions()
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
    """Initialize and run all worker tasks"""
    global _STOP

    init_audit_file()

    # Initialize Angel Broker client
    angel_client = AngelClient()

    # Connect to Angel Broker
    await angel_client.connect_async()

    # Create cash manager
    cash_mgr = create_cash_manager(
        angel_client=angel_client,
        max_alloc_pct=ALLOC_PCT,
        max_daily_loss=MAX_DAILY_LOSS,
        max_position_size=MAX_POSITION_SIZE,
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
    
    # Start end-of-day report scheduler
    logger.info("📅 Starting end-of-day report scheduler...")
    tasks.append(schedule_end_of_day_report(cash_mgr, angel_client))
    
    # Start data fetcher for each symbol (runs every 5 minutes)
    logger.info("🚀 Starting background data fetchers (5-minute interval)...")
    for symbol in SYMBOLS:
        bar_mgr = bar_managers.get(symbol)
        tasks.append(data_fetcher_loop(symbol, angel_client, bar_mgr))
        logger.info("[%s] 📡 Data fetcher thread started", symbol)
    
    # Start worker loop for each symbol
    logger.info("🚀 Starting worker loops...")
    for symbol in SYMBOLS:
        bar_mgr = bar_managers.get(symbol)
        tasks.append(worker_loop(symbol, angel_client, cash_mgr, bar_mgr))
        logger.info("[%s] 🔄 Worker thread started", symbol)

    send_telegram("🚀 Angel Broker Bot Started (LIVE TRADING)")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Tasks cancelled")
    finally:
        angel_client.disconnect()
        send_telegram("🛑 Angel Broker Bot Stopped")


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
