import asyncio
import signal

from core.config import BROKER, ANGEL_MODE, IBKR_MODE
from core.logger import setup_logging
from core.utils import send_telegram
from core.broker_orchestrator import run_multi_broker, stop_all_workers

logger = setup_logging()


def _signal_handler(sig, frame):
    msg = f"⚠️ Signal {sig} received: Bot shutting down/restarting..."
    logger.info(msg)
    send_telegram(msg)
    stop_all_workers()


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def run():
    # With separate containers, each runs a single broker
    if BROKER == "ANGEL":
        logger.info(f"🇮🇳 Starting Angel One Bot (Mode: {ANGEL_MODE}) [AsyncIO]")
        send_telegram(f"🚀 Angel One Bot Starting (Mode: {ANGEL_MODE})")
    elif BROKER == "IBKR":
        logger.info(f"🇺🇸 Starting IBKR Bot (Mode: {IBKR_MODE}) [AsyncIO]")
        send_telegram(f"🚀 IBKR Bot Starting (Mode: {IBKR_MODE})")
    else:
        logger.error(f"❌ Invalid BROKER setting: {BROKER}. Must be ANGEL or IBKR")
        send_telegram(f"❌ Invalid BROKER: {BROKER}")
        return

    try:
        asyncio.run(run_multi_broker())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        msg = f"🚨 CRITICAL: Bot crashed with error: {str(e)[:200]}"
        logger.exception("Main runtime error")
        send_telegram(msg)
    finally:
        logger.info("Main loop exiting...")


if __name__ == "__main__":
    run()
