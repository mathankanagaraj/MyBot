#!/usr/bin/env python3
"""
Test script for heartbeat functionality
"""
import asyncio
import sys
import os
from datetime import datetime

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from core.logger import logger

# Mock the _STOP_EVENT for testing
class MockEvent:
    def is_set(self):
        return False

_STOP_EVENT = MockEvent()

async def sleep_until_next(seconds):
    """Sleep for a period but allow cancellation."""
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        return

async def heartbeat_task(interval=5):  # Use 5 seconds for testing
    """Continuous heartbeat to show bot is alive."""
    logger.info("� Heartbeat task started")
    heartbeat_count = 0

    while not _STOP_EVENT.is_set():
        try:
            heartbeat_count += 1
            now_utc = datetime.utcnow()
            logger.info(f"� Heartbeat #{heartbeat_count}: {now_utc.strftime('%H:%M:%S')} UTC")

            # Sleep for interval, but check for cancellation
            await sleep_until_next(interval)

        except asyncio.CancelledError:
            logger.info("� Heartbeat task cancelled")
            break
        except Exception as e:
            logger.error(f"� Heartbeat task error: {e} (type: {type(e).__name__})")
            import traceback
            logger.error(f"� Heartbeat traceback: {traceback.format_exc()}")
            await sleep_until_next(1)  # Retry sooner on error

    logger.info("� Heartbeat task stopped")

async def test_heartbeat():
    """Test heartbeat for 30 seconds"""
    print("Starting heartbeat test for 30 seconds...")

    # Start heartbeat task
    heartbeat = asyncio.create_task(heartbeat_task(interval=5))

    # Let it run for 30 seconds
    await asyncio.sleep(30)

    # Stop it
    heartbeat.cancel()
    try:
        await heartbeat
    except Exception:
        pass

    print("Heartbeat test completed")

if __name__ == "__main__":
    asyncio.run(test_heartbeat())