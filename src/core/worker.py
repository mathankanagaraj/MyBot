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
from core.signal_engine import detect_5m_entry, detect_15m_bias
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
    logger.info("[%s] Worker task started", symbol)
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
                    logger.info("[%s] Position closed detected via Angel API poll", symbol)
                    cash_mgr.force_release(symbol)
                    send_telegram(f"✅ {symbol} position closed")

                await asyncio.sleep(MONITOR_INTERVAL)
                continue

            # Get latest bars from BarManager
            df5, df15 = await bar_manager.get_resampled()

            if df15.empty:
                await asyncio.sleep(60)  # Wait for more data
                continue

            # Detect 15m bias
            bias = detect_15m_bias(df15)
            if not bias:
                await asyncio.sleep(60)
                continue

            # Avoid duplicate triggers
            now = datetime.utcnow()
            if last_15m_signal_time and (now - last_15m_signal_time) < timedelta(minutes=15):
                await asyncio.sleep(60)
                continue

            # Notify 15m bias found
            send_telegram(f"📊 [{symbol}] 15m Trend: {bias}. Looking for 5m entry...")
            last_15m_signal_time = now

            checks = 0
            entered = False

            # Look for 5m entry confirmation
            while checks < MAX_5M_CHECKS and not entered and not _STOP:
                await asyncio.sleep(60)  # Wait for next 1m bar

                df5_new, df15_new = await bar_manager.get_resampled()
                if df5_new.empty or df15_new.empty:
                    checks += 1
                    continue

                # Revalidate 15m bias hasn't flipped
                bias_now = detect_15m_bias(df15_new)
                if bias_now != bias:
                    send_telegram(f"⚠️ {symbol} 15m bias changed {bias} → {bias_now}, aborting")
                    break

                # Check 5m entry conditions
                entry_ok, details = detect_5m_entry(df5_new, bias)
                if not entry_ok:
                    checks += 1
                    continue

                # Entry signal confirmed!
                logger.info(f"[{symbol}] Entry signal confirmed: {bias}")

                # Get underlying price
                # For indices: Get futures price (for signal accuracy)
                # For stocks: Get stock price
                from core.config import INDEX_FUTURES

                if symbol in INDEX_FUTURES:
                    # Get current monthly futures price for indices
                    underlying = await angel_client.get_futures_price(symbol)
                else:
                    # Get stock price
                    underlying = await angel_client.get_last_price(symbol, exchange="NSE")

                if not underlying:
                    send_telegram(f"❌ {symbol} failed to get underlying price")
                    break

                # Select option contract (ALWAYS OPTIONS, never futures)
                # For indices: Uses futures price to select index option strike
                # For stocks: Uses stock price to select stock option strike
                opt_contract, reason = await find_option_contract_async(angel_client, symbol, bias, underlying)
                if not opt_contract:
                    send_telegram(f"❌ {symbol} option selection failed: {reason}")
                    break

                # Get option premium
                prem = await angel_client.get_last_price(opt_contract["symbol"], exchange="NFO")
                if prem is None or prem < MIN_PREMIUM:
                    send_telegram(f"❌ {symbol} premium too low: ₹{prem}")
                    break

                # Calculate position size
                lot_size = opt_contract.get("lot_size", 1)
                per_lot_cost = float(prem) * float(lot_size)
                qty = MAX_CONTRACTS_PER_TRADE
                est_cost = per_lot_cost * qty

                # Check if we can open position
                can_open = await cash_mgr.can_open_position(symbol, est_cost)
                if not can_open:
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
                    f"Placing order for {symbol} {bias}: "
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
                    send_telegram(f"❌ {symbol} order placement failed")
                    cash_mgr.force_release(symbol)
                    break

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

            await asyncio.sleep(60)

        except Exception as e:
            logger.exception("[%s] Worker exception: %s", symbol, e)
            send_telegram(f"⚠️ Error in {symbol} worker: {str(e)[:100]}")
            await asyncio.sleep(2)

    logger.info("[%s] Worker exiting", symbol)


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

    # Start worker tasks
    tasks = []
    for symbol in SYMBOLS:
        bar_mgr = bar_managers.get(symbol)
        tasks.append(worker_loop(symbol, angel_client, cash_mgr, bar_mgr))

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
