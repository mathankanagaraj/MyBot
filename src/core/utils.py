# core/utils.py
import csv
import json
import os
from datetime import datetime

import requests

from core.config import (
    AUDIT_CSV,
    IBKR_TELEGRAM_CHAT_ID,
    IBKR_TELEGRAM_TOKEN,
    ANGEL_TELEGRAM_CHAT_ID,
    ANGEL_TELEGRAM_TOKEN,
)
from core.logger import logger


def send_telegram(text: str, broker: str = "ANGEL"):
    """
    Send Telegram notification using broker-specific tokens.
    
    Args:
        text: Message to send
        broker: "ANGEL" or "IBKR" (determines which Telegram bot to use)
    """
    # Select appropriate token and chat_id based on broker
    if broker.upper() == "IBKR":
        token = IBKR_TELEGRAM_TOKEN
        chat_id = IBKR_TELEGRAM_CHAT_ID
    else:  # Default to Angel One
        token = ANGEL_TELEGRAM_TOKEN
        chat_id = ANGEL_TELEGRAM_CHAT_ID
    
    if not token or not chat_id:
        logger.info(f"[{broker}] Telegram not configured: %s", text)
        return
    
    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=5)
    except Exception:
        logger.exception(f"[{broker}] Failed to send Telegram")


def init_audit_file():
    os.makedirs(os.path.dirname(AUDIT_CSV), exist_ok=True)
    if not os.path.exists(AUDIT_CSV):
        with open(AUDIT_CSV, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "timestamp",
                    "symbol",
                    "bias",
                    "option",
                    "entry_price",
                    "stop",
                    "target",
                    "exit_price",
                    "outcome",
                    "holding_seconds",
                    "details",
                ]
            )


def write_audit_row(**kwargs):
    os.makedirs(os.path.dirname(AUDIT_CSV), exist_ok=True)
    with open(AUDIT_CSV, "a", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                kwargs.get("timestamp", datetime.utcnow().isoformat()),
                kwargs.get("symbol"),
                kwargs.get("bias"),
                kwargs.get("option"),
                kwargs.get("entry_price"),
                kwargs.get("stop"),
                kwargs.get("target"),
                kwargs.get("exit_price"),
                kwargs.get("outcome"),
                kwargs.get("holding_seconds"),
                json.dumps(kwargs.get("details") or {}, default=str),
            ]
        )


# simple metrics aggregator
METRICS = {"trades": 0, "opened": 0, "closed": 0, "errors": 0}
