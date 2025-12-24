# core/angelone/orb_worker_angel.py
"""
ORB (Opening Range Breakout) Worker for Angel One.

Trades Nifty 50 and Bank Nifty options using ORB strategy:
1. Build ORB range from first 30 minutes (9:15 - 9:45 IST)
2. Enter on valid breakout (entire candle outside ORB)
3. SL/TP based on ATR and ORB range with 1:1.5 risk-reward
4. Force exit 15 minutes before market close (15:15 IST)
5. Only one trade per day per symbol
"""

import asyncio
from datetime import datetime, time, timedelta
from typing import Dict
import pytz

from core.logger import logger
from core.config import (
    ANGEL_SYMBOLS,
    ANGEL_INDEX_FUTURES,
    ORB_DURATION_MINUTES,
    ORB_ATR_LENGTH,
    ORB_ATR_MULTIPLIER,
    ORB_RISK_REWARD,
    ORB_MAX_ENTRY_HOUR,
    ORB_MAX_ENTRY_MINUTE,
    ORB_BREAKOUT_TIMEFRAME,
    NSE_MARKET_OPEN_HOUR,
    NSE_MARKET_OPEN_MINUTE,
    NSE_MARKET_CLOSE_HOUR,
    NSE_MARKET_CLOSE_MINUTE,
    ANGEL_TIMEZONE,
)
from core.orb_signal_engine import (
    calculate_atr,
    calculate_orb_range,
    calculate_orb_risk,
    detect_orb_breakout,
    get_orb_sl_tp,
    check_orb_trade_allowed,
    should_force_exit,
    resample_to_timeframe,
    get_seconds_until_next_candle,
)
from core.utils import send_telegram, write_audit_row
from core.bar_manager import BarManager
from core.angelone.client import AngelClient
from core.angelone.option_selector import find_option_contract_async
from core.angelone.utils import is_market_open, get_ist_now
from core.scheduler import run_strategy_loop


# -----------------------------
# Global State
# -----------------------------
_STOP_EVENT = asyncio.Event()
ORB_TRADE_TAKEN_TODAY: Dict[str, bool] = {}  # Track if trade taken per symbol
ORB_ACTIVE_POSITIONS: Dict[str, Dict] = {}  # Track active positions per symbol

# IST timezone
IST = pytz.timezone(ANGEL_TIMEZONE)

# Market times
MARKET_OPEN_TIME = time(NSE_MARKET_OPEN_HOUR, NSE_MARKET_OPEN_MINUTE)
MARKET_CLOSE_TIME = time(NSE_MARKET_CLOSE_HOUR, NSE_MARKET_CLOSE_MINUTE)

# Cache for order book to avoid rate limiting (60 second TTL)
_ORDER_BOOK_CACHE = {"data": None, "timestamp": None}
_ORDER_BOOK_CACHE_TTL = 60  # seconds
_ORDER_BOOK_CACHE_LOCK = None  # asyncio.Lock, initialized at runtime

# Cache for positions to avoid rate limiting (60 second TTL)
_POSITIONS_CACHE = {"data": None, "timestamp": None}
_POSITIONS_CACHE_TTL = 60  # seconds
_POSITIONS_CACHE_LOCK = None  # asyncio.Lock, initialized at runtime


async def get_cached_order_book(angel_client) -> list:
    """
    Get order book with caching to avoid rate limiting.
    Cache is valid for 60 seconds. Uses lock to prevent concurrent refreshes.
    """
    import time as time_module
    global _ORDER_BOOK_CACHE, _ORDER_BOOK_CACHE_LOCK
    now = time_module.time()
    
    # Check if cache is valid (fast path, no lock needed)
    if (_ORDER_BOOK_CACHE["data"] is not None and 
        _ORDER_BOOK_CACHE["timestamp"] is not None and
        (now - _ORDER_BOOK_CACHE["timestamp"]) < _ORDER_BOOK_CACHE_TTL):
        logger.debug(f"Using cached order book (age: {int(now - _ORDER_BOOK_CACHE['timestamp'])}s)")
        return _ORDER_BOOK_CACHE["data"]
    
    # Cache expired or empty - need to refresh (acquire lock to prevent concurrent refreshes)
    async with _ORDER_BOOK_CACHE_LOCK:
        # Double-check after acquiring lock (another task may have just refreshed)
        now = time_module.time()
        if (_ORDER_BOOK_CACHE["data"] is not None and 
            _ORDER_BOOK_CACHE["timestamp"] is not None and
            (now - _ORDER_BOOK_CACHE["timestamp"]) < _ORDER_BOOK_CACHE_TTL):
            logger.debug(f"Cache refreshed by another task (age: {int(now - _ORDER_BOOK_CACHE['timestamp'])}s)")
            return _ORDER_BOOK_CACHE["data"]
        
        # Fetch fresh data (only one task does this)
        try:
            order_book = await angel_client.get_order_book()
            _ORDER_BOOK_CACHE["data"] = order_book
            _ORDER_BOOK_CACHE["timestamp"] = time_module.time()
            logger.debug("Fetched fresh order book from broker")
            return order_book
        except Exception as e:
            logger.error(f"Error fetching order book: {e}")
            # Return cached data even if stale, or empty list
            return _ORDER_BOOK_CACHE["data"] if _ORDER_BOOK_CACHE["data"] is not None else []


async def get_cached_positions(angel_client) -> list:
    """
    Get positions with caching to avoid rate limiting.
    Cache is valid for 60 seconds. Uses lock to prevent concurrent refreshes.
    """
    import time as time_module
    global _POSITIONS_CACHE, _POSITIONS_CACHE_LOCK
    now = time_module.time()
    
    # Check if cache is valid (fast path, no lock needed)
    if (_POSITIONS_CACHE["data"] is not None and 
        _POSITIONS_CACHE["timestamp"] is not None and
        (now - _POSITIONS_CACHE["timestamp"]) < _POSITIONS_CACHE_TTL):
        logger.debug(f"Using cached positions (age: {int(now - _POSITIONS_CACHE['timestamp'])}s)")
        return _POSITIONS_CACHE["data"]
    
    # Cache expired or empty - need to refresh (acquire lock to prevent concurrent refreshes)
    async with _POSITIONS_CACHE_LOCK:
        # Double-check after acquiring lock (another task may have just refreshed)
        now = time_module.time()
        if (_POSITIONS_CACHE["data"] is not None and 
            _POSITIONS_CACHE["timestamp"] is not None and
            (now - _POSITIONS_CACHE["timestamp"]) < _POSITIONS_CACHE_TTL):
            logger.debug(f"Cache refreshed by another task (age: {int(now - _POSITIONS_CACHE['timestamp'])}s)")
            return _POSITIONS_CACHE["data"]
        
        # Fetch fresh data (only one task does this)
        try:
            positions = await angel_client.get_positions()
            _POSITIONS_CACHE["data"] = positions
            _POSITIONS_CACHE["timestamp"] = time_module.time()
            logger.debug("Fetched fresh positions from broker")
            return positions
        except Exception as e:
            logger.error(f"Error fetching positions: {e}")
            # Return cached data even if stale, or empty list
            return _POSITIONS_CACHE["data"] if _POSITIONS_CACHE["data"] is not None else []


# -----------------------------
# Helper Functions
# -----------------------------
async def check_all_symbols_traded_today(symbols: list, angel_client) -> dict:
    """
    Batch check if any of the given symbols were traded today.
    Fetches order book once and checks all symbols against it.
    
    Args:
        symbols: List of symbols to check
        angel_client: AngelClient instance
        
    Returns:
        Dict mapping symbol to bool (True if traded today)
    """
    result = {symbol: False for symbol in symbols}
    
    try:
        # Get today's date
        today_str = get_ist_now().strftime("%d-%b-%Y").upper()  # Format: 23-DEC-2025
        
        # Use cached order book (already fetched during startup)
        order_book = await get_cached_order_book(angel_client)
        
        if not order_book:
            logger.debug("No order book data from cache")
            return result
        
        # Check all symbols against the order book
        for order in order_book:
            order_date = order.get('ordertime', '')
            if order_date:
                order_date_only = order_date.split(' ')[0].upper()
                
                if order_date_only == today_str:
                    trading_symbol = order.get('tradingsymbol', '')
                    logger.debug(f"Checking order: {trading_symbol} from {order_date_only}")
                    # Check which symbol this order belongs to
                    for symbol in symbols:
                        if trading_symbol.startswith(symbol) and not result[symbol]:
                            logger.info(f"[{symbol}] 💾 Found order from today in broker history: {trading_symbol}")
                            result[symbol] = True
        
        return result
        
    except Exception as e:
        logger.error(f"Error checking broker history for all symbols: {e}")
        # On error, return False for all
        return result


async def check_symbol_traded_today(symbol: str, angel_client: AngelClient) -> bool:
    """
    Check if we already placed an ORB entry order for this symbol today.
    Uses broker's order book as single source of truth.
    
    Returns:
        True if symbol was already traded today, False otherwise
    """
    try:
        from datetime import datetime
        
        # Get today's date
        today_str = get_ist_now().strftime("%d-%b-%Y").upper()  # Format: 23-DEC-2025
        
        # Get order book from broker (using cache to avoid rate limiting)
        order_book = await get_cached_order_book(angel_client)
        
        if not order_book:
            logger.debug(f"[{symbol}] No order book data from broker")
            return False
        
        # Check if any order for this symbol's options was placed today
        for order in order_book:
            order_date = order.get('ordertime', '')
            # ordertime format: "23-Dec-2025 09:30:15" - extract date part
            if order_date:
                order_date_only = order_date.split(' ')[0].upper()
                
                if order_date_only == today_str:
                    # Check if order is for this symbol's option
                    trading_symbol = order.get('tradingsymbol', '')
                    if trading_symbol.startswith(symbol):
                        logger.info(f"[{symbol}] 💾 Found order from today in broker history: {trading_symbol}")
                        return True
        
        return False
        
    except Exception as e:
        logger.error(f"[{symbol}] Error checking broker history: {e}")
        # On error, check local state as fallback
        return ORB_TRADE_TAKEN_TODAY.get(symbol, False)


def get_orb_end_time() -> datetime:
    """Get ORB period end time for today."""
    now = get_ist_now()
    orb_start = datetime.combine(now.date(), MARKET_OPEN_TIME)
    orb_end = orb_start + timedelta(minutes=ORB_DURATION_MINUTES)
    return IST.localize(orb_end)

    logger.info("🔄 ORB Daily state reset")


async def is_symbol_occupied(
    symbol: str, angel_client: AngelClient, include_local: bool = True, silent: bool = False
) -> bool:
    """
    Check if a symbol is already 'occupied' by an open position or active order.
    Ensures we don't duplicate trades on restarts.

    Args:
        symbol: The symbol to check.
        angel_client: The Angel One client instance.
        include_local: Whether to check local tracking (ORB_ACTIVE_POSITIONS).
                       Set to False when detecting if a position was closed on the broker.
        silent: If True, suppress shield log messages (used by cleanup task).
    """
    try:
        # 1. Local state check
        if include_local and symbol in ORB_ACTIVE_POSITIONS:
            if not silent:
                logger.debug(
                    f"[{symbol}] 🛡️ Symbol Shield: Occupied (Active in Local Memory). The bot is already tracking an active trade for this symbol."
                )
            return True

        # 2. Broker Positions check
        positions = await get_cached_positions(angel_client)
        for pos in positions:
            # For Angel, we check the underlying symbol match in positions
            # Usually positions are for the OPTION symbol, so we check if any netqty > 0
            if int(pos.get("netqty", 0)) != 0:
                # If we find any NFO position, we assume it's our trade for simplicity
                # or we can check the symbol name starts with our underlying
                if pos.get("symbolname") == symbol or pos.get(
                    "tradingsymbol", ""
                ).startswith(symbol):
                    if not silent:
                        logger.debug(
                            f"[{symbol}] 🛡️ Symbol Shield: Occupied (Broker Position Found: {pos.get('tradingsymbol')})"
                        )
                    return True

        # 3. Broker Order Book check (pending orders)
        order_book = await get_cached_order_book(angel_client)
        for order in order_book:
            status = order.get("status", "")
            if status in ["ordered", "trigger pending", "open"]:
                if order.get("symbolname") == symbol or order.get(
                    "tradingsymbol", ""
                ).startswith(symbol):
                    if not silent:
                        logger.debug(
                            f"[{symbol}] 🛡️ Symbol Shield: Occupied (Open Order Found: {order.get('tradingsymbol')})"
                        )
                    return True

        return False
    except Exception as e:
        logger.error(f"[{symbol}] Error checking occupancy: {e}")
        return True  # Err on the side of caution


async def recover_active_positions(angel_client: AngelClient):
    """
    Scan broker for any existing positions and populate local state.
    """
    try:
        logger.info("🔍 Scanning for existing Angel positions to recover state...")
        positions = await get_cached_positions(angel_client)
        for pos in positions:
            if int(pos.get("netqty", 0)) != 0:
                tradingsymbol = pos.get("tradingsymbol", "")
                # Find which of our symbols this belongs to
                for symbol in ANGEL_SYMBOLS:
                    if tradingsymbol.startswith(symbol):
                        logger.info(
                            f"[{symbol}] Recovered active position: {tradingsymbol}"
                        )
                        ORB_ACTIVE_POSITIONS[symbol] = {
                            "direction": (
                                "LONG" if int(pos.get("netqty")) > 0 else "SHORT"
                            ),
                            "option_symbol": tradingsymbol,
                            "qty": abs(int(pos.get("netqty"))),
                            "entry_time": get_ist_now(),
                        }
                        ORB_TRADE_TAKEN_TODAY[symbol] = True

                        # Send one-time notification on restart
                        send_telegram(
                            f"🔄 ORB Startup Recovery (ANGEL): {symbol}\n"
                            f"Found existing {ORB_ACTIVE_POSITIONS[symbol]['direction']} position on broker. Tracking active trade.",
                            broker="ANGEL",
                        )
    except Exception as e:
        logger.error(f"Error during Angel state recovery: {e}")


async def wait_for_orb_complete():
    """Wait until ORB period is complete."""
    orb_end = get_orb_end_time()

    while not _STOP_EVENT.is_set():
        now = get_ist_now()
        if now >= orb_end:
            logger.info("✅ ORB period complete")
            return True

        remaining = (orb_end - now).total_seconds()
        # Wait in small chunks to allow responsiveness
        wait_chunk = min(60, remaining)

        try:
            await asyncio.wait_for(
                asyncio.shield(_STOP_EVENT.wait()), timeout=wait_chunk
            )
            return False  # Stop event was set
        except asyncio.TimeoutError:
            continue
    return False


# -----------------------------
# Execute ORB Entry
# -----------------------------
async def execute_orb_entry(
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    angel_client: AngelClient,
    cash_mgr,
) -> bool:
    """
    Execute ORB entry order with SL/TP.

    Args:
        symbol: Trading symbol (NIFTY, BANKNIFTY)
        direction: "LONG" or "SHORT"
        entry_price: Entry price
        stop_loss: Stop loss price
        take_profit: Take profit price
        angel_client: Angel One client
        cash_mgr: Cash manager instance

    Returns:
        True if order placed successfully
    """
    try:
        # Determine option type
        option_type = "CE" if direction == "LONG" else "PE"

        if await is_symbol_occupied(symbol, angel_client):
            logger.warning(
                f"[{symbol}] Aborting entry: Symbol is already occupied by a position or open order."
            )
            return False

        logger.info(f"[{symbol}] 📥 ORB ENTRY: {direction} at {entry_price:.2f}")
        logger.info(f"[{symbol}]   Option Type: {option_type}")
        logger.info(f"[{symbol}]   SL: {stop_loss:.2f}, TP: {take_profit:.2f}")

        # Get underlying price for option selection
        # For indices (NIFTY/BANKNIFTY), always use SPOT price for option selection
        # even though we used futures for ORB strategy confirmation
        if symbol in ANGEL_INDEX_FUTURES:
            # Use spot index price (NSE) for option selection
            ltp = await angel_client.get_last_price(symbol, exchange="NSE")
            logger.debug(f"[{symbol}] Using SPOT price for option selection: {ltp}")
        else:
            # For stocks, use direct price
            ltp = await angel_client.get_last_price(symbol)
        
        if not ltp:
            logger.error(f"[{symbol}] Failed to get LTP for option selection")
            return False

        # Convert direction to bias for option selector
        bias = "BULL" if direction == "LONG" else "BEAR"

        # Select option contract (current monthly expiry)
        option_selection, status = await find_option_contract_async(
            angel_client=angel_client,
            symbol=symbol,
            bias=bias,
            underlying_price=ltp,
        )

        if not option_selection or status != "ok":
            logger.error(f"[{symbol}] Failed to select option contract: {status}")
            return False

        logger.info(
            f"[{symbol}]   Selected: {option_selection.symbol} Strike: {option_selection.strike}"
        )

        # Get option LTP
        option_ltp = await angel_client.get_ltp(option_selection.token, exchange="NFO")
        if not option_ltp or option_ltp <= 0:
            logger.error(f"[{symbol}] Failed to get option LTP")
            return False

        # Calculate quantity based on cash allocation (70% of initial balance rule)
        available_exposure = await cash_mgr.available_exposure()

        # We allocate a portion of the available exposure to this specific trade
        # For example, if we have 7 symbols, we might want to split the 70% allocation.
        # But the USER said "use only 70% balance from the initial cash" for the order.
        # So we'll use the available exposure directly for this order's capacity.
        qty = max(
            1,
            int(available_exposure / (option_ltp * option_selection.lot_size)),
        )

        logger.debug(f"[{symbol}] Option LTP: ₹{option_ltp:.2f}, Calculated qty: {qty}")

        # Calculate SL/TP in option premium points using a Delta approximation (0.7 for ITM)
        # logic: option_ltp ± (underlying_points_distance * delta)
        delta = 0.7
        underlying_risk_pts = abs(entry_price - stop_loss)

        # 1. Technical (Technical points based)
        tech_option_sl = option_ltp - (underlying_risk_pts * delta)

        # 2. Risk Management (Cap risk at 50% of premium)
        max_premium_risk_pct = 0.50
        min_option_sl = option_ltp * (1 - max_premium_risk_pct)

        # Final Option SL: Technical with a Floor
        option_sl_price = max(
            tech_option_sl, min_option_sl, 0.1
        )  # 0.1 is absolute floor for Angel

        # 3. Recalibrate TP based on the REALIZED premium risk and RR ratio (ORB_RISK_REWARD)
        realized_premium_risk = option_ltp - option_sl_price
        option_tp_price = option_ltp + (realized_premium_risk * ORB_RISK_REWARD)

        logger.info(f"[{symbol}] 📋 REALISTIC OPTION LEVELS")
        logger.info(f"[{symbol}]   Premium Entry: ₹{option_ltp:.2f}")
        logger.info(
            f"[{symbol}]   Premium Risk: ₹{realized_premium_risk:.2f} (Capped at {max_premium_risk_pct*100}%)"
        )
        logger.info(
            f"[{symbol}]   Option SL Price: ₹{option_sl_price:.2f}, TP: ₹{option_tp_price:.2f} (RR: 1:{ORB_RISK_REWARD})"
        )

        # Check if we can open this position (risk limits and balance)
        total_qty = qty * option_selection.lot_size
        estimated_cost = option_ltp * total_qty
        
        can_trade = await cash_mgr.can_open_position(symbol, estimated_cost)
        if not can_trade:
            logger.error(
                f"[{symbol}] ❌ Cannot open position due to risk limits or insufficient balance"
            )
            send_telegram(
                f"❌ Order Rejected: {symbol}\nRisk limit exceeded or insufficient balance\nCost: ₹{estimated_cost:.2f}",
                broker="ANGEL"
            )
            return False
        logger.info(f"[{symbol}] ✅ Risk check passed. Estimated cost: ₹{estimated_cost:.2f}")

        # Place bracket order
        order_result = await angel_client.place_bracket_order(
            option_symbol=option_selection.symbol,
            option_token=option_selection.token,
            quantity=total_qty,
            stop_loss_price=round(option_sl_price, 1),
            target_price=round(option_tp_price, 1),
        )

        if order_result and order_result.get("status") == "success":
            # Track position with bracket order details
            ORB_ACTIVE_POSITIONS[symbol] = {
                "direction": direction,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "option_symbol": option_selection.symbol,
                "option_token": option_selection.token,
                "qty": qty,
                "entry_time": get_ist_now(),
                "entry_order_id": order_result.get("entry_order_id"),
                "sl_order_id": order_result.get("sl_order_id"),
                "target_order_id": order_result.get("target_order_id"),
            }
            ORB_TRADE_TAKEN_TODAY[symbol] = True

            # Telegram notification
            msg = (
                f"🎯 ORB ENTRY: {symbol} {direction}\n"
                f"Option: {option_selection.symbol}\n"
                f"Entry: ₹{entry_price:.2f}\n"
                f"SL: ₹{stop_loss:.2f} | TP: ₹{take_profit:.2f}\n"
                f"Qty: {qty}"
            )
            send_telegram(msg, broker="ANGEL")
            
            # Send portfolio update after entry
            await send_portfolio_summary_telegram(angel_client, "AFTER ENTRY")
            
            logger.info(f"[{symbol}] ✅ ORB Entry order placed successfully")

            # Audit log
            write_audit_row(
                timestamp=get_ist_now().isoformat(),
                symbol=symbol,
                bias=direction,
                option=option_selection.symbol,
                entry_price=entry_price,
                stop=stop_loss,
                target=take_profit,
                details={
                    "strategy": "ORB",
                    "qty": qty,
                    "option_strike": option_selection.strike,
                }
            )

            return True
        else:
            logger.error(f"[{symbol}] ORB Entry order failed: {order_result}")
            return False

    except Exception as e:
        logger.exception(f"[{symbol}] ORB Entry error: {e}")
        send_telegram(f"❌ ORB Entry Error ({symbol}): {str(e)[:100]}", broker="ANGEL")
        return False


# -----------------------------
# Force Exit Position
# -----------------------------
async def force_exit_position(
    symbol: str, angel_client: AngelClient, reason: str = "EOD"
):
    """
    Force close any open position for symbol.
    
    Steps:
    1. Cancel all open orders (SL and TP)
    2. Close position with market order
    3. Clean up tracking
    """
    # Check if we have local tracking for this position
    has_local_tracking = symbol in ORB_ACTIVE_POSITIONS
    position = ORB_ACTIVE_POSITIONS.get(symbol, {})
    
    # Even if not in local tracking, check broker for actual positions
    positions = await get_cached_positions(angel_client)
    broker_position = None
    
    for pos in positions:
        trading_symbol = pos.get("tradingsymbol", "")
        # Check if this position belongs to our symbol (starts with symbol name)
        if trading_symbol.startswith(symbol) and int(pos.get("netqty", 0)) != 0:
            broker_position = pos
            break
    
    # If no position found anywhere, nothing to exit
    if not has_local_tracking and not broker_position:
        logger.debug(f"[{symbol}] No position to force exit (not in tracking or broker)")
        return
    
    logger.warning(
        f"[{symbol}] ⚠️ FORCE EXIT ({reason}): Closing {'tracked' if has_local_tracking else 'broker'} position"
    )

    try:
        # Step 1: Cancel SL and TP orders (only if we have local tracking)
        cancelled_count = 0
        
        if has_local_tracking:
            # Cancel stop-loss order
            sl_order_id = position.get("sl_order_id")
            if sl_order_id:
                try:
                    await asyncio.to_thread(
                        angel_client.smart_api.cancelOrder, sl_order_id, "STOPLOSS"
                    )
                    cancelled_count += 1
                    logger.debug(f"[{symbol}] Cancelled SL order: {sl_order_id}")
                except Exception as e:
                    logger.warning(f"[{symbol}] Failed to cancel SL order {sl_order_id}: {e}")
            
            # Cancel target order
            target_order_id = position.get("target_order_id")
            if target_order_id:
                try:
                    await asyncio.to_thread(
                        angel_client.smart_api.cancelOrder, target_order_id, "NORMAL"
                    )
                    cancelled_count += 1
                    logger.debug(f"[{symbol}] Cancelled Target order: {target_order_id}")
                except Exception as e:
                    logger.warning(f"[{symbol}] Failed to cancel Target order {target_order_id}: {e}")
            
            if cancelled_count > 0:
                logger.info(f"[{symbol}] Cancelled {cancelled_count} open order(s)")
                await asyncio.sleep(1)  # Give time for cancellations to process
        else:
            # No local tracking - try to cancel all orders for this symbol from broker
            logger.info(f"[{symbol}] No local tracking - checking broker for open orders to cancel")
            order_book = await get_cached_order_book(angel_client)
            for order in order_book:
                trading_symbol = order.get("tradingsymbol", "")
                order_status = order.get("status", "")
                if trading_symbol.startswith(symbol) and order_status in ["open", "pending", "trigger pending"]:
                    order_id = order.get("orderid")
                    variety = order.get("variety", "NORMAL")
                    try:
                        await asyncio.to_thread(
                            angel_client.smart_api.cancelOrder, order_id, variety
                        )
                        cancelled_count += 1
                        logger.debug(f"[{symbol}] Cancelled order: {order_id} ({variety})")
                    except Exception as e:
                        logger.warning(f"[{symbol}] Failed to cancel order {order_id}: {e}")
            
            if cancelled_count > 0:
                logger.info(f"[{symbol}] Cancelled {cancelled_count} broker order(s)")
                await asyncio.sleep(1)

        # Step 2: Close position with market order
        # Use broker position if we found one, otherwise use local tracking
        if broker_position:
            option_symbol = broker_position.get("tradingsymbol")
            option_token = broker_position.get("symboltoken")
            qty = abs(int(broker_position.get("netqty", 0)))
            side = "SELL" if int(broker_position.get("netqty", 0)) > 0 else "BUY"
        else:
            option_symbol = position.get("option_symbol")
            option_token = position.get("option_token")
            # Need to check actual position for qty
            qty = None
            side = None

        # If we have qty and side, close the position
        if qty and side and option_symbol and option_token:
            close_result = await angel_client.place_order(
                symbol=option_symbol,
                token=option_token,
                qty=qty,
                side=side,
                order_type="MARKET",
            )

            direction = position.get('direction', 'UNKNOWN') if has_local_tracking else ('LONG' if side == 'SELL' else 'SHORT')
            entry_price = position.get('entry_price', 0.0) if has_local_tracking else 0.0
            
            msg = (
                f"⚠️ ORB FORCE EXIT ({reason}): {symbol}\n"
                f"Option: {option_symbol}\n"
                f"Direction: {direction}\n"
            )
            if entry_price > 0:
                msg += f"Entry: ₹{entry_price:.2f}\n"
            msg += (
                f"Qty: {qty} (Action: {side})\n"
                f"Status: {'Success' if close_result else 'Failed'}"
            )
            send_telegram(msg, broker="ANGEL")
            logger.info(f"[{symbol}] ✅ Position closed with {side} market order")
        else:
            logger.warning(f"[{symbol}] Could not determine position details for closing")

        # Step 3: Remove from tracking if present
        if has_local_tracking:
            del ORB_ACTIVE_POSITIONS[symbol]
            logger.info(f"[{symbol}] Removed from active positions tracking")

    except Exception as e:
        logger.exception(f"[{symbol}] Force exit error: {e}")
        send_telegram(f"❌ Force Exit Error ({symbol}): {str(e)[:100]}", broker="ANGEL")


# -----------------------------
# ORB Signal Monitor
# -----------------------------
async def orb_signal_monitor(
    symbol: str,
    angel_client: AngelClient,
    cash_mgr,
    bar_manager: BarManager,
):
    """
    Monitor for ORB breakout signals for a symbol.

    Args:
        symbol: Trading symbol
        angel_client: Angel One client
        cash_mgr: Cash manager
        bar_manager: Bar manager for historical data
    """
    logger.debug(f"[{symbol}] 📊 ORB Signal Monitor started")

    orb_high = None
    orb_low = None
    orb_complete = False

    while not _STOP_EVENT.is_set():
        try:
            now = get_ist_now()

            # Check if market is open
            if not is_market_open():
                logger.debug(f"[{symbol}] Market closed, waiting...")
                await asyncio.sleep(60)
                continue

            # Check force exit time
            if should_force_exit(
                now, MARKET_CLOSE_TIME, exit_before_minutes=15, symbol=symbol
            ):
                logger.warning(f"[{symbol}] EOD Force exit time reached")
                send_telegram(
                    f"🕒 [{symbol}] ORB Strategy: EOD Force exit time reached (15:15 IST)",
                    broker="ANGEL",
                )
                await force_exit_position(symbol, angel_client, reason="EOD")
                break

            # --- PROACTIVE SYMBOL SHIELD ---
            # We only perform the 'shield' check if we haven't officially marked the trade as taken today.
            if not ORB_TRADE_TAKEN_TODAY.get(symbol, False):
                if await is_symbol_occupied(symbol, angel_client):
                    logger.info(
                        f"[{symbol}] 🛡️ Symbol Shield: Occupancy found (Position/Order on Broker). Marking trade as taken today."
                    )
                    ORB_TRADE_TAKEN_TODAY[symbol] = True

            # If trade taken, just sleep and check for EOD exit
            if ORB_TRADE_TAKEN_TODAY.get(symbol, False):
                # Check if position was actively closed on broker (occupied returns false while trade_taken is true)
                # CRITICAL: We pass include_local=False to check the actual broker state.
                if not await is_symbol_occupied(
                    symbol, angel_client, include_local=False
                ):
                    if symbol in ORB_ACTIVE_POSITIONS:
                        logger.info(
                            f"[{symbol}] 🏁 Trade detected as CLOSED on broker side."
                        )
                        send_telegram(
                            f"🏁 ORB Trade Closed (ANGEL): {symbol}. Position cleared on broker.",
                            broker="ANGEL",
                        )
                        
                        # Send portfolio update after exit
                        await send_portfolio_summary_telegram(angel_client, "AFTER EXIT")
                        del ORB_ACTIVE_POSITIONS[symbol]

                # Check for EOD exit
                if should_force_exit(
                    now, MARKET_CLOSE_TIME, exit_before_minutes=15, symbol=symbol
                ):
                    await force_exit_position(symbol, angel_client, reason="EOD")
                    break
                await asyncio.sleep(60)
                continue

            # Get historical data and resample to 30m for breakout detection
            df_1m = await bar_manager.get_bars_df(lookback_minutes=180)
            if df_1m.empty:
                logger.debug(f"[{symbol}] No bar data available")
                await asyncio.sleep(30)
                continue

            # Resample to 30-minute candles for higher conviction breakouts
            df = resample_to_timeframe(df_1m, ORB_BREAKOUT_TIMEFRAME)
            if df.empty:
                logger.debug(f"[{symbol}] No 30m bars available yet")
                await asyncio.sleep(30)
                continue

            # Calculate ORB range (only once after ORB period)
            if not orb_complete:
                orb_data = calculate_orb_range(
                    df=df_1m,  # Use 1m bars for precision
                    market_open_time=MARKET_OPEN_TIME,
                    orb_duration_minutes=ORB_DURATION_MINUTES,
                    symbol=symbol,
                )

                orb_high = orb_data.get("orb_high")
                orb_low = orb_data.get("orb_low")
                orb_complete = orb_data.get("orb_complete", False)

                if orb_complete and orb_high and orb_low:
                    msg = (
                        f"📊 ORB Range Set: {symbol}\n"
                        f"High: ₹{orb_high:.2f}\n"
                        f"Low: ₹{orb_low:.2f}\n"
                        f"Range: ₹{orb_high - orb_low:.2f}"
                    )
                    send_telegram(msg, broker="ANGEL")
                else:
                    # If failed to establish ORB range and it's getting late (e.g. > 10:15 AM), abort
                    if now.time() > time(10, 15):
                        logger.error(
                            f"[{symbol}] ❌ Failed to establish ORB range by 10:15 IST. Missing early data? Aborting for today."
                        )
                        break

                    sleep_sec = get_seconds_until_next_candle(
                        now, ORB_BREAKOUT_TIMEFRAME
                    )
                    logger.debug(
                        f"[{symbol}] Waiting {sleep_sec}s for ORB completion..."
                    )
                    await asyncio.sleep(sleep_sec)
                    continue

            # Check if trade already taken
            # Note: We rely on in-memory flag that was seeded from broker on startup
            # and gets set to True when we place an order. This avoids repeated API calls.
            if ORB_TRADE_TAKEN_TODAY.get(symbol, False):
                logger.debug(
                    f"[{symbol}] Trade already taken today, monitoring position"
                )
                await asyncio.sleep(60)
                continue

            # Check if within allowed entry hours
            allowed, reason = check_orb_trade_allowed(
                current_hour=now.hour,
                max_entry_hour=ORB_MAX_ENTRY_HOUR,
                trade_taken_today=ORB_TRADE_TAKEN_TODAY.get(symbol, False),
                symbol=symbol,
                current_minute=now.minute,
                max_entry_minute=ORB_MAX_ENTRY_MINUTE,
            )

            if not allowed:
                if reason == "past_max_entry_hour":
                    logger.info(f"[{symbol}] Past max entry time, stopping monitor")
                    send_telegram(
                        f"🏁 [{symbol}] ORB Strategy: Past max entry time ({ORB_MAX_ENTRY_HOUR:02d}:{ORB_MAX_ENTRY_MINUTE:02d}). Stopping monitor for today.",
                        broker="ANGEL",
                    )
                    break
                await asyncio.sleep(60)
                continue

            # --- BREAKOUT DETECTION ON LAST CLOSED BAR ---
            # Angel Resample uses label='right' (End Time).
            # So a completed bar ending at 10:00 is labeled 10:00.
            # Convert now to naive if needed, but assuming comparison works (both IST aware or naive).
            # Robust cutoff:
            # cutoff = now
            # completed_bars = df[df.index <= cutoff]

            # Note: df comes from resample_to_timeframe which might effectively return IST timestamps if BarManager uses IST.
            # Assuming safe comparison.

            # Convert cutoff to naive datetime to match DataFrame index
            cutoff = now.replace(tzinfo=None)
            completed_bars = df[df.index <= cutoff]

            if completed_bars.empty:
                logger.debug(f"[{symbol}] No completed bars found yet")
                await asyncio.sleep(30)
                continue

            # Detect breakout on the latest completed bar
            breakout_data = detect_orb_breakout(
                df=completed_bars,
                orb_high=orb_high,
                orb_low=orb_low,
                symbol=symbol,
            )

            breakout = breakout_data.get("breakout")

            if breakout:
                entry_price = breakout_data.get("price")
                atr = calculate_atr(df, period=ORB_ATR_LENGTH) or (
                    (orb_high - orb_low) / 2
                )
                risk_pts = calculate_orb_risk(
                    atr=atr,
                    orb_range=orb_high - orb_low,
                    atr_multiplier=ORB_ATR_MULTIPLIER,
                    symbol=symbol,
                )
                stop_loss, take_profit = get_orb_sl_tp(
                    entry_price=entry_price,
                    direction=breakout,
                    risk_pts=risk_pts,
                    rr_ratio=ORB_RISK_REWARD,
                    symbol=symbol,
                )

                # Execute entry
                success = await execute_orb_entry(
                    symbol=symbol,
                    direction=breakout,
                    entry_price=entry_price,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    angel_client=angel_client,
                    cash_mgr=cash_mgr,
                )

                if success:
                    ORB_TRADE_TAKEN_TODAY[symbol] = True
                    send_telegram(
                        f"✅ ORB Breakout (ANGEL): {symbol} {breakout}\nEntry: ₹{entry_price:.2f}",
                        broker="ANGEL",
                    )
                    break

            # Wait for next candle close
            sleep_sec = get_seconds_until_next_candle(now, ORB_BREAKOUT_TIMEFRAME)
            logger.debug(
                f"[{symbol}] Analysis complete. Sleeping {sleep_sec}s until next candle close..."
            )
            await asyncio.sleep(sleep_sec)

        except asyncio.CancelledError:
            logger.debug(f"[{symbol}] ORB Signal Monitor cancelled")
            break
        except Exception as e:
            logger.exception(f"[{symbol}] ORB Signal Monitor error: {e}")
            await asyncio.sleep(30)

    logger.debug(f"[{symbol}] 📊 ORB Signal Monitor stopped")


# -----------------------------


async def send_portfolio_summary_telegram(angel_client: AngelClient, title: str = "PORTFOLIO STATUS"):
    """
    Send portfolio summary to Telegram for Angel One.
    """
    try:
        # Get positions from Angel One
        positions = await get_cached_positions(angel_client)
        
        # Calculate totals
        total_pnl = 0
        position_value = 0
        active_positions = []
        
        for pos in positions:
            qty = int(pos.get('netqty', 0))
            if qty != 0:  # Only active positions
                symbol = pos.get('symbolname', 'Unknown')
                trading_symbol = pos.get('tradingsymbol', '')
                avg_price = float(pos.get('netavgprice', 0))
                ltp = float(pos.get('ltp', 0))
                pnl = float(pos.get('unrealisedprofitloss', 0))
                
                total_pnl += pnl
                position_value += abs(qty * ltp)
                
                active_positions.append({
                    'symbol': symbol,
                    'trading_symbol': trading_symbol,
                    'qty': qty,
                    'avg_price': avg_price,
                    'ltp': ltp,
                    'pnl': pnl
                })
        
        # Build message
        msg = f"📊 {title}\n"
        msg += f"={'='*40}\n"
        msg += f"📈 Positions: ₹{position_value:,.2f}\n"
        
        if active_positions:
            msg += f"\n📍 Active ({len(active_positions)}):"
            for pos in active_positions:
                pnl_emoji = "🟢" if pos['pnl'] >= 0 else "🔴"
                symbol_display = pos['symbol'] if len(pos['trading_symbol']) > 20 else pos['trading_symbol']
                msg += f"\n{pnl_emoji} {symbol_display}: {pos['qty']} @ ₹{pos['avg_price']:.2f} | P&L: ₹{pos['pnl']:+.2f}"
            
            overall_emoji = "🟢" if total_pnl >= 0 else "🔴"
            msg += f"\n\n{overall_emoji} Total P&L: ₹{total_pnl:+,.2f}"
        else:
            msg += f"\n📍 No positions"
        
        send_telegram(msg, broker="ANGEL")
        
    except Exception as e:
        logger.error(f"Error sending Angel One portfolio summary to Telegram: {e}")


async def position_cleanup_task(angel_client, interval=60):
    """
    Periodic cleanup task to detect manually closed positions.
    Runs every 60 seconds to check if tracked positions still exist on broker.
    If a position was manually closed, remove it from ORB_ACTIVE_POSITIONS.
    """
    logger.info("🧹 Position cleanup task started")
    try:
        while not _STOP_EVENT.is_set():
            await asyncio.sleep(interval)
            
            # Get list of symbols currently tracked
            tracked_symbols = list(ORB_ACTIVE_POSITIONS.keys())
            
            # Also check symbols with trade_taken flag but not in active positions
            trade_taken_symbols = [s for s in ORB_TRADE_TAKEN_TODAY.keys() if s not in tracked_symbols]
            all_symbols_to_check = tracked_symbols + trade_taken_symbols
            
            if all_symbols_to_check:
                logger.debug(f"🧹 Cleanup checking: {all_symbols_to_check}")
                # Check each symbol
                for symbol in all_symbols_to_check:
                    try:
                        # Check if position still exists on broker (include_local=False, silent=True)
                        still_occupied = await is_symbol_occupied(
                            symbol, angel_client, include_local=False, silent=True
                        )
                        
                        if not still_occupied:
                            # Position was closed (manually or via bot)
                            logger.info(
                                f"[{symbol}] 🧹 Cleanup: Position no longer on broker. Removing from tracking."
                            )
                            if symbol in ORB_ACTIVE_POSITIONS:
                                del ORB_ACTIVE_POSITIONS[symbol]
                            
                            # Also clear the trade taken flag to allow re-entry
                            if symbol in ORB_TRADE_TAKEN_TODAY:
                                del ORB_TRADE_TAKEN_TODAY[symbol]
                                logger.info(
                                    f"[{symbol}] 🔓 Cleanup: Cleared trade-taken flag. Symbol available for re-entry."
                                )
                        # Don't log "still occupied" - reduces noise since monitoring loops already check
                    except Exception as e:
                        logger.error(f"[{symbol}] Error in cleanup check: {e}")
                        
    except asyncio.CancelledError:
        logger.info("🧹 Position cleanup task cancelled")
    except Exception as e:
        logger.error(f"🧹 Position cleanup task error: {e}")
    finally:
        logger.info("🧹 Position cleanup task stopped")


async def heartbeat_task(interval=60):
    """Continuous heartbeat to show bot is alive."""
    logger.info("💓 Heartbeat task started")
    heartbeat_count = 0
    try:
        while not _STOP_EVENT.is_set():
            await asyncio.sleep(interval)
            heartbeat_count += 1
            now_ist = get_ist_now()
            logger.info(
                f"💓 Heartbeat #{heartbeat_count}: {now_ist.strftime('%H:%M:%S')} IST - Bot is alive"
            )
    except asyncio.CancelledError:
        logger.info("💓 Heartbeat task cancelled")
    except Exception as e:
        logger.error(f"💓 Heartbeat task error: {e}")
    finally:
        logger.info("💓 Heartbeat task stopped")


# ---------------------------------------------------------
# Daily Session Process Logic
# ---------------------------------------------------------


async def _async_daily_session():
    """Actual daily trading logic running inside the subprocess."""
    logger.info("🌅 Starting Daily Session (Subprocess)")
    ws_client = None
    angel_client = None

    try:
        from core.angelone.client import AngelClient
        from core.cash_manager import LiveCashManager
        from core.angelone.client import AngelWebSocket
        import threading

        # CRITICAL: Re-initialize stop event in the new process's event loop
        global _STOP_EVENT, _ORDER_BOOK_CACHE_LOCK, _POSITIONS_CACHE_LOCK
        _STOP_EVENT = asyncio.Event()
        _ORDER_BOOK_CACHE_LOCK = asyncio.Lock()
        _POSITIONS_CACHE_LOCK = asyncio.Lock()

        angel_client = AngelClient()
        await angel_client.connect_async()
        
        # Start position cleanup background task
        cleanup_task = asyncio.create_task(
            position_cleanup_task(angel_client, interval=60)
        )
        logger.info("🧹 Position cleanup task launched")
        
        # Send PRE-MARKET portfolio summary
        await send_portfolio_summary_telegram(angel_client, "PRE-MARKET")
        
        # Fetch order book ONCE and populate cache (single API call for all symbols)
        logger.info("📋 Fetching order book from broker to check today's trades...")
        try:
            order_book = await angel_client.get_order_book()
            # Populate cache immediately
            import time as time_module
            global _ORDER_BOOK_CACHE
            _ORDER_BOOK_CACHE["data"] = order_book
            _ORDER_BOOK_CACHE["timestamp"] = time_module.time()
            logger.info(f"✅ Order book cached ({len(order_book)} orders)")
        except Exception as e:
            logger.error(f"❌ Failed to fetch order book on startup: {e}")
            order_book = []
        
        # Now check which symbols were traded (uses cached data)
        traded_symbols = await check_all_symbols_traded_today(ANGEL_SYMBOLS, angel_client)
        for symbol, is_traded in traded_symbols.items():
            if is_traded:
                ORB_TRADE_TAKEN_TODAY[symbol] = True

        cash_mgr = LiveCashManager(angel_client, broker="ANGEL")
        await cash_mgr.check_and_log_start_balance()

        # Startup Recovery: Scan for existing positions
        await recover_active_positions(angel_client)

        # STRATEGY: Use futures for ORB breakout confirmation, SPOT for option selection
        # - NIFTY/BANKNIFTY: Use front-month futures for ORB range building and breakout detection
        # - Stocks: Use spot price directly (no futures)
        # - Option selection: Always use SPOT price (NSE) for strike selection
        
        # Resolve futures contracts (NFO) for indices only
        fut_contracts = {}
        for symbol in ANGEL_INDEX_FUTURES:
            contract = await angel_client.get_current_futures_contract(symbol)
            if contract:
                fut_contracts[symbol] = contract
            else:
                logger.error(
                    f"[{symbol}] Could not resolve futures contract. Using spot (NSE) as fallback."
                )

        # Create bar managers for each symbol
        bar_managers = {symbol: BarManager(symbol) for symbol in ANGEL_SYMBOLS}

        # Load historical data
        # - For indices: Use futures contract for ORB strategy
        # - For stocks: Use spot price directly
        for symbol, bar_mgr in bar_managers.items():
            logger.info(f"[{symbol}] Loading historical data...")
            try:
                # Use futures contract if available (indices only)
                if symbol in ANGEL_INDEX_FUTURES and symbol in fut_contracts:
                    fut_symbol = fut_contracts[symbol]["symbol"]
                    logger.info(
                        f"[{symbol}] Fetching historical data for future: {fut_symbol}"
                    )
                    df_hist = await angel_client.req_historic_1m(
                        fut_symbol, duration_days=5, exchange="NFO"
                    )
                else:
                    df_hist = await angel_client.req_historic_1m(
                        symbol, duration_days=5, exchange="NSE"
                    )

                if df_hist is not None and not df_hist.empty:
                    await bar_mgr.initialize_from_historical(df_hist)
                    logger.info(f"[{symbol}] Loaded {len(df_hist)} historical bars")
                else:
                    logger.warning(f"[{symbol}] No historical data found")
            except Exception as e:
                logger.error(f"[{symbol}] Failed to load historical data: {e}")

        # Start WebSocket
        logger.info("🚀 Starting Angel WebSocket...")
        try:
            ws_client = AngelWebSocket(
                auth_token=angel_client.auth_token,
                api_key=angel_client.api_key,
                client_code=angel_client.client_code,
                feed_token=angel_client.feed_token,
                bar_managers=bar_managers,
                loop=asyncio.get_running_loop(),
            )

            for symbol in ANGEL_SYMBOLS:
                if symbol in ANGEL_INDEX_FUTURES and symbol in fut_contracts:
                    token = fut_contracts[symbol]["token"]
                    exchange = "NFO"
                    disp_symbol = fut_contracts[symbol]["symbol"]
                else:
                    token = angel_client.get_symbol_token(symbol, "NSE")
                    exchange = "NSE"
                    disp_symbol = symbol

                if token:
                    ws_client.add_symbol(symbol, token, exchange)
                    logger.info(
                        f"[{symbol}] Subscribed to WebSocket: {disp_symbol} (Token: {token}, Exch: {exchange})"
                    )
                else:
                    logger.error(
                        f"[{symbol}] Could not find token for WebSocket subscription"
                    )

            ws_thread = threading.Thread(target=ws_client.connect, daemon=True)
            ws_thread.start()
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Failed to start WebSocket: {e}")

        # Wait for ORB to complete
        if await wait_for_orb_complete():
            # Start signal monitors
            tasks = []
            for symbol in ANGEL_SYMBOLS:
                tasks.append(
                    asyncio.create_task(
                        orb_signal_monitor(
                            symbol, angel_client, cash_mgr, bar_managers[symbol]
                        )
                    )
                )

            if tasks:
                await asyncio.gather(*tasks)

            # Send POST-MARKET portfolio summary
            await send_portfolio_summary_telegram(angel_client, "POST-MARKET")
            
            send_telegram(
                "✅ ORB Daily Session: All symbol monitors finished.", broker="ANGEL"
            )
        else:
            logger.info("ORB wait aborted (Bot stopping)")
            send_telegram("⚠️ ORB Daily Session: Aborted during wait.", broker="ANGEL")

    except Exception as e:
        logger.error(f"Error in daily monitoring session: {e}", exc_info=True)

    finally:
        # Cancel cleanup task
        if 'cleanup_task' in locals() and cleanup_task:
            cleanup_task.cancel()
            try:
                await cleanup_task
            except asyncio.CancelledError:
                logger.info("🧹 Position cleanup task cancelled")
        
        # Session Cleanup
        try:
            if ws_client:
                # Attempt to disable auto-reconnect
                if hasattr(ws_client.sws, "max_retry_attempt"):
                    ws_client.sws.max_retry_attempt = 0

                ws_client.sws.close_connection()
                logger.info("WebSocket closed")
        except Exception:
            pass

        if angel_client:
            try:
                angel_client.disconnect()
                logger.info("Client disconnected")
            except Exception as e:
                logger.warning(f"Client disconnect warning: {e}")

        logger.info("👋 Daily Session Finished (Subprocess exiting)")


def _run_session_in_process():
    """Entry point for the daily session subprocess."""
    try:
        asyncio.run(_async_daily_session())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f"Process error: {e}")


async def run_orb_angel_workers():
    """
    Main entry point for Angel One ORB strategy workers.
    Uses the shared scheduler to manage the daily loop and process isolation.
    """
    from core.config import (
        NSE_MARKET_OPEN_HOUR,
        NSE_MARKET_OPEN_MINUTE,
    )

    # We use 15:30 as close if not defined (NSE standard)
    close_h = 15
    close_m = 30

    # Check and log holidays at startup
    try:
        from core.holiday_checker import get_upcoming_nse_holidays, is_nse_trading_day, get_next_nse_trading_day
        from datetime import datetime
        import pytz
        
        ist = pytz.timezone("Asia/Kolkata")
        today = datetime.now(ist)
        
        # Check if today is a holiday
        if not is_nse_trading_day(today):
            logger.info(f"📅 Today ({today.strftime('%Y-%m-%d %A')}) is an NSE holiday")
            next_trading_day = get_next_nse_trading_day(today)
            logger.info(f"📅 Next NSE trading day: {next_trading_day.strftime('%Y-%m-%d %A')}")
            send_telegram(
                f"📅 Today is NSE Holiday\n"
                f"Next trading day: {next_trading_day.strftime('%Y-%m-%d %A')}",
                broker="ANGEL",
            )
        
        # Log upcoming holidays (next 30 days)
        upcoming = get_upcoming_nse_holidays(30)
        if upcoming:
            logger.info(f"📅 Upcoming NSE holidays (next 30 days): {len(upcoming)} holiday(s)")
            for holiday_date, name in upcoming[:3]:  # Show first 3
                logger.info(f"   • {holiday_date.strftime('%Y-%m-%d %A')}: {name}")
    except Exception as e:
        logger.warning(f"Failed to check NSE holidays at startup: {e}")

    # Send Startup Message
    send_telegram(
        f"🚀 Angel One ORB Bot Starting\n"
        f"Symbols: {', '.join(ANGEL_SYMBOLS)}\n"
        f"ORB Duration: {ORB_DURATION_MINUTES} minutes\n"
        f"R:R Ratio: 1:{ORB_RISK_REWARD}",
        broker="ANGEL",
    )

    await run_strategy_loop(
        broker_name="ANGEL",
        strategy_name="ORB",
        session_func=_run_session_in_process,
        stop_event=_STOP_EVENT,
        market_open_hour=NSE_MARKET_OPEN_HOUR,
        market_open_minute=NSE_MARKET_OPEN_MINUTE,
        market_close_hour=close_h,
        market_close_minute=close_m,
        timezone_str="Asia/Kolkata",
        pre_connect_minutes=30,
        process_isolation=True,
        heartbeat_func=heartbeat_task,
    )


def stop_orb_angel_workers():
    """Signal ORB workers to stop."""
    _STOP_EVENT.set()
    logger.info("🛑 Stop signal sent to ORB Angel workers")
