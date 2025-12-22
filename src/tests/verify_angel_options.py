# tests/verify_angel_options.py
import asyncio
import sys
from pathlib import Path
from datetime import datetime

# Add src to path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from core.angelone.client import AngelClient
from core.angelone.option_selector import find_option_contract_async
from core.logger import logger


async def test_angel_options():
    client = AngelClient()
    try:
        logger.info("Connecting to Angel One...")
        await client.connect_async()

        test_cases = [
            ("NIFTY", "BULL", 24500),  # Index (Weekly/Nearest)
            ("BANKNIFTY", "BEAR", 52000),  # Index (Weekly/Nearest)
            ("RELIANCE", "BULL", 1400),  # Stock (Monthly)
            ("INFY", "BEAR", 1800),  # Stock (Monthly)
        ]

        for symbol, bias, price in test_cases:
            logger.info(
                f"\n--- Testing Option Selection for: {symbol} (Bias: {bias}, Price: {price}) ---"
            )

            # Debug: List expiries
            is_index = symbol in ["NIFTY", "BANKNIFTY"]
            inst_type = "OPTIDX" if is_index else "OPTSTK"
            expiries = sorted(
                list(
                    set(
                        [
                            inst.get("expiry")
                            for inst in client.scrip_master
                            if inst.get("exch_seg") == "NFO"
                            and inst.get("instrumenttype") == inst_type
                            and symbol in inst.get("name", "")
                        ]
                    )
                )
            )
            logger.info(f"Available Expiries for {symbol}: {expiries}")

            # Debug: Check symbol names for 23DEC2025 if it exists
            target_debug_expiry = "23DEC2025"
            debug_symbols = [
                inst.get("symbol")
                for inst in client.scrip_master
                if inst.get("exch_seg") == "NFO"
                and inst.get("instrumenttype") == inst_type
                and symbol in inst.get("name", "")
                and inst.get("expiry") == target_debug_expiry
            ][:5]
            if debug_symbols:
                logger.info(
                    f"Sample Symbols for {target_debug_expiry}: {debug_symbols}"
                )
            else:
                logger.info(f"No symbols found for {target_debug_expiry}")

            # Debug: Check symbol names for nearest expiry
            if expiries:
                nearest = expiries[0]
                sample_symbols = [
                    inst.get("symbol")
                    for inst in client.scrip_master
                    if inst.get("exch_seg") == "NFO"
                    and inst.get("instrumenttype") == inst_type
                    and symbol in inst.get("name", "")
                    and inst.get("expiry") == nearest
                ][:5]
                logger.info(f"Sample Symbols for {nearest}: {sample_symbols}")

            option, status = await find_option_contract_async(
                client, symbol, bias, price
            )

            if option:
                logger.info(f"✅ Success: {option.symbol}")
                logger.info(f"   Token: {option.token}")
                logger.info(f"   Strike: {option.strike}")
                logger.info(f"   Expiry: {option.expiry}")
                logger.info(f"   Right: {option.right}")

                today = datetime.now().date()
                days_to_expiry = (option.expiry - today).days
                logger.info(f"   Days to expiry: {days_to_expiry}")

                if symbol in ["NIFTY", "BANKNIFTY"]:
                    if days_to_expiry <= 7:
                        logger.info(
                            f"   [OK] Index picked a near-term (weekly) expiry."
                        )
                    else:
                        logger.info(
                            f"   [NOTE] Index picked expiry {days_to_expiry} days away. (Might be the next available)"
                        )
                else:
                    if option.expiry.month == today.month:
                        logger.info(f"   [OK] Stock picked current monthly expiry.")
                    else:
                        logger.info(f"   [CHECK] Stock picked expiry: {option.expiry}")
            else:
                logger.error(f"❌ Failed: {status}")

    except Exception as e:
        logger.exception(f"Test failed: {e}")
    finally:
        client.disconnect()


if __name__ == "__main__":
    asyncio.run(test_angel_options())
