import asyncio
import signal

from core.config import MODE, BROKER, ANGEL_MODE, IBKR_MODE
from core.logger import setup_logging
from core.utils import send_telegram

# Import appropriate worker based on BROKER config
if BROKER in ['IBKR', 'BOTH']:
    from core.multi_broker_worker import run_multi_broker, stop_all_workers
else:
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
    if BROKER in ['IBKR', 'BOTH']:
        logger.info(f"Starting Multi-Broker Bot (BROKER={BROKER}, Angel={ANGEL_MODE}, IBKR={IBKR_MODE}) [AsyncIO]")
        send_telegram(f"🚀 Multi-Broker Bot Starting\n👼 Angel: {ANGEL_MODE}\n🏦 IBKR: {IBKR_MODE}")
    else:
        logger.info("Starting Intraday Options Bot (mode=%s) [AsyncIO]", MODE)
        send_telegram(f"🚀 Bot Starting Up (Mode: {MODE})")
    
    try:
        if BROKER in ['IBKR', 'BOTH']:
            asyncio.run(run_multi_broker())
        else:
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
