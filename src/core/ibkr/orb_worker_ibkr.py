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
def get_orb_end_time() -> datetime:
    """Get ORB period end time for today."""
    now = get_us_et_now()
    orb_start = datetime.combine(now.date(), MARKET_OPEN_TIME)
    orb_end = orb_start + timedelta(minutes=ORB_DURATION_MINUTES)
    return US_ET.localize(orb_end)


def reset_daily_state():
    """Reset daily tracking variables."""
    global ORB_TRADE_TAKEN_TODAY, ORB_ACTIVE_POSITIONS
    ORB_TRADE_TAKEN_TODAY = {symbol: False for symbol in ORB_IBKR_SYMBOLS}
    ORB_ACTIVE_POSITIONS = {}
    logger.info("🔄 ORB Daily state reset (IBKR)")


async def wait_for_orb_complete():
    """Wait until ORB period is complete."""
    orb_end = get_orb_end_time()
    now = get_us_et_now()

    if now >= orb_end:
        logger.info("✅ ORB period already complete")
        return True

    wait_seconds = (orb_end - now).total_seconds()
    logger.info(
        f"⏳ Waiting {wait_seconds:.0f}s for ORB period to complete ({orb_end.strftime('%H:%M')} ET)"
    )

    send_telegram(
        f"📊 ORB Strategy (IBKR): Building opening range until {orb_end.strftime('%H:%M')} ET\n"
        f"Symbols: {', '.join(ORB_IBKR_SYMBOLS)}",
        broker="IBKR",
    )

    try:
        await asyncio.wait_for(asyncio.shield(_STOP_EVENT.wait()), timeout=wait_seconds)
        return False  # Stop event was set
    except asyncio.TimeoutError:
        return True  # ORB period complete


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
        symbol: ES, NQ, SPX, or NDX
        underlying_contract: Qualified Index or Future contract
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
            target_exchange = "CBOE" if underlying_contract.secType == "IND" else "CME"
            for attempt in range(1, 4):
                try:
                    logger.debug(
                        f"[{symbol}] Requesting option chains (Attempt {attempt}/3) for {underlying_contract.secType} (conId: {underlying_contract.conId}) exchange={target_exchange}..."
                    )
                    # Narrowing by exchange helps performance and avoids timeouts
                    chains = await asyncio.wait_for(
                        ibkr_client.ib.reqSecDefOptParamsAsync(
                            underlying_contract.symbol,
                            target_exchange,
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
        target_exchange = "CBOE" if underlying_contract.secType == "IND" else "CME"
        expiries_map = {}  # expiry -> list of tradingClasses
        strikes_set = set()

        for c in chains:
            if c.exchange == target_exchange:
                for exp in c.expirations:
                    if exp not in expiries_map:
                        expiries_map[exp] = []
                    expiries_map[exp].append(c.tradingClass)
                for s in c.strikes:
                    strikes_set.add(s)

        if not expiries_map:
            logger.warning(
                f"[{symbol}] No matching expiries found in chains for {target_exchange}"
            )
            return None

        # today's date for 0 DTE
        today = get_us_et_now().strftime("%Y%m%d")
        selected_trading_class = None

        if today in expiries_map:
            # Prefer the standard symbol if multiple classes exist
            classes = expiries_map[today]
            selected_trading_class = symbol if symbol in classes else classes[0]
        else:
            logger.warning(f"[{symbol}] No 0 DTE expiry available for {today}")
            # Fall back to nearest available expiry
            sorted_expiries = sorted(expiries_map.keys())
            if not sorted_expiries:
                return None
            today = sorted_expiries[0]
            classes = expiries_map[today]
            selected_trading_class = symbol if symbol in classes else classes[0]
            logger.info(
                f"[{symbol}] Using nearest expiry: {today} (Class: {selected_trading_class})"
            )

        # Find ATM strike
        strikes = sorted(list(strikes_set))
        if not strikes:
            return None
        atm_strike = min(strikes, key=lambda x: abs(x - underlying_price))

        logger.info(
            f"[{symbol}] Selected Option: Strike={atm_strike}, Expiry={today}, Right={right}, Class={selected_trading_class}, Exchange={target_exchange}"
        )

        # Create option contract
        if underlying_contract.secType == "IND":
            option = Option(symbol, today, atm_strike, right, "SMART", multiplier="100")
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

        if not qualified:
            logger.error(f"[{symbol}] Failed to qualify option contract: {option}")
            return None

        return {
            "contract": qualified[0],
            "symbol": symbol,
            "strike": atm_strike,
            "expiry": today,
            "right": right,
        }

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
    quantity: int = IBKR_QUANTITY,
) -> bool:
    """
    Execute ORB entry order with SL/TP for IBKR.

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

        logger.info(f"[{symbol}] 📥 ORB ENTRY: {direction} at {entry_price:.2f}")
        logger.info(
            f"[{symbol}]   Option Type: {'Call' if option_right == 'C' else 'Put'}"
        )
        logger.info(f"[{symbol}]   SL: {stop_loss:.2f}, TP: {take_profit:.2f}")

        # Get underlying contract (Index or Future)
        if symbol in ["ES", "NQ"]:
            underlying = await ibkr_client.get_front_month_contract(symbol)
        else:
            underlying = Index(symbol, "CBOE", "USD")
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
        ibkr_client.ib.reqMktData(option_contract, "", False, False)
        await asyncio.sleep(1)
        option_ticker = ibkr_client.ib.ticker(option_contract)
        option_price = option_ticker.last or option_ticker.close or option_ticker.bid

        if not option_price:
            logger.error(f"[{symbol}] Failed to get option price")
            return False

        logger.info(f"[{symbol}]   Option Price: ${option_price:.2f}")

        # Calculate SL/TP in option price terms
        # Using the risk/reward ratio on the underlying to set option SL/TP
        risk_pct = abs(stop_loss - entry_price) / entry_price
        reward_pct = abs(take_profit - entry_price) / entry_price

        option_sl = option_price * (1 - risk_pct)
        option_tp = option_price * (1 + reward_pct)

        logger.info(f"[{symbol}]   Option SL: ${option_sl:.2f}, TP: ${option_tp:.2f}")

        # Place bracket order
        qty = quantity

        order_result = await ibkr_client.place_bracket_order(
            option_contract=option_contract,
            quantity=qty,
            stop_loss_price=max(0.05, round(option_sl, 2)),  # Minimum $0.05
            target_price=round(option_tp, 2),
        )

        if order_result and order_result.get("parent_trade"):
            # Track position
            ORB_ACTIVE_POSITIONS[symbol] = {
                "direction": direction,
                "entry_price": entry_price,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "option_contract": option_contract,
                "option_price": option_price,
                "qty": qty,
                "entry_time": get_us_et_now(),
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
            logger.error(f"[{symbol}] ORB Entry order failed: {order_result}")
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
    """Force close any open position for symbol."""
    if symbol not in ORB_ACTIVE_POSITIONS:
        return

    position = ORB_ACTIVE_POSITIONS[symbol]

    try:
        logger.warning(
            f"[{symbol}] ⚠️ FORCE EXIT ({reason}): Closing {position['direction']}"
        )

        # Get current positions from API
        positions = await ibkr_client.get_positions()

        for pos in positions:
            if pos.get("symbol") == symbol and pos.get("quantity", 0) != 0:
                # Close position with market order
                option_contract = position.get("option_contract")
                qty = abs(pos.get("quantity", 0))

                from ib_async import MarketOrder

                order = MarketOrder("SELL" if pos.get("quantity") > 0 else "BUY", qty)
                ibkr_client.ib.placeOrder(option_contract, order)

                await asyncio.sleep(2)

                msg = (
                    f"⚠️ ORB FORCE EXIT ({reason}): {symbol}\n"
                    f"Position: {position['direction']}\n"
                    f"Entry: ${position['entry_price']:.2f}"
                )
                send_telegram(msg, broker="IBKR")
                logger.info(f"[{symbol}] ✅ Position closed successfully")
                break

        # Remove from tracking
        del ORB_ACTIVE_POSITIONS[symbol]

    except Exception as e:
        logger.exception(f"[{symbol}] Force exit error: {e}")
        send_telegram(f"❌ Force Exit Error ({symbol}): {str(e)[:100]}", broker="IBKR")


# -----------------------------
# ORB Signal Monitor
# -----------------------------
async def orb_signal_monitor(
    symbol: str,
    ibkr_client: IBKRClient,
):
    """
    Monitor for ORB breakout signals for a symbol.

    Args:
        symbol: Trading symbol (SPX, NDX)
        ibkr_client: IBKR client
    """
    logger.info(f"[{symbol}] 📊 ORB Signal Monitor started")

    # Get underlying contract (Front month for futures)
    if symbol in ["ES", "NQ"]:
        underlying = await ibkr_client.get_front_month_contract(symbol)
    else:
        underlying = Index(symbol, "CBOE", "USD")
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
            now = get_us_et_now()

            # Check if market is open
            if not is_us_market_open():
                logger.debug(f"[{symbol}] Market closed, waiting...")
                await asyncio.sleep(60)
                continue

            # Check force exit time (15 min before close)
            if should_force_exit(
                now, MARKET_CLOSE_TIME, exit_before_minutes=15, symbol=symbol
            ):
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
                    symbol=symbol,
                    direction=breakout,
                    quantity=IBKR_QUANTITY,
                    stop_loss=stop_loss,
                    take_profit=take_profit,
                    entry_price=entry_price,
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
        # Initialize client and clear cache
        ibkr_client = IBKRClient()
        ibkr_client.option_chains_cache.clear()
        await ibkr_client.connect_async()

        # Wait for ORB period to complete
        if await wait_for_orb_complete():
            # Start signal monitors
            tasks = []
            for symbol in ORB_IBKR_SYMBOLS:
                task = asyncio.create_task(orb_signal_monitor(symbol, ibkr_client))
                tasks.append(task)

            if tasks:
                await asyncio.gather(*tasks)
        else:
            logger.info("ORB wait aborted")

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
