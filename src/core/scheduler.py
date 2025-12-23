import asyncio
import multiprocessing
import pytz
from datetime import datetime, timedelta
from typing import Optional, Callable

from core.logger import logger
from core.utils import send_telegram


async def run_strategy_loop(
    broker_name: str,
    strategy_name: str,
    session_func: Callable,
    stop_event: asyncio.Event,
    market_open_hour: int,
    market_open_minute: int,
    market_close_hour: int,
    market_close_minute: int,
    timezone_str: str,
    pre_connect_minutes: int = 30,
    process_isolation: bool = False,
    heartbeat_func: Optional[Callable] = None,
):
    """
    Generic Daily Strategy Loop.
    Handles startup checks, market hours, sleep cycles, and daily session execution.

    Args:
        broker_name: Name of broker (e.g. "ANGEL")
        strategy_name: Name of strategy (e.g. "ORB")
        session_func: Function to execute for the daily session.
                      If process_isolation is True, this must be a top-level function suitable for multiprocessing.Main logic should be inside this function.
                      If process_isolation is False, this function must be async.
        stop_event: Event to signal shutdown.
        market_open_hour: Market open hour (24h).
        market_open_minute: Market open minute.
        market_close_hour: Market close hour.
        market_close_minute: Market close minute.
        timezone_str: Timezone string (e.g. "Asia/Kolkata").
        pre_connect_minutes: Minutes before market open to wake up/connect.
        process_isolation: If True, spawns a separte Process for session_func. If False, awaits session_func().
        heartbeat_func: Optional async function to run as background heartbeat.
    """
    tz = pytz.timezone(timezone_str)

    logger.info(f"🚀 Starting {broker_name} {strategy_name} Strategy Loop")
    logger.info(
        f"   Market: {market_open_hour:02d}:{market_open_minute:02d} - {market_close_hour:02d}:{market_close_minute:02d} ({timezone_str})"
    )

    # Start Heartbeat
    heartbeat_task = None
    if heartbeat_func:
        heartbeat_task = asyncio.create_task(heartbeat_func())
        logger.info("💓 Heartbeat task started")

    try:
        while not stop_event.is_set():
            now = datetime.now(tz)

            # Define Market Times for TODAY
            market_open = now.replace(
                hour=market_open_hour,
                minute=market_open_minute,
                second=0,
                microsecond=0,
            )
            market_close = now.replace(
                hour=market_close_hour,
                minute=market_close_minute,
                second=0,
                microsecond=0,
            )
            pre_connect_time = market_open - timedelta(minutes=pre_connect_minutes)

            # ---------------------------------------------------------
            # 1. Post-Market Check (Too Late)
            # ---------------------------------------------------------
            if now >= market_close:
                logger.info(
                    f"⏳ Current time ({now.strftime('%H:%M')}) is past market close. Skipping daily session."
                )

            else:
                # ---------------------------------------------------------
                # 2. Pre-Market Wait
                # ---------------------------------------------------------
                if now < pre_connect_time:
                    wait_seconds = (pre_connect_time - now).total_seconds()
                    wait_hours = wait_seconds / 3600
                    logger.info(
                        f"⏳ Market closed. Sleeping {wait_hours:.2f}h ({wait_seconds:.0f}s) until pre-market ({pre_connect_time.strftime('%H:%M')})..."
                    )
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(stop_event.wait()), timeout=wait_seconds
                        )
                        if stop_event.is_set():
                            break
                    except asyncio.TimeoutError:
                        logger.info("⏰ Waking up for daily session...")

                if stop_event.is_set():
                    break

                # ---------------------------------------------------------
                # 3. Daily Session Execution
                # ---------------------------------------------------------
                logger.info(f"🌅 Starting Daily Session ({broker_name})")

                if process_isolation:
                    # Spawn Process
                    logger.info("🚀 Spawning Daily Session Process")
                    # Note: target func must be picked logic.
                    p = multiprocessing.Process(target=session_func)
                    p.start()

                    # Monitor Process
                    while p.is_alive():
                        if stop_event.is_set():
                            logger.info("🛑 Stopping daily session process...")
                            p.terminate()
                            p.join()
                            break
                        await asyncio.sleep(1)

                    if p.is_alive():
                        p.join()

                    if not stop_event.is_set():
                        logger.info("✅ Daily Session Process Finished")

                else:
                    # Async Await
                    try:
                        if asyncio.iscoroutinefunction(session_func):
                            await session_func()
                        else:
                            # If user provided a synch function for non-isolated mode, generic handling?
                            # Assume async if no isolation, or wrapped.
                            await session_func()
                    except Exception as e:
                        logger.error(f"Error in daily session: {e}", exc_info=True)

            if stop_event.is_set():
                break

            # ---------------------------------------------------------
            # 4. End of Day Sleep
            # ---------------------------------------------------------
            now = datetime.now(tz)
            # Target: Next Start Time (Pre-Connect)
            next_start = now.replace(
                hour=market_open_hour,
                minute=market_open_minute,
                second=0,
                microsecond=0,
            )
            next_start -= timedelta(minutes=pre_connect_minutes)

            # If next_start is in the past (e.g. it was morning), move to tomorrow
            if now >= next_start:
                next_start += timedelta(days=1)

            # Skip weekends (Sat=5, Sun=6)
            while next_start.weekday() > 4:
                next_start += timedelta(days=1)

            wait_sec = (next_start - now).total_seconds()
            wait_hours = wait_sec / 3600

            logger.info(
                f"💤 Daily cycle complete. Sleeping {wait_hours:.2f}h until {next_start.strftime('%d-%b %H:%M')}"
            )

            send_telegram(
                f"💤 Daily cycle complete ({broker_name} {strategy_name}).\n"
                f"Market is closed. Sleeping until {next_start.strftime('%d-%b %H:%M')}.",
                broker=broker_name,
            )

            try:
                await asyncio.wait_for(
                    asyncio.shield(stop_event.wait()), timeout=wait_sec
                )
                break
            except asyncio.TimeoutError:
                pass

    except Exception as e:
        logger.error(f"Fatal error in strategy loop: {e}", exc_info=True)
    finally:
        if heartbeat_task and not heartbeat_task.done():
            heartbeat_task.cancel()

        send_telegram(
            f"👋 {broker_name} {strategy_name} Worker Shutdown", broker=broker_name
        )
        logger.info(f"👋 {broker_name} {strategy_name} Loop Shutdown")
