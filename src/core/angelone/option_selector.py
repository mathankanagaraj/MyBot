# core/angelone/option_selector.py
import re
from datetime import datetime
from typing import Optional, Tuple
from dataclasses import dataclass

from core.config import INDEX_FUTURES
from core.logger import logger


@dataclass
class OptionSelection:
    symbol: str
    token: str
    strike: float
    expiry: datetime.date
    expiry_str: str
    right: str
    lot_size: int


async def find_option_contract_async(
    angel_client, symbol: str, bias: str, underlying_price: float
) -> Tuple[Optional[OptionSelection], str]:
    """
    Find suitable NSE option contract for trading.
    Returns typed OptionSelection object.
    """
    try:
        if not angel_client.scrip_master:
            return None, "scrip_master_not_loaded"

        return await find_current_monthly_option(
            angel_client, symbol, bias, underlying_price
        )

    except Exception as e:
        logger.exception(f"Error selecting option: {e}")
        return None, f"error:{e}"


async def find_current_monthly_option(
    angel_client, symbol: str, bias: str, underlying_price: float
) -> Tuple[Optional[OptionSelection], str]:
    """
    Find current monthly option contract for any symbol (index or stock).
    """
    try:
        # Determine option type
        right = "CE" if bias == "BULL" else "PE"

        # Determine instrument type
        is_index = symbol in INDEX_FUTURES
        instrument_type = "OPTIDX" if is_index else "OPTSTK"

        today = datetime.now().date()
        current_month = today.month
        current_year = today.year

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

                # Filter by expiry:
                # 1. Skip past dates
                if expiry_date < today:
                    continue

                # 2. For stocks, only consider current month (for liquidity/stability)
                if not is_index:
                    if not (
                        expiry_date.month == current_month
                        and expiry_date.year == current_year
                    ):
                        continue

                # 3. For indices, we allow weekly (near-term) expiries.
                # We'll pick the nearest one later.

                # Check option type (CE/PE)
                symbol_name = instrument.get("symbol", "")
                if not symbol_name.endswith(right):
                    continue

                # Extract strike price
                try:
                    # NSE option format: SYMBOLDDMMMYYSTRIKECE/PE
                    match = re.search(
                        r"[A-Z]+(\d{2}[A-Z]{3}\d{2})(\d+)(CE|PE)$", symbol_name
                    )
                    if not match:
                        continue

                    strike_str = match.group(2)
                    strike = float(strike_str)

                except (ValueError, AttributeError):
                    continue

                options.append(
                    OptionSelection(
                        symbol=symbol_name,
                        token=instrument.get("token"),
                        strike=strike,
                        expiry=expiry_date,
                        expiry_str=expiry_str,
                        right=right,
                        lot_size=int(instrument.get("lotsize", 1)),
                    )
                )

        if not options:
            period_type = "weekly/nearest" if is_index else "monthly"
            return None, f"no_{period_type}_options_found_for_{symbol}"

        # Sort by expiry (nearest first), then by strike
        options.sort(key=lambda x: (x.expiry, abs(x.strike - underlying_price)))

        # Select strikes closer to ATM for better delta (0.6-0.7 range)
        # Target: 0.5-1% ITM (In-The-Money)
        best_option = None

        if bias == "BULL":
            # For BULL (CE): Select strike slightly below spot (0.5-1% ITM)
            # This gives delta around 0.6-0.7
            target_strike = underlying_price * 0.995  # 0.5% below spot

            # Find strikes below spot price (ITM for CE)
            itm_options = [opt for opt in options if opt.strike < underlying_price]

            if itm_options:
                # Sort by strike descending (highest ITM strike first)
                itm_options.sort(key=lambda x: x.strike, reverse=True)
                # Pick the strike closest to target (prefer slightly ITM)
                best_option = min(
                    itm_options, key=lambda x: abs(x.strike - target_strike)
                )
            else:
                # Fallback: if no ITM available, pick nearest strike
                best_option = min(
                    options, key=lambda x: abs(x.strike - underlying_price)
                )

        else:  # BEAR
            # For BEAR (PE): Select strike slightly above spot (0.5-1% ITM)
            # This gives delta around -0.6 to -0.7
            target_strike = underlying_price * 1.005  # 0.5% above spot

            # Find strikes above spot price (ITM for PE)
            itm_options = [opt for opt in options if opt.strike > underlying_price]

            if itm_options:
                # Sort by strike ascending (lowest ITM strike first)
                itm_options.sort(key=lambda x: x.strike)
                # Pick the strike closest to target (prefer slightly ITM)
                best_option = min(
                    itm_options, key=lambda x: abs(x.strike - target_strike)
                )
            else:
                # Fallback: if no ITM available, pick nearest strike
                best_option = min(
                    options, key=lambda x: abs(x.strike - underlying_price)
                )

        if not best_option:
            return None, "no_suitable_option"

        logger.info(
            f"Selected option: {best_option.symbol} | Strike: {best_option.strike} | "
            f"Underlying: {underlying_price:.2f} | "
            f"ITM by: {abs(best_option.strike - underlying_price):.2f} | "
            f"Expiry: {best_option.expiry} | Lot Size: {best_option.lot_size}"
        )

        return best_option, "ok"

    except Exception as e:
        logger.exception(f"Error selecting option for {symbol}: {e}")
        return None, f"error:{e}"
