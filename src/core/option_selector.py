# core/option_selector.py
import re
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
                    # NSE option format: SYMBOLDDMMMYYSTRIKECE/PE
                    # Examples: RELIANCE30DEC251200PE, NIFTY26DEC2421000CE
                    # Pattern: SYMBOL(letters) + DD(2 digits) + MMM(3 letters) + YY(2 digits) + STRIKE(digits) + CE/PE
                    match = re.search(r'[A-Z]+(\d{2}[A-Z]{3}\d{2})(\d+)(CE|PE)$', symbol_name)
                    if not match:
                        continue
                    
                    # Group 1: Date (e.g., "30DEC25")
                    # Group 2: Strike (e.g., "1200")
                    # Group 3: Type (CE/PE)
                    strike_str = match.group(2)
                    strike = float(strike_str)

                except (ValueError, AttributeError):
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

        # Select 1-2 strikes ITM for better delta and directional exposure
        # This works for all underlyings (indices and stocks) with different strike intervals
        
        best_option = None
        
        if bias == "BULL":
            # For BULL (CE): Select strike below spot (ITM)
            # Target: 1-2 strikes below spot, roughly 0.5-1.5% ITM
            target_strike = underlying_price * 0.99  # 1% below spot as target
            
            # Find strikes below spot price (ITM for CE)
            itm_options = [opt for opt in options if opt["strike"] < underlying_price]
            
            if itm_options:
                # Sort by strike descending (highest ITM strike first)
                itm_options.sort(key=lambda x: x["strike"], reverse=True)
                
                # Pick the strike closest to target (typically 1-2 strikes ITM)
                best_option = min(itm_options, key=lambda x: abs(x["strike"] - target_strike))
            else:
                # Fallback: if no ITM available, pick nearest strike
                best_option = min(options, key=lambda x: abs(x["strike"] - underlying_price))
                
        else:  # BEAR
            # For BEAR (PE): Select strike above spot (ITM)
            # Target: 1-2 strikes above spot, roughly 0.5-1.5% ITM
            target_strike = underlying_price * 1.01  # 1% above spot as target
            
            # Find strikes above spot price (ITM for PE)
            itm_options = [opt for opt in options if opt["strike"] > underlying_price]
            
            if itm_options:
                # Sort by strike ascending (lowest ITM strike first)
                itm_options.sort(key=lambda x: x["strike"])
                
                # Pick the strike closest to target (typically 1-2 strikes ITM)
                best_option = min(itm_options, key=lambda x: abs(x["strike"] - target_strike))
            else:
                # Fallback: if no ITM available, pick nearest strike
                best_option = min(options, key=lambda x: abs(x["strike"] - underlying_price))

        if not best_option:
            return None, "no_suitable_option"

        logger.info(
            f"Selected option: {best_option['symbol']} | Strike: {best_option['strike']} | "
            f"Underlying: {underlying_price:.2f} | "
            f"ITM by: {abs(best_option['strike'] - underlying_price):.2f} | "
            f"Expiry: {best_option['expiry']} | Lot Size: {best_option['lot_size']}"
        )

        return best_option, "ok"

    except Exception as e:
        logger.exception(f"Error selecting option for {symbol}: {e}")
        return None, f"error:{e}"
