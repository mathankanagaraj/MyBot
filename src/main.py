import asyncio
import signal

from core.config import MODE
from core.logger import setup_logging
from core.utils import send_telegram
from core.worker import run_all_workers, stop_all_workers

logger = setup_logging()


def _signal_handler(sig, frame):
    msg = f"⚠️ Signal {sig} received: Bot shutting down/restarting..."
    logger.info(msg)
    send_telegram(msg)
    stop_all_workers()


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def run():
    logger.info("Starting Intraday Options Bot (mode=%s) [AsyncIO]", MODE)
    try:
        send_telegram(f"🚀 Bot Starting Up (Mode: {MODE})")
        asyncio.run(run_all_workers())
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
