# core/angel_client.py
import asyncio
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import pyotp
import requests
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2

from core.config import (
    ANGEL_API_KEY,
    ANGEL_CLIENT_CODE,
    ANGEL_PASSWORD,
    ANGEL_PIN,
    ANGEL_TOTP_SECRET,
    SCRIP_MASTER_URL,
)
from core.logger import logger
from core.angelone.rate_limiter import APIRateLimiter


class AngelClient:
    """
    Angel Broker SmartAPI client for NSE trading.
    Handles authentication, data fetching, and order execution.
    """

    def __init__(self, enable_rate_limiting: bool = True):
        self.api_key = ANGEL_API_KEY
        self.client_code = ANGEL_CLIENT_CODE
        self.password = ANGEL_PASSWORD
        self.pin = ANGEL_PIN
        self.totp_secret = ANGEL_TOTP_SECRET

        self.smart_api = None
        self.connected = False
        self.auth_token = None
        self.refresh_token = None
        self.feed_token = None

        # Symbol master cache
        self.scrip_master = None
        self.symbol_cache = {}

        self.alert_sent = False

        # API Rate Limiter (90% of official limits for safety)
        self.rate_limiter = APIRateLimiter(
            enabled=enable_rate_limiting, safety_margin=0.9
        )
        
        # Connection health tracking
        self._last_successful_call = datetime.now()
        self._failed_call_count = 0
        self._circuit_breaker_open = False
        self._circuit_breaker_reset_time = None

    async def connect_async(self, retry_backoff=1.0, max_backoff=60.0):
        """
        Connect to Angel Broker API with TOTP authentication.
        Retries with exponential backoff on failure.
        """
        backoff = retry_backoff

        while True:
            try:
                logger.info("Connecting to Angel Broker SmartAPI...")

                # Initialize SmartConnect
                self.smart_api = SmartConnect(api_key=self.api_key)

                # Generate TOTP
                totp = pyotp.TOTP(self.totp_secret).now()

                # Try MPIN login if configured
                data = None
                if self.pin:
                    logger.info("Attempting login with MPIN...")
                    data = self._login_with_mpin(self.client_code, self.pin, totp)

                # Fallback to password login if MPIN not used or failed (and no data returned yet)
                if not data:
                    if self.pin:
                        logger.warning(
                            "MPIN login failed or returned no data, trying password login..."
                        )
                    # Generate session with password
                    data = self.smart_api.generateSession(
                        self.client_code, self.password, totp
                    )

                if data and data.get("status"):
                    self.auth_token = data["data"]["jwtToken"]
                    self.refresh_token = data["data"]["refreshToken"]
                    self.feed_token = self.smart_api.getfeedToken()

                    self.connected = True
                    self.alert_sent = False

                    logger.info("✅ Connected to Angel Broker SmartAPI")

                    # Load scrip master
                    await self.load_scrip_master()

                    if self.alert_sent:
                        try:
                            from core.utils import send_telegram

                            send_telegram("✅ Angel Broker connected successfully", broker="ANGEL")
                        except Exception:
                            pass
                        self.alert_sent = False

                    return
                else:
                    raise Exception(f"Login failed: {data}")

            except Exception as e:
                logger.error("Angel Broker connection failed: %s", e)

                if not self.alert_sent:
                    try:
                        from core.utils import send_telegram

                        send_telegram(
                            f"⚠️ Angel Broker connection failed: {str(e)[:100]}"
                        , broker="ANGEL")
                    except Exception:
                        pass
                    self.alert_sent = True

                await asyncio.sleep(backoff)
                backoff = min(backoff * 2.0, max_backoff)

    def _login_with_mpin(self, client_code, pin, totp):
        """
        Custom login method using MPIN.
        SmartApi-python v1.5.5 doesn't natively support loginByMPIN, so we implement it here.
        """
        try:
            # Add the MPIN route if not present
            if "api.login.mpin" not in self.smart_api._routes:
                self.smart_api._routes["api.login.mpin"] = (
                    "/rest/auth/angelbroking/user/v1/loginByMPIN"
                )

            # Prepare payload - assuming 'mpin' key based on standard practices
            # If this fails, we might need to try 'password' key with MPIN value
            params = {"clientcode": client_code, "password": pin, "totp": totp}

            # Note: Some sources say use 'password' field for MPIN in loginByPassword
            # But since loginByPassword is explicitly disallowed, we try loginByMPIN.
            # We'll try sending 'password' key first as that's what generateSession does,
            # but to the new endpoint.

            response = self.smart_api._postRequest("api.login.mpin", params)

            if response and response.get("status"):
                # Manually set tokens as generateSession does
                jwtToken = response["data"]["jwtToken"]
                self.smart_api.setAccessToken(jwtToken)
                refreshToken = response["data"]["refreshToken"]
                feedToken = response["data"]["feedToken"]
                self.smart_api.setRefreshToken(refreshToken)
                self.smart_api.setFeedToken(feedToken)

                # Get profile to set user ID
                user = self.smart_api.getProfile(refreshToken)
                if user and user.get("data"):
                    self.smart_api.setUserId(user["data"]["clientcode"])

                return response

            # If failed, try with 'mpin' key just in case
            params = {"clientcode": client_code, "mpin": pin, "totp": totp}
            response = self.smart_api._postRequest("api.login.mpin", params)

            if response and response.get("status"):
                # Manually set tokens
                jwtToken = response["data"]["jwtToken"]
                self.smart_api.setAccessToken(jwtToken)
                refreshToken = response["data"]["refreshToken"]
                feedToken = response["data"]["feedToken"]
                self.smart_api.setRefreshToken(refreshToken)
                self.smart_api.setFeedToken(feedToken)

                user = self.smart_api.getProfile(refreshToken)
                if user and user.get("data"):
                    self.smart_api.setUserId(user["data"]["clientcode"])

                return response

            return None

        except Exception as e:
            logger.error(f"MPIN login error: {e}")
            return None

    def disconnect(self):
        """Disconnect from Angel Broker API"""
        try:
            if self.smart_api:
                self.smart_api.terminateSession(self.client_code)
            self.connected = False
            logger.info("Disconnected from Angel Broker")
        except Exception:
            logger.exception("Error disconnecting from Angel Broker")
    
    def _mark_api_success(self):
        """Mark successful API call for health tracking"""
        self._last_successful_call = datetime.now()
        if self._failed_call_count > 0:
            self._failed_call_count = 0
            logger.info("API calls recovered, resetting failure count")
    
    def _mark_api_failure(self):
        """Mark failed API call and potentially open circuit breaker"""
        self._failed_call_count += 1
        
        # Open circuit breaker after 5 consecutive failures
        if self._failed_call_count >= 5 and not self._circuit_breaker_open:
            self._circuit_breaker_open = True
            self._circuit_breaker_reset_time = datetime.now() + timedelta(seconds=60)
            logger.error(
                f"🚨 Circuit breaker OPENED after {self._failed_call_count} failures. "
                f"Will reset at {self._circuit_breaker_reset_time.strftime('%H:%M:%S')}"
            )
            try:
                from core.utils import send_telegram
                send_telegram(
                    f"⚠️ AngelOne API circuit breaker opened after {self._failed_call_count} failures. "
                    "Will attempt reconnection in 60 seconds."
                , broker="ANGEL")
            except Exception:
                pass
    
    def _check_circuit_breaker(self) -> bool:
        """Check if circuit breaker allows requests. Returns True if request can proceed."""
        if not self._circuit_breaker_open:
            return True
        
        # Check if reset time has passed
        if datetime.now() >= self._circuit_breaker_reset_time:
            logger.info("🔄 Circuit breaker reset time reached, closing breaker")
            self._circuit_breaker_open = False
            self._failed_call_count = 0
            return True
        
        return False

    async def load_scrip_master(self):
        """
        Download and cache the OpenAPI Scrip Master file.
        This contains all tradable instruments with their symbol tokens.
        Falls back to hardcoded essential instruments if download fails.
        """
        try:
            logger.info("Downloading OpenAPI Scrip Master...")
            response = requests.get(SCRIP_MASTER_URL, timeout=30)
            response.raise_for_status()

            self.scrip_master = response.json()
            logger.info(
                f"Loaded {len(self.scrip_master)} instruments from Scrip Master"
            )

            # Build symbol cache for quick lookup
            for instrument in self.scrip_master:
                key = f"{instrument.get('name')}_{instrument.get('exch_seg')}"
                self.symbol_cache[key] = instrument

            return True

        except Exception as e:
            logger.warning("Failed to load Scrip Master from URL: %s", e)
            logger.info("Loading fallback instrument data...")

            # Fallback: Load essential instruments manually
            self._load_fallback_instruments()
            return True

    def _load_fallback_instruments(self):
        """
        Load essential instrument data as fallback when Scrip Master is unavailable.
        """
        # Essential indices and stocks for NIFTY options trading
        fallback_instruments = [
            # Indices
            {"token": "99926000", "symbol": "NIFTY", "name": "NIFTY", "exch_seg": "NSE", "instrumenttype": "AMXIDX"},
            {"token": "99926009", "symbol": "BANKNIFTY", "name": "BANKNIFTY", "exch_seg": "NSE", "instrumenttype": "AMXIDX"},

            # Common stocks (add more as needed)
            {"token": "738561", "symbol": "RELIANCE", "name": "RELIANCE", "exch_seg": "NSE", "instrumenttype": "AMXIDX"},
            {"token": "3045", "symbol": "SBIN", "name": "SBIN", "exch_seg": "NSE", "instrumenttype": "AMXIDX"},
            {"token": "11536", "symbol": "TCS", "name": "TCS", "exch_seg": "NSE", "instrumenttype": "AMXIDX"},
            {"token": "2885", "symbol": "INFY", "name": "INFY", "exch_seg": "NSE", "instrumenttype": "AMXIDX"},
            {"token": "14977", "symbol": "HDFCBANK", "name": "HDFCBANK", "exch_seg": "NSE", "instrumenttype": "AMXIDX"},
            {"token": "1394", "symbol": "ICICIBANK", "name": "ICICIBANK", "exch_seg": "NSE", "instrumenttype": "AMXIDX"},
            {"token": "317", "symbol": "AXISBANK", "name": "AXISBANK", "exch_seg": "NSE", "instrumenttype": "AMXIDX"},
            {"token": "1660", "symbol": "BAJFINANCE", "name": "BAJFINANCE", "exch_seg": "NSE", "instrumenttype": "AMXIDX"},
            {"token": "1922", "symbol": "BHARTIARTL", "name": "BHARTIARTL", "exch_seg": "NSE", "instrumenttype": "AMXIDX"},
            {"token": "526", "symbol": "HINDUNILVR", "name": "HINDUNILVR", "exch_seg": "NSE", "instrumenttype": "AMXIDX"},
        ]

        self.scrip_master = fallback_instruments

        # Build symbol cache
        for instrument in self.scrip_master:
            key = f"{instrument.get('name')}_{instrument.get('exch_seg')}"
            self.symbol_cache[key] = instrument

        logger.info(f"Loaded {len(self.scrip_master)} fallback instruments")

    def get_symbol_token(self, symbol: str, exchange: str = "NSE") -> Optional[str]:
        """
        Get symbol token for a given symbol and exchange.

        Args:
            symbol: Trading symbol (e.g., "RELIANCE", "NIFTY")
            exchange: Exchange segment (NSE, NFO, BSE, etc.)

        Returns:
            Symbol token string or None
        """
        try:
            # Special handling for Indices on NSE
            if exchange == "NSE":
                if symbol == "NIFTY":
                    return "99926000"
                elif symbol == "BANKNIFTY":
                    return "99926009"

            key = f"{symbol}_{exchange}"

            if key in self.symbol_cache:
                return self.symbol_cache[key].get("token")

            # Search in scrip master
            for instrument in self.scrip_master or []:
                # Check against 'name' (underlying) OR 'symbol' (trading symbol)
                if (
                    instrument.get("name") == symbol
                    or instrument.get("symbol") == symbol
                ) and instrument.get("exch_seg") == exchange:
                    token = instrument.get("token")
                    self.symbol_cache[key] = instrument
                    return token

            # Fallback: Try searchScrip API for dynamic lookup
            if self.smart_api and self.connected:
                try:
                    logger.info(f"Searching for symbol {symbol} on {exchange} via API...")
                    search_result = self.smart_api.searchScrip(exchange, symbol)

                    if search_result and 'data' in search_result:
                        for item in search_result['data']:
                            if item.get('symbol') == symbol or item.get('tradingsymbol') == symbol:
                                token = str(item.get('token'))
                                # Cache the result
                                instrument = {
                                    'token': token,
                                    'symbol': item.get('symbol', symbol),
                                    'name': item.get('name', symbol),
                                    'exch_seg': exchange
                                }
                                self.symbol_cache[key] = instrument
                                logger.info(f"Found symbol {symbol} with token {token}")
                                return token

                except Exception as api_error:
                    logger.warning(f"API search failed for {symbol}: {api_error}")

            logger.warning(f"Symbol token not found for {symbol} on {exchange}")
            return None

        except Exception as e:
            logger.exception(f"Error getting symbol token for {symbol}: {e}")
            return None

    async def req_historic_1m(
        self, symbol: str, duration_days: int = 2, exchange: str = "NSE"
    ) -> Optional[pd.DataFrame]:
        """
        Fetch historical 1-minute candle data.

        Args:
            symbol: Trading symbol
            duration_days: Number of days of history to fetch
            exchange: Exchange segment

        Returns:
            DataFrame with OHLCV data indexed by datetime (UTC)
        """
        try:
            symbol_token = self.get_symbol_token(symbol, exchange)
            if not symbol_token:
                logger.error(
                    f"Cannot fetch history: symbol token not found for {symbol}"
                )
                return None

            # Calculate from/to dates
            to_date = datetime.now()
            from_date = to_date - timedelta(days=duration_days)

            # Format dates for API
            from_date_str = from_date.strftime("%Y-%m-%d %H:%M")
            to_date_str = to_date.strftime("%Y-%m-%d %H:%M")

            # Fetch candle data
            historic_param = {
                "exchange": exchange,
                "symboltoken": symbol_token,
                "interval": "ONE_MINUTE",
                "fromdate": from_date_str,
                "todate": to_date_str,
            }

            logger.debug(
                f"[{symbol}] Requesting 1m data: {from_date_str} to {to_date_str} ({duration_days:.4f} days)"
            )

            # Rate limiting for getCandleData (3/sec, 180/min, 5000/hour)
            await self.rate_limiter.acquire("getCandleData")

            # Add timeout to prevent indefinite blocking
            data = await asyncio.wait_for(
                asyncio.to_thread(self.smart_api.getCandleData, historic_param),
                timeout=10.0,
            )

            if not data or not data.get("data"):
                # Check for specific error codes
                if data and data.get("errorcode"):
                    error_code = data.get("errorcode")
                    error_msg = data.get("message", "Unknown error")

                    if error_code == "AB1004":
                        logger.warning(
                            f"[{symbol}] ⚠️ API Rate Limit (AB1004): {error_msg}. "
                            f"Request: {from_date_str} to {to_date_str}"
                        )
                    else:
                        logger.warning(
                            f"[{symbol}] API Error {error_code}: {error_msg}. "
                            f"Request: {from_date_str} to {to_date_str}"
                        )
                else:
                    logger.warning(f"No historical data returned for {symbol}")
                return None

            # Convert to DataFrame
            candles = data["data"]
            df = pd.DataFrame(
                candles, columns=["datetime", "open", "high", "low", "close", "volume"]
            )

            # Convert datetime to pandas datetime
            df["datetime"] = pd.to_datetime(df["datetime"])

            # Convert to UTC (Angel returns IST)
            # Check if datetime is already timezone aware
            if df["datetime"].dt.tz is None:
                df["datetime"] = (
                    df["datetime"]
                    .dt.tz_localize("Asia/Kolkata")
                    .dt.tz_convert("UTC")
                    .dt.tz_localize(None)
                )
            else:
                df["datetime"] = (
                    df["datetime"].dt.tz_convert("UTC").dt.tz_localize(None)
                )

            # Set index and select OHLCV columns
            df = df.set_index("datetime")[["open", "high", "low", "close", "volume"]]

            # Convert to numeric
            for col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")

            logger.debug(f"[{symbol}] Successfully fetched {len(df)} 1m candles")
            return df

        except Exception as e:
            error_str = str(e)

            # Detect rate limiting in exception message
            if (
                "AB1004" in error_str
                or "Try After Sometime" in error_str
                or "Access denied" in error_str
            ):
                logger.error(
                    f"[{symbol}] 🚫 API Rate Limit Exception: {error_str[:200]}"
                )
            else:
                logger.exception(f"Error fetching historical data for {symbol}: {e}")
            return None

    async def get_ltp(self, token: str, exchange: str = "NFO") -> Optional[float]:
        """
        Get last traded price by token (used by RoboOrderManager).

        Args:
            token: Symbol token
            exchange: Exchange segment (default NFO for options)

        Returns:
            Last traded price or None
        """
        try:
            if not self.connected:
                logger.error(f"Not connected to Angel Broker when fetching LTP for token {token}")
                return None

            # Find symbol from token
            symbol = None
            if self.scrip_master:
                for instrument in self.scrip_master:
                    if instrument.get("token") == token and instrument.get("exch_seg") == exchange:
                        symbol = instrument.get("symbol")
                        break

            if not symbol:
                logger.error(f"Symbol not found for token {token} in {exchange}")
                return None

            # Rate limiting for ltpData (10/sec, 500/min, 5000/hour)
            await self.rate_limiter.acquire("ltpData")

            # Use LTP API with timeout
            ltp_data = await asyncio.wait_for(
                asyncio.to_thread(
                    self.smart_api.ltpData, exchange, symbol, token
                ),
                timeout=5.0,
            )

            if ltp_data and ltp_data.get("data"):
                return float(ltp_data["data"]["ltp"])

            logger.warning(f"No LTP data returned for token {token}")
            return None

        except asyncio.TimeoutError:
            logger.error(f"Timeout getting LTP for token {token} (5s exceeded)")
            return None
        except Exception as e:
            logger.exception(f"Error getting LTP for token {token}: {e}")
            return None

    async def get_last_price(
        self, symbol: str, exchange: str = "NSE"
    ) -> Optional[float]:
        """
        Get current last traded price for a symbol.

        Args:
            symbol: Trading symbol
            exchange: Exchange segment

        Returns:
            Last traded price or None
        """
        try:
            if not self.connected:
                logger.error(f"Not connected to Angel Broker when fetching price for {symbol}")
                return None
                
            symbol_token = self.get_symbol_token(symbol, exchange)
            if not symbol_token:
                return None

            # Rate limiting for ltpData (10/sec, 500/min, 5000/hour)
            await self.rate_limiter.acquire("ltpData")

            # Use LTP API with timeout
            ltp_data = await asyncio.wait_for(
                asyncio.to_thread(
                    self.smart_api.ltpData, exchange, symbol, symbol_token
                ),
                timeout=5.0,
            )

            if ltp_data and ltp_data.get("data"):
                return float(ltp_data["data"]["ltp"])

            logger.warning(f"No LTP data returned for {symbol}")
            return None

        except asyncio.TimeoutError:
            logger.error(f"Timeout getting last price for {symbol} (5s exceeded)")
            return None
        except Exception as e:
            logger.exception(f"Error getting last price for {symbol}: {e}")
            return None

    async def get_futures_price(self, symbol: str) -> Optional[float]:
        """
        Get current monthly futures price for an index (NIFTY/BANKNIFTY).
        Used for signal generation on indices.

        Args:
            symbol: Index symbol (NIFTY or BANKNIFTY)

        Returns:
            Current futures price or None
        """
        try:
            # Find current monthly futures contract
            today = datetime.now().date()
            current_month = today.month
            current_year = today.year

            futures_symbol = None
            futures_token = None

            for instrument in self.scrip_master or []:
                if (
                    instrument.get("exch_seg") == "NFO"
                    and instrument.get("instrumenttype") == "FUTIDX"
                    and symbol in instrument.get("name", "")
                ):
                    # Parse expiry
                    expiry_str = instrument.get("expiry")
                    if not expiry_str:
                        continue

                    try:
                        expiry_date = datetime.strptime(expiry_str, "%d%b%Y").date()
                    except ValueError:
                        try:
                            expiry_date = datetime.strptime(
                                expiry_str, "%Y-%m-%d"
                            ).date()
                        except ValueError:
                            continue

                    # Check if current month
                    if (
                        expiry_date.month == current_month
                        and expiry_date.year == current_year
                    ):
                        futures_symbol = instrument.get("symbol")
                        futures_token = instrument.get("token")
                        break

            if not futures_symbol or not futures_token:
                logger.warning(f"Current monthly futures not found for {symbol}")
                return None

            # Rate limiting for ltpData (10/sec, 500/min, 5000/hour)
            await self.rate_limiter.acquire("ltpData")

            # Get LTP for futures
            ltp_data = self.smart_api.ltpData("NFO", futures_symbol, futures_token)

            if ltp_data and ltp_data.get("data"):
                price = float(ltp_data["data"]["ltp"])
                logger.debug(f"Futures price for {symbol}: ₹{price}")
                return price

            return None

        except Exception as e:
            logger.exception(f"Error getting futures price for {symbol}: {e}")
            return None

    async def get_index_spot_price(self, symbol: str) -> Optional[float]:
        """
        Get current spot index price for NIFTY/BANKNIFTY.
        Used for option strike selection (underlying price).

        Args:
            symbol: Index symbol (NIFTY or BANKNIFTY)

        Returns:
            Current spot index price or None
        """
        try:
            if not self.connected:
                logger.error(f"Not connected when fetching spot price for {symbol}")
                return None

            # For indices, use NSE exchange to get spot price
            # The symbol format in scrip master for spot indices is usually just "NIFTY" or "BANKNIFTY"
            symbol_token = self.get_symbol_token(symbol, "NSE")
            if not symbol_token:
                logger.error(f"Could not find spot token for {symbol}")
                return None

            # Rate limiting for ltpData
            await self.rate_limiter.acquire("ltpData")

            # Get spot LTP
            ltp_data = await asyncio.wait_for(
                asyncio.to_thread(
                    self.smart_api.ltpData, "NSE", symbol, symbol_token
                ),
                timeout=5.0,
            )

            if ltp_data and ltp_data.get("data"):
                price = float(ltp_data["data"]["ltp"])
                logger.debug(f"Spot index price for {symbol}: ₹{price}")
                return price

            logger.warning(f"No spot price data for {symbol}")
            return None

        except asyncio.TimeoutError:
            logger.error(f"Timeout getting spot price for {symbol}")
            return None
        except Exception as e:
            logger.exception(f"Error getting spot index price for {symbol}: {e}")
            return None

    async def place_order(
        self,
        symbol: str,
        token: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        order_type: str = "MARKET",
        price: float = 0.0,
        triggerprice: Optional[float] = None,
        product_type: str = "INTRADAY",
        variety: str = "NORMAL",
    ) -> Optional[Dict]:
        """
        Place a single order on Angel One.

        Args:
            symbol: Trading symbol
            token: Symbol token
            exchange: NSE/NFO/etc.
            transaction_type: BUY or SELL
            quantity: Order quantity
            order_type: MARKET, LIMIT, STOPLOSS_LIMIT
            price: Limit price (for LIMIT orders)
            triggerprice: Trigger price (for stop-loss)
            product_type: INTRADAY, DELIVERY, etc.
            variety: Order variety (NORMAL, STOPLOSS, AMO, etc.)

        Returns:
            Dict with order response or None
        """
        try:
            # Validate connection
            if not self.connected:
                logger.error("Cannot place order: Not connected to Angel Broker")
                return None
            
            # Validate inputs
            if quantity <= 0:
                logger.error(f"Invalid quantity: {quantity}")
                return None
                
            order_params = {
                "variety": variety,
                "tradingsymbol": symbol,
                "symboltoken": token,
                "transactiontype": transaction_type,
                "exchange": exchange,
                "ordertype": order_type,
                "producttype": product_type,
                "duration": "DAY",
                "quantity": str(quantity),
            }

            if order_type in ["LIMIT", "STOPLOSS_LIMIT"] and price > 0:
                order_params["price"] = str(price)
            if triggerprice:
                order_params["triggerprice"] = str(triggerprice)

            # Place order in a thread to avoid blocking
            response = await asyncio.wait_for(
                asyncio.to_thread(self.smart_api.placeOrder, order_params),
                timeout=10.0,
            )

            if response:
                if isinstance(response, str):
                    logger.info(f"Order placed (ID only): {response}")
                    return {
                        "status": True,
                        "message": "SUCCESS",
                        "data": {"orderid": response},
                    }
                elif isinstance(response, dict) and response.get("status"):
                    logger.info(f"Order placed: {response}")
                    return response
                else:
                    logger.error(f"Order placement failed: {response}")
                    return None
            else:
                logger.error("Order placement failed: No response")
                return None

        except Exception as e:
            logger.exception(f"Error placing order: {e}")
            return None

    async def wait_for_fill(self, order_id: str, timeout: float = 10.0) -> bool:
        """
        Poll Angel One API until the order is filled or timeout.

        Args:
            order_id: Order ID to check
            timeout: Max seconds to wait

        Returns:
            True if filled, False if timeout
        """
        import time

        start = time.time()
        while time.time() - start < timeout:
            try:
                # API Call to fetch order book is distinct from order history usually
                # orderBook() returns list of all orders. getOrderHistory returns history of specific order?
                # SmartAPI typically uses orderBook() to get status of all orders.
                # Let's use orderBook() and filter as it's more reliable.

                # Note: self.smart_api.orderBook() is blocking
                book = await asyncio.to_thread(self.smart_api.orderBook)

                if book and book.get("data"):
                    for order in book["data"]:
                        if order.get("orderid") == order_id:
                            status = order.get("status")  # 'complete', 'rejected', etc.
                            if status == "complete":
                                return True
                            elif status == "rejected":
                                logger.error(
                                    f"Order {order_id} rejected: {order.get('text')}"
                                )
                                return False
            except Exception:
                pass

            await asyncio.sleep(1.0)

        return False

    async def place_bracket_order(
        self,
        option_symbol: str,
        option_token: str,
        quantity: int,
        stop_loss_price: float,
        target_price: float,
        exchange: str = "NFO",
        product_type: str = "INTRADAY",
    ) -> Optional[Dict]:
        """
        Simulate a bracket order.
        1. Market BUY entry
        2. Wait for Fill
        3. Stop-loss SELL
        4. Target SELL

        NOTE: This does NOT link SL and Target (No native OCO).
        The worker loop MUST monitor these and cancel the other when one fills.

        Args:
            option_symbol: Option trading symbol
            option_token: Option symbol token
            quantity: Lots
            stop_loss_price: Stop-loss trigger/limit
            target_price: Target price
            exchange: Exchange (usually NFO for options)
            product_type: INTRADAY/DELIVERY

        Returns:
            Dict with entry, SL, target order IDs
        """
        try:
            # --- Place Entry ---
            entry_order = await self.place_order(
                symbol=option_symbol,
                token=option_token,
                exchange=exchange,
                transaction_type="BUY",
                quantity=quantity,
                order_type="MARKET",
                product_type=product_type,
            )
            if not entry_order or not entry_order.get("data"):
                logger.error("Entry order failed")
                return None
            entry_order_id = entry_order["data"]["orderid"]

            # --- Wait for Entry Fill ---
            logger.info(f"Entry {entry_order_id} placed. Waiting for fill...")
            filled = await self.wait_for_fill(entry_order_id, timeout=15.0)

            if not filled:
                logger.error(
                    "Entry order not filled within timeout; cancelling SL/TP placement logic."
                )
                # We do NOT cancel the entry order here automatically to avoid race conditions,
                # but we return what we have. Worker should handle this.
                return {"entry_order_id": entry_order_id}

            # --- Place Stop-loss ---
            # Angel One 'STOPLOSS_LIMIT' requires 'triggerprice' and 'price'
            # variety must be 'STOPLOSS' usually for trigger orders
            sl_order = await self.place_order(
                symbol=option_symbol,
                token=option_token,
                exchange=exchange,
                transaction_type="SELL",
                quantity=quantity,
                order_type="STOPLOSS_LIMIT",
                price=stop_loss_price,
                triggerprice=stop_loss_price,
                product_type=product_type,
                variety="STOPLOSS",  # Important: usually STOPLOSS variety for SL orders
            )
            sl_order_id = sl_order.get("data", {}).get("orderid") if sl_order else None
            if not sl_order_id:
                logger.warning("Stop-loss order placement failed")

            # --- Place Target ---
            target_order = await self.place_order(
                symbol=option_symbol,
                token=option_token,
                exchange=exchange,
                transaction_type="SELL",
                quantity=quantity,
                order_type="LIMIT",
                price=target_price,
                product_type=product_type,
                variety="NORMAL",
            )
            target_order_id = (
                target_order.get("data", {}).get("orderid") if target_order else None
            )
            if not target_order_id:
                logger.warning("Target order placement failed")

            result = {
                "entry_order_id": entry_order_id,
                "sl_order_id": sl_order_id,
                "target_order_id": target_order_id,
            }

            logger.info(f"Bracket order setup complete: {result}")
            return result

        except Exception as e:
            logger.exception(f"Error placing bracket order: {e}")
            return None

    async def get_positions(self) -> List[Dict]:
        """
        Get current open positions.

        Returns:
            List of position dicts
        """
        try:
            # Rate limiting for getPosition (1/sec)
            await self.rate_limiter.acquire("getPosition")

            # Use asyncio.to_thread with timeout for blocking call
            response = await asyncio.wait_for(
                asyncio.to_thread(self.smart_api.position),
                timeout=5.0
            )

            if response and response.get("data"):
                return response["data"]

            return []

        except asyncio.TimeoutError:
            logger.error("Timeout getting positions (5s exceeded)")
            return []
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
            # Rate limiting for getRMS (2/sec)
            await self.rate_limiter.acquire("getRMS")

            # Get RMS Limits (Risk Management System) - use asyncio.to_thread for blocking call
            rms = await asyncio.wait_for(
                asyncio.to_thread(self.smart_api.rmsLimit),
                timeout=5.0
            )

            if rms and rms.get("data"):
                data = rms["data"]
                return {
                    "AvailableFunds": float(data.get("availablecash", 0)),
                    "UtilizedFunds": float(data.get("utilisedpayout", 0)),
                    "TotalFunds": float(data.get("net", 0)),
                }

            logger.warning("No data returned from rmsLimit API")
            return {}

        except asyncio.TimeoutError:
            logger.error("Timeout getting account summary (5s exceeded)")
            return {}
        except Exception as e:
            logger.exception(f"Error getting account summary: {e}")
            return {}


class AngelWebSocket:
    """
    Wrapper for Angel One WebSocket V2.
    Handles real-time data streaming and updates BarManagers.
    """

    def __init__(
        self, auth_token, api_key, client_code, feed_token, bar_managers, loop=None
    ):
        """
        Args:
            auth_token: JWT auth token
            api_key: API Key
            client_code: Client Code
            feed_token: Feed Token
            bar_managers: Dict mapping symbol -> BarManager
            loop: Asyncio event loop (required for thread-safe updates)
        """
        self.sws = SmartWebSocketV2(auth_token, api_key, client_code, feed_token)
        self.bar_managers = bar_managers
        self.loop = loop or asyncio.get_event_loop()
        self.token_map = {}  # Map token -> symbol
        self.subscribed_tokens = set()

    def _on_data(self, wsapp, message):
        """Callback for incoming tick data"""
        try:
            # Message is a dict with 'token', 'last_traded_price', 'last_traded_time', etc.
            token = message.get("token")
            if not token:
                return

            symbol = self.token_map.get(token)
            if not symbol:
                return

            bar_manager = self.bar_managers.get(symbol)
            if not bar_manager:
                return

            price = float(message.get("last_traded_price", 0))
            if price == 0:
                return

            # Angel One WebSocket V2 sends price in paise (integer) for some segments?
            # User logs show NIFTY ~ 2600000, which is 100x.
            # We need to detect if it's in paise and convert to rupees.
            # Heuristic: If price is > 100000 (e.g. 1 lakh) for a stock/index that shouldn't be, it's likely paise.
            # NIFTY is ~24000. 2400000 is paise.
            # MRF is ~1.3 Lakh. 13000000 is paise.
            # Let's assume if it's > 200000 it MIGHT be paise, but MRF is an exception.
            # Better approach: The API doc says "last_traded_price" is in paise for some, rupees for others?
            # Actually, SmartAPI V2 usually sends in paise.
            # Let's divide by 100.

            # WAIT: If we divide blindly, what if it IS rupees?
            # Let's look at the logs again: NIFTY 2619395.00.
            # If we divide by 100 -> 26193.95. This matches current Nifty levels.
            # INFY 161510.00 -> 1615.10. Matches INFY levels.
            # So it IS consistently 100x.

            price = price / 100.0

            # Timestamp handling
            ts_raw = message.get("exchange_timestamp") or message.get(
                "last_traded_time"
            )
            if isinstance(ts_raw, int):
                # Check if timestamp is in milliseconds (13 digits)
                if ts_raw > 9999999999:
                    timestamp = datetime.fromtimestamp(ts_raw / 1000)
                else:
                    timestamp = datetime.fromtimestamp(ts_raw)
            else:
                timestamp = datetime.now()

            # Pass to BarManager using thread-safe scheduling
            if self.loop and self.loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    bar_manager.process_tick(price, timestamp, 0), self.loop
                )
            else:
                logger.error("Event loop is not running, cannot process tick")

        except Exception as e:
            logger.error(f"WebSocket data error: {e}")

    def _on_open(self, wsapp):
        logger.info("✅ Angel WebSocket Connected")

        # Subscribe to all tokens
        if self.token_map:
            tokens = list(self.token_map.keys())
            # Mode 1: LTP (Fastest)
            correlation_id = "subscribe_all"
            mode = 1

            # Split tokens by exchange (NSE vs NFO)
            # This is a simplification; ideally we store exchange info
            # For now, let's assume all are NSE (1) or NFO (2)
            # We'll try subscribing as NSE first
            token_list = [{"exchangeType": 1, "tokens": tokens}]

            self.sws.subscribe(correlation_id, mode, token_list)
            logger.info(f"Subscribed to {len(tokens)} tokens")

    def _on_close(self, wsapp):
        logger.warning("Angel WebSocket Closed")

    def _on_error(self, wsapp, error):
        logger.error(f"Angel WebSocket Error: {error}")

    def connect(self):
        """Start the WebSocket connection (blocking)"""
        self.sws.on_data = self._on_data
        self.sws.on_open = self._on_open
        self.sws.on_close = self._on_close
        self.sws.on_error = self._on_error
        self.sws.connect()

    def add_symbol(self, symbol, token, exchange="NSE"):
        """Add a symbol to be tracked"""
        self.token_map[token] = symbol
