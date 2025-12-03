# core/angel_client.py
import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import pandas as pd
import pyotp
import requests
from SmartApi import SmartConnect

from core.config import (
    ANGEL_API_KEY,
    ANGEL_CLIENT_CODE,
    ANGEL_PASSWORD,
    ANGEL_PIN,
    ANGEL_TOTP_SECRET,
    SCRIP_MASTER_URL,
)
from core.logger import logger
from core.rate_limiter import APIRateLimiter


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
        self.rate_limiter = APIRateLimiter(enabled=enable_rate_limiting, safety_margin=0.9)

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
                        logger.warning("MPIN login failed or returned no data, trying password login...")
                    # Generate session with password
                    data = self.smart_api.generateSession(self.client_code, self.password, totp)

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

                            send_telegram("✅ Angel Broker connected successfully")
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

                        send_telegram(f"⚠️ Angel Broker connection failed: {str(e)[:100]}")
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
                self.smart_api._routes["api.login.mpin"] = "/rest/auth/angelbroking/user/v1/loginByMPIN"

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

    async def load_scrip_master(self):
        """
        Download and cache the OpenAPI Scrip Master file.
        This contains all tradable instruments with their symbol tokens.
        """
        try:
            logger.info("Downloading OpenAPI Scrip Master...")
            response = requests.get(SCRIP_MASTER_URL, timeout=30)
            response.raise_for_status()

            self.scrip_master = response.json()
            logger.info(f"Loaded {len(self.scrip_master)} instruments from Scrip Master")

            # Build symbol cache for quick lookup
            for instrument in self.scrip_master:
                key = f"{instrument.get('name')}_{instrument.get('exch_seg')}"
                self.symbol_cache[key] = instrument

            return True

        except Exception as e:
            logger.exception("Failed to load Scrip Master: %s", e)
            return False

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
                if instrument.get("name") == symbol and instrument.get("exch_seg") == exchange:
                    token = instrument.get("token")
                    self.symbol_cache[key] = instrument
                    return token

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
                logger.error(f"Cannot fetch history: symbol token not found for {symbol}")
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
            await self.rate_limiter.acquire('getCandleData')
            
            data = self.smart_api.getCandleData(historic_param)

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
            df = pd.DataFrame(candles, columns=["datetime", "open", "high", "low", "close", "volume"])

            # Convert datetime to pandas datetime
            df["datetime"] = pd.to_datetime(df["datetime"])

            # Convert to UTC (Angel returns IST)
            # Check if datetime is already timezone aware
            if df["datetime"].dt.tz is None:
                df["datetime"] = df["datetime"].dt.tz_localize("Asia/Kolkata").dt.tz_convert("UTC").dt.tz_localize(None)
            else:
                df["datetime"] = df["datetime"].dt.tz_convert("UTC").dt.tz_localize(None)

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
            if "AB1004" in error_str or "Try After Sometime" in error_str:
                logger.error(
                    f"[{symbol}] 🚫 API Rate Limit Exception: {error_str[:200]}"
                )
            else:
                logger.exception(f"Error fetching historical data for {symbol}: {e}")
            return None

    async def get_last_price(self, symbol: str, exchange: str = "NSE") -> Optional[float]:
        """
        Get current last traded price for a symbol.

        Args:
            symbol: Trading symbol
            exchange: Exchange segment

        Returns:
            Last traded price or None
        """
        try:
            symbol_token = self.get_symbol_token(symbol, exchange)
            if not symbol_token:
                return None

            # Rate limiting for ltpData (10/sec, 500/min, 5000/hour)
            await self.rate_limiter.acquire('ltpData')
            
            # Use LTP API
            ltp_data = self.smart_api.ltpData(exchange, symbol, symbol_token)

            if ltp_data and ltp_data.get("data"):
                return float(ltp_data["data"]["ltp"])

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
                            expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
                        except ValueError:
                            continue

                    # Check if current month
                    if expiry_date.month == current_month and expiry_date.year == current_year:
                        futures_symbol = instrument.get("symbol")
                        futures_token = instrument.get("token")
                        break

            if not futures_symbol or not futures_token:
                logger.warning(f"Current monthly futures not found for {symbol}")
                return None

            # Rate limiting for ltpData (10/sec, 500/min, 5000/hour)
            await self.rate_limiter.acquire('ltpData')
            
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

    def place_order(
        self,
        symbol: str,
        token: str,
        exchange: str,
        transaction_type: str,
        quantity: int,
        order_type: str = "MARKET",
        price: float = 0.0,
        product_type: str = "INTRADAY",
    ) -> Optional[Dict]:
        """
        Place an order on Angel Broker.

        Args:
            symbol: Trading symbol
            token: Symbol token
            exchange: Exchange (NSE, NFO, etc.)
            transaction_type: BUY or SELL
            quantity: Order quantity
            order_type: MARKET or LIMIT
            price: Limit price (for LIMIT orders)
            product_type: INTRADAY, DELIVERY, etc.

        Returns:
            Order response dict or None
        """
        try:
            order_params = {
                "variety": "NORMAL",
                "tradingsymbol": symbol,
                "symboltoken": token,
                "transactiontype": transaction_type,
                "exchange": exchange,
                "ordertype": order_type,
                "producttype": product_type,
                "duration": "DAY",
                "quantity": str(quantity),
            }

            if order_type == "LIMIT":
                order_params["price"] = str(price)

            response = self.smart_api.placeOrder(order_params)

            if response and response.get("status"):
                logger.info(f"Order placed: {response}")
                return response
            else:
                logger.error(f"Order placement failed: {response}")
                return None

        except Exception as e:
            logger.exception(f"Error placing order: {e}")
            return None

    def place_bracket_order(
        self,
        option_symbol: str,
        option_token: str,
        quantity: int,
        stop_loss_price: float,
        target_price: float,
        exchange: str = "NFO",
    ) -> Optional[Dict]:
        """
        Simulate bracket order using separate entry, SL, and target orders.
        Angel Broker doesn't support true bracket orders, so we place:
        1. Market BUY order (entry)
        2. Stop-loss SELL order
        3. Limit SELL order (target)

        Args:
            option_symbol: Option trading symbol
            option_token: Option symbol token
            quantity: Number of lots
            stop_loss_price: Stop loss price
            target_price: Target price
            exchange: Exchange (usually NFO for options)

        Returns:
            Dict with order IDs or None
        """
        try:
            # Place entry order (Market BUY)
            entry_order = self.place_order(
                symbol=option_symbol,
                token=option_token,
                exchange=exchange,
                transaction_type="BUY",
                quantity=quantity,
                order_type="MARKET",
            )

            if not entry_order or not entry_order.get("data"):
                logger.error("Entry order failed")
                return None

            entry_order_id = entry_order["data"]["orderid"]

            # Wait a bit for entry order to fill
            time.sleep(1)

            # Place stop-loss order (Stop-loss SELL)
            sl_order = self.place_order(
                symbol=option_symbol,
                token=option_token,
                exchange=exchange,
                transaction_type="SELL",
                quantity=quantity,
                order_type="STOPLOSS_LIMIT",
                price=stop_loss_price,
            )

            # Place target order (Limit SELL)
            target_order = self.place_order(
                symbol=option_symbol,
                token=option_token,
                exchange=exchange,
                transaction_type="SELL",
                quantity=quantity,
                order_type="LIMIT",
                price=target_price,
            )

            result = {
                "entry_order_id": entry_order_id,
                "sl_order_id": sl_order.get("data", {}).get("orderid") if sl_order else None,
                "target_order_id": target_order.get("data", {}).get("orderid") if target_order else None,
            }

            logger.info(f"Bracket order placed: {result}")
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
            await self.rate_limiter.acquire('getPosition')
            
            response = self.smart_api.position()

            if response and response.get("data"):
                return response["data"]

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
            await self.rate_limiter.acquire('getRMS')
            
            # Get RMS Limits (Risk Management System)
            rms = self.smart_api.rmsLimit()

            if rms and rms.get("data"):
                data = rms["data"]
                return {
                    "AvailableFunds": float(data.get("availablecash", 0)),
                    "UtilizedFunds": float(data.get("utilisedpayout", 0)),
                    "TotalFunds": float(data.get("net", 0)),
                }

            return {}

        except Exception as e:
            logger.exception(f"Error getting account summary: {e}")
            return {}

    def subscribe_realtime_bars(self, symbol: str, bar_manager):
        """
        Subscribe to real-time data for a symbol.
        Note: Angel WebSocket implementation would go here.
        For now, we'll use polling as a simpler alternative.
        """
        logger.warning("Real-time WebSocket streaming not yet implemented for Angel Broker")
        logger.info(f"Using polling mode for {symbol}")
        return None

    def unsubscribe_realtime_bars(self, subscription):
        """Unsubscribe from real-time data"""
        pass
