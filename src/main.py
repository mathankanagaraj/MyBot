import asyncio
import signal

from core.config import BROKER, ANGEL_MODE, IBKR_MODE
from core.logger import setup_logging
from core.utils import send_telegram

logger = setup_logging()


def _signal_handler(sig, frame):
    msg = f"⚠️ Signal {sig} received: Bot shutting down/restarting..."
    logger.info(msg)
    
    if BROKER == "ANGEL":
        send_telegram(msg, broker="ANGEL")
        from core.angelone.worker import stop_angel_workers
        stop_angel_workers()
    elif BROKER == "IBKR":
        send_telegram(msg, broker="IBKR")
        from core.ibkr.worker import stop_ibkr_workers
        stop_ibkr_workers()
    else:
        send_telegram(msg, broker="ANGEL")  # Default fallback


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


async def run_multi_broker():
    """
    Run the broker worker based on BROKER configuration.
    With separate containers, each container runs ONE broker:
    - angel_bot container: Runs Angel One worker
    - ibkr_bot container: Runs IBKR worker
    """

    # With separate containers, run only the configured broker
    if BROKER == "ANGEL":
        logger.info(f"🇮🇳 Starting Angel One Bot (Mode: {ANGEL_MODE}) [AsyncIO]")
        send_telegram(f"🚀 Angel One Bot Starting (Mode: {ANGEL_MODE})", broker="ANGEL")
        try:
            from core.angelone.worker import run_angel_workers

            await run_angel_workers()
        except Exception as e:
            logger.exception(f"Error in Angel worker: {e}")
            send_telegram(f"🚨 Angel worker error: {str(e)[:100]}", broker="ANGEL")
        finally:
            logger.info("👋 Angel worker shutdown complete")

    elif BROKER == "IBKR":
        logger.info(f"🇺🇸 Starting IBKR Bot (Mode: {IBKR_MODE}) [AsyncIO]")
        send_telegram(f"🚀 IBKR Bot Starting (Mode: {IBKR_MODE})", broker="IBKR")
        try:
            from core.ibkr.worker import run_ibkr_workers

            await run_ibkr_workers()
        except Exception as e:
            logger.exception(f"Error in IBKR worker: {e}")
            send_telegram(f"🚨 IBKR worker error: {str(e)[:100]}", broker="IBKR")
        finally:
            logger.info("👋 IBKR worker shutdown complete")

    else:
        error_msg = f"❌ Invalid BROKER configuration: {BROKER}. Must be ANGEL or IBKR"
        logger.error(error_msg)
        send_telegram(error_msg, broker="ANGEL")  # Default fallback


def run():
    logger.info("🚀 Starting BOT main loop")
    try:
        asyncio.run(run_multi_broker())
        logger.info("✅ Bot completed successfully")
    except KeyboardInterrupt:
        logger.info("⚠️ Bot interrupted by user")
    except Exception as e:
        msg = f"🚨 CRITICAL: Bot crashed with error: {str(e)[:200]}"
        logger.exception("Main runtime error")
        # Send to both brokers in case of critical failure
        send_telegram(msg, broker="ANGEL")
        send_telegram(msg, broker="IBKR")
    finally:
        logger.info("👋 Bot shutdown complete")


if __name__ == "__main__":
    run()
