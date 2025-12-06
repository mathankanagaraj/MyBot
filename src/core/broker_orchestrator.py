# core/broker_orchestrator.py
"""
Multi-broker orchestrator that routes to the correct broker worker.
With separate Docker containers, each container runs ONE broker:
- angel_bot container: Runs Angel One worker
- ibkr_bot container: Runs IBKR worker
"""
from core.config import BROKER
from core.logger import logger
from core.utils import send_telegram


async def run_multi_broker():
    """
    Run the broker worker based on BROKER configuration.
    With separate containers, each container runs ONE broker:
    - angel_bot container: Runs Angel One worker
    - ibkr_bot container: Runs IBKR worker
    """
    logger.info(f"🚀 Starting broker worker: {BROKER}")

    # With separate containers, run only the configured broker
    if BROKER == "ANGEL":
        logger.info("🇮🇳 Starting Angel One worker...")
        try:
            from core.angelone.worker import run_angel_workers

            await run_angel_workers()
        except Exception as e:
            logger.exception(f"Error in Angel worker: {e}")
            send_telegram(f"🚨 Angel worker error: {str(e)[:100]}")
        finally:
            logger.info("👋 Angel worker shutdown complete")

    elif BROKER == "IBKR":
        logger.info("🇺🇸 Starting IBKR worker...")
        try:
            from core.ibkr.worker import run_ibkr_workers

            await run_ibkr_workers()
        except Exception as e:
            logger.exception(f"Error in IBKR worker: {e}")
            send_telegram(f"🚨 IBKR worker error: {str(e)[:100]}")
        finally:
            logger.info("👋 IBKR worker shutdown complete")

    else:
        error_msg = f"❌ Invalid BROKER configuration: {BROKER}. Must be ANGEL or IBKR"
        logger.error(error_msg)
        send_telegram(error_msg)


def stop_all_workers():
    """Stop all broker workers"""
    from core.config import BROKER

    if BROKER == "ANGEL":
        from core.angelone.worker import stop_angel_workers

        stop_angel_workers()
    elif BROKER == "IBKR":
        from core.ibkr.worker import stop_ibkr_workers

        stop_ibkr_workers()
