# core/option_selector.py
from datetime import datetime
from typing import Optional, Tuple

from core.config import INDEX_FUTURES
from core.logger import logger


async def find_option_contract_async(
    angel_client, symbol: str, bias: str, underlying_price: float
) -> Tuple[Optional[dict], str]:
    """
    Find suitable NSE option contract for trading.

    Strategy:
    - For NIFTY/BANKNIFTY: Use futures data for signals, trade INDEX OPTIONS
    - For stocks: Use stock data for signals, trade STOCK OPTIONS

    All trading is in OPTIONS (current monthly expiry).

    Args:
        angel_client: AngelClient instance
        symbol: Underlying symbol (e.g., "NIFTY", "RELIANCE")
        bias: Trading bias ("BULL" or "BEAR")
        underlying_price: Current underlying price (from futures for indices, stock for stocks)

    Returns:
        Tuple of (option_contract_dict, reason)
        option_contract_dict contains: symbol, token, strike, expiry, right, lot_size
    """
    try:
        if not angel_client.scrip_master:
            return None, "scrip_master_not_loaded"

        # All symbols trade OPTIONS (current monthly expiry)
        return await find_current_monthly_option(angel_client, symbol, bias, underlying_price)

    except Exception as e:
        logger.exception(f"Error selecting option: {e}")
        return None, f"error:{e}"


async def find_current_monthly_option(
    angel_client, symbol: str, bias: str, underlying_price: float
) -> Tuple[Optional[dict], str]:
    """
    Find current monthly option contract for any symbol (index or stock).

    Args:
        angel_client: AngelClient instance
        symbol: Symbol (NIFTY, BANKNIFTY, or stock)
        bias: Trading bias ("BULL" or "BEAR")
        underlying_price: Current price

    Returns:
        Tuple of (option_contract_dict, reason)
    """
    try:
        # Determine option type
        right = "CE" if bias == "BULL" else "PE"

        # Get current date
        today = datetime.now().date()
        current_month = today.month
        current_year = today.year

        # Determine instrument type
        is_index = symbol in INDEX_FUTURES
        instrument_type = "OPTIDX" if is_index else "OPTSTK"

        # Filter options for this underlying
        options = []
        for instrument in angel_client.scrip_master:
            # Check if it's an option for our symbol
            if (
                instrument.get("exch_seg") == "NFO"
                and instrument.get("instrumenttype") == instrument_type
                and symbol in instrument.get("name", "")
            ):
                # Parse expiry date
                expiry_str = instrument.get("expiry")
                if not expiry_str:
                    continue

                try:
                    expiry_date = datetime.strptime(expiry_str, "%d%b%Y").date()
                except ValueError:
                    try:
                        expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
                    except ValueError:
                        continue

                # Only consider current month expiry
                if not (expiry_date.month == current_month and expiry_date.year == current_year):
                    continue

                # Check option type (CE/PE)
                symbol_name = instrument.get("symbol", "")
                if not symbol_name.endswith(right):
                    continue

                # Extract strike price
                try:
                    # NSE option format: NIFTY24DEC21000CE or RELIANCE24DEC2500CE
                    # Extract numeric part before CE/PE
                    strike_str = ""
                    for i, char in enumerate(symbol_name):
                        if char.isdigit():
                            strike_str += char
                        elif strike_str and (symbol_name[i : i + 2] in ["CE", "PE"]):
                            break

                    if not strike_str:
                        continue

                    # Strikes are typically in whole numbers for indices, may need division for stocks
                    strike = float(strike_str)
                    if strike > 10000:  # Likely in paise
                        strike = strike / 100.0

                except (ValueError, IndexError):
                    continue

                options.append(
                    {
                        "symbol": symbol_name,
                        "token": instrument.get("token"),
                        "strike": strike,
                        "expiry": expiry_date,
                        "expiry_str": expiry_str,
                        "right": right,
                        "lot_size": int(instrument.get("lotsize", 1)),
                    }
                )

        if not options:
            return None, f"no_current_month_options_found_for_{symbol}"

        # Sort by expiry (nearest first), then by strike
        options.sort(key=lambda x: (x["expiry"], abs(x["strike"] - underlying_price)))

        # Select best option based on strike proximity
        best_option = None
        best_diff = float("inf")

        for opt in options[:20]:  # Check top 20 candidates
            strike = opt["strike"]

            # Simple selection: slightly OTM
            if bias == "BULL":
                target_strike = underlying_price * 1.02  # 2% OTM
            else:
                target_strike = underlying_price * 0.98  # 2% OTM

            diff = abs(strike - target_strike)

            if diff < best_diff:
                best_diff = diff
                best_option = opt

        if not best_option:
            return None, "no_suitable_option"

        logger.info(
            f"Selected option: {best_option['symbol']} | Strike: {best_option['strike']} | "
            f"Expiry: {best_option['expiry']} | Lot Size: {best_option['lot_size']}"
        )

        return best_option, "ok"

    except Exception as e:
        logger.exception(f"Error selecting option for {symbol}: {e}")
        return None, f"error:{e}"
