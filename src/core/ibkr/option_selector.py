# core/ibkr/option_selector.py
"""
Option selection logic for IBKR US stock options.
Selects ITM options based on bias (CALL/PUT) and underlying price.
"""

import asyncio
from typing import Optional, Tuple, Any, List
from dataclasses import dataclass

from core.config import (
    OPTION_MIN_DTE, 
    OPTION_MAX_DTE,
    FUTURES_OPTION_MIN_DTE,
    FUTURES_OPTION_MAX_DTE,
    IBKR_FUTURES_EXCHANGES
)
from core.logger import logger


@dataclass
class OptionSelection:
    symbol: str
    contract: Any
    strike: float
    expiry: str
    right: str
    dte: int
    premium: float
    lot_size: int
    token: str


async def find_ibkr_option_contract(
    ibkr_client, symbol: str, bias: str, underlying_price: float
) -> Tuple[Optional[OptionSelection], str]:
    """
    Find suitable option contract for US stock based on bias and underlying price.

    Args:
        ibkr_client: IBKRClient instance
        symbol: Stock symbol (e.g., 'SPY', 'AAPL')
        bias: 'BULL' or 'BEAR'
        underlying_price: Current stock price

    Returns:
        (OptionSelection, reason_string)
    """
    try:
        logger.info(
            f"[{symbol}] Selecting option: Bias={bias}, Price=${underlying_price:.2f}"
        )

        # Determine if this is a futures symbol and use appropriate DTE range
        is_futures = symbol in IBKR_FUTURES_EXCHANGES
        min_dte = FUTURES_OPTION_MIN_DTE if is_futures else OPTION_MIN_DTE
        max_dte = FUTURES_OPTION_MAX_DTE if is_futures else OPTION_MAX_DTE
        
        if is_futures:
            logger.info(f"[{symbol}] Futures option - using extended DTE range: {min_dte}-{max_dte} days")

        # Get option chain with DTE filtering
        options = await ibkr_client.get_option_chain(
            symbol, underlying_price, min_dte, max_dte
        )
        if not options:
            return (
                None,
                f"No options available in {OPTION_MIN_DTE}-{OPTION_MAX_DTE} DTE range",
            )

        # Determine option type
        option_type = "C" if bias == "BULL" else "P"
        filtered = [opt for opt in options if opt["right"] == option_type]
        if not filtered:
            return None, f"No {option_type} options found"

        # Options already filtered by DTE in get_option_chain, no need to filter again
        # Just verify they have DTE field
        valid_options = [opt for opt in filtered if "dte" in opt]

        if not valid_options:
            return None, "No options with valid DTE"

        # Select strike (1-2 levels ITM)
        selected = _select_strike(valid_options, underlying_price, bias, symbol)

        # Get option premium asynchronously
        premium = await _get_option_price(ibkr_client, selected["contract"])
        if premium is None or premium <= 0:
            return None, "Could not get valid option price"

        # Prepare final selection
        # Ensure lot_size is an integer
        multiplier = getattr(selected["contract"], "multiplier", "100")
        try:
            lot_size = int(multiplier) if multiplier else 100
        except (ValueError, TypeError):
            lot_size = 100

        result = OptionSelection(
            symbol=selected["symbol"],
            contract=selected["contract"],
            strike=selected["strike"],
            expiry=selected["expiry"],
            right=selected["right"],
            dte=selected["dte"],
            premium=premium,
            lot_size=lot_size,
            token=selected["symbol"],
        )

        logger.info(
            f"[{symbol}] Selected: {result.symbol} Strike=${result.strike:.2f} "
            f"DTE={result.dte} Premium=${result.premium:.2f}"
        )

        return result, "Success"

    except Exception as e:
        logger.exception(f"[{symbol}] Error selecting option: {e}")
        return None, str(e)


def _select_strike(options: List[dict], underlying: float, bias: str, symbol: str = "") -> dict:
    """
    Select ITM option 1-2 strikes deep from ATM for better delta.
    Falls back to nearest ATM if insufficient ITM strikes exist.
    
    Strategy:
    - BULL CALL: Select strike 1-2 levels below current price (ITM)
    - BEAR PUT: Select strike 1-2 levels above current price (ITM)
    """
    if bias == "BULL":
        # Get all ITM calls (strike < current price)
        itm = sorted([o for o in options if o["strike"] < underlying], 
                     key=lambda x: x["strike"], reverse=True)  # Sort descending
        
        if len(itm) >= 2:
            # Select 2nd ITM strike (1-2 levels deep)
            selected = itm[1]
            logger.info(f"[{symbol}] BULL: Selected 2nd ITM strike ${selected['strike']:.2f} (underlying: ${underlying:.2f})")
            return selected
        elif len(itm) >= 1:
            # Only 1 ITM available, use it
            selected = itm[0]
            logger.info(f"[{symbol}] BULL: Selected 1st ITM strike ${selected['strike']:.2f} (underlying: ${underlying:.2f})")
            return selected
        else:
            # No ITM, fallback to nearest ATM
            selected = min(options, key=lambda x: abs(x["strike"] - underlying))
            logger.warning(f"[{symbol}] BULL: No ITM available, using ATM ${selected['strike']:.2f}")
            return selected
            
    else:  # BEAR
        # Get all ITM puts (strike > current price)
        itm = sorted([o for o in options if o["strike"] > underlying],
                     key=lambda x: x["strike"])  # Sort ascending
        
        if len(itm) >= 2:
            # Select 2nd ITM strike (1-2 levels deep)
            selected = itm[1]
            logger.info(f"[{symbol}] BEAR: Selected 2nd ITM strike ${selected['strike']:.2f} (underlying: ${underlying:.2f})")
            return selected
        elif len(itm) >= 1:
            # Only 1 ITM available, use it
            selected = itm[0]
            logger.info(f"[{symbol}] BEAR: Selected 1st ITM strike ${selected['strike']:.2f} (underlying: ${underlying:.2f})")
            return selected
        else:
            # No ITM, fallback to nearest ATM
            selected = min(options, key=lambda x: abs(x["strike"] - underlying))
            logger.warning(f"[{symbol}] BEAR: No ITM available, using ATM ${selected['strike']:.2f}")
            return selected


async def _get_option_price(
    ibkr_client, contract, timeout: float = 5.0
) -> Optional[float]:
    """
    Get current price for an option contract using polling.
    Args:
        ibkr_client: IBKRClient instance
        contract: IB Option contract
        timeout: maximum seconds to wait for valid price

    Returns:
        Option price or None
    """
    try:
        await ibkr_client.ib.qualifyContractsAsync(contract)
        ticker = ibkr_client.ib.reqMktData(contract, "", False, False)

        start_time = asyncio.get_event_loop().time()
        price = None

        while asyncio.get_event_loop().time() - start_time < timeout:
            bid = getattr(ticker, "bid", 0)
            ask = getattr(ticker, "ask", 0)
            last = getattr(ticker, "last", 0)
            close = getattr(ticker, "close", 0)

            # Handle potential nan or None values safely?
            # Usually ib_async initializes them to nan or 0.0.
            # We assume non-NaN positive values are valid.

            if bid > 0 and ask > 0:
                price = (bid + ask) / 2
                break
            elif last > 0:
                price = last
                break
            elif close > 0:
                price = close
                break
            await asyncio.sleep(0.1)

        ibkr_client.ib.cancelMktData(contract)
        return float(price) if price else None

    except Exception as e:
        logger.exception(f"Error getting option price: {e}")
        return None
