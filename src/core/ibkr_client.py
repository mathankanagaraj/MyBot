# core/ibkr_client.py
"""
Interactive Brokers client for US stock options trading.
Uses ib_insync library to connect to TWS/Gateway.
"""
import asyncio
from datetime import datetime
from typing import Dict, List, Optional

import pandas as pd
from ib_insync import IB, Stock, Option, MarketOrder, LimitOrder, util

from core.config import (
    IB_HOST,
    IB_PORT,
    IB_CLIENT_ID,
    IBKR_MODE,
    IBKR_PAPER_BALANCE,
)
from core.logger import logger


class IBKRClient:
    """
    IBKR API client for US stock options trading.
    Handles connection, data fetching, option selection, and order placement.
    """

    def __init__(self):
        self.ib = IB()
        self.connected = False
        self.mode = IBKR_MODE
        self.paper_balance = IBKR_PAPER_BALANCE
        self.option_chains_cache = {}  # Cache option chains by symbol

    async def connect_async(self, retry_backoff=1.0, max_backoff=60.0):
        """
        Connect to Interactive Brokers TWS/Gateway.
        Retries with exponential backoff on failure.
        """
        backoff = retry_backoff

        while True:
            try:
                logger.info(
                    f"[IBKR] Connecting to IB Gateway at {IB_HOST}:{IB_PORT}..."
                )

                # Connect to IB
                await self.ib.connectAsync(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID)

                self.connected = True
                logger.info(f"✅ [IBKR] Connected successfully (Mode: {self.mode})")

                # Get account summary
                if self.mode == "LIVE":
                    account_summary = await self.get_account_summary_async()
                    logger.info(
                        f"[IBKR] Account Balance: ${account_summary.get('AvailableFunds', 0):,.2f}"
                    )
                else:
                    logger.info(
                        f"[IBKR] Paper Trading Balance: ${self.paper_balance:,.2f}"
                    )

                return

            except Exception as e:
                logger.error(f"[IBKR] Connection failed: {repr(e)}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, max_backoff)

    def disconnect(self):
        """Disconnect from Interactive Brokers"""
        try:
            if self.ib.isConnected():
                self.ib.disconnect()
            self.connected = False
            logger.info("[IBKR] Disconnected from IB Gateway")
        except Exception as e:
            logger.exception(f"[IBKR] Error disconnecting: {e}")

    async def req_historic_1m(
        self,
        symbol: str,
        duration_days: float = 1,
    ) -> Optional[pd.DataFrame]:
        """
        Fetch historical 1-minute candle data for a US stock.

        Args:
            symbol: Stock symbol (e.g., 'SPY', 'AAPL')
            duration_days: Number of days of history to fetch

        Returns:
            DataFrame with OHLCV data indexed by datetime (UTC)
        """
        try:
            # Create stock contract
            contract = Stock(symbol, "SMART", "USD")

            # Qualify the contract
            await self.ib.qualifyContractsAsync(contract)

            # Calculate duration string
            if duration_days <= 1:
                duration_str = f"{int(duration_days * 24 * 60 * 60)} S"  # seconds
            else:
                duration_str = f"{int(duration_days)} D"

            # Request historical data
            logger.debug(
                f"[IBKR] [{symbol}] Requesting {duration_days} days of 1m data..."
            )

            # Use formatDate=1 (string) to match reference project
            bars = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=duration_str,
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,  # Regular trading hours only
                formatDate=1,  # Return as string (YYYYMMDD  HH:mm:ss)
            )

            if not bars:
                logger.warning(f"[IBKR] [{symbol}] No historical data returned")
                return None

            # Convert to DataFrame
            df = util.df(bars)

            if df is None or df.empty:
                return None

            # Rename columns to match Angel One format
            # Reference project: df['datetime'] = pd.to_datetime(df['date'])
            df["datetime"] = pd.to_datetime(df["date"])

            # Handle timezone conversion manually as in reference project
            if df["datetime"].dt.tz is not None:
                df["datetime"] = (
                    df["datetime"].dt.tz_convert("UTC").dt.tz_localize(None)
                )
            else:
                # IBKR returns exchange time (ET) for US stocks with formatDate=1
                # We need to convert ET to UTC
                # Assuming input is ET (naive), localize to ET then convert to UTC
                df["datetime"] = (
                    df["datetime"]
                    .dt.tz_localize("America/New_York")
                    .dt.tz_convert("UTC")
                    .dt.tz_localize(None)
                )

            # Set datetime as index and select columns
            df = df.set_index("datetime")[["open", "high", "low", "close", "volume"]]

            logger.debug(f"[IBKR] [{symbol}] Successfully fetched {len(df)} 1m bars")
            return df

        except Exception as e:
            logger.exception(f"[IBKR] Error fetching historical data for {symbol}: {e}")
            return None

    async def get_option_chain(
        self, symbol: str, underlying_price: float
    ) -> List[Dict]:
        """
        Get option chain for a stock.

        Args:
            symbol: Stock symbol
            underlying_price: Current stock price

        Returns:
            List of option contract dictionaries
        """
        try:
            # Create stock contract
            stock = Stock(symbol, "SMART", "USD")
            await self.ib.qualifyContractsAsync(stock)

            # Get option chains
            chains = await self.ib.reqSecDefOptParamsAsync(
                stock.symbol, "", stock.secType, stock.conId
            )

            if not chains:
                logger.warning(f"[IBKR] [{symbol}] No option chains found")
                return []

            # Get the first chain (usually the one we want)
            chain = chains[0]

            # Filter strikes around current price (±20%)
            min_strike = underlying_price * 0.8
            max_strike = underlying_price * 1.2
            strikes = [s for s in chain.strikes if min_strike <= s <= max_strike]

            # Get near-term expirations (within 7 days)
            today = datetime.now()
            near_expiries = [
                exp
                for exp in chain.expirations
                if 2 <= (datetime.strptime(exp, "%Y%m%d") - today).days <= 7
            ]

            options = []

            # Create option contracts for each strike/expiry combination
            for expiry in near_expiries[:3]:  # Limit to 3 nearest expiries
                for strike in strikes:
                    # Create CALL and PUT contracts
                    for right in ["C", "P"]:
                        option = Option(symbol, expiry, strike, right, "SMART")
                        options.append(
                            {
                                "symbol": f"{symbol}{expiry[2:]}{right}{int(strike)}",
                                "strike": strike,
                                "expiry": expiry,
                                "right": right,
                                "contract": option,
                            }
                        )

            logger.info(f"[IBKR] [{symbol}] Found {len(options)} option contracts")
            return options

        except Exception as e:
            logger.exception(f"[IBKR] Error getting option chain for {symbol}: {e}")
            return []

    async def get_last_price(
        self, symbol: str, contract_type: str = "STOCK"
    ) -> Optional[float]:
        """
        Get current last traded price for a symbol.

        Args:
            symbol: Trading symbol
            contract_type: "STOCK" or "OPTION"

        Returns:
            Last traded price or None
        """
        try:
            if contract_type == "STOCK":
                contract = Stock(symbol, "SMART", "USD")
            else:
                # For options, symbol should be the full option symbol
                # This is simplified - would need proper parsing
                logger.warning(
                    f"[IBKR] Option price lookup not fully implemented: {symbol}"
                )
                return None

            await self.ib.qualifyContractsAsync(contract)

            # Request market data
            ticker = self.ib.reqMktData(contract, "", False, False)
            await asyncio.sleep(1)  # Give it time to populate

            # Get last price
            if ticker.last > 0:
                price = ticker.last
            elif ticker.close > 0:
                price = ticker.close
            else:
                logger.warning(f"[IBKR] [{symbol}] No valid price data")
                return None

            # Cancel market data
            self.ib.cancelMktData(contract)

            return float(price)

        except Exception as e:
            logger.exception(f"[IBKR] Error getting last price for {symbol}: {e}")
            return None

    async def place_order(
        self,
        contract: Option,
        action: str,
        quantity: int,
        order_type: str = "MARKET",
        limit_price: float = 0.0,
    ) -> Optional[Dict]:
        """
        Place an order for an option contract.

        Args:
            contract: IB Option contract
            action: "BUY" or "SELL"
            quantity: Number of contracts
            order_type: "MARKET" or "LIMIT"
            limit_price: Limit price (for LIMIT orders)

        Returns:
            Order dict or None
        """
        try:
            # Qualify the contract first
            await self.ib.qualifyContractsAsync(contract)

            # Create order
            if order_type == "MARKET":
                order = MarketOrder(action, quantity)
            else:
                order = LimitOrder(action, quantity, limit_price)

            # Place order
            trade = self.ib.placeOrder(contract, order)

            # Wait for order to be acknowledged
            await asyncio.sleep(1)

            if trade.orderStatus.status in ["Submitted", "Filled", "PreSubmitted"]:
                logger.info(
                    f"[IBKR] Order placed: {trade.order.action} {trade.order.totalQuantity} @ {trade.orderStatus.status}"
                )
                return {
                    "order_id": trade.order.orderId,
                    "status": trade.orderStatus.status,
                    "action": action,
                    "quantity": quantity,
                }
            else:
                logger.error(f"[IBKR] Order failed: {trade.orderStatus.status}")
                return None

        except Exception as e:
            logger.exception(f"[IBKR] Error placing order: {e}")
            return None

    async def place_bracket_order(
        self,
        option_contract: Option,
        quantity: int,
        stop_loss_price: float,
        target_price: float,
    ) -> Optional[Dict]:
        """
        Simulate bracket order using separate entry, SL, and target orders.

        Args:
            option_contract: IB Option contract
            quantity: Number of contracts
            stop_loss_price: Stop loss price
            target_price: Target price

        Returns:
            Dict with order IDs or None
        """
        try:
            # Place entry order (Market BUY)
            entry = await self.place_order(
                option_contract, action="BUY", quantity=quantity, order_type="MARKET"
            )

            if not entry:
                logger.error("[IBKR] Entry order failed")
                return None

            # Wait for fill
            await asyncio.sleep(2)

            # Place stop-loss order
            sl_order = await self.place_order(
                option_contract,
                action="SELL",
                quantity=quantity,
                order_type="LIMIT",
                limit_price=stop_loss_price,
            )

            # Place target order
            target_order = await self.place_order(
                option_contract,
                action="SELL",
                quantity=quantity,
                order_type="LIMIT",
                limit_price=target_price,
            )

            result = {
                "entry_order_id": entry["order_id"],
                "sl_order_id": sl_order["order_id"] if sl_order else None,
                "target_order_id": target_order["order_id"] if target_order else None,
            }

            logger.info(f"[IBKR] Bracket order placed: {result}")
            return result

        except Exception as e:
            logger.exception(f"[IBKR] Error placing bracket order: {e}")
            return None

    async def get_positions(self) -> List[Dict]:
        """
        Get current open positions.

        Returns:
            List of position dictionaries
        """
        try:
            positions = self.ib.positions()

            result = []
            for pos in positions:
                result.append(
                    {
                        "symbol": pos.contract.symbol,
                        "position": pos.position,
                        "avgCost": pos.avgCost,
                        "marketValue": pos.marketValue,
                        "unrealizedPNL": pos.unrealizedPNL,
                    }
                )

            return result

        except Exception as e:
            logger.exception(f"[IBKR] Error getting positions: {e}")
            return []

    async def get_account_summary_async(self) -> Dict:
        """
        Get account summary including available funds and margins.

        Returns:
            Dict with account details
        """
        try:
            if self.mode == "PAPER":
                # Return paper trading balance
                return {
                    "AvailableFunds": self.paper_balance,
                    "TotalCashValue": self.paper_balance,
                    "NetLiquidation": self.paper_balance,
                }

            # Get account values for live trading
            account_values = self.ib.accountValues()

            summary = {}
            for value in account_values:
                if value.tag == "AvailableFunds":
                    summary["AvailableFunds"] = float(value.value)
                elif value.tag == "TotalCashValue":
                    summary["TotalCashValue"] = float(value.value)
                elif value.tag == "NetLiquidation":
                    summary["NetLiquidation"] = float(value.value)

            return summary

        except Exception as e:
            logger.exception(f"[IBKR] Error getting account summary: {e}")
            return {}
