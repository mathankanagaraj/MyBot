# core/rate_limiter.py
"""
API Rate Limiter for Angel One API endpoints.
Implements token bucket algorithm with multiple time windows.
"""

import asyncio
import time
from collections import deque
from typing import Optional

from core.logger import logger


class TimeWindow:
    """
    Sliding window rate limiter for a specific time period.
    Tracks request timestamps and enforces rate limits.
    """

    def __init__(self, max_requests: int, window_seconds: float):
        """
        Initialize time window.

        Args:
            max_requests: Maximum number of requests allowed in the window
            window_seconds: Size of the time window in seconds
        """
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.requests = deque()  # Timestamps of recent requests
        self.lock = asyncio.Lock()

    def _clean_old_requests(self):
        """Remove requests outside the current time window"""
        now = time.time()
        cutoff = now - self.window_seconds

        while self.requests and self.requests[0] < cutoff:
            self.requests.popleft()

    def can_proceed(self) -> bool:
        """Check if a new request can be made without exceeding the limit"""
        self._clean_old_requests()
        return len(self.requests) < self.max_requests

    def time_until_available(self) -> float:
        """
        Calculate seconds to wait until a request slot is available.

        Returns:
            Seconds to wait (0 if can proceed immediately)
        """
        self._clean_old_requests()

        if len(self.requests) < self.max_requests:
            return 0.0

        # Wait until the oldest request expires
        oldest = self.requests[0]
        now = time.time()
        wait_time = (oldest + self.window_seconds) - now

        return max(0.0, wait_time)

    async def consume(self):
        """Record a request in this time window"""
        async with self.lock:
            self.requests.append(time.time())


class TokenBucket:
    """
    Token bucket rate limiter with multiple time windows.
    Enforces per-second, per-minute, and per-hour limits simultaneously.
    """

    def __init__(
        self,
        per_second: Optional[int] = None,
        per_minute: Optional[int] = None,
        per_hour: Optional[int] = None,
        safety_margin: float = 0.9,
    ):
        """
        Initialize token bucket with multiple time windows.

        Args:
            per_second: Maximum requests per second (None to disable)
            per_minute: Maximum requests per minute (None to disable)
            per_hour: Maximum requests per hour (None to disable)
            safety_margin: Use this fraction of the limit (0.9 = 90%)
        """
        self.windows = []

        if per_second:
            limit = max(1, int(per_second * safety_margin))
            self.windows.append(("second", TimeWindow(limit, 1.0)))

        if per_minute:
            limit = max(1, int(per_minute * safety_margin))
            self.windows.append(("minute", TimeWindow(limit, 60.0)))

        if per_hour:
            limit = max(1, int(per_hour * safety_margin))
            self.windows.append(("hour", TimeWindow(limit, 3600.0)))

    async def acquire(self, endpoint_name: str = ""):
        """
        Wait until all time windows allow a request.

        Args:
            endpoint_name: Name of the endpoint (for logging)
        """
        max_wait = 0.0
        blocking_window = None

        # Check all time windows
        for window_name, window in self.windows:
            if not window.can_proceed():
                wait_time = window.time_until_available()
                if wait_time > max_wait:
                    max_wait = wait_time
                    blocking_window = window_name

        # If we need to wait, log it
        if max_wait > 0:
            logger.debug(
                "⏳ Rate limit: waiting %.2fs for %s (%s window)",
                max_wait,
                endpoint_name,
                blocking_window,
            )
            await asyncio.sleep(max_wait)

        # Consume token from all windows
        for _, window in self.windows:
            await window.consume()


class APIRateLimiter:
    """
    Rate limiter for Angel One API endpoints.
    Manages separate rate limits for each API endpoint.
    """

    def __init__(self, enabled: bool = True, safety_margin: float = 0.9):
        """
        Initialize API rate limiter.

        Args:
            enabled: Whether rate limiting is enabled
            safety_margin: Use this fraction of the limit (0.9 = 90%)
        """
        self.enabled = enabled
        self.safety_margin = safety_margin

        # Define rate limits for each Angel One API endpoint
        # Based on official documentation
        self.limiters = {
            # Historical data - MOST CRITICAL (3/sec, 180/min, 5000/hour)
            "getCandleData": TokenBucket(
                per_second=3, per_minute=180, per_hour=5000, safety_margin=safety_margin
            ),
            # Get LTP - HIGH USAGE (10/sec, 500/min, 5000/hour)
            "ltpData": TokenBucket(
                per_second=10, per_minute=500, per_hour=5000, safety_margin=safety_margin
            ),
            # Place order (20/sec, 500/min, 1000/hour)
            "placeOrder": TokenBucket(
                per_second=20, per_minute=500, per_hour=1000, safety_margin=safety_margin
            ),
            # Modify order (20/sec, 500/min, 1000/hour)
            "modifyOrder": TokenBucket(
                per_second=20, per_minute=500, per_hour=1000, safety_margin=safety_margin
            ),
            # Cancel order (20/sec, 500/min, 1000/hour)
            "cancelOrder": TokenBucket(
                per_second=20, per_minute=500, per_hour=1000, safety_margin=safety_margin
            ),
            # Get positions (1/sec)
            "getPosition": TokenBucket(per_second=1, safety_margin=safety_margin),
            # Get RMS/balance (2/sec)
            "getRMS": TokenBucket(per_second=2, safety_margin=safety_margin),
            # Get order book (1/sec)
            "getOrderBook": TokenBucket(per_second=1, safety_margin=safety_margin),
            # Quote/market data (10/sec, 500/min, 5000/hour)
            "quote": TokenBucket(
                per_second=10, per_minute=500, per_hour=5000, safety_margin=safety_margin
            ),
        }

        logger.info(
            "🚦 API Rate Limiter initialized (enabled=%s, safety_margin=%.0f%%)",
            enabled,
            safety_margin * 100,
        )

    async def acquire(self, endpoint: str):
        """
        Wait until we can make a request to the specified endpoint.

        Args:
            endpoint: API endpoint name (e.g., 'getCandleData', 'ltpData')
        """
        if not self.enabled:
            return

        limiter = self.limiters.get(endpoint)
        if limiter:
            await limiter.acquire(endpoint)
        else:
            logger.warning(f"⚠️ No rate limiter defined for endpoint: {endpoint}")

    def get_stats(self) -> dict:
        """
        Get statistics about rate limiter usage.

        Returns:
            Dictionary with stats for each endpoint
        """
        stats = {}
        for endpoint, limiter in self.limiters.items():
            window_stats = {}
            for window_name, window in limiter.windows:
                window._clean_old_requests()
                window_stats[window_name] = {
                    "current": len(window.requests),
                    "limit": window.max_requests,
                    "utilization": len(window.requests) / window.max_requests * 100,
                }
            stats[endpoint] = window_stats
        return stats
