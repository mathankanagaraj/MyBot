# core/angelone/worker.py
"""
Fully refactored Angel One broker worker implementation.
Handles NSE market trading with options via Angel One.
Async-safe, cancellation-aware, with heartbeat, data fetchers, signal monitors, and startup checks.
Uses a single background market-state watcher to centralize market-hours logic.
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
    ANGEL_SYMBOLS,
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


# -----------------------------
# Global State
# -----------------------------
_STOP_EVENT = (
    asyncio.Event()
)  # Global stop event (hard stop at daily close or external)
_LAST_MARKET_OPEN_STATE = None

# Market watcher managed state (single source of truth)
MARKET_OPEN_STATE = False
# Event used to notify workers of state changes. The watcher will set() the old event
# and replace it with a fresh Event each time the state changes to wake all waiters.
MARKET_STATE_EVENT = asyncio.Event()

# OCO Order Tracking: symbol -> {'sl_order_id': str, 'target_order_id': str}
ACTIVE_OCO_ORDERS = {}

# Global trade entry lock to prevent simultaneous order placement across symbols
_TRADE_ENTRY_LOCK = asyncio.Lock()


# -----------------------------
# Helper Functions
# -----------------------------
async def sleep_until_next(seconds):
    """Sleep for a period but allow cancellation."""
    try:
        await asyncio.sleep(seconds)
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


async def _notify_market_state_local(is_open: bool):
    """
    Local wrapper to call notify_market_state but swallow exceptions.
    """
    try:
        await notify_market_state(is_open)
    except Exception:
        logger.debug("notify_market_state failed", exc_info=True)


async def wait_for_market():
    """
    Block until MARKET_OPEN_STATE becomes True or _STOP_EVENT is set.
    Returns True if market is open and we should continue.
    Returns False if _STOP_EVENT was set and work should stop.
    """
    global MARKET_OPEN_STATE, MARKET_STATE_EVENT

    # Fast path
    if MARKET_OPEN_STATE:
        return True

    while not MARKET_OPEN_STATE:
        if _STOP_EVENT.is_set():
            return False
        # Wait on the current event to be set by the watcher; watcher will replace it after setting.
        try:
            await MARKET_STATE_EVENT.wait()
        except asyncio.CancelledError:
            return False
        # Loop checks MARKET_OPEN_STATE again, if changed to True we'll exit with True.
    return True


# -----------------------------
# Market State Watcher
# -----------------------------
async def market_state_watcher(poll_interval=5):
    """
    Single background coroutine that manages market open/closed state for all workers.
    Responsibilities:
    - Maintain MARKET_OPEN_STATE (single source of truth)
    - Trigger MARKET_STATE_EVENT to wake workers when state changes
    - Set _STOP_EVENT at hard close (15:30 IST) ONLY during active trading
    - Sleep until next market open if started after hours
    - Notify via notify_market_state on changes
    """
    global MARKET_OPEN_STATE, MARKET_STATE_EVENT, _STOP_EVENT, _LAST_MARKET_OPEN_STATE

    logger.info("🕒 Market state watcher started (AngelOne - NSE)")

    # Initialize last state using is_market_open() if possible
    try:
        initial_state = is_market_open() if MARKET_HOURS_ONLY else True
    except Exception:
        initial_state = False

    MARKET_OPEN_STATE = initial_state
    _LAST_MARKET_OPEN_STATE = initial_state
    # Ensure first notification about start (the notify function handles a first-run message)
    await _notify_market_state_local(MARKET_OPEN_STATE)
    
    # If market is already open at initialization, set the event so waiters can proceed
    if MARKET_OPEN_STATE:
        try:
            MARKET_STATE_EVENT.set()
        except Exception:
            logger.debug("Failed to set initial MARKET_STATE_EVENT", exc_info=True)
    
    logger.info(
        f"🕒 Initial market state: {'OPEN ✅' if MARKET_OPEN_STATE else 'CLOSED 🚫'}"
    )

    # Track if we were open during this session (to distinguish hard close from startup after hours)
    was_open_today = MARKET_OPEN_STATE

    try:
        while not _STOP_EVENT.is_set():
            now_ist = get_ist_now()

            # Hard cutoff: if time >= 15:30 IST and we were open today, stop the bot
            if now_ist.time() >= time(15, 30):
                if was_open_today:
                    # Only set stop event if we were trading today (hard close scenario)
                    if MARKET_OPEN_STATE:
                        logger.info(
                            "🛑 Market hard close reached (15:30 IST) - transitioning to CLOSED"
                        )
                    MARKET_OPEN_STATE = False

                    # Wake all waiters then set the global stop event
                    old_event = MARKET_STATE_EVENT
                    try:
                        old_event.set()
                    except Exception:
                        pass
                    MARKET_STATE_EVENT = asyncio.Event()

                    # Notify and set stop
                    try:
                        await notify_market_state(False)
                    except Exception:
                        logger.debug(
                            "notify_market_state failed at hard close", exc_info=True
                        )

                    _STOP_EVENT.set()
                    logger.info("🛑 _STOP_EVENT set by market watcher (hard close at 15:30 IST)")
                    send_telegram("🛑 [AngelOne] Trading stopped - Market closed at 15:30 IST", broker="ANGEL")
                    break
                else:
                    # Started after 15:30, just keep MARKET_OPEN_STATE = False
                    # Let run_angel_workers handle the sleep logic
                    MARKET_OPEN_STATE = False

            # Soft open/close using util, only if MARKET_HOURS_ONLY is True
            if MARKET_HOURS_ONLY:
                try:
                    is_open_now = is_market_open()
                except Exception as e:
                    logger.error("is_market_open() failed in watcher: %s", e)
                    is_open_now = False
            else:
                is_open_now = True

            # If state changed, wake waiters and notify
            if is_open_now != MARKET_OPEN_STATE:
                MARKET_OPEN_STATE = is_open_now

                # Track if market opened during this session
                if is_open_now:
                    was_open_today = True

                # Wake all waiting workers using the old event then swap in a fresh event
                old_event = MARKET_STATE_EVENT
                try:
                    old_event.set()
                except Exception:
                    logger.debug("Failed to set MARKET_STATE_EVENT", exc_info=True)
                MARKET_STATE_EVENT = asyncio.Event()

                # Notify external (telegram/log)
                try:
                    await notify_market_state(MARKET_OPEN_STATE)
                except Exception:
                    logger.debug("notify_market_state failed", exc_info=True)

                logger.info(
                    f"🔔 Market state changed: {'OPEN ✅' if MARKET_OPEN_STATE else 'CLOSED 🚫'}"
                )

            await asyncio.sleep(poll_interval)
    except asyncio.CancelledError:
        logger.info("Market state watcher cancelled")
    except Exception as e:
        logger.exception("Market watcher crashed: %s", e)
    finally:
        # Final wake to ensure any waiters exit if stopping
        try:
            MARKET_STATE_EVENT.set()
        except Exception:
            pass

    logger.info("🕒 Market state watcher stopped")


# -----------------------------
# Heartbeat
# -----------------------------
async def heartbeat_task(interval=60):
    """Continuous heartbeat to show bot is alive."""
    logger.info("💓 Heartbeat task started")
    heartbeat_count = 0
    
    while not _STOP_EVENT.is_set():
        try:
            heartbeat_count += 1
            now_utc = datetime.utcnow()
            logger.info(f"💓 Heartbeat #{heartbeat_count}: {now_utc.strftime('%H:%M:%S')} UTC")
            
            # Sleep for interval, but check for cancellation
            await sleep_until_next(interval)
            
        except asyncio.CancelledError:
            logger.info("💓 Heartbeat task cancelled")
            break
        except Exception as e:
            logger.error(f"💓 Heartbeat task error: {e}")
            await sleep_until_next(10)  # Retry sooner on error
    
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
        send_telegram(msg, broker="ANGEL")

    except Exception as e:
        logger.exception("Error generating end-of-day report: %s", e)
        send_telegram(f"⚠️ Error generating end-of-day report: {str(e)[:100]}", broker="ANGEL")


async def eod_scheduler_task(cash_mgr, angel_client, bar_managers):
    """
    Background task that schedules end-of-day report at market close.
    """
    logger.info("📅 End-of-day report scheduler started")

    while not _STOP_EVENT.is_set():
        try:
            # Calculate wait time until market close (15:30 IST)
            wait_seconds = get_seconds_until_market_close()

            now_ist = get_ist_now()
            if now_ist.time() >= time(15, 30):
                # Market already closed today
                await sleep_until_next(60)
                continue

            logger.info(
                f"⏰ End-of-day report scheduled in {wait_seconds/3600:.1f} hours"
            )

            # Wait until market close
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


async def execute_entry_order(
    symbol, bias, angel_client, cash_mgr, underlying_price
):
    """
    Execute entry order with option selection and bracket order placement for Angel One.
    Uses global lock to prevent simultaneous order placement.
    
    Pre-Trade Validation Flow (using Angel One API as single source of truth):
    1. Lock Acquisition - Acquire global trade lock
    2. Live Position Check - Query broker API for existing positions (CRITICAL)
    3. Balance Verification - Check available funds from broker
    4. Exposure Check - Verify 70% daily allocation limit
    5. Option Selection - Find appropriate contract
    6. Premium Validation - Ensure premium meets minimum
    7. Position Sizing - Calculate lots and cost
    8. Final Risk Check - Verify can_open_position
    9. Order Placement - Execute bracket order
    10. Trade Counting - Increment only on success
    
    Note: Does NOT rely on local cache for position verification.
          Only Angel One API is the source of truth.
    """
    # Acquire global lock to prevent simultaneous trades
    async with _TRADE_ENTRY_LOCK:
        logger.info("[%s] 🔒 Acquired trade entry lock", symbol)
        
        # 1. Check real-time positions from broker API (SINGLE SOURCE OF TRUTH)
        # Retry up to 3 times for API reliability
        logger.info("[%s] 🔍 Checking live positions from Angel One API...", symbol)
        live_positions = None
        for attempt in range(3):
            try:
                live_positions = await angel_client.get_positions()
                break  # Success, exit retry loop
            except Exception as e:
                logger.warning("[%s] ⚠️ Position check attempt %d/3 failed: %s", symbol, attempt + 1, e)
                if attempt < 2:  # Don't sleep on last attempt
                    await asyncio.sleep(1)  # Wait 1 second before retry
        
        # If all retries failed, block trade for safety
        if live_positions is None:
            logger.error("[%s] ❌ CRITICAL: Failed to verify positions after 3 attempts", symbol)
            send_telegram(
                f"❌ [{symbol}] Trade blocked\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Failed to verify positions from broker\n"
                f"Retried 3 times - blocking for safety"
            , broker="ANGEL")
            logger.info("[%s] 🔓 Released trade entry lock", symbol)
            return False
        
        # Check for existing positions
        has_position = False
        for pos in live_positions:
            netqty = int(pos.get('netqty', '0'))
            if netqty != 0:
                pos_symbol = pos.get('tradingsymbol', '')
                # Check if this position matches our symbol (underlying)
                # Example: BANKNIFTY matches BANKNIFTY30DEC2559300PE
                if symbol in pos_symbol or pos_symbol.startswith(symbol):
                    has_position = True
                    logger.error(
                        "[%s] ❌ Live position exists in broker: %s (Qty: %d)",
                        symbol,
                        pos_symbol,
                        netqty
                    )
                    send_telegram(
                        f"❌ [{symbol}] Trade blocked\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📊 Live Position Found:\n"
                        f"Symbol: {pos_symbol}\n"
                        f"Quantity: {netqty}\n"
                        f"P&L: ₹{float(pos.get('pnl', 0)):,.2f}\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"❌ Cannot open duplicate position"
                    , broker="ANGEL")
                    logger.info("[%s] 🔓 Released trade entry lock", symbol)
                    return False
        
        if not has_position:
            logger.info("[%s] ✅ No existing positions found in broker", symbol)
        
        # 2. Get current cash status and calculate limits
        balance_info = await cash_mgr.get_account_balance()
        available_funds = balance_info["available_funds"]
        
        # 3. Calculate available exposure based on 70% of daily start balance
        available_exposure = await cash_mgr.available_exposure()
        
        logger.info(
            "[%s] 💰 Balance check: Available funds: ₹%.2f | Available exposure (70%% limit): ₹%.2f",
            symbol,
            available_funds,
            available_exposure
        )
        
        # 4. Early exit if no exposure available
        if available_exposure <= 0:
            logger.error("[%s] ❌ No exposure available (70%% daily limit reached)", symbol)
            send_telegram(
                f"❌ [{symbol}] No exposure available\n"
                f"Daily allocation limit (70%) reached\n"
                f"Available funds: ₹{available_funds:,.2f}"
            , broker="ANGEL")
            logger.info("[%s] 🔓 Released trade entry lock", symbol)
            return False
        
        # Select option contract
        logger.info("[%s] 🔍 Selecting option contract...", symbol)
        opt_selection, reason = await find_option_contract_async(
            angel_client, symbol, bias, underlying_price
        )
        if not opt_selection:
            logger.error("[%s] ❌ Option selection failed: %s", symbol, reason)
            send_telegram(f"❌ {symbol} option selection failed: {reason}", broker="ANGEL")
            logger.info("[%s] 🔓 Released trade entry lock", symbol)
            return False

        logger.info("[%s] ✅ Selected option: %s", symbol, opt_selection.symbol)

        # Get option premium
        logger.info("[%s] 💰 Fetching option premium...", symbol)
        prem = await angel_client.get_last_price(opt_selection.symbol, exchange="NFO")
        if prem is None or prem < MIN_PREMIUM:
            logger.error(
                "[%s] ❌ Premium too low: ₹%s (min: ₹%.2f)", symbol, prem, MIN_PREMIUM
            )
            send_telegram(f"❌ {symbol} premium too low: ₹{prem}", broker="ANGEL")
            logger.info("[%s] 🔓 Released trade entry lock", symbol)
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

        # Check if we can open position (re-check with lock held)
        can_open = await cash_mgr.can_open_position(symbol, est_cost)
        if not can_open:
            logger.error("[%s] ❌ Insufficient funds or risk limit reached", symbol)
            available_exposure = await cash_mgr.available_exposure()
            send_telegram(
                f"❌ [{symbol}] Trade blocked\n"
                f"Required: ₹{est_cost:,.2f}\n"
                f"Available: ₹{available_exposure:,.2f}\n"
                f"Current balance: ₹{available_funds:,.2f}"
            , broker="ANGEL")
            logger.info("[%s] 🔓 Released trade entry lock", symbol)
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

        # Suppose `api` is your SmartAPI client implementing required methods
        from core.angelone.robo_order_manager import RoboOrderManager

        manager = RoboOrderManager(angel_client)

        bracket = await manager.place_robo_order(
            symbol=opt_selection.symbol,
            token=opt_selection.token,
            quantity=qty * lot_size,
            side="BUY",
            sl_points=stop_price,
            target_points=target_price,
        )

        if bracket is None:
            logger.error("[%s] ❌ Order placement failed", symbol)
            send_telegram(f"❌ {symbol} order placement failed", broker="ANGEL")
            cash_mgr.force_release(symbol)
            logger.info("[%s] 🔓 Released trade entry lock", symbol)
            return False

        # Store IDs for OCO management
        # Note: place_bracket_order in client.py returns dict with keys: entry_order_id, sl_order_id, target_order_id
        ACTIVE_OCO_ORDERS[symbol] = bracket
        
        # Increment trade count only after successful order placement
        cash_mgr.increment_trade_count()

        logger.info("[%s] ✅ Order placed successfully! (Trade #%d)", symbol, cash_mgr.total_trades_today)
        
        # Get post-trade balance summary
        balance_info_post = await cash_mgr.get_account_balance()
        available_exposure_post = await cash_mgr.available_exposure()
        open_positions_count = len(cash_mgr.open_positions)
        
        send_telegram(
            f"✅ Entered {symbol} {bias}\n"
            f"Option: {opt_selection.symbol}\n"
            f"Entry: ₹{prem:.2f} | SL: ₹{stop_price:.2f} | TP: ₹{target_price:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Cash Summary:\n"
            f"Position Cost: ₹{est_cost:,.2f}\n"
            f"Available Funds: ₹{balance_info_post['available_funds']:,.2f}\n"
            f"Available Exposure: ₹{available_exposure_post:,.2f}\n"
            f"Open Positions: {open_positions_count}"
        , broker="ANGEL")

        logger.info("[%s] 🔓 Released trade entry lock", symbol)
        
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
async def search_5m_entry(
    symbol, bias, angel_client, cash_mgr, bar_manager, context="ENTRY"
):
    """
    Search for 5m entry confirmation over multiple candles for Angel One.
    This function assumes caller ensured market is open (via wait_for_market or checking MARKET_OPEN_STATE).
    """
    checks = 0

    while checks < MAX_5M_CHECKS and not _STOP_EVENT.is_set():
        checks += 1

        # Check market state quickly; abort if closed or stopping
        if not MARKET_OPEN_STATE:
            logger.info(
                "[%s] 💤 Market not active, aborting %s entry search", symbol, context
            )
            return False

        now_ist = get_ist_now()

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

        # Abort if stop requested or market closed while waiting
        if _STOP_EVENT.is_set() or not MARKET_OPEN_STATE:
            logger.info(
                "[%s] 💤 Market closed after wait, aborting %s entry search",
                symbol,
                context,
            )
            return False

        # Get fresh data
        now_utc = get_ist_now().astimezone(pytz.UTC).replace(tzinfo=None)
        df5_new, df15_new = await bar_manager.get_resampled(current_time=now_utc)
        if df5_new.empty or df15_new.empty:
            logger.debug("[%s] ⚠️ Empty dataframe at 5m check #%d", symbol, checks)
            continue

        # Revalidate 15m bias
        bias_now = detect_15m_bias(df15_new, symbol=symbol)
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
            , broker="ANGEL")
            return False

        # Check 5m entry
        entry_ok, details = detect_5m_entry(df5_new, bias, symbol=symbol)
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

        # Get underlying price for option selection
        # For indices: Use SPOT index price (not futures) for strike selection
        # Note: Bar data and signals are based on futures price for better volume/liquidity
        if symbol in INDEX_FUTURES:
            logger.info("[%s] 📊 Fetching spot index price for option selection...", symbol)
            underlying = await angel_client.get_index_spot_price(symbol)
        else:
            logger.info("[%s] 📊 Fetching stock price...", symbol)
            underlying = await angel_client.get_last_price(symbol, exchange="NSE")

        if not underlying:
            logger.error("[%s] ❌ Failed to get underlying price", symbol)
            send_telegram(f"❌ {symbol} failed to get underlying price", broker="ANGEL")
            return False

        logger.info("[%s] 💰 Underlying price: ₹%.2f", symbol, underlying)

        # Execute order
        success = await execute_entry_order(
            symbol, bias, angel_client, cash_mgr, underlying
        )
        return success

    logger.info("[%s] ⛔ No %s entry after %d checks", symbol, context, checks)
    return False


# -----------------------------
# Startup Signal Check
# -----------------------------
async def handle_startup_signal(symbol, angel_client, cash_mgr, bar_manager):
    """
    Check for recent 15m signal on startup and search for entry if found.
    The function will no-op early if the market is not open.
    """
    try:
        # If market not open yet, skip startup signal handling
        if not MARKET_OPEN_STATE:
            logger.debug(
                "[%s] Startup: market not active, skipping startup signal check", symbol
            )
            return

        # Get current data
        now_utc = datetime.utcnow()
        df5_startup, df15_startup = await bar_manager.get_resampled(current_time=now_utc)
        if df5_startup.empty or df15_startup.empty:
            return

        # Detect 15m bias
        startup_bias = detect_15m_bias(df15_startup, symbol=symbol)
        if not startup_bias:
            return

        # Calculate how old this signal is
        latest_15m_time = df15_startup.index[-1]

        # Ensure timezone compatibility
        if latest_15m_time.tzinfo is None:
            # Assume naive time is UTC (as per bar_manager storage)
            latest_15m_time = pytz.utc.localize(latest_15m_time)

        now_utc = get_ist_now().astimezone(pytz.UTC)
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
            , broker="ANGEL")

            # Search for 5m entry
            await search_5m_entry(
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

    # 1. Check for startup signals (only if market open)
    if MARKET_OPEN_STATE:
        await handle_startup_signal(symbol, angel_client, cash_mgr, bar_manager)

    # 2. Main Loop
    while not _STOP_EVENT.is_set():
        try:
            # Wait until market opens (returns False if stop requested)
            ok = await wait_for_market()
            if not ok:
                break

            # Market is open — run monitor cycle
            now_ist = get_ist_now()

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

            # Quick re-check; abort cycle if closing or stopping
            if _STOP_EVENT.is_set() or not MARKET_OPEN_STATE:
                continue

            # Convert IST to UTC for bar_manager
            now_utc = get_ist_now().astimezone(pytz.UTC).replace(tzinfo=None)
            df5, df15 = await bar_manager.get_resampled(current_time=now_utc)

            if df15.empty:
                logger.warning("[%s] ⚠️ No 15m data available, waiting...", symbol)
                await sleep_until_next(60)
                continue

            # Detect 15m bias
            logger.info(
                "[%s] 🔍 Checking 15m bias at %s IST (total bars accumulated: 5m=%d, 15m=%d)...",
                symbol,
                get_ist_now().strftime("%H:%M:%S"),
                len(df5),
                len(df15),
            )
            bias = detect_15m_bias(df15, symbol=symbol)

            if not bias:
                continue

            logger.info(
                "[%s] 🎯 NEW 15m signal: %s at %s IST - Starting 5m entry search...",
                symbol,
                bias,
                get_ist_now().strftime("%H:%M:%S"),
            )
            send_telegram(
                f"📊 [{symbol}] 15m Trend: {bias} at {get_ist_now().strftime('%H:%M')} IST. Looking for 5m entry..."
            , broker="ANGEL")

            await search_5m_entry(
                symbol, bias, angel_client, cash_mgr, bar_manager, "ENTRY"
            )

            # If we currently hold a position we should still manage OCOs and monitor closure
            if symbol in cash_mgr.open_positions:
                # 1. Manage OCO (Cancel SL/TP if other fills)
                await manage_oco_orders(symbol, angel_client)

                # 2. Check positions via API occasionally
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
                        # Continue monitoring OCO & positions
                        await sleep_until_next(MONITOR_INTERVAL)
                except Exception as e:
                    logger.error(f"Error checking positions: {e}")
                    await sleep_until_next(60)

        except Exception as e:
            logger.exception("[%s] ❌ Signal monitor exception: %s", symbol, e)
            await sleep_until_next(60)

    logger.info("[%s] 👋 Signal monitor stopped", symbol)


async def calculate_wait_time(current_time, start_time, end_time, is_weekday, now_ist):
    """Calculate wait time until next market open"""
    if current_time >= end_time or not is_weekday:
        # Wait until tomorrow 09:00
        next_start = datetime.combine(now_ist.date() + timedelta(days=1), start_time)
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
        f"💤 Market closed. Sleeping {wait_hours:.1f} hours until "
        f"{next_start.strftime('%Y-%m-%d %H:%M')} IST (09:00 market open)"
    )

    return wait_seconds


# -----------------------------
# Main Worker Function
# -----------------------------
async def run_angel_workers():
    """Initialize and run all Angel One worker tasks in a daily loop"""

    logger.info("🤖 Bot process started")
    init_audit_file()

    # Start heartbeat task immediately and continuously
    heartbeat = asyncio.create_task(heartbeat_task())

    # Start market watcher immediately
    market_watcher_task = asyncio.create_task(market_state_watcher())

    try:
        while not _STOP_EVENT.is_set():
            try:
                now_ist = get_ist_now()
                current_time = now_ist.time()

                # Define active window: 09:00 to 15:30
                start_time = time(9, 0)
                end_time = time(15, 30)

                # Check if we are in the active window (Mon-Fri, 09:00-15:30)
                is_weekday = now_ist.weekday() <= 4  # 0=Mon, 4=Fri
                is_active_window = is_weekday and (
                    start_time <= current_time < end_time
                )

                if not is_active_window:
                    # Calculate wait time until next start (09:00 AM)
                    wait_seconds = await calculate_wait_time(
                        current_time, start_time, end_time, is_weekday, now_ist
                    )

                    # Sleep in chunks but respect _STOP_EVENT
                    while wait_seconds > 0 and not _STOP_EVENT.is_set():
                        sleep_chunk = min(wait_seconds, 60)
                        await asyncio.sleep(sleep_chunk)
                        wait_seconds -= sleep_chunk

                    if _STOP_EVENT.is_set():
                        break

                # Before proceeding, ensure market watcher has set market state
                if not MARKET_OPEN_STATE:
                    # Wait for watcher to signal open (or stop)
                    ok = await wait_for_market()
                    if not ok:
                        break

                # 🌅 Start Daily Cycle
                logger.info("🌅 Starting daily trading cycle...")
                send_telegram("🌅 [Angel] Bot waking up for trading day...", broker="ANGEL")

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

                logger.info("✅ Connected to Angel")
                send_telegram("✅ Connected to Angel", broker="ANGEL")

                # Wait for portfolio sync
                logger.info("⏳ Waiting 5s for portfolio sync...")
                await asyncio.sleep(5)

                # Initialize BarManagers
                bar_managers = {}
                logger.info("Initializing BarManagers and loading historical data...")

                for symbol in ANGEL_SYMBOLS:
                    bar_mgr = BarManager(symbol, max_bars=2880)
                    bar_managers[symbol] = bar_mgr

                    # Load historical data
                    logger.info("[%s] Loading historical data...", symbol)
                    df_hist = await angel_client.req_historic_1m(
                        symbol, duration_days=2
                    )
                    if df_hist is not None and not df_hist.empty:
                        await bar_mgr.initialize_from_historical(df_hist)
                        logger.info(
                            "[%s] Loaded %d historical bars", symbol, len(df_hist)
                        )
                    else:
                        logger.warning("[%s] Failed to load historical data", symbol)

                # Check for existing open positions on startup (e.g., after restart)
                logger.info("Checking for existing open positions...")
                try:
                    positions = await angel_client.get_positions()
                    if positions:
                        for pos in positions:
                            symbol_name = pos.get("tradingsymbol", "")
                            # Check if this is an option position for our tracked symbols
                            for tracked_symbol in ANGEL_SYMBOLS:
                                if tracked_symbol in symbol_name:
                                    qty = int(pos.get("netqty", 0))
                                    if qty != 0:  # Open position
                                        # Calculate position value (qty * avg_price)
                                        avg_price = float(pos.get("totalbuyavgprice", 0) or pos.get("totalsellavgprice", 0))
                                        position_value = abs(qty * avg_price)
                                        
                                        # Register the position if not already tracked
                                        if tracked_symbol not in cash_mgr.open_positions:
                                            cash_mgr.open_positions[tracked_symbol] = position_value
                                            logger.info(
                                                f"Registered existing position: {tracked_symbol} "
                                                f"({symbol_name}) @ ₹{position_value:,.2f}"
                                            )
                                        break
                        
                        if cash_mgr.open_positions:
                            total_locked = sum(cash_mgr.open_positions.values())
                            logger.info(f"Total capital locked in existing positions: ₹{total_locked:,.2f}")
                            send_telegram(
                                f"🔄 **Startup Position Check**\n"
                                f"Found {len(cash_mgr.open_positions)} open position(s)\n"
                                f"Capital Locked: ₹{total_locked:,.2f}"
                            , broker="ANGEL")
                except Exception as e:
                    logger.error(f"Error checking existing positions: {e}")

                # Pre-market check (must be done AFTER checking existing positions)
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
                for symbol in ANGEL_SYMBOLS:
                    token = angel_client.get_symbol_token(symbol, "NSE")
                    if token:
                        ws_client.add_symbol(symbol, token, "NSE")
                    else:
                        logger.error(f"Could not find token for {symbol}")

                # Start WebSocket Thread
                import threading

                ws_thread = threading.Thread(target=ws_client.connect, daemon=True)
                ws_thread.start()

                # Launch Workers as tasks (so they can be cancelled individually)
                tasks = []

                # 1. EOD Scheduler
                tasks.append(
                    asyncio.create_task(
                        eod_scheduler_task(cash_mgr, angel_client, bar_managers)
                    )
                )

                # 2. Signal Monitors (one per symbol)
                logger.info("🚀 Starting signal monitors...")
                for symbol in ANGEL_SYMBOLS:
                    bar_mgr = bar_managers.get(symbol)
                    tasks.append(
                        asyncio.create_task(
                            angel_signal_monitor(
                                symbol, angel_client, cash_mgr, bar_mgr
                            )
                        )
                    )

                send_telegram("🚀 Angel Broker Bot Started (LIVE TRADING)", broker="ANGEL")

                # Wait for all tasks to complete or for _STOP_EVENT
                # We await gather here; tasks should honor _STOP_EVENT and exit cleanly.
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
                send_telegram(
                    f"🚨 CRITICAL: Angel Bot daily loop error: {str(e)[:100]}"
                , broker="ANGEL")
                await sleep_until_next(60)

        # Main while loop end
    finally:
        # Ensure all background tasks are stopped
        logger.info("Shutting down run_angel_workers, setting _STOP_EVENT")
        _STOP_EVENT.set()

        # Cancel market watcher and heartbeat if still running
        if not market_watcher_task.done():
            market_watcher_task.cancel()
            try:
                await market_watcher_task
            except Exception:
                pass

        if not heartbeat.done():
            heartbeat.cancel()
            try:
                await heartbeat
            except Exception:
                pass

    logger.info("run_angel_workers exiting")


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
            send_telegram("🔔 BOT started outside NSE market hours", broker="ANGEL")
        else:
            logger.info("🔔 BOT started during NSE market hours")
            send_telegram("🔔 BOT started during NSE market hours", broker="ANGEL")
        return

    if is_open != _LAST_MARKET_OPEN_STATE:
        if is_open:
            send_telegram("🔔 NSE Market is OPEN", broker="ANGEL")
        else:
            send_telegram("🛑 NSE Market is CLOSED", broker="ANGEL")
        _LAST_MARKET_OPEN_STATE = is_open
