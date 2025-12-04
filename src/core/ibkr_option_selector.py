# core/ibkr_option_selector.py
"""
Option selection logic for IBKR US stock options.
Selects ITM options based on bias (CALL/PUT) and underlying price.
"""
from datetime import datetime
from typing import Dict, Optional, Tuple

from core.config import (
    OPTION_MIN_DTE,
    OPTION_MAX_DTE,
)
from core.logger import logger


async def find_ibkr_option_contract(
    ibkr_client, symbol: str, bias: str, underlying_price: float
) -> Tuple[Optional[Dict], str]:
    """
    Find suitable option contract for US stock based on bias and underlying price.
    
    Args:
        ibkr_client: IBKRClient instance
        symbol: Stock symbol (e.g., 'SPY', 'AAPL')
        bias: 'BULLISH' or 'BEARISH'
        underlying_price: Current stock price
        
    Returns:
        (option_contract_dict, reason_string)
    """
    try:
        logger.info(f"[IBKR] [{symbol}] Selecting option: Bias={bias}, Price=${underlying_price:.2f}")
        
        # Get option chain
        options = await ibkr_client.get_option_chain(symbol, underlying_price)
        
        if not options:
            return None, "No options available"
        
        # Determine option type based on bias
        option_type = 'C' if bias == 'BULLISH' else 'P'
        
        # Filter by option type
        filtered = [opt for opt in options if opt['right'] == option_type]
        
        if not filtered:
            return None, f"No {option_type} options found"
        
        # Check expiration (DTE filter)
        today = datetime.now()
        valid_options = []
        
        for opt in filtered:
            expiry_date = datetime.strptime(opt['expiry'], '%Y%m%d')
            dte = (expiry_date - today).days
            
            if OPTION_MIN_DTE <= dte <= OPTION_MAX_DTE:
                opt['dte'] = dte
                valid_options.append(opt)
        
        if not valid_options:
            return None, f"No options with {OPTION_MIN_DTE}-{OPTION_MAX_DTE} DTE"
        
        # Select ITM option (strike below price for CALL, above for PUT)
        if bias == 'BULLISH':
            # For CALL: ITM means strike < underlying
            # Choose strike closest to underlying but below it
            itm_options = [opt for opt in valid_options if opt['strike'] < underlying_price]
            if itm_options:
                selected = max(itm_options, key=lambda x: x['strike'])  # Highest strike below price
            else:
                # No ATM available, use ATM
                selected = min(valid_options, key=lambda x: abs(x['strike'] - underlying_price))
        else:
            # For PUT: ITM means strike > underlying
            # Choose strike closest to underlying but above it
            itm_options = [opt for opt in valid_options if opt['strike'] > underlying_price]
            if itm_options:
                selected = min(itm_options, key=lambda x: x['strike'])  # Lowest strike above price
            else:
                # No ITM available, use ATM
                selected = min(valid_options, key=lambda x: abs(x['strike'] - underlying_price))
        
        # Get option premium (price)
        premium = await get_option_price(ibkr_client, selected['contract'])
        
        if premium is None or premium <= 0:
            return None, "Could not get valid option price"
        
        # Format result
        result = {
            'symbol': selected['symbol'],
            'contract': selected['contract'],
            'strike': selected['strike'],
            'expiry': selected['expiry'],
            'right': selected['right'],
            'dte': selected['dte'],
            'premium': premium,
            'lot_size': 100,  # US options are always 100 shares per contract
            'token': selected['symbol']  # Use symbol as token for consistency
        }
        
        logger.info(
            f"[IBKR] [{symbol}] Selected: {result['symbol']} "
            f"Strike=${result['strike']:.2f} DTE={result['dte']} Premium=${premium:.2f}"
        )
        
        return result, "Success"

    except Exception as e:
        logger.exception(f"[IBKR] Error selecting option for {symbol}: {e}")
        return None, str(e)


async def get_option_price(ibkr_client, contract) -> Optional[float]:
    """
    Get current price for an option contract.
    
    Args:
        ibkr_client: IBKRClient instance
        contract: IB Option contract
        
    Returns:
        Option price or None
    """
    try:
        # Qualify contract
        await ibkr_client.ib.qualifyContractsAsync(contract)
        
        # Request market data
        ticker = ibkr_client.ib.reqMktData(contract, '', False, False)
        
        # Wait for data to populate
        import asyncio
        await asyncio.sleep(2)
        
        # Try to get bid/ask midpoint
        if ticker.bid > 0 and ticker.ask > 0:
            price = (ticker.bid + ticker.ask) / 2
        elif ticker.last > 0:
            price = ticker.last
        elif ticker.close > 0:
            price = ticker.close
        else:
            logger.warning(f"[IBKR] No valid price data for {contract.symbol}")
            ibkr_client.ib.cancelMktData(contract)
            return None
        
        # Cancel market data
        ibkr_client.ib.cancelMktData(contract)
        
        return float(price)

    except Exception as e:
        logger.exception(f"[IBKR] Error getting option price: {e}")
        return None
