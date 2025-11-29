# core/logger.py
import logging
import os
from core.config import LOG_DIR, LOG_FILE

def setup_logging():
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
    logger = logging.getLogger("intraday_bot")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(LOG_FILE)
        sh = logging.StreamHandler()
        fmt = logging.Formatter("%(asctime)s — %(levelname)s — %(name)s — %(message)s")
        fh.setFormatter(fmt)
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger

# create module-level logger
logger = setup_logging()