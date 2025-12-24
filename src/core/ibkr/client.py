"""
Interactive Brokers client for US stock options trading.
Uses ib_async library to connect to TWS/Gateway.
"""

import asyncio
from datetime import datetime, timezone
from typing import Dict, List, Optional

import logging
import pandas as pd
from ib_async import IB, Stock, Option, Index, Future, util

from core.config import (
    IB_HOST,
    IB_PORT,
    IB_CLIENT_ID,
    IBKR_MODE,
    IBKR_PAPER_BALANCE,
    IBKR_FUTURES_EXCHANGES,
)
from core.logger import logger

# Map of Indices to their primary exchange
# SPX -> CBOE, NDX -> NASDAQ
IBKR_INDICES = {
    "SPX": "CBOE",
    "NDX": "NASDAQ",
    "RUT": "CBOE",
    "VIX": "CBOE",
}


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

        # Silence ib_async/ib_insync ambiguous contract logs
        logging.getLogger("ib_async").setLevel(logging.WARNING)
        logging.getLogger("ib_insync").setLevel(logging.WARNING)

    def _get_contract(self, symbol: str):
        """Helper to get Stock or Index or Future contract based on symbol."""
        if symbol in IBKR_INDICES:
            return Index(symbol, IBKR_INDICES[symbol], "USD")
        if symbol in IBKR_FUTURES_EXCHANGES:
            return Future(
                symbol=symbol, exchange=IBKR_FUTURES_EXCHANGES[symbol], currency="USD"
            )
        return Stock(symbol, "SMART", "USD")

    async def get_front_month_contract(self, symbol: str) -> Optional[Future]:
        """
        Get the front-month (most active) Future contract for a symbol.
        """
        try:
            if symbol not in IBKR_FUTURES_EXCHANGES:
                logger.error(f"Symbol {symbol} not found in IBKR_FUTURES_EXCHANGES")
                return None

            exchange = IBKR_FUTURES_EXCHANGES[symbol]
            # Create a generic future contract to request details
            contract = Future(symbol=symbol, exchange=exchange, currency="USD")
            details = await self.ib.reqContractDetailsAsync(contract)

            if not details:
                logger.warning(f"No contract details found for {symbol}")
                return None

            # Log candidates for debugging
            logger.debug(f"[{symbol}] Found {len(details)} potential contract matches.")
            for d in details:
                logger.debug(
                    f"  - Candidate: {d.contract.localSymbol} (conId: {d.contract.conId}, Expiry: {d.contract.lastTradeDateOrContractMonth})"
                )

            # Filter out expired contracts and sort by expiration
            # IBKR usually returns many expiries; we want the nearest future front month
            today_str = datetime.now().strftime("%Y%m%d")
            valid_details = [
                d
                for d in details
                if d.contract.lastTradeDateOrContractMonth >= today_str
            ]

            if not valid_details:
                logger.warning(f"No non-expired contract details found for {symbol}")
                valid_details = details

            sorted_details = sorted(
                valid_details, key=lambda x: x.contract.lastTradeDateOrContractMonth
            )
            front_month = sorted_details[0].contract

            # Qualify to get conId etc.
            qualified = await self.ib.qualifyContractsAsync(front_month)
            if qualified:
                logger.info(
                    f"[{symbol}] Front month: {qualified[0].localSymbol} (Expiry: {qualified[0].lastTradeDateOrContractMonth})"
                )
                return qualified[0]

            return None

        except Exception as e:
            logger.exception(f"Error getting front month for {symbol}: {e}")
            return None

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

    async def ensure_connected(self):
        """Check connection status and reconnect if needed."""
        if not self.ib.isConnected():
            logger.warning("⚠️ IBKR connection lost. Attempting to reconnect...")
            await self.connect_async()

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
        contract: Optional[any] = None,
    ) -> Optional[pd.DataFrame]:
        """
        Fetch historical 1-minute candle data for a US stock/index/future.

        Args:
            symbol: Symbol (e.g., 'SPY', 'SPX', 'ES')
            duration_days: Number of days of history to fetch
            contract: Optional qualified contract to avoid ambiguity
        """
        try:
            if not contract:
                contract = self._get_contract(symbol)
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
        self,
        symbol: str,
        bar_size: str = "15 mins",
        duration_str: str = "1 D",
        contract: Optional[any] = None,
    ) -> Optional[pd.DataFrame]:
        """
        Fetch historical bars directly at specified interval (no resampling).

        Args:
            symbol: Symbol
            bar_size: Bar size ("5 mins", "15 mins", "1 hour", etc.)
            duration_str: Duration ("1 D" for 1 day, "2 D" for 2 days)
            contract: Optional qualified contract to avoid ambiguity
        """
        try:
            if not contract:
                contract = self._get_contract(symbol)
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
        """
        try:
            # --- Underlying Contract ---
            contract = self._get_contract(symbol)
            await self.ib.qualifyContractsAsync(contract)

            chains = await self.ib.reqSecDefOptParamsAsync(
                contract.symbol, "", contract.secType, contract.conId
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
                contract = self._get_contract(symbol)
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
            # Determine TIF - Futures options usually require 'DAY' for bracket children
            tif = "DAY" if option_contract.secType == "FOP" else "GTC"
            logger.info(
                f"[{option_contract.symbol}] Using TIF={tif} for bracket orders (secType={option_contract.secType})"
            )

            bracket = self.ib.bracketOrder(
                action="BUY",
                quantity=quantity,
                limitPrice=entry_price,
                takeProfitPrice=target_price,
                stopLossPrice=stop_loss_price,
                tif=tif,
            )
            parent_order, tp_order, sl_order = bracket

            # 4. Correctly link and configure bracket
            # parent_order.orderId is 0 here, IB will assign it upon placement
            # bracketOrder() helper already links parentId, but we'll be explicit for safety
            # transmit=True only on the FINAL child order
            parent_order.transmit = False
            tp_order.transmit = False
            sl_order.transmit = True

            # Use a unique OCA group
            oca_group = (
                f"ORB_{option_contract.symbol}_{datetime.now().strftime('%H%M%S')}"
            )
            tp_order.ocaGroup = oca_group
            sl_order.ocaGroup = oca_group
            tp_order.ocaType = 1
            sl_order.ocaType = 1

            # 5. Place all three orders sequentially
            # ib_async handles orderId management automatically behind the scenes
            parent_trade = self.ib.placeOrder(option_contract, parent_order)
            tp_trade = self.ib.placeOrder(option_contract, tp_order)
            sl_trade = self.ib.placeOrder(option_contract, sl_order)

            # 6. Wait for acknowledgment and check for rejections
            # We poll briefly to see if the order was accepted or rejected
            max_wait = 5.0
            waited = 0.0
            while waited < max_wait:
                status = parent_trade.orderStatus.status
                if status in ["Submitted", "PreSubmitted", "Filled", "ApiPending"]:
                    logger.info(
                        f"[{option_contract.symbol}] Bracket order accepted. Status: {status}"
                    )
                    break
                if status in ["Rejected", "Cancelled", "Inactive"]:
                    # Check for rejection reason in logs
                    reason = "Unknown rejection"
                    if parent_trade.log:
                        # Find last entry with a message
                        msgs = [
                            entry.message for entry in parent_trade.log if entry.message
                        ]
                        if msgs:
                            reason = msgs[-1]
                    logger.error(
                        f"[{option_contract.symbol}] Parent order was {status}. Reason: {reason}"
                    )
                    return {
                        "status": "failed",
                        "error": reason,
                        "order_status": status,
                    }

                await asyncio.sleep(0.5)
                waited += 0.5

            # If we timed out without a definitive status
            status = parent_trade.orderStatus.status
            if status not in ["Submitted", "PreSubmitted", "Filled", "ApiPending"]:
                logger.warning(
                    f"[{option_contract.symbol}] Bracket order status uncertain: {status}"
                )

            return {
                "status": "success",
                "entry_order_id": parent_order.orderId,
                "sl_order_id": sl_order.orderId,
                "target_order_id": tp_order.orderId,
                "parent_trade": parent_trade,
                "tp_trade": tp_trade,
                "sl_trade": sl_trade,
                "oca_group": oca_group,
            }

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
                        "contract": pos.contract,
                    }
                )

            return result

        except Exception as e:
            logger.exception(f"Error getting positions: {e}")
            return []

    async def get_open_orders(self) -> List:
        """
        Get current open orders (Trades) from IBKR.

        Returns:
            List of Trade objects
        """
        try:
            return self.ib.openTrades()
        except Exception as e:
            logger.exception(f"Error getting open orders: {e}")
            return []

    async def get_account_summary_async(self, currency: str = "USD") -> Dict:
        """
        Get account summary including available funds and margins for a specific currency.

        Args:
            currency: Currency to filter for (default: USD)

        Returns:
            Dict with account details for the specified currency
        """
        try:
            # Get account values from IBKR (works for both live and paper trading)
            account_values = self.ib.accountValues()

            summary = {}
            for value in account_values:
                # For currency-specific balances, use the "ByCurrency" fields which give actual currency balances
                # not converted to base currency
                if value.tag == "CashBalance" and value.currency == currency:
                    summary["CashBalance"] = float(value.value)
                elif value.tag == "TotalCashBalance" and value.currency == currency:
                    summary["TotalCashBalance"] = float(value.value)
                elif value.tag == "NetLiquidationByCurrency" and value.currency == currency:
                    summary["NetLiquidationByCurrency"] = float(value.value)
                elif value.tag == "AvailableFunds-S" and value.currency == currency:
                    # AvailableFunds for securities segment in specified currency
                    summary["AvailableFunds"] = float(value.value)

            # If we didn't get AvailableFunds, use CashBalance or NetLiquidationByCurrency as fallback
            if "AvailableFunds" not in summary:
                if "TotalCashBalance" in summary:
                    summary["AvailableFunds"] = summary["TotalCashBalance"]
                elif "NetLiquidationByCurrency" in summary:
                    summary["AvailableFunds"] = summary["NetLiquidationByCurrency"]
                elif "CashBalance" in summary:
                    summary["AvailableFunds"] = summary["CashBalance"]

            # Use NetLiquidationByCurrency as the total value in that currency
            if "NetLiquidation" not in summary and "NetLiquidationByCurrency" in summary:
                summary["NetLiquidation"] = summary["NetLiquidationByCurrency"]

            # Use TotalCashBalance as TotalCashValue
            if "TotalCashValue" not in summary and "TotalCashBalance" in summary:
                summary["TotalCashValue"] = summary["TotalCashBalance"]

            logger.info(f"Account summary ({currency}): {summary}")
            return summary

        except Exception as e:
            logger.exception(f"Error getting account summary: {e}")
            return {}
