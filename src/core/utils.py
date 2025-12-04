# core/utils.py
import csv
import json
import os
from datetime import datetime
from datetime import time as dtime

import pytz
import requests

from core.config import (
    AUDIT_CSV,
    NSE_MARKET_CLOSE_HOUR,
    NSE_MARKET_CLOSE_MINUTE,
    NSE_MARKET_OPEN_HOUR,
    NSE_MARKET_OPEN_MINUTE,
    TELEGRAM_CHAT_ID,
    TELEGRAM_TOKEN,
)
from core.logger import logger


def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.debug("Telegram not configured: %s", text)
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

# Market hours (IST - Indian Standard Time)
IST = pytz.timezone("Asia/Kolkata")


def get_ist_now():
    """Get current time in IST timezone"""
    return datetime.now(IST)


def utc_to_ist(utc_dt):
    """
    Convert UTC datetime to IST datetime for display.
    
    Args:
        utc_dt: datetime object (naive or UTC-aware)
        
    Returns:
        IST-aware datetime
    """
    if utc_dt.tzinfo is None:
        utc_dt = utc_dt.replace(tzinfo=pytz.utc)
    return utc_dt.astimezone(IST)


def is_market_open(now_utc=None):
    """
    Check if NSE market is open.
    NSE Trading Hours: Monday-Friday, 9:15 AM - 3:30 PM IST
    """
    if not now_utc:
        now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    now_ist = now_utc.astimezone(IST)

    # Check if weekend
    if now_ist.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False

    # NSE market hours: 9:15 AM - 3:30 PM IST
    open_t = dtime(NSE_MARKET_OPEN_HOUR, NSE_MARKET_OPEN_MINUTE)
    close_t = dtime(NSE_MARKET_CLOSE_HOUR, NSE_MARKET_CLOSE_MINUTE)

    # Strict check: Open if time is >= 9:15 AND < 15:30
    # We use < 15:30 because at 15:30:00 market is technically closed for new candle formation
    is_open = open_t <= now_ist.time() < close_t

    if not is_open:
        logger.debug(
            "NSE Market Closed Check: IST=%s (Weekday=%s) Open=%s Close=%s",
            now_ist,
            now_ist.weekday(),
            open_t,
            close_t,
        )

    return is_open


def get_seconds_until_market_close(now_utc=None):
    """
    Calculate seconds until NSE market close (3:30 PM IST).
    If already past market close, returns seconds until next trading day's close.

    Returns:
        Number of seconds until market close
    """
    from datetime import timedelta

    if not now_utc:
        now_utc = datetime.utcnow().replace(tzinfo=pytz.utc)
    now_ist = now_utc.astimezone(IST)

    # Create market close time for today
    close_time = now_ist.replace(
        hour=NSE_MARKET_CLOSE_HOUR,
        minute=NSE_MARKET_CLOSE_MINUTE,
        second=0,
        microsecond=0
    )

    # If we're past market close today, target next trading day
    if now_ist >= close_time:
        # Move to next day
        close_time += timedelta(days=1)
        
        # Skip weekends
        while close_time.weekday() >= 5:  # Saturday = 5, Sunday = 6
            close_time += timedelta(days=1)

    # Calculate seconds difference
    seconds = (close_time - now_ist).total_seconds()
    return max(0, int(seconds))

