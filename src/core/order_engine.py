# core/order_engine.py
from core.logger import logger


def place_market_buy_sync(angel_client, symbol, token, exchange, qty):
    """
    Place a market BUY order synchronously.

    Args:
        angel_client: AngelClient instance
        symbol: Trading symbol
        token: Symbol token
        exchange: Exchange (NSE, NFO, etc.)
        qty: Quantity

    Returns:
        Order response or None
    """
    try:
        order = angel_client.place_order(
            symbol=symbol,
            token=token,
            exchange=exchange,
            transaction_type="BUY",
            quantity=qty,
            order_type="MARKET",
        )
        return order
    except Exception:
        logger.exception("place_market_buy_sync error")
        return None


def place_market_sell_sync(angel_client, symbol, token, exchange, qty):
    """
    Place a market SELL order synchronously.

    Args:
        angel_client: AngelClient instance
        symbol: Trading symbol
        token: Symbol token
        exchange: Exchange (NSE, NFO, etc.)
        qty: Quantity

    Returns:
        Order response or None
    """
    try:
        order = angel_client.place_order(
            symbol=symbol,
            token=token,
            exchange=exchange,
            transaction_type="SELL",
            quantity=qty,
            order_type="MARKET",
        )
        return order
    except Exception:
        logger.exception("place_market_sell_sync error")
        return None
