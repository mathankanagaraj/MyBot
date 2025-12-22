# tests/verify_future_contracts.py
import asyncio
import sys
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).resolve().parent.parent / "src"))

from core.ibkr.client import IBKRClient
from core.ibkr.orb_worker_ibkr import get_0dte_option_chain
from core.logger import logger


async def test_contracts():
    client = IBKRClient()
    try:
        logger.info("Connecting to IBKR...")
        await client.connect_async()

        for symbol in ["ES", "NQ"]:
            logger.info(f"\n--- Testing symbol: {symbol} ---")

            # 1. Get front month contract
            future = await client.get_front_month_contract(symbol)
            if not future:
                logger.error(f"Failed to get front month for {symbol}")
                continue

            logger.info(
                f"Qualified Future: {future.localSymbol} (conId: {future.conId})"
            )

            # 2. Get last price
            ticker = await client.ib.reqTickersAsync(future)
            price = ticker[0].last or ticker[0].close
            if not price:
                logger.warning("No live price, trying snapshot...")
                client.ib.reqMktData(future, "", False, False)
                await asyncio.sleep(2)
                ticker = client.ib.ticker(future)
                price = ticker.last or ticker.close

            if not price:
                logger.error(f"Could not get price for {future.localSymbol}")
                continue

            logger.info(f"Current Price: {price}")

            # 3. Get 0 DTE option chain
            option_data = await get_0dte_option_chain(
                client, symbol, future, price, "C"
            )
            if option_data:
                logger.info(
                    f"Successfully found 0 DTE Call: {option_data['contract'].localSymbol}"
                )
                logger.info(
                    f"Strike: {option_data['strike']}, Expiry: {option_data['expiry']}"
                )
            else:
                logger.error(f"Failed to find 0 DTE option for {symbol}")

            # 4. Get historical data
            logger.info(
                f"Fetching historical 30m data for {symbol} using qualified contract..."
            )
            df = await client.get_historical_bars_direct(
                symbol=symbol, bar_size="30 mins", duration_str="1 D", contract=future
            )
            if df is not None and not df.empty:
                logger.info(
                    f"Successfully fetched {len(df)} bars. Latest close: {df['close'].iloc[-1]}"
                )
            else:
                logger.error(f"Failed to fetch historical data for {symbol}")

    except Exception as e:
        logger.exception(f"Test failed: {e}")
    finally:
        client.disconnect()


if __name__ == "__main__":
    asyncio.run(test_contracts())
