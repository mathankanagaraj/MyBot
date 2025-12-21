"""
Interactive Brokers client for US stock options trading.
Uses ib_async library to connect to TWS/Gateway.
"""

import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pandas as pd
from ib_async import IB, Stock, Option, util

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
                logger.info(f"Connecting to IB Gateway at {IB_HOST}:{IB_PORT}...")

                # Connect to IB
                await self.ib.connectAsync(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID)

                self.connected = True
                logger.info(f"✅ Connected successfully (Mode: {self.mode})")

                # Get and log account summary (works for both live and paper trading)
                account_summary = await self.get_account_summary_async()
                available_funds = account_summary.get("AvailableFunds", 0)
                logger.info(f"Account Balance: ${available_funds:,.2f}")

                return

            except Exception as e:
                logger.error(f"Connection failed: {repr(e)}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, max_backoff)

    def disconnect(self):
        """Disconnect from Interactive Brokers"""
        try:
            if self.ib.isConnected():
                self.ib.disconnect()
            self.connected = False
            logger.info("Disconnected from IB Gateway")
        except Exception as e:
            logger.exception(f"Error disconnecting: {e}")

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
                          Use 0.0104 for 15 minutes (15/1440)
                          Use 1.0 for full trading day

        Returns:
            DataFrame with OHLCV data indexed by datetime (UTC)
        """
        try:
            contract = Stock(symbol, "SMART", "USD")
            await self.ib.qualifyContractsAsync(contract)

            # Calculate appropriate duration string based on duration_days
            # IBKR supports: S (seconds), D (days), W (weeks), M (months), Y (years)
            if duration_days < 0.1:  # Less than ~2.4 hours, use seconds
                duration_seconds = int(duration_days * 24 * 3600)
                duration_str = f"{duration_seconds} S"
            elif duration_days <= 1:
                duration_str = "1 D"
            else:
                duration_str = f"{int(duration_days)} D"

            logger.debug(f"[{symbol}] Requesting {duration_str} of 1m bars...")

            bars = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=duration_str,
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,  # returns naive string timestamps (US/Eastern)
            )

            if not bars:
                logger.warning(f"[{symbol}] No historical data returned")
                return None

            df = util.df(bars)
            if df is None or df.empty:
                return None

            # Convert the 'date' column (handle both naive ET and aware timestamps) → UTC naive
            df["datetime"] = pd.to_datetime(df["date"])

            # Check if the first timestamp is timezone-aware
            if df["datetime"].iloc[0].tzinfo is None:
                # Naive: assume America/New_York (IBKR default for US stocks)
                df["datetime"] = df["datetime"].dt.tz_localize("America/New_York")

            # Convert to UTC and then make naive (consistent with bot internals)
            df["datetime"] = df["datetime"].dt.tz_convert("UTC").dt.tz_localize(None)

            df = df.set_index("datetime")[["open", "high", "low", "close", "volume"]]

            logger.debug(f"[{symbol}] Fetched {len(df)} bars.")
            return df

        except Exception:
            logger.exception(f"Error fetching historical data for {symbol}")
            return None

    async def get_historical_bars_direct(
        self, symbol: str, bar_size: str = "15 mins", duration_str: str = "1 D"
    ) -> Optional[pd.DataFrame]:
        """
        Fetch historical bars directly at specified interval (no resampling).

        Args:
            symbol: Stock symbol (e.g., 'SPY', 'AAPL')
            bar_size: Bar size ("5 mins", "15 mins", "1 hour", etc.)
            duration_str: Duration ("1 D" for 1 day, "2 D" for 2 days)

        Returns:
            DataFrame with OHLCV data indexed by datetime (UTC naive)
        """
        try:
            contract = Stock(symbol, "SMART", "USD")
            await self.ib.qualifyContractsAsync(contract)

            logger.debug(f"[{symbol}] Requesting {duration_str} of {bar_size} bars...")

            bars = await self.ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr=duration_str,
                barSizeSetting=bar_size,
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )

            if not bars:
                logger.warning(f"[{symbol}] No historical {bar_size} data returned")
                return None

            df = util.df(bars)
            if df is None or df.empty:
                return None

            # Convert timestamps to UTC naive
            df["datetime"] = pd.to_datetime(df["date"])

            if df["datetime"].iloc[0].tzinfo is None:
                df["datetime"] = df["datetime"].dt.tz_localize("America/New_York")

            df["datetime"] = df["datetime"].dt.tz_convert("UTC").dt.tz_localize(None)
            df = df.set_index("datetime")[["open", "high", "low", "close", "volume"]]

            logger.info(
                f"[{symbol}] Fetched {len(df)} {bar_size} bars (latest close: ${df['close'].iloc[-1]:.2f})"
            )
            return df

        except Exception:
            logger.exception(f"Error fetching {bar_size} historical data for {symbol}")
            return None

    async def get_option_chain(
        self, symbol: str, underlying_price: float, min_dte: int = 2, max_dte: int = 7
    ) -> List[Dict]:
        """
        Get option chain for symbol filtered by DTE range.
        Returns options for ALL expiries within the DTE range.

        Args:
            symbol: Stock symbol
            underlying_price: Current stock price
            min_dte: Minimum days to expiry (default 2)
            max_dte: Maximum days to expiry (default 7)
        """
        try:
            # --- Underlying Contract ---
            stock = Stock(symbol, "SMART", "USD")
            await self.ib.qualifyContractsAsync(stock)

            chains = await self.ib.reqSecDefOptParamsAsync(
                stock.symbol, "", stock.secType, stock.conId
            )

            if not chains:
                logger.warning(f"[{symbol}] No option chains found")
                return []

            chain = chains[0]

            # --- Strike Filtering ---
            min_strike = underlying_price * 0.8
            max_strike = underlying_price * 1.2
            strikes = sorted(
                [
                    s
                    for s in chain.strikes
                    if isinstance(s, (int, float)) and min_strike <= s <= max_strike
                ]
            )

            if not strikes:
                logger.warning(f"[{symbol}] No strikes in ±20% range")
                return []

            # --- Expiry Filtering by DTE Range ---
            today = datetime.now(timezone.utc)
            valid_expiries = []

            for exp_str in chain.expirations:
                try:
                    exp = datetime.strptime(exp_str, "%Y%m%d").replace(
                        tzinfo=timezone.utc
                    )
                except ValueError:
                    continue

                dte = (exp - today).total_seconds() / 86400

                # Only keep expiries within the DTE range
                if min_dte <= dte <= max_dte:
                    valid_expiries.append((exp_str, dte))

            if not valid_expiries:
                logger.warning(
                    f"[{symbol}] No expiries found in {min_dte}-{max_dte} DTE range"
                )
                return []

            # Sort by DTE (closest first)
            valid_expiries.sort(key=lambda x: x[1])

            logger.info(
                f"[{symbol}] Found {len(valid_expiries)} valid expiries in {min_dte}-{max_dte} DTE range"
            )

            # --- Build Option Contracts for ALL valid expiries ---
            options = []

            for expiry, dte in valid_expiries:
                exp_date = datetime.strptime(expiry, "%Y%m%d")
                expiry_yymmdd = exp_date.strftime("%y%m%d")

                for strike in strikes:
                    for right in ["C", "P"]:
                        option = Option(symbol, expiry, strike, right, "SMART")

                        # OCC format: [root 6 chars][yymmdd][C/P][strike*1000 padded to 8 digits]
                        root = symbol.ljust(6)
                        strike1000 = f"{int(round(strike * 1000)):08d}"
                        occ_symbol = f"{root}{expiry_yymmdd}{right}{strike1000}"

                        options.append(
                            {
                                "symbol": occ_symbol,
                                "strike": strike,
                                "expiry": expiry,
                                "right": right,
                                "contract": option,
                                "dte": dte,
                            }
                        )

            logger.info(
                f"[{symbol}] Created {len(options)} option contracts across {len(valid_expiries)} expiries"
            )
            return options

        except Exception as e:
            logger.exception(f"Error getting option chain for {symbol}: {e}")
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
                logger.warning(f"Option price lookup not fully implemented: {symbol}")
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
                logger.warning(f"[{symbol}] No valid price data")
                return None

            # Cancel market data
            self.ib.cancelMktData(contract)

            return float(price)

        except Exception as e:
            logger.exception(f"Error getting last price for {symbol}: {e}")
            return None

    async def place_bracket_order(
        self,
        option_contract: Option,
        quantity: int,
        stop_loss_price: float,
        target_price: float,
    ) -> Optional[Dict]:
        """
        Place bracket order using IB's bracketOrder() helper with defensive coding.
        Polls for price data and order acknowledgment instead of blind sleeps.

        Args:
            option_contract: IB Option contract
            quantity: Number of contracts
            stop_loss_price: Stop loss price
            target_price: Target price

        Returns:
            Dict with order IDs and Trade objects or None
        """
        try:
            # 1. Qualify contract
            logger.info(f"Qualifying option contract: {option_contract.symbol}")
            await self.ib.qualifyContractsAsync(option_contract)

            # 2. Request market data and wait for useful prices (poll, not blind sleep)
            ticker = self.ib.reqMktData(option_contract, "", False, False)
            max_wait = 5.0
            waited = 0.0
            poll_interval = 0.25
            entry_price = None
            while waited < max_wait:
                # Prefer ask for buy (marketable limit)
                if getattr(ticker, "ask", 0) and ticker.ask > 0:
                    entry_price = round(ticker.ask, 2)
                    break
                if getattr(ticker, "last", 0) and ticker.last > 0:
                    entry_price = round(ticker.last * 1.01, 2)
                    break
                await asyncio.sleep(poll_interval)
                waited += poll_interval

            self.ib.cancelMktData(option_contract)

            if entry_price is None:
                logger.error("No valid price data for entry order (timeout)")
                return None

            logger.info(f"Entry price (marketable limit): {entry_price}")

            # 3. Create bracket orders using helper
            bracket = self.ib.bracketOrder(
                action="BUY",
                quantity=quantity,
                limitPrice=entry_price,
                takeProfitPrice=target_price,
                stopLossPrice=stop_loss_price,
                tif="GTC",
            )
            parent_order, tp_order, sl_order = (
                bracket  # bracketOrder => [parent, takeProfit, stopLoss]
            )

            # Ensure children are not transmitted until parent acknowledged (defensive)
            parent_order.transmit = False
            tp_order.transmit = False
            sl_order.transmit = True  # final transmit = True so IB receives all at once when last submitted

            # 4. Place parent
            parent_trade = self.ib.placeOrder(option_contract, parent_order)

            # 5. Wait for orderId assignment and acknowledgement (poll, with timeout)
            max_wait = 10.0
            waited = 0.0
            poll_interval = 0.25
            while waited < max_wait:
                # orderId may be assigned in parent_trade.order.orderId
                if getattr(parent_trade, "order", None) and getattr(
                    parent_trade.order, "orderId", 0
                ):
                    # also check status to be in an acceptable state
                    status = getattr(parent_trade, "orderStatus", None)
                    if status and status.status in (
                        "PreSubmitted",
                        "Submitted",
                        "Filled",
                        "ApiPending",
                    ):
                        break
                await asyncio.sleep(poll_interval)
                waited += poll_interval

            if not getattr(parent_trade.order, "orderId", None):
                logger.error("Parent order did not receive valid orderId in time")
                return None

            parent_id = parent_trade.order.orderId
            logger.info(
                f"Parent accepted. orderId={parent_id}, status={parent_trade.orderStatus.status}"
            )

            # 6. Attach the real parentId to children BEFORE placing them
            tp_order.parentId = parent_id
            sl_order.parentId = parent_id

            # (optional) ensure OCA group & type set (bracketOrder usually does this)
            oca = f"OCA_{parent_id}"
            tp_order.ocaGroup = oca
            sl_order.ocaGroup = oca
            tp_order.ocaType = 1
            sl_order.ocaType = 1

            # 7. Place children (tp then sl or sl then tp—final one should have transmit=True)
            tp_trade = self.ib.placeOrder(option_contract, tp_order)
            sl_trade = self.ib.placeOrder(option_contract, sl_order)

            # 8. Quick poll to ensure children were accepted by IB
            await asyncio.sleep(0.5)
            # log statuses for visibility
            logger.info(
                f"Orders placed: parent={parent_id}, tp={tp_order.orderId}, sl={sl_order.orderId}. "
                f"parentStatus={parent_trade.orderStatus.status}, tpStatus={tp_trade.orderStatus.status}, slStatus={sl_trade.orderStatus.status}"
            )

            result = {
                "entry_order_id": parent_id,
                "sl_order_id": sl_order.orderId,
                "target_order_id": tp_order.orderId,
                "parent_trade": parent_trade,
                "tp_trade": tp_trade,
                "sl_trade": sl_trade,
                "oca_group": oca,
            }
            return result

        except Exception:
            logger.exception("Error placing bracket order")
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
            logger.exception(f"Error getting positions: {e}")
            return []

    async def get_account_summary_async(self) -> Dict:
        """
        Get account summary including available funds and margins.

        Returns:
            Dict with account details
        """
        try:
            # Get account values from IBKR (works for both live and paper trading)
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
            logger.exception(f"Error getting account summary: {e}")
            return {}
