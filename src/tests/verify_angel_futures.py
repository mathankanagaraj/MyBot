# tests/verify_angel_futures.py
import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).resolve().parent.parent / "src"))

from core.angelone.client import AngelClient
from core.logger import logger


async def test_angel_futures():
    client = AngelClient()
    try:
        logger.info("Connecting to Angel One...")
        await client.connect_async()

        for symbol in ["NIFTY", "BANKNIFTY"]:
            logger.info(f"\n--- Testing symbol: {symbol} ---")

            # 1. Get current futures contract
            contract = await client.get_current_futures_contract(symbol)
            if not contract:
                logger.error(f"Failed to get current future for {symbol}")
                continue

            fut_symbol = contract["symbol"]
            token = contract["token"]
            logger.info(f"Resolved Future: {fut_symbol} (Token: {token})")

            # 2. Get historical data (NFO)
            logger.info(f"Fetching historical 1m data for {fut_symbol} (NFO)...")
            df = await client.req_historic_1m(
                fut_symbol, duration_days=1, exchange="NFO"
            )

            if df is not None and not df.empty:
                logger.info(
                    f"Successfully fetched {len(df)} bars. Latest close: {df['close'].iloc[-1]}"
                )
            else:
                logger.error(f"Failed to fetch historical data for {fut_symbol}")

    except Exception as e:
        logger.exception(f"Test failed: {e}")
    finally:
        client.disconnect()


if __name__ == "__main__":
    asyncio.run(test_angel_futures())
