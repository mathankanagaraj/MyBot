# core/ibkr/orb_worker_ibkr.py
"""
ORB (Opening Range Breakout) Worker for IBKR.

Trades SPX and NDX index options using ORB strategy:
1. Build ORB range from first 30 minutes (9:30 - 10:00 AM ET)
2. Enter on valid breakout (entire candle outside ORB)
3. Uses 0 DTE (same-day expiry) index options
4. SL/TP based on ATR and ORB range with 1:1.5 risk-reward
5. Force exit 15 minutes before market close (3:45 PM ET)
6. Only one trade per day per symbol
"""

import asyncio
import math
from datetime import datetime, time, timedelta
from typing import Dict, Optional
import pytz

from ib_async import Index, Option, FuturesOption

from core.logger import logger
from core.config import (
    ORB_IBKR_SYMBOLS,
    ORB_DURATION_MINUTES,
    ORB_ATR_LENGTH,
    ORB_ATR_MULTIPLIER,
    ORB_RISK_REWARD,
    ORB_MAX_ENTRY_HOUR,
    ORB_BREAKOUT_TIMEFRAME,
    US_MARKET_OPEN_HOUR,
    US_MARKET_OPEN_MINUTE,
    US_MARKET_CLOSE_HOUR,
    US_MARKET_CLOSE_MINUTE,
    IBKR_TIMEZONE,
    IBKR_QUANTITY,
)
from core.orb_signal_engine import (
    calculate_atr,
    calculate_orb_range,
    calculate_orb_risk,
    detect_orb_breakout,
    get_orb_sl_tp,
    check_orb_trade_allowed,
    should_force_exit,
    get_seconds_until_next_30m_close,
    resample_to_timeframe,
)
from core.utils import send_telegram
from core.ibkr.client import IBKRClient
from core.ibkr.utils import is_us_market_open, get_us_et_now
from core.scheduler import run_strategy_loop
from core.cash_manager import LiveCashManager


# -----------------------------
# Global State
# -----------------------------
_STOP_EVENT = asyncio.Event()
ORB_TRADE_TAKEN_TODAY: Dict[str, bool] = {}  # Track if trade taken per symbol
ORB_ACTIVE_POSITIONS: Dict[str, Dict] = {}  # Track active positions per symbol

# US Eastern timezone
US_ET = pytz.timezone(IBKR_TIMEZONE)

# Market times
MARKET_OPEN_TIME = time(US_MARKET_OPEN_HOUR, US_MARKET_OPEN_MINUTE)
MARKET_CLOSE_TIME = time(US_MARKET_CLOSE_HOUR, US_MARKET_CLOSE_MINUTE)


# -----------------------------
# Helper Functions
# -----------------------------
async def check_symbol_traded_today(symbol: str, ibkr_client: IBKRClient) -> bool:
    """
    Check if we already placed an ORB entry order for this symbol today.
    Uses broker's order/trade history as single source of truth.
    
    Returns:
        True if symbol was already traded today, False otherwise
    """
    try:
        # Get today's date range
        today_start = get_us_et_now().replace(hour=0, minute=0, second=0, microsecond=0)
        
        # Check recent trades (fills)
        fills = ibkr_client.ib.fills()
        for fill in fills:
            # Check if fill is from today and matches our symbol
            if hasattr(fill, 'time') and fill.time.date() >= today_start.date():
                # For options, the contract will have the underlying symbol
                contract = fill.contract
                contract_symbol = getattr(contract, 'symbol', '')
                
                if contract_symbol == symbol:
                    logger.info(f"[{symbol}] 💾 Found filled order from today in broker history")
                    return True
        
        # Also check open trades (might not be filled yet but submitted today)
        trades = await ibkr_client.get_open_orders()
        for trade in trades:
            if hasattr(trade, 'contract') and trade.contract.symbol == symbol:
                # Check if order was placed today
                if hasattr(trade, 'log') and trade.log:
                    first_log = trade.log[0]
                    if hasattr(first_log, 'time') and first_log.time.date() >= today_start.date():
                        logger.info(f"[{symbol}] 💾 Found open order from today in broker history")
                        return True
        
        return False
        
    except Exception as e:
        logger.error(f"[{symbol}] Error checking broker history: {e}")
        # On error, check local state as fallback
        return ORB_TRADE_TAKEN_TODAY.get(symbol, False)


def get_orb_end_time() -> datetime:
    """Get ORB period end time for today."""
    now = get_us_et_now()
    orb_start = datetime.combine(now.date(), MARKET_OPEN_TIME)
    orb_end = orb_start + timedelta(minutes=ORB_DURATION_MINUTES)
    return US_ET.localize(orb_end)


def reset_daily_state():
    """Reset daily tracking variables."""
    logger.info("🔄 ORB Daily state reset (IBKR)")


async def is_symbol_occupied(
    symbol: str, ibkr_client: IBKRClient, include_local: bool = True
) -> bool:
    """
    Check if a symbol is already 'occupied' by an open position or active order.
    Ensures we don't double-dip or place redundant orders after restarts.

    Args:
        symbol: The symbol to check.
        ibkr_client: The IBKR client instance.
        include_local: Whether to check local tracking (ORB_ACTIVE_POSITIONS).
                       Set to False when detecting if a position was closed on the broker.
    """
    try:
        # 1. Check local state first
        if include_local and symbol in ORB_ACTIVE_POSITIONS:
            # Use debug level for repetitive logs to reduce noise
            logger.debug(
                f"[{symbol}] 🛡️ Symbol Shield: Occupied (Active in Local Memory). The bot is already tracking an active trade for this symbol."
            )
            return True

        # 2. Check Broker Positions
        positions = await ibkr_client.get_positions()
        for pos in positions:
            if pos.get("symbol") == symbol and abs(pos.get("position", 0)) > 0:
                logger.debug(
                    f"[{symbol}] 🛡️ Symbol Shield: Occupied (Broker Position Found)"
                )
                # Re-link contract if missing
                if symbol not in ORB_ACTIVE_POSITIONS:
                    ORB_ACTIVE_POSITIONS[symbol] = {"contract": pos.get("contract")}
                return True

        # 3. Check Broker Open Orders (Trades)
        trades = await ibkr_client.get_open_orders()
        for trade in trades:
            if trade.contract.symbol == symbol and not trade.isDone():
                logger.debug(
                    f"[{symbol}] 🛡️ Symbol Shield: Occupied (Open Order/Trade Found)"
                )
                return True

        return False
    except Exception as e:
        logger.error(f"[{symbol}] Error checking occupancy: {e}")
        return True  # Err on the side of caution


async def recover_active_positions(ibkr_client: IBKRClient):
    """
    Scan broker for any existing ORB-relevant positions and populate local state.
    Called on startup to prevent duplicates.
    """
    try:
        logger.info("🔍 Scanning for existing positions to recover state...")
        positions = await ibkr_client.get_positions()
        for pos in positions:
            symbol = pos.get("symbol")
            if symbol in ORB_IBKR_SYMBOLS and abs(pos.get("position", 0)) > 0:
                logger.info(f"[{symbol}] Recovered active position from broker")
                ORB_ACTIVE_POSITIONS[symbol] = {
                    "direction": "LONG" if pos.get("position") > 0 else "SHORT",
                    "option_contract": pos.get("contract"),
                    "qty": abs(pos.get("position")),
                    "entry_time": get_us_et_now(),
                }
                ORB_TRADE_TAKEN_TODAY[symbol] = True

                # Send one-time notification on restart
                send_telegram(
                    f"🔄 ORB Startup Recovery (IBKR): {symbol}\n"
                    f"Found existing {ORB_ACTIVE_POSITIONS[symbol]['direction']} position on broker. Tracking active trade.",
                    broker="IBKR",
                )
    except Exception as e:
        logger.error(f"Error during state recovery: {e}")


async def wait_for_orb_complete(ibkr_client: IBKRClient):
    """Wait until ORB period is complete with connection monitoring."""
    orb_end = get_orb_end_time()

    while not _STOP_EVENT.is_set():
        now = get_us_et_now()
        if now >= orb_end:
            logger.info("✅ ORB period complete")
            return True

        # Check connection health
        if not ibkr_client.ib.isConnected():
            logger.warning("⚠️ IBKR connection lost during ORB wait. Reconnecting...")
            await ibkr_client.connect_async()

        remaining = (orb_end - now).total_seconds()
        # Wait in small chunks to allow connection checking but also respond to stop event
        wait_chunk = min(60, remaining)

        try:
            await asyncio.wait_for(
                asyncio.shield(_STOP_EVENT.wait()), timeout=wait_chunk
            )
            return False  # Stop event was set
        except asyncio.TimeoutError:
            # Continue loop to check connection and time
            continue
    return False


async def get_0dte_option_chain(
    ibkr_client: IBKRClient,
    symbol: str,
    underlying_contract: any,
    underlying_price: float,
    right: str,
) -> Optional[Dict]:
    """
    Get 0 DTE (same-day expiry) option for SPX/NDX index or ES/NQ future.

    Args:
        ibkr_client: IBKR client
        symbol: ES, NQ, SPX, NDX, or Stock symbol (NVDA, etc.)
        underlying_contract: Qualified Index, Future, or Stock contract
        underlying_price: Current underlying price
        right: "C" for call, "P" for put

    Returns:
        Option contract dict or None
    """
    try:
        # Use cache if available
        if symbol in ibkr_client.option_chains_cache:
            logger.debug(f"[{symbol}] Using cached option chains")
            chains = ibkr_client.option_chains_cache[symbol]
        else:
            # Get option chain with 3 retries
            chains = None
            if underlying_contract.secType == "IND":
                query_exchange = "CBOE"
            elif underlying_contract.secType == "FUT":
                query_exchange = "CME"
            else:
                query_exchange = ""  # Empty for stocks to get all exchanges

            for attempt in range(1, 4):
                try:
                    logger.debug(
                        f"[{symbol}] Requesting option chains (Attempt {attempt}/3) for {underlying_contract.secType} (conId: {underlying_contract.conId}) exchange={query_exchange}..."
                    )
                    # Narrowing by exchange helps performance and avoids timeouts
                    chains = await asyncio.wait_for(
                        ibkr_client.ib.reqSecDefOptParamsAsync(
                            underlying_contract.symbol,
                            query_exchange,
                            underlying_contract.secType,
                            underlying_contract.conId,
                        ),
                        timeout=15.0,
                    )
                    if chains:
                        ibkr_client.option_chains_cache[symbol] = chains
                        break
                except asyncio.TimeoutError:
                    logger.warning(f"[{symbol}] Timeout on attempt {attempt}/3")
                    if attempt == 3:
                        logger.warning(
                            f"[{symbol}] All 3 attempts to fetch option chains timed out. Attempting direct fallback..."
                        )
                        break
                    await asyncio.sleep(2)
                except Exception as e:
                    logger.error(f"[{symbol}] Error on attempt {attempt}/3: {e}")
                    if attempt == 3:
                        break
                    await asyncio.sleep(2)

        # today's date for expiry
        today = get_us_et_now().strftime("%Y%m%d")

        if not chains:
            # DIRECT FALLBACK for ES/NQ: Construct and qualify common contracts
            if symbol not in ["ES", "NQ"]:
                logger.error(
                    f"[{symbol}] Failed to get option chains and no fallback for this symbol."
                )
                return None

            logger.info(f"[{symbol}] Running Direct Fallback for 0DTE discovery...")
            # Predict the ATM-ish strikes to minimize qualifying calls
            # (ES uses 5 pt steps usually, NQ uses 20-50)
            step = 5 if symbol == "ES" else 10
            base_strike = round(underlying_price / step) * step

            candidates = []
            for i in range(-2, 3):  # Check 5 nearby strikes
                strike = float(base_strike + (i * step))
                opt = FuturesOption(symbol, today, strike, right, exchange="CME")
                candidates.append(opt)

            logger.debug(
                f"[{symbol}] Probing {len(candidates)} candidate strikes around {base_strike}"
            )
            qualified = await ibkr_client.ib.qualifyContractsAsync(*candidates)

            if not qualified:
                logger.error(
                    f"[{symbol}] Fallback failed: Could not qualify any 0DTE candidates."
                )
                return None

            # Use the most ATM of the qualified ones
            best_opt = min(qualified, key=lambda x: abs(x.strike - underlying_price))
            logger.info(
                f"[{symbol}] ✅ Fallback success! Found: {best_opt.localSymbol}"
            )

            return {
                "contract": best_opt,
                "symbol": symbol,
                "strike": best_opt.strike,
                "expiry": today,
                "right": right,
            }

        if not chains:
            logger.warning(f"[{symbol}] No option chains found after retries")
            return None

        # Aggregate all available expiries and their trading classes across all matching chains
        if underlying_contract.secType == "IND":
            target_exchange = "CBOE"
        elif underlying_contract.secType == "FUT":
            target_exchange = "CME"
        else:
            # For Stocks, the exchange field in the Option Chain results varies (AMEX, PHLX, etc.)
            # We want to aggregate from all of them, or just use SMART for the contract later.
            # Here we set a 'preferred' exchange to filter results if many are returned.
            target_exchange = ""

        expiries_map = {}  # expiry -> list of tradingClasses
        strikes_per_expiry = {}  # expiry -> set of strikes

        for c in chains:
            # If target_exchange is empty, we accept all chains (good for stocks)
            if not target_exchange or c.exchange == target_exchange:
                for exp in c.expirations:
                    if exp not in expiries_map:
                        expiries_map[exp] = []
                        strikes_per_expiry[exp] = set()
                    expiries_map[exp].append(c.tradingClass)
                    # Add strikes for this specific expiry
                    for s in c.strikes:
                        strikes_per_expiry[exp].add(s)

        if not expiries_map:
            logger.warning(
                f"[{symbol}] No matching expiries found in chains for {target_exchange}"
            )
            return None

        # Log available expiries for debugging
        sorted_expiries = sorted(expiries_map.keys())
        logger.debug(f"[{symbol}] Available expiries: {sorted_expiries[:10]}")  # Show first 10
        
        # Select expiry based on underlying type
        from datetime import timedelta, datetime
        now = get_us_et_now()
        today_str = now.strftime("%Y%m%d")
        selected_expiry = None
        selected_trading_class = None

        # For futures (ES/NQ), prefer weekly expiry (better liquidity/premiums)
        if underlying_contract.secType == "FUT":
            # Get next Friday's date (weekly expiry)
            days_until_friday = (4 - now.weekday()) % 7  # Friday is weekday 4
            if days_until_friday == 0:  # If today is Friday, use today
                days_until_friday = 0
            next_friday = now + timedelta(days=days_until_friday)
            target_expiry = next_friday.strftime("%Y%m%d")
            
            # Look for target weekly expiry or nearest after
            candidates = [e for e in sorted_expiries if e >= target_expiry]
            if candidates:
                selected_expiry = candidates[0]
                logger.debug(f"[{symbol}] Using weekly expiry for futures: {selected_expiry} (Target: {target_expiry})")
            else:
                # Fallback to nearest available
                selected_expiry = sorted_expiries[0] if sorted_expiries else None
                logger.warning(f"[{symbol}] No weekly expiry found, using nearest: {selected_expiry}")
        else:
            # For stocks/indices: Use weekly expiry (>2 DTE)
            # Find the nearest expiry that's at least 2 trading days away
            from datetime import timedelta
            min_dte_date = (now + timedelta(days=2)).strftime("%Y%m%d")
            
            logger.info(f"[{symbol}] Looking for stock option expiry >= {min_dte_date} (2+ DTE)")
            
            # Find the closest expiry that's >= 2 DTE
            # Prefer Friday expiries (standard weekly options), but accept any day
            candidates = [e for e in sorted_expiries if e >= min_dte_date]
            
            if candidates:
                # Try to find a Friday expiry first (more liquid)
                from datetime import datetime as dt
                friday_candidates = []
                for exp_str in candidates[:5]:  # Check first 5 expiries
                    try:
                        exp_date = dt.strptime(exp_str, "%Y%m%d")
                        if exp_date.weekday() == 4:  # Friday
                            friday_candidates.append(exp_str)
                    except:
                        pass
                
                if friday_candidates:
                    selected_expiry = friday_candidates[0]
                    logger.debug(f"[{symbol}] Using Friday weekly expiry: {selected_expiry}")
                else:
                    # No Friday found, use first available
                    selected_expiry = candidates[0]
                    logger.debug(f"[{symbol}] Using nearest available expiry: {selected_expiry}")
            else:
                # Fallback to nearest available
                selected_expiry = sorted_expiries[0] if sorted_expiries else None
                logger.warning(f"[{symbol}] No expiry >=2 DTE found, using nearest: {selected_expiry}")

        if not selected_expiry:
            logger.error(f"[{symbol}] No valid expiry found")
            return None

        # Select trading class - for futures use standard mini contract class (ECNQ for NQ, MESNQ for ES)
        classes = expiries_map[selected_expiry]
        logger.info(f"[{symbol}] Available trading classes for {selected_expiry}: {classes[:5]}")  # Show first 5
        
        if underlying_contract.secType == "FUT":
            # For NQ/ES futures, prefer standard mini contract class
            if symbol == "NQ":
                # ECNQ is the standard E-mini NASDAQ-100 options
                selected_trading_class = "ECNQ" if "ECNQ" in classes else classes[0]
            elif symbol == "ES":
                # MESNQ is the Micro E-mini S&P 500 options (or ES for standard)
                selected_trading_class = "ES" if "ES" in classes else classes[0]
            else:
                selected_trading_class = classes[0]
        else:
            # For stocks, use the symbol itself as trading class (standard)
            selected_trading_class = symbol if symbol in classes else classes[0]
        
        logger.info(f"[{symbol}] Selected trading class: {selected_trading_class} (from {classes})")
        today = selected_expiry  # Use selected expiry for contract creation

        # Get strikes specific to this expiry
        strikes = sorted(list(strikes_per_expiry.get(selected_expiry, set())))
        if not strikes:
            logger.error(f"[{symbol}] No strikes available for expiry {selected_expiry}")
            return None

        logger.debug(f"[{symbol}] Available strikes for {selected_expiry}: {len(strikes)} strikes (range: {strikes[0]}-{strikes[-1]})")
        
        # Show strikes near the underlying price for debugging
        nearby_strikes = [s for s in strikes if abs(s - underlying_price) <= 10]
        logger.debug(f"[{symbol}] Strikes near underlying price {underlying_price}: {nearby_strikes[:15]}")

        if right == "C":
            # For Calls: Pick the highest strike below underlying price
            itm_strikes = [s for s in strikes if s < underlying_price]
            if itm_strikes:
                # Find the closest ITM strike (highest below underlying)
                atm_strike = itm_strikes[-1]
            else:
                # If no ITM strikes, use the lowest available
                atm_strike = strikes[0]
        else:
            # For Puts: Pick the lowest strike above underlying price
            itm_strikes = [s for s in strikes if s > underlying_price]
            if itm_strikes:
                atm_strike = itm_strikes[0]
            else:
                atm_strike = strikes[-1]
        
        # For stocks, validate strike exists in the list (handles non-standard intervals like 2.5/5)
        if underlying_contract.secType in ["IND", "STK"] and atm_strike not in strikes:
            # Strike calculated doesn't exist, find nearest valid strike
            nearest_strike = min(strikes, key=lambda x: abs(x - atm_strike))
            logger.warning(f"[{symbol}] Calculated strike {atm_strike} not in available strikes, using nearest: {nearest_strike}")
            atm_strike = nearest_strike

        logger.info(
            f"[{symbol}] Selected ITM Option: Strike={atm_strike}, Expiry={today}, Right={right}, Class={selected_trading_class}, Exchange={target_exchange}"
        )

        # Create option contract
        if underlying_contract.secType in ["IND", "STK"]:
            # For stocks: Don't specify tradingClass for weekly options, let IBKR resolve it
            option = Option(
                symbol=symbol,
                lastTradeDateOrContractMonth=today,
                strike=atm_strike,
                right=right,
                exchange="SMART",
                multiplier="100",  # Standard for US Options
                currency="USD",
            )
        else:
            # For FuturesOptions, specifying tradingClass is CRITICAL to avoid ambiguity (ES vs EW3)
            option = FuturesOption(
                symbol,
                today,
                atm_strike,
                right,
                exchange=target_exchange,
                tradingClass=selected_trading_class,
            )

        qualified = await ibkr_client.ib.qualifyContractsAsync(option)
        
        logger.info(f"[{symbol}] Qualification result: {len(qualified) if qualified else 0} contracts")

        if not qualified or qualified[0] is None:
            logger.warning(
                f"[{symbol}] ⚠️ Failed to qualify option expiry {today}. Trying next available expiry..."
            )
            # Try up to 3 next expiries (some might not have contracts)
            candidates_after_first = [e for e in sorted_expiries if e > selected_expiry][:3]
            
            for next_expiry in candidates_after_first:
                if underlying_contract.secType not in ["IND", "STK"]:
                    break
                    
                logger.debug(f"[{symbol}] Attempting with next expiry: {next_expiry}")
                
                # Recalculate strike for the new expiry
                next_strikes = sorted(list(strikes_per_expiry.get(next_expiry, set())))
                if next_strikes:
                    if right == "C":
                        itm_strikes = [s for s in next_strikes if s < underlying_price]
                        atm_strike = itm_strikes[-1] if itm_strikes else next_strikes[0]
                    else:
                        itm_strikes = [s for s in next_strikes if s > underlying_price]
                        atm_strike = itm_strikes[0] if itm_strikes else next_strikes[-1]
                    
                    logger.debug(f"[{symbol}] Recalculated strike for {next_expiry}: {atm_strike}")
                    option.lastTradeDateOrContractMonth = next_expiry
                    option.strike = atm_strike
                    today = next_expiry  # Update for return value
                    qualified = await ibkr_client.ib.qualifyContractsAsync(option)
                    
                    if qualified and qualified[0] is not None:
                        logger.info(f"[{symbol}] ✅ Successfully qualified with expiry {next_expiry}")
                        break
                else:
                    logger.warning(f"[{symbol}] No strikes available for next expiry {next_expiry}")
                
        if not qualified or qualified[0] is None:
            logger.warning(
                f"[{symbol}] ⚠️ Failed to qualify option with full parameters. Trying aggressive fallback (relaxing Exchange/Class)..."
            )
            # Aggressive Fallback: Omit tradingClass and exchange, let IBKR search SMART
            option.tradingClass = ""
            option.exchange = (
                "SMART"
                if underlying_contract.secType in ["IND", "STK"]
                else option.exchange
            )
            qualified = await ibkr_client.ib.qualifyContractsAsync(option)

        if not qualified or qualified[0] is None:
            logger.warning(
                f"[{symbol}] ⚠️ Final fallback: Attempting Nuclear Discovery (Broadest Search)..."
            )
            # Nuclear Fallback for stocks: Strip everything but the essentials
            # This allows IBKR to return whatever it finds for that symbol/expiry/strike
            option.exchange = "SMART"
            option.tradingClass = ""
            option.multiplier = ""  # Often fixes qualification issues for stocks

            details = await ibkr_client.ib.reqContractDetailsAsync(option)
            if details:
                qualified = [details[0].contract]
                logger.info(
                    f"[{symbol}] ✅ Found matching contract via discovery: {qualified[0].localSymbol} (Details: {len(details)} matches)"
                )
            else:
                # Last resort: Try nearby strikes (round to common intervals: 2.5, 5)
                logger.warning(f"[{symbol}] Trying nearby strikes (rounding to common intervals)")
                for strike_adjustment in [2.5, 5.0, -2.5, -5.0]:
                    adjusted_strike = round((atm_strike + strike_adjustment) * 2) / 2  # Round to nearest 0.5
                    logger.info(f"[{symbol}] Trying adjusted strike: {adjusted_strike}")
                    option.strike = adjusted_strike
                    option.multiplier = "100"
                    details = await ibkr_client.ib.reqContractDetailsAsync(option)
                    if details:
                        qualified = [details[0].contract]
                        logger.info(f"[{symbol}] ✅ Found with adjusted strike {adjusted_strike}: {qualified[0].localSymbol}")
                        atm_strike = adjusted_strike  # Update for return value
                        break

        if not qualified or qualified[0] is None:
            logger.error(
                f"[{symbol}] ❌ Failed to qualify option contract after all fallbacks: {option}"
            )
            return None

        logger.info(f"[{symbol}] ✅ Successfully qualified option. Contract conId: {getattr(qualified[0], 'conId', 'N/A')}")
        logger.debug(f"[{symbol}] Contract type: {type(qualified[0])}")
        
        result = {
            "contract": qualified[0],
            "symbol": symbol,
            "strike": atm_strike,
            "expiry": today,
            "right": right,
        }
        logger.debug(f"[{symbol}] Returning option_data with contract conId: {result['contract'].conId if hasattr(result['contract'], 'conId') else 'N/A'}")
        return result

    except Exception as e:
        logger.exception(f"[{symbol}] Error getting 0 DTE option: {e}")
        return None


# -----------------------------
# Execute ORB Entry
# -----------------------------
async def execute_orb_entry(
    symbol: str,
    direction: str,
    entry_price: float,
    stop_loss: float,
    take_profit: float,
    ibkr_client: IBKRClient,
    cash_mgr: LiveCashManager,
    quantity: int = IBKR_QUANTITY,
    underlying=None,
) -> bool:
    """
    Execute ORB entry order with SL/TP.

    Args:
           symbol: Trading symbol (SPX, NDX)
           direction: "LONG" or "SHORT"
           entry_price: Entry price (index level)
           stop_loss: Stop loss price
           take_profit: Take profit price
           ibkr_client: IBKR client

       Returns:
           True if order placed successfully
    """
    try:
        # Determine option type
        option_right = "C" if direction == "LONG" else "P"

        # The Shield check is now done proactively in the monitor loop.
        # This check serves as a final safety guard.
        if await is_symbol_occupied(symbol, ibkr_client):
            logger.warning(
                f"[{symbol}] 🛡️ Symbol Shield: BLOCKING Entry. Symbol is already active in a position or open order."
            )
            return False

        logger.info(
            f"[{symbol}] 🛡️ Symbol Shield: CLEAR for Entry. No existing positions or orders found."
        )

        logger.info(f"[{symbol}] 📥 ORB ENTRY: {direction} at {entry_price:.2f}")
        logger.info(
            f"[{symbol}]   Option Type: {'Call' if option_right == 'C' else 'Put'}"
        )
        logger.info(f"[{symbol}]   SL: {stop_loss:.2f}, TP: {take_profit:.2f}")

        # Use cached underlying
        if not underlying:
            # Last ditch effort if it failed at startup
            if symbol in ["ES", "NQ"]:
                underlying = await ibkr_client.get_front_month_contract(symbol)
            else:
                from ib_async import Stock

                underlying = Stock(symbol, "SMART", "USD")
                await ibkr_client.ib.qualifyContractsAsync(underlying)

        if not underlying:
            logger.error(f"[{symbol}] Failed to get underlying contract")
            return False

        ticker = await ibkr_client.ib.reqTickersAsync(underlying)

        if not ticker or ticker[0].last is None:
            # Try market data snapshot
            ibkr_client.ib.reqMktData(underlying, "", False, False)
            await asyncio.sleep(1)
            ticker = ibkr_client.ib.ticker(underlying)
            underlying_price = ticker.last or ticker.close
        else:
            underlying_price = ticker[0].last

        if not underlying_price:
            logger.error(f"[{symbol}] Failed to get underlying price")
            return False

        logger.info(f"[{symbol}]   Underlying: {underlying_price:.2f}")

        # Get 0 DTE option
        option_data = await get_0dte_option_chain(
            ibkr_client, symbol, underlying, underlying_price, option_right
        )

        if not option_data or not option_data.get("contract"):
            logger.error(f"[{symbol}] Failed to get valid 0 DTE option contract")
            return False

        option_contract = option_data["contract"]

        # Get option price for SL/TP calculation
        option_price = None
        ibkr_client.ib.reqMktData(option_contract, "", False, False)

        # Try up to 5 times to get a valid price (checks Last, Mid, then Model)
        for attempt in range(1, 6):
            await asyncio.sleep(0.5 * attempt)
            option_ticker = ibkr_client.ib.ticker(option_contract)

            # 1. Try Last price
            if option_ticker.last and not math.isnan(option_ticker.last):
                option_price = option_ticker.last
                logger.debug(f"[{symbol}] Identified Last price: {option_price}")
                break

            # 2. Try Mid price (Bid/Ask)
            if (
                option_ticker.bid
                and option_ticker.ask
                and not math.isnan(option_ticker.bid)
                and not math.isnan(option_ticker.ask)
            ):
                option_price = (option_ticker.bid + option_ticker.ask) / 2
                logger.debug(f"[{symbol}] Identified Mid price: {option_price}")
                break

            # 3. Try Model price
            if (
                hasattr(option_ticker, 'modelOptComp')
                and option_ticker.modelOptComp
                and hasattr(option_ticker.modelOptComp, 'modelPrice')
                and option_ticker.modelOptComp.modelPrice
                and not math.isnan(option_ticker.modelOptComp.modelPrice)
            ):
                option_price = option_ticker.modelOptComp.modelPrice
                logger.debug(f"[{symbol}] Identified Model price: {option_price}")
                break

            logger.warning(
                f"[{symbol}] Option price retrieval attempt {attempt}/5 failed (Ticker: {option_ticker})"
            )

        if option_price is None or math.isnan(option_price) or option_price <= 0:
            logger.error(
                f"[{symbol}] ❌ Failed to get valid option price after retries. Aborting trade entry."
            )
            return False

        logger.info(f"[{symbol}]   Final Option Price: ${option_price:.2f}")

        # Calculate SL/TP in option price terms using a Delta approximation (0.7 for ITM)
        # logic: option_price ± (underlying_points_distance * delta)
        delta = 0.7
        underlying_risk_pts = abs(entry_price - stop_loss)

        # 1. Technical (Technical points based)
        tech_option_sl = option_price - (underlying_risk_pts * delta)

        # 2. Risk Management (Cap risk at 50% of premium)
        max_premium_risk_pct = 0.50
        min_option_sl = option_price * (1 - max_premium_risk_pct)

        # Final Option SL: Technical with a Floor
        option_sl = max(tech_option_sl, min_option_sl)

        # 3. Recalibrate TP based on the REALIZED premium risk and RR ratio
        # This ensures the target is realistic relative to the risk taken on the premium
        realized_premium_risk = option_price - option_sl
        option_tp = option_price + (realized_premium_risk * ORB_RISK_REWARD)

        # --- TICK SIZE & SL FLOOR ---
        # Get minimum tick size for the contract
        details = await ibkr_client.ib.reqContractDetailsAsync(option_contract)
        min_tick = details[0].minTick if details else 0.05

        # Round to nearest tick
        option_sl = round(option_sl / min_tick) * min_tick
        option_tp = round(option_tp / min_tick) * min_tick

        # Ensure SL is at least min_tick
        option_sl = max(min_tick, option_sl)

        logger.info(f"[{symbol}] 📋 REALISTIC OPTION LEVELS")
        logger.info(f"[{symbol}]   Premium Entry: ${option_price:.2f}")
        logger.info(
            f"[{symbol}]   Premium Risk: ${realized_premium_risk:.2f} (Capped at {max_premium_risk_pct*100}%)"
        )
        logger.info(
            f"[{symbol}]   Option SL Price: ${option_sl:.2f}, TP: ${option_tp:.2f} (RR: 1:{ORB_RISK_REWARD})"
        )

        # --- CAPITAL MANAGER (70% Rule) ---
        available_exposure = await cash_mgr.available_exposure()

        # We use the available exposure to determine the quantity
        # Cost = option_price * qty * 100
        qty = quantity
        trade_cost = option_price * qty * 100

        logger.debug(
            f"[{symbol}] 🛡️ Risk Guard: Checking capital allocation (70% rule)..."
        )
        logger.debug(
            f"[{symbol}] 🛡️ Risk Guard: Trade Cost ${trade_cost:.2f} | Available 70% Limit ${available_exposure:.2f} | Trading Qty {qty}"
        )

        if trade_cost > available_exposure:
            logger.warning(
                f"[{symbol}] 🛡️ Risk Guard: Trade cost ${trade_cost:.2f} exceeds 70% allocation limit of ${available_exposure:.2f}"
            )
            # Scale down to 1 if possible
            if qty > 1:
                qty = 1
                trade_cost = option_price * qty * 100
                if trade_cost > available_exposure:
                    return False
                logger.debug(
                    f"[{symbol}] 🛡️ Risk Guard: Scaled down qty to {qty} to fit allocation limit."
                )
            else:
                return False

        # Additional balance check
        balance_info = await cash_mgr.get_account_balance()
        if trade_cost > balance_info["available_funds"]:
            logger.error(
                f"[{symbol}] 🛡️ Risk Guard: Insufficient raw cash for trade: Need ${trade_cost:.2f}, Have ${balance_info['available_funds']:.2f}"
            )
            return False

        logger.info(f"[{symbol}] 🛡️ Risk Guard: PASS. Order proceed.")

        # Place bracket order

        order_result = await ibkr_client.place_bracket_order(
            option_contract=option_contract,
            quantity=qty,
            stop_loss_price=max(0.05, round(option_sl, 2)),  # Minimum $0.05
            target_price=round(option_tp, 2),
        )

        if order_result and order_result.get("status") == "success":
            # Track position with bracket order details
            ORB_ACTIVE_POSITIONS[symbol] = {
                "direction": direction,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "option_contract": option_contract,
                "option_price": option_price,
                "qty": qty,
                "entry_time": get_us_et_now(),
                "entry_order_id": order_result.get("entry_order_id"),
                "sl_order_id": order_result.get("sl_order_id"),
                "target_order_id": order_result.get("target_order_id"),
                "oca_group": order_result.get("oca_group"),
            }
            ORB_TRADE_TAKEN_TODAY[symbol] = True

            # Telegram notification
            msg = (
                f"🎯 ORB ENTRY (IBKR): {symbol} {direction}\n"
                f"Option: {option_data['strike']} {option_right} 0DTE\n"
                f"Index: ${entry_price:.2f}\n"
                f"SL: ${stop_loss:.2f} | TP: ${take_profit:.2f}\n"
                f"Qty: {qty}"
            )
            send_telegram(msg, broker="IBKR")
            logger.info(f"[{symbol}] ✅ ORB Entry order placed successfully")

            return True
        else:
            reason = (
                order_result.get("error", "Unknown rejection")
                if order_result
                else "Submission failed"
            )
            logger.error(f"[{symbol}] ORB Entry order failed: {reason}")
            send_telegram(f"❌ ORB Entry Rejected ({symbol}): {reason}", broker="IBKR")
            return False

    except Exception as e:
        logger.exception(f"[{symbol}] ORB Entry error: {e}")
        send_telegram(f"❌ ORB Entry Error ({symbol}): {str(e)[:100]}", broker="IBKR")
        return False


# -----------------------------
# Force Exit Position
# -----------------------------
async def force_exit_position(
    symbol: str, ibkr_client: IBKRClient, reason: str = "EOD"
):
    """
    Force close any open position for symbol.
    
    Steps:
    1. Cancel all open orders (SL and TP from bracket)
    2. Close position with market order
    3. Clean up tracking
    """
    if symbol not in ORB_ACTIVE_POSITIONS:
        return

    position = ORB_ACTIVE_POSITIONS[symbol]

    try:
        logger.warning(
            f"[{symbol}] ⚠️ FORCE EXIT ({reason}): Closing {position['direction']}"
        )

        # Step 1: Cancel all open orders for this symbol
        # This cancels the SL and TP orders from the bracket
        open_orders = await ibkr_client.get_open_orders()
        cancelled_count = 0
        
        for trade in open_orders:
            # Cancel if order belongs to this symbol's contract
            if hasattr(trade, 'contract') and trade.contract.symbol == symbol:
                try:
                    ibkr_client.ib.cancelOrder(trade.order)
                    cancelled_count += 1
                    logger.debug(f"[{symbol}] Cancelled order ID: {trade.order.orderId}")
                except Exception as e:
                    logger.warning(f"[{symbol}] Failed to cancel order {trade.order.orderId}: {e}")
        
        if cancelled_count > 0:
            logger.info(f"[{symbol}] Cancelled {cancelled_count} open order(s)")
            await asyncio.sleep(1)  # Give time for cancellations to process

        # Step 2: Close position with market order
        # We're trading OPTIONS, so we need to close the option position
        option_contract = position.get("option_contract")
        
        if not option_contract:
            logger.error(f"[{symbol}] No option contract in position tracking")
            return
        
        # Get positions and find our option
        positions = await ibkr_client.get_positions()
        
        logger.debug(f"[{symbol}] Found {len(positions)} total positions in account")
        logger.debug(f"[{symbol}] Looking for option contract conId: {getattr(option_contract, 'conId', 'N/A')}")
        
        position_found = False
        for pos in positions:
            # Match by option contract (not underlying symbol)
            logger.debug(f"[{symbol}] Checking position: {pos.get('contract').symbol if pos.get('contract') else 'N/A'}, conId: {getattr(pos.get('contract'), 'conId', 'N/A')}")
            
            if hasattr(pos.get("contract"), "conId") and hasattr(option_contract, "conId"):
                if pos["contract"].conId == option_contract.conId:
                    position_found = True
                    qty = abs(pos.get("position", 0))
                    
                    logger.info(f"[{symbol}] Found matching option position: qty={qty}")
                    
                    if qty == 0:
                        logger.warning(f"[{symbol}] Position quantity is 0, nothing to close")
                        break
                    
                    from ib_async import MarketOrder

                    action = "SELL" if pos.get("position", 0) > 0 else "BUY"
                    order = MarketOrder(action, qty)
                    trade = ibkr_client.ib.placeOrder(option_contract, order)

                    # Wait for fill confirmation
                    await asyncio.sleep(2)
                    
                    # Check if filled
                    status = trade.orderStatus.status if hasattr(trade, 'orderStatus') else "Unknown"
                    
                    msg = (
                        f"⚠️ ORB FORCE EXIT ({reason}): {symbol}\n"
                        f"Position: {position['direction']}\n"
                        f"Entry: ${position['entry_price']:.2f}\n"
                        f"Qty: {qty} (Action: {action})\n"
                        f"Status: {status}"
                    )
                    send_telegram(msg, broker="IBKR")
                    logger.info(f"[{symbol}] ✅ Position closed with {action} market order (Status: {status})")
                    break
        
        if not position_found:
            logger.warning(f"[{symbol}] No open position found in broker (already closed or not synced)")
            # Still send notification
            msg = (
                f"⚠️ ORB FORCE EXIT ({reason}): {symbol}\n"
                f"Position: {position['direction']}\n"
                f"Note: No open position found in broker"
            )
            send_telegram(msg, broker="IBKR")

        # Step 3: Remove from tracking
        del ORB_ACTIVE_POSITIONS[symbol]
        logger.info(f"[{symbol}] Removed from active positions tracking")

    except Exception as e:
        logger.exception(f"[{symbol}] Force exit error: {e}")
        send_telegram(f"❌ Force Exit Error ({symbol}): {str(e)[:100]}", broker="IBKR")


# -----------------------------
# ORB Signal Monitor
# -----------------------------
async def orb_signal_monitor(
    symbol: str,
    ibkr_client: IBKRClient,
    cash_mgr: LiveCashManager,
):
    """
    Monitor for ORB breakout signals for a symbol.

    Args:
        symbol: Trading symbol (SPX, NDX)
        ibkr_client: IBKR client
    """
    logger.info(f"[{symbol}] 📊 ORB Signal Monitor started")

    # Get underlying contract (Index, Future, or Stock)
    if symbol in ["ES", "NQ"]:
        underlying = await ibkr_client.get_front_month_contract(symbol)
    elif symbol in ["SPX", "NDX"]:
        underlying = Index(symbol, "CBOE", "USD")
        await ibkr_client.ib.qualifyContractsAsync(underlying)
    else:
        from ib_async import Stock

        underlying = Stock(symbol, "SMART", "USD")
        await ibkr_client.ib.qualifyContractsAsync(underlying)

    if not underlying:
        logger.error(
            f"[{symbol}] Could not qualify underlying contract. Stopping monitor."
        )
        return

    orb_high = None
    orb_low = None
    orb_complete = False

    while not _STOP_EVENT.is_set():
        try:
            # Ensure connection is active
            await ibkr_client.ensure_connected()

            now = get_us_et_now()

            # --- PROACTIVE SYMBOL SHIELD ---
            # We only perform the 'shield' check if we haven't officially marked the trade as taken today.
            # This avoids redundant logging once we are already in 'monitoring' mode.
            if not ORB_TRADE_TAKEN_TODAY.get(symbol, False):
                if await is_symbol_occupied(symbol, ibkr_client):
                    logger.info(
                        f"[{symbol}] 🛡️ Symbol Shield: Occupancy found (Position/Order on Broker). Marking trade as taken today."
                    )
                    ORB_TRADE_TAKEN_TODAY[symbol] = True

            # If trade taken, just sleep and check for EOD exit
            if ORB_TRADE_TAKEN_TODAY.get(symbol, False):
                # Check if position was actively closed on broker (occupied returns false while trade_taken is true)
                # CRITICAL: We pass include_local=False to check the actual broker state.
                if not await is_symbol_occupied(
                    symbol, ibkr_client, include_local=False
                ):
                    if symbol in ORB_ACTIVE_POSITIONS:
                        logger.info(
                            f"[{symbol}] 🏁 Trade detected as CLOSED on broker side."
                        )
                        send_telegram(
                            f"🏁 ORB Trade Closed (IBKR): {symbol}. Position cleared on broker.",
                            broker="IBKR",
                        )
                        del ORB_ACTIVE_POSITIONS[symbol]

                # Check for EOD exit even if trade taken
                if should_force_exit(
                    now, MARKET_CLOSE_TIME, exit_before_minutes=15, symbol=symbol
                ):
                    await force_exit_position(symbol, ibkr_client, reason="EOD")
                    break
                await asyncio.sleep(60)
                continue

            # Check if market is open
            if not is_us_market_open():
                logger.debug(f"[{symbol}] Market closed, waiting...")
                await asyncio.sleep(60)
                continue

            # Check force exit time (15 min before close)
            if should_force_exit(
                now, MARKET_CLOSE_TIME, exit_before_minutes=15, symbol=symbol
            ):
                logger.warning(f"[{symbol}] EOD Force exit time reached")
                send_telegram(
                    f"🕒 [{symbol}] ORB Strategy (IBKR): EOD Force exit time reached (15:45 ET)",
                    broker="IBKR",
                )
                await force_exit_position(symbol, ibkr_client, reason="EOD")
                break

            # Get historical 1m data for ORB calculation precision and timezone alignment
            df_1m_utc = await ibkr_client.req_historic_1m(
                symbol=symbol, duration_days=1, contract=underlying
            )

            if df_1m_utc is None or df_1m_utc.empty:
                logger.debug(f"[{symbol}] No 1m bar data available")
                await asyncio.sleep(30)
                continue

            # Convert to local ET naive for strategy logic (aligned with MARKET_OPEN_TIME)
            df_1m = df_1m_utc.copy()
            df_1m.index = (
                df_1m.index.tz_localize("UTC")
                .tz_convert(IBKR_TIMEZONE)
                .tz_localize(None)
            )

            # Resample to 30-minute bars for breakout detection conviction
            df = resample_to_timeframe(df_1m, ORB_BREAKOUT_TIMEFRAME)

            if df.empty:
                logger.debug(f"[{symbol}] No {ORB_BREAKOUT_TIMEFRAME}m bars available")
                await asyncio.sleep(30)
                continue

            logger.info(
                f"[{symbol}] Fetched {len(df_1m)} 1m bars. Resampled to {len(df)} {ORB_BREAKOUT_TIMEFRAME}m bars (latest close: ${df['close'].iloc[-1]:.2f})"
            )

            # Calculate ORB range (only once after ORB period)
            if not orb_complete:
                orb_data = calculate_orb_range(
                    df=df_1m,  # Use 1m bars for ORB precision
                    market_open_time=MARKET_OPEN_TIME,
                    orb_duration_minutes=ORB_DURATION_MINUTES,
                    symbol=symbol,
                )

                orb_high = orb_data.get("orb_high")
                orb_low = orb_data.get("orb_low")
                orb_complete = orb_data.get("orb_complete", False)

                if orb_complete and orb_high and orb_low:
                    msg = (
                        f"📊 ORB Range Set (IBKR): {symbol}\n"
                        f"High: ${orb_high:.2f}\n"
                        f"Low: ${orb_low:.2f}\n"
                        f"Range: ${orb_high - orb_low:.2f}"
                    )
                    send_telegram(msg, broker="IBKR")
                else:
                    sleep_sec = get_seconds_until_next_30m_close(now)
                    logger.info(
                        f"[{symbol}] ORB range building... Sleeping {sleep_sec}s until next candle close"
                    )
                    await asyncio.sleep(sleep_sec)
                    continue

            # Check if trade already taken
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
            )

            if not allowed:
                if reason == "past_max_entry_hour":
                    logger.info(f"[{symbol}] Past max entry hour, stopping monitor")
                    send_telegram(
                        f"🏁 [{symbol}] ORB Strategy (IBKR): Past max entry hour ({ORB_MAX_ENTRY_HOUR}:00). Stopping monitor for today.",
                        broker="IBKR",
                    )
                    break
                await asyncio.sleep(60)
                continue

            # --- BREAKOUT DETECTION ON LAST CLOSED BAR ---
            # Define cutoff: Now - 30 mins
            # Convert now (ET Aware) to UTC Naive to match df.index
            now_utc = now.astimezone(pytz.UTC).replace(tzinfo=None)
            cutoff = now_utc - timedelta(minutes=ORB_BREAKOUT_TIMEFRAME)
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

                # Execute Entry
                success = await execute_orb_entry(
                    ibkr_client=ibkr_client,
                    cash_mgr=cash_mgr,
                    symbol=symbol,
                    direction=breakout,
                    quantity=IBKR_QUANTITY,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    entry_price=entry_price,
                    underlying=underlying,
                )

                if success:
                    ORB_TRADE_TAKEN_TODAY[symbol] = True
                    send_telegram(
                        f"✅ ORB Breakout (IBKR): {symbol} {breakout}\nEntry: ${entry_price:.2f}",
                        broker="IBKR",
                    )
                    break

            # Optimization: Sleep until next candle close
            sleep_sec = get_seconds_until_next_30m_close(now)
            logger.info(
                f"[{symbol}] Analysis complete. Sleeping {sleep_sec}s until next candle close..."
            )
            await asyncio.sleep(sleep_sec)

        except asyncio.CancelledError:
            logger.info(f"[{symbol}] ORB Signal Monitor cancelled")
            break
        except Exception as e:
            logger.exception(f"[{symbol}] ORB Signal Monitor error: {e}")
            await asyncio.sleep(30)

    logger.info(f"[{symbol}] 📊 ORB Signal Monitor stopped")


# -----------------------------
# Main ORB Worker
# -----------------------------
async def heartbeat_task(interval=60):
    """Continuous heartbeat to show bot is alive."""
    logger.info("💓 Heartbeat task started")
    heartbeat_count = 0
    try:
        while not _STOP_EVENT.is_set():
            await asyncio.sleep(interval)
            heartbeat_count += 1
            now_utc = datetime.now(pytz.UTC)
            logger.info(
                f"💓 Heartbeat #{heartbeat_count}: {now_utc.strftime('%H:%M:%S')} UTC - Bot is alive"
            )
    except asyncio.CancelledError:
        logger.info("💓 Heartbeat task cancelled")
    except Exception as e:
        logger.error(f"💓 Heartbeat task error: {e}")
    finally:
        logger.info("💓 Heartbeat task stopped")


async def _async_ibkr_session():
    """
    Daily trading session logic for IBKR.
    Connects, waits for ORB, and runs signal monitors.
    """
    logger.info("🌅 Starting Daily Session (IBKR)")
    ibkr_client = None
    try:
        # CRITICAL: Re-initialize stop event in the current process's event loop
        global _STOP_EVENT
        _STOP_EVENT = asyncio.Event()

        # Initialize client and clear cache
        ibkr_client = IBKRClient()
        ibkr_client.option_chains_cache.clear()
        await ibkr_client.connect_async()
        
        # Check broker for any trades placed today and mark them
        for symbol in ORB_IBKR_SYMBOLS:
            if await check_symbol_traded_today(symbol, ibkr_client):
                ORB_TRADE_TAKEN_TODAY[symbol] = True

        # Startup Recovery: Scan broker for existing ORB-relevant positions
        await recover_active_positions(ibkr_client)

        cash_mgr = LiveCashManager(ibkr_client, broker="IBKR")
        await cash_mgr.check_and_log_start_balance()

        # Wait for ORB period to complete
        if await wait_for_orb_complete(ibkr_client):
            # Start signal monitors
            tasks = []
            for symbol in ORB_IBKR_SYMBOLS:
                task = asyncio.create_task(
                    orb_signal_monitor(symbol, ibkr_client, cash_mgr)
                )
                tasks.append(task)

            if tasks:
                await asyncio.gather(*tasks)

            send_telegram(
                "✅ ORB Daily Session (IBKR): All symbol monitors finished.",
                broker="IBKR",
            )
        else:
            logger.info("ORB wait aborted")
            send_telegram(
                "⚠️ ORB Daily Session (IBKR): Aborted during wait.", broker="IBKR"
            )

    except Exception as e:
        logger.error(f"Error in IBKR daily session: {e}", exc_info=True)
    finally:
        if ibkr_client:
            try:
                ibkr_client.disconnect()
                logger.info("IBKR Client Disconnected")
            except Exception:
                pass


async def run_orb_ibkr_workers():
    """
    Main entry point for IBKR ORB strategy workers.
    Uses the shared scheduler to manage the daily loop.
    """
    logger.info("🚀 Starting IBKR ORB Workers (Daily Loop Mode)")
    logger.info(f"   Symbols: {ORB_IBKR_SYMBOLS}")
    logger.info(f"   ORB Duration: {ORB_DURATION_MINUTES} minutes")
    logger.info(f"   Risk/Reward: 1:{ORB_RISK_REWARD}")

    send_telegram(
        f"🚀 IBKR ORB Bot Starting\n"
        f"Symbols: {', '.join(ORB_IBKR_SYMBOLS)}\n"
        f"ORB Duration: {ORB_DURATION_MINUTES} minutes\n"
        f"R:R Ratio: 1:{ORB_RISK_REWARD}\n"
        f"Expiry: 0 DTE",
        broker="IBKR",
    )

    await run_strategy_loop(
        broker_name="IBKR",
        strategy_name="ORB",
        session_func=_async_ibkr_session,
        stop_event=_STOP_EVENT,
        market_open_hour=US_MARKET_OPEN_HOUR,
        market_open_minute=US_MARKET_OPEN_MINUTE,
        market_close_hour=US_MARKET_CLOSE_HOUR,
        market_close_minute=US_MARKET_CLOSE_MINUTE,
        timezone_str=IBKR_TIMEZONE,
        pre_connect_minutes=60,
        process_isolation=False,
        heartbeat_func=heartbeat_task,
    )


def stop_orb_ibkr_workers():
    """Stop all IBKR ORB workers."""
    _STOP_EVENT.set()
