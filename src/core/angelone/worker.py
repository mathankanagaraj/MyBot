# core/angelone/worker.py
"""
Fully refactored Angel One broker worker implementation.
Handles NSE market trading with options via Angel One.
Async-safe, cancellation-aware, with heartbeat, data fetchers, signal monitors, and startup checks.
"""
import asyncio
from datetime import datetime, time, timedelta

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
    MARKET_HOURS_ONLY,
    INDEX_FUTURES,
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
    get_seconds_until_market_close,
)

_STOP_EVENT = asyncio.Event()  # Global stop event
_LAST_MARKET_OPEN_STATE = None

# OCO Order Tracking: symbol -> {'sl_id': str, 'target_id': str}
ACTIVE_OCO_ORDERS = {}


# -----------------------------
# Helper Functions
# -----------------------------
async def sleep_until_next(seconds):
    """Sleep for a period but allow cancellation."""
    try:
        await asyncio.wait_for(asyncio.sleep(seconds), timeout=seconds)
    except asyncio.CancelledError:
        return


def compute_stop_target(entry_price):
    """Calculate stop loss and target prices based on risk parameters"""
    if RISK_PER_CONTRACT and float(RISK_PER_CONTRACT) > 0:
        risk = float(RISK_PER_CONTRACT)
    else:
        risk = float(RISK_PCT_OF_PREMIUM) * float(entry_price)

    # Minimum stop price cannot be negative or zero (set min to 0.05 or similar for valid output)
    stop = max(0.05, float(entry_price) - risk)
    target = float(entry_price) + RR_RATIO * risk
    return stop, target, risk


# -----------------------------
# Heartbeat
# -----------------------------
async def heartbeat_task(interval=60):
    """Continuous heartbeat to show bot is alive."""
    logger.info("💓 Heartbeat task started")
    while not _STOP_EVENT.is_set():
        now_utc = datetime.utcnow()
        logger.info(f"💓 Heartbeat: {now_utc.strftime('%H:%M:%S')} UTC")
        await sleep_until_next(interval)
    logger.info("💓 Heartbeat task stopped")


# -----------------------------
# End of Day Reporting
# -----------------------------
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


async def eod_scheduler_task(cash_mgr, angel_client, bar_managers):
    """
    Background task that schedules end-of-day report at market close.
    """
    logger.info("📅 End-of-day report scheduler started")

    while not _STOP_EVENT.is_set():
        try:
            # Calculate wait time until market close (15:30 IST)
            wait_seconds = get_seconds_until_market_close()

            # If market is already closed, wait_seconds might be large (until next day),
            # or negative if we are just past close.
            # get_seconds_until_market_close usually returns seconds from NOW until today's 15:30.
            # If it's past 15:30, it might return 0 or negative.

            now_ist = get_ist_now()
            if now_ist.time() >= time(15, 30):
                # Market already closed today
                await sleep_until_next(60)
                continue

            logger.info(
                f"⏰ End-of-day report scheduled in {wait_seconds/3600:.1f} hours"
            )

            # Wait until market close
            # Use sleep_until_next to handle cancellation
            await sleep_until_next(wait_seconds)

            if _STOP_EVENT.is_set():
                break

            logger.info("🏁 Daily trading session ended (15:30 reached)")

            # Finalize any pending bars
            logger.info("💾 Finalizing last minute bars...")
            for symbol, bar_mgr in bar_managers.items():
                await bar_mgr.finalize_bar()

            # Generate report
            await end_of_day_report(cash_mgr, angel_client)

            # Wait a bit to avoid repeat
            await sleep_until_next(300)

        except Exception as e:
            logger.exception("Error in end-of-day scheduler: %s", e)
            await sleep_until_next(60)

    logger.info("📅 End-of-day report scheduler exiting")


# -----------------------------
# Execute Order
# -----------------------------
async def manage_oco_orders(symbol, angel_client):
    """
    Check status of SL and Target orders.
    If one is filled (complete), cancel the other.
    """
    if symbol not in ACTIVE_OCO_ORDERS:
        return

    txn_ids = ACTIVE_OCO_ORDERS[symbol]
    sl_id = txn_ids.get("sl_order_id")
    tp_id = txn_ids.get("target_order_id")

    if not sl_id or not tp_id:
        return

    try:
        # Fetch status of all orders
        # Note: Optimization would be to fetch individual status if API allows, but orderBook is standard
        book = await asyncio.to_thread(angel_client.smart_api.orderBook)

        sl_status = None
        tp_status = None

        if book and book.get("data"):
            for order in book["data"]:
                oid = order.get("orderid")
                status = order.get(
                    "status"
                )  # complete, cancelled, rejected, open, trigger pending

                if oid == sl_id:
                    sl_status = status
                elif oid == tp_id:
                    tp_status = status

        # Logic: One Cancels Other
        if sl_status == "complete":
            logger.info(f"[{symbol}] 🛑 SL Hit! Cancelling Target {tp_id}")
            try:
                await asyncio.to_thread(
                    angel_client.smart_api.cancelOrder, tp_id, "NORMAL"
                )
            except Exception as e:
                logger.error(f"[{symbol}] Failed to cancel Target: {e}")
            del ACTIVE_OCO_ORDERS[symbol]

        elif tp_status == "complete":
            logger.info(f"[{symbol}] 🎯 Target Hit! Cancelling SL {sl_id}")
            try:
                await asyncio.to_thread(
                    angel_client.smart_api.cancelOrder, sl_id, "STOPLOSS"
                )
            except Exception as e:
                logger.error(f"[{symbol}] Failed to cancel SL: {e}")
            del ACTIVE_OCO_ORDERS[symbol]

        elif sl_status in ["cancelled", "rejected"] and tp_status in [
            "cancelled",
            "rejected",
        ]:
            logger.info(
                f"[{symbol}] Both SL and Target cancelled/rejected. Cleaning up OCO."
            )
            del ACTIVE_OCO_ORDERS[symbol]

    except Exception as e:
        logger.error(f"[{symbol}] OCO Check Error: {e}")


async def execute_angel_entry_order(
    symbol, bias, angel_client, cash_mgr, underlying_price
):
    """
    Execute entry order with option selection and bracket order placement for Angel One.
    """
    # Select option contract
    logger.info("[%s] 🔍 Selecting option contract...", symbol)
    opt_selection, reason = await find_option_contract_async(
        angel_client, symbol, bias, underlying_price
    )
    if not opt_selection:
        logger.error("[%s] ❌ Option selection failed: %s", symbol, reason)
        send_telegram(f"❌ {symbol} option selection failed: {reason}")
        return False

    logger.info("[%s] ✅ Selected option: %s", symbol, opt_selection.symbol)

    # Get option premium
    logger.info("[%s] 💰 Fetching option premium...", symbol)
    prem = await angel_client.get_last_price(opt_selection.symbol, exchange="NFO")
    if prem is None or prem < MIN_PREMIUM:
        logger.error(
            "[%s] ❌ Premium too low: ₹%s (min: ₹%.2f)", symbol, prem, MIN_PREMIUM
        )
        send_telegram(f"❌ {symbol} premium too low: ₹{prem}")
        return False

    logger.info("[%s] 💰 Option premium: ₹%.2f", symbol, prem)

    # Calculate position size
    lot_size = opt_selection.lot_size
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

    if stop_price < 0.05:
        stop_price = 0.05

    # Place bracket order
    logger.info(
        f"[{symbol}] 📤 Placing bracket order: {bias} "
        f"Entry=₹{prem:.2f}, SL=₹{stop_price:.2f}, TP=₹{target_price:.2f}"
    )

    bracket = await angel_client.place_bracket_order(
        option_symbol=opt_selection.symbol,
        option_token=opt_selection.token,
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

    # Store IDs for OCO management
    # Note: place_bracket_order in client.py returns dict with keys: entry_order_id, sl_order_id, target_order_id
    ACTIVE_OCO_ORDERS[symbol] = bracket

    logger.info("[%s] ✅ Order placed successfully!", symbol)
    send_telegram(
        f"✅ Entered {symbol} {bias}\n"
        f"Option: {opt_selection.symbol}\n"
        f"Entry: ₹{prem:.2f} | SL: ₹{stop_price:.2f} | TP: ₹{target_price:.2f}"
    )

    # Write audit
    write_audit_row(
        timestamp=get_ist_now().isoformat(),
        symbol=symbol,
        bias=bias,
        option=opt_selection.symbol,
        entry_price=prem,
        stop=stop_price,
        target=target_price,
        exit_price=0,
        outcome="OPEN",
        holding_seconds=0,
        details="Entry confirmed",
    )

    return True


# -----------------------------
# 5m Entry Search
# -----------------------------
async def search_angel_5m_entry(
    symbol, bias, angel_client, cash_mgr, bar_manager, context="ENTRY"
):
    """
    Search for 5m entry confirmation over multiple candles for Angel One.
    """
    checks = 0

    while checks < MAX_5M_CHECKS and not _STOP_EVENT.is_set():
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
        await sleep_until_next(sleep_seconds)

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


# -----------------------------
# Startup Signal Check
# -----------------------------
async def handle_angel_startup_signal(symbol, angel_client, cash_mgr, bar_manager):
    """
    Check for recent 15m signal on startup and search for entry if found.
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

        # Ensure timezone compatibility
        if latest_15m_time.tzinfo is None:
            # Assume naive time is UTC (as per bar_manager storage)
            latest_15m_time = pytz.utc.localize(latest_15m_time)

        now_utc = now_ist.astimezone(pytz.UTC)
        time_since_signal = (now_utc - latest_15m_time).total_seconds() / 60

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


# -----------------------------
# Signal Monitor Loop
# -----------------------------
async def angel_signal_monitor(symbol, angel_client, cash_mgr, bar_manager):
    """
    Dedicated signal monitor for a symbol.
    """
    logger.info("[%s] 👀 Signal monitor started", symbol)

    # 1. Check for startup signals
    await handle_angel_startup_signal(symbol, angel_client, cash_mgr, bar_manager)

    # 2. Main Loop
    while not _STOP_EVENT.is_set():
        try:
            now_ist = get_ist_now()

            # Strict Market Close Check (15:30)
            if now_ist.time() >= time(15, 30):
                logger.info(
                    "[%s] 🛑 Market closed (15:30 reached), stopping monitor", symbol
                )
                break

            # Market hours guard
            if MARKET_HOURS_ONLY and not is_market_open():
                logger.debug("[%s] 💤 Market closed, monitor sleeping...", symbol)
                await sleep_until_next(300)
                continue

                # Check for existing position
                # We check cash_mgr caching for speed, but also sync with API occasionally
                if symbol in cash_mgr.open_positions:
                    logger.info(
                        "[%s] ⏸️ Position exists, monitoring OCO & closure...", symbol
                    )

                    # 1. Manage OCO (Cancel SL/TP if other fills)
                    await manage_oco_orders(symbol, angel_client)

                    # 2. Check positions
                    try:
                        positions = await angel_client.get_positions()
                        still_open = False
                        for p in positions:
                            if (
                                symbol in p.get("tradingsymbol", "")
                                and int(p.get("netqty", 0)) != 0
                            ):
                                still_open = True
                                break

                        if not still_open:
                            logger.info(
                                "[%s] ✅ Position closed externally (or via OCO), resuming.",
                                symbol,
                            )
                            cash_mgr.force_release(symbol)
                            # Also clear OCO if still exists
                            if symbol in ACTIVE_OCO_ORDERS:
                                del ACTIVE_OCO_ORDERS[symbol]
                        else:
                            await sleep_until_next(MONITOR_INTERVAL)
                            continue
                    except Exception as e:
                        logger.error(f"Error checking positions: {e}")
                        await sleep_until_next(60)
                        continue

            # Wait for next 15m candle close
            next_15m_close = get_next_candle_close_time(now_ist, "15min")
            sleep_seconds = get_seconds_until_next_close(now_ist, "15min")

            logger.info(
                "[%s] ⏰ Waiting for 15m close at %s IST (sleeping %ds)",
                symbol,
                next_15m_close.strftime("%H:%M:%S"),
                sleep_seconds,
            )
            await sleep_until_next(sleep_seconds)

            # Wake up and get data
            now_ist = get_ist_now()
            if now_ist.time() >= time(15, 30):
                break

            # Convert IST to UTC for bar_manager
            now_utc = now_ist.astimezone(pytz.UTC).replace(tzinfo=None)
            df5, df15 = await bar_manager.get_resampled(current_time=now_utc)

            if df15.empty:
                logger.warning("[%s] ⚠️ No 15m data available, waiting...", symbol)
                await sleep_until_next(60)
                continue

            # Detect 15m bias
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
                "[%s] 🎯 NEW 15m signal: %s at %s IST - Starting 5m entry search...",
                symbol,
                bias,
                now_ist.strftime("%H:%M:%S"),
            )
            send_telegram(
                f"📊 [{symbol}] 15m Trend: {bias} at {now_ist.strftime('%H:%M')} IST. Looking for 5m entry..."
            )

            await search_angel_5m_entry(
                symbol, bias, angel_client, cash_mgr, bar_manager, "ENTRY"
            )

        except Exception as e:
            logger.exception("[%s] ❌ Signal monitor exception: %s", symbol, e)
            await sleep_until_next(60)


# -----------------------------
# Main Worker Function
# -----------------------------
async def run_angel_workers():
    """Initialize and run all Angel One worker tasks in a daily loop"""

    logger.info("🤖 Bot process started")
    init_audit_file()

    # Start heartbeat task immediately and continuously
    heartbeat = asyncio.create_task(heartbeat_task())

    while not _STOP_EVENT.is_set():
        try:
            now_ist = get_ist_now()
            current_time = now_ist.time()

            # Define active window: 09:00 to 15:30
            start_time = time(9, 0)
            end_time = time(15, 30)

            # Check if we are in the active window (Mon-Fri, 09:00-15:30)
            is_weekday = now_ist.weekday() <= 4  # 0=Mon, 4=Fri
            is_active_window = is_weekday and (start_time <= current_time < end_time)

            if not is_active_window:
                # Calculate wait time until next start (09:00 AM)
                if current_time >= end_time or not is_weekday:
                    # Wait until tomorrow 09:00
                    next_start = datetime.combine(
                        now_ist.date() + timedelta(days=1), start_time
                    )
                else:
                    # Wait until today 09:00
                    next_start = datetime.combine(now_ist.date(), start_time)

                # Skip weekends
                while next_start.weekday() > 4:
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

                # Sleep in chunks
                while wait_seconds > 0 and not _STOP_EVENT.is_set():
                    sleep_chunk = min(wait_seconds, 60)
                    await asyncio.sleep(sleep_chunk)
                    wait_seconds -= sleep_chunk

                if _STOP_EVENT.is_set():
                    break

            # 🌅 Start Daily Cycle
            logger.info("🌅 Starting daily trading cycle...")
            send_telegram("🌅 Bot waking up for trading day...")

            # Initialize Angel Broker client
            angel_client = AngelClient()

            # Connect
            await angel_client.connect_async()
            if not angel_client.connected:
                logger.error(
                    "❌ Failed to connect to Angel Broker. Retrying in 1 minute..."
                )
                await sleep_until_next(60)
                continue

            # Create cash manager
            cash_mgr = create_cash_manager(
                angel_client=angel_client,
                max_alloc_pct=ALLOC_PCT,
                max_daily_loss_pct=MAX_DAILY_LOSS_PCT,
                max_position_pct=MAX_POSITION_PCT,
            )

            # Initialize BarManagers
            bar_managers = {}
            logger.info("Initializing BarManagers and loading historical data...")

            for symbol in SYMBOLS:
                bar_mgr = BarManager(symbol, max_bars=2880)
                bar_managers[symbol] = bar_mgr

                # Load historical data
                logger.info("[%s] Loading historical data...", symbol)
                df_hist = await angel_client.req_historic_1m(symbol, duration_days=2)
                if df_hist is not None and not df_hist.empty:
                    await bar_mgr.initialize_from_historical(df_hist)
                    logger.info("[%s] Loaded %d historical bars", symbol, len(df_hist))
                else:
                    logger.warning("[%s] Failed to load historical data", symbol)

            # Pre-market check
            await cash_mgr.check_and_log_start_balance()

            # Start WebSocket
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

            # Subscribe
            for symbol in SYMBOLS:
                token = angel_client.get_symbol_token(symbol, "NSE")
                if token:
                    ws_client.add_symbol(symbol, token, "NSE")
                else:
                    logger.error(f"Could not find token for {symbol}")

            # Start WebSocket Thread
            import threading

            ws_thread = threading.Thread(target=ws_client.connect, daemon=True)
            ws_thread.start()

            # Launch Workers
            tasks = []

            # 1. EOD Scheduler
            tasks.append(eod_scheduler_task(cash_mgr, angel_client, bar_managers))

            # 2. Signal Monitors
            logger.info("🚀 Starting signal monitors...")
            for symbol in SYMBOLS:
                bar_mgr = bar_managers.get(symbol)
                tasks.append(
                    angel_signal_monitor(symbol, angel_client, cash_mgr, bar_mgr)
                )

            send_telegram("🚀 Angel Broker Bot Started (LIVE TRADING)")

            # Wait for tasks
            try:
                await asyncio.gather(*tasks)
            except asyncio.CancelledError:
                logger.info("Tasks cancelled")
            except Exception as e:
                logger.exception("Error in daily task group: %s", e)

            # Cleanup
            logger.info("👋 Disconnecting from Angel Broker...")
            angel_client.disconnect()

            # Wait briefly before loop restarts (if not stopped)
            await sleep_until_next(5)

        except Exception as e:
            logger.exception("CRITICAL: Error in main daily loop: %s", e)
            send_telegram(f"🚨 CRITICAL: Angel Bot daily loop error: {str(e)[:100]}")
            await sleep_until_next(60)

    # Wait for heartbeat if stopped
    if not heartbeat.done():
        await heartbeat


def stop_angel_workers():
    """Stop all Angel One workers"""
    _STOP_EVENT.set()
    logger.info("🛑 Stop signal sent to all workers")


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
