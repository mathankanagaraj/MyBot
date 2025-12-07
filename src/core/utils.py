# core/utils.py
import csv
import json
import os
from datetime import datetime

import requests

from core.config import (
    AUDIT_CSV,
    TELEGRAM_CHAT_ID,
    TELEGRAM_TOKEN,
)
from core.logger import logger


def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info("Telegram not configured: %s", text)
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=5)
    except Exception:
        logger.exception("Failed to send Telegram")


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
