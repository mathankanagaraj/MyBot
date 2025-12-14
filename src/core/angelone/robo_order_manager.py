import asyncio
import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Dict, Tuple, Any, Union

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ---------- Exceptions ----------
class RoboOrderError(Exception):
    pass


class UnsupportedInstrumentError(RoboOrderError):
    pass


class OrderRejectedError(RoboOrderError):
    pass


# ---------- Helper: Exponential backoff ----------
async def _sleep_backoff(attempt: int):
    await asyncio.sleep(min(0.5 * (2**attempt), 8.0))  # cap backoff at 8s


# ---------- Main Manager ----------
class RoboOrderManager:
    """
    RoboOrderManager wraps a SmartAPI-like client (self.client) and provides:
      - place_robo_order(...)
      - fallbacks and robust validation

    Required methods on the underlying client (self.client):
      - async place_order(**params) -> response dict
      - async get_ltp(symbol_token_or_token) -> float
      - async get_instrument_info(symbol_or_token) -> dict (contains tick_size, upper/lower circuit if available, supports_robo boolean if available)
      - async get_order_status(orderid) -> dict
      - async cancel_order(orderid) -> dict
    """

    def __init__(self, client, max_retries: int = 3):
        self.client = client
        self.max_retries = max_retries

    # --------------------- public API ---------------------
    async def place_robo_order(
        self,
        symbol: str,
        token: str,
        quantity: int,
        side: str,  # "BUY" or "SELL"
        sl_points: Union[float, Decimal],
        target_points: Union[float, Decimal],
        entry_price: Optional[Union[float, Decimal]] = None,
        exchange: str = "NFO",
        duration: str = "DAY",
        aggressive_buffer_ticks: int = 2,
        fallback_to_manual: bool = True,
        wait_for_fill_timeout: float = 15.0,
    ) -> Dict[str, Any]:
        """
        High-level function to place a ROBO bracket order (ROBO / BO producttype).

        Args:
            symbol: tradingsymbol
            token: symbol token
            quantity: lot size (int)
            side: "BUY" or "SELL"
            sl_points: stoploss offset in points from entry (positive number)
            target_points: target offset in points from entry (positive number)
            entry_price: optional explicit entry LIMIT price (Decimal/float). If not provided, will derive a safe price from LTP.
            exchange: exchange code (default "NFO")
            duration: "DAY" typically
            aggressive_buffer_ticks: how many ticks to buffer when computing safe limit price
            fallback_to_manual: if ROBO is rejected, attempt the manual bracket alternative
            wait_for_fill_timeout: seconds to wait for entry fill when doing manual bracket fallback

        Returns:
            dict with keys describing outcome. Example:
            {
              "mode": "ROBO" or "MANUAL",
              "robo_response": {...} or None,
              "entry_order_id": "...",
              "sl_order_id": "...",
              "target_order_id": "...",
              "notes": "..."
            }
        Raises:
            RoboOrderError on unrecoverable failure
        """
        side = side.upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")

        # 1) Check instrument support and metadata
        inst = await self._get_instrument_info(token, symbol)
        tick = Decimal(str(inst.get("tick_size", 0.05)))  # fallback tick
        supports_robo = inst.get(
            "supports_robo", True
        )  # assume True unless told otherwise
        lower_circuit = inst.get("lower_circuit")  # optional
        upper_circuit = inst.get("upper_circuit")  # optional

        if not supports_robo:
            msg = f"Instrument {symbol}/{token} does not support ROBO per instrument metadata."
            logger.warning(msg)
            if not fallback_to_manual:
                raise UnsupportedInstrumentError(msg)

        # 2) Determine entry price
        if entry_price is None:
            entry_price = await self._get_safe_limit_price(
                token, side, tick, aggressive_buffer_ticks
            )
        entry_price = Decimal(str(entry_price))

        # 3) Compute SL and Target absolute prices
        sl_price, target_price = self._compute_sl_target(
            entry_price, Decimal(str(sl_points)), Decimal(str(target_points)), side
        )

        # 4) Validate prices against circuits and tick
        self._validate_price_within_limits(
            entry_price, tick, lower_circuit, upper_circuit
        )
        self._validate_price_within_limits(sl_price, tick, lower_circuit, upper_circuit)
        self._validate_price_within_limits(
            target_price, tick, lower_circuit, upper_circuit
        )

        # 5) Round prices to tick size
        entry_price = self._round_to_tick(entry_price, tick)
        sl_price = self._round_to_tick(sl_price, tick)
        target_price = self._round_to_tick(target_price, tick)

        logger.info(
            f"Placing ROBO order: {side} {quantity} {symbol} @ {entry_price}. SL={sl_price}, TG={target_price}"
        )

        # 6) Attempt native ROBO placement (retries on transient errors)
        robo_params = {
            "variety": "ROBO",
            "tradingsymbol": symbol,
            "symboltoken": token,
            "transactiontype": side,
            "exchange": exchange,
            "ordertype": "LIMIT",
            "producttype": "BO",
            "duration": duration,
            "price": str(entry_price),
            "stoploss": str(sl_price),
            "squareoff": str(target_price),
            "quantity": str(quantity),
        }

        attempt = 0
        last_exc = None
        while attempt <= self.max_retries:
            try:
                resp = await self.client.place_order(**robo_params)
                # Inspect response for rejection/ack
                if not self._is_order_success(resp):
                    # If deterministic rejection (validation error), break and fallback
                    reason = self._extract_order_error(resp)
                    logger.warning(f"ROBO placement rejected: {reason}")
                    raise OrderRejectedError(reason)
                # Return success structure. Response may contain orderid in resp["data"]["orderid"]
                logger.info(f"ROBO placement success: {resp}")
                return {
                    "mode": "ROBO",
                    "robo_response": resp,
                    "entry_order_id": self._extract_orderid(resp),
                    "sl_order_id": None,
                    "target_order_id": None,
                    "notes": "Native ROBO placed. Broker handles SL/Target legs.",
                }
            except OrderRejectedError as ore:
                last_exc = ore
                logger.warning(f"ROBO rejected on attempt {attempt}: {ore}")
                break  # deterministic rejection -> fallback
            except Exception as e:
                last_exc = e
                logger.warning(
                    f"Transient error placing ROBO on attempt {attempt}: {e}"
                )
                attempt += 1
                if attempt > self.max_retries:
                    logger.exception("ROBO placement failed after retries")
                    break
                await _sleep_backoff(attempt)

        # 7) Fallback to manual bracket (if enabled)
        if fallback_to_manual:
            try:
                logger.info("Attempting manual bracket fallback")
                manual_result = await self._place_manual_bracket(
                    symbol=symbol,
                    token=token,
                    quantity=quantity,
                    side=side,
                    entry_price=entry_price,
                    sl_price=sl_price,
                    target_price=target_price,
                    exchange=exchange,
                    wait_for_fill_timeout=wait_for_fill_timeout,
                )
                return {
                    "mode": "MANUAL",
                    "robo_response": None,
                    **manual_result,
                }
            except Exception as e:
                logger.exception(f"Manual bracket fallback failed: {e}")
                raise RoboOrderError(
                    f"ROBO failed and manual fallback failed: {e}"
                ) from e

        # If we reach here and fallback disabled or failed:
        raise RoboOrderError(f"ROBO placement failed: {last_exc}")

    # --------------------- helper: manual bracket ---------------------
    async def _place_manual_bracket(
        self,
        symbol: str,
        token: str,
        quantity: int,
        side: str,
        entry_price: Decimal,
        sl_price: Decimal,
        target_price: Decimal,
        exchange: str,
        wait_for_fill_timeout: float = 15.0,
    ) -> Dict[str, Any]:
        """
        Place a manual bracket:
           1) Place entry LIMIT (BUY/SELL)
           2) Wait for it to get filled (polling)
           3) Place separate SL (STOPLOSS_LIMIT) and Target (LIMIT) orders as opposite-side SELL/BUY
        Returns dict with entry_order_id, sl_order_id, target_order_id
        """

        # Place entry
        entry_params = {
            "symbol": symbol,
            "token": token,
            "transaction_type": side,
            "exchange": exchange,
            "order_type": "LIMIT",
            "price": float(entry_price),
            "product_type": "INTRADAY",
            "quantity": quantity,
            "variety": "NORMAL",
        }

        entry_resp = await self.client.place_order(**entry_params)
        if not self._is_order_success(entry_resp):
            reason = self._extract_order_error(entry_resp)
            logger.error(f"Manual entry order rejected: {reason}")
            raise OrderRejectedError(f"Entry order rejected: {reason}")
        entry_id = self._extract_orderid(entry_resp)
        logger.info(f"Manual entry placed. Order ID: {entry_id}")

        # Poll for fill
        filled = await self._wait_for_fill(entry_id, timeout=wait_for_fill_timeout)
        if not filled:
            # Decide policy: cancel entry or leave it?
            logger.warning(
                f"Entry {entry_id} not filled within {wait_for_fill_timeout}s. Returning entry id only."
            )
            return {
                "entry_order_id": entry_id,
                "sl_order_id": None,
                "target_order_id": None,
                "notes": "Entry not filled within timeout; manual SL/Target not placed.",
            }

        # Place SL and Target as opposite transaction type
        exit_side = "SELL" if side == "BUY" else "BUY"

        # Stop-loss (STOPLOSS_LIMIT) — ensure trigger value and price set appropriately.
        sl_params = {
            "symbol": symbol,
            "token": token,
            "transaction_type": exit_side,
            "exchange": exchange,
            "order_type": "STOPLOSS_LIMIT",
            "triggerprice": float(sl_price),
            "price": float(sl_price),
            "product_type": "INTRADAY",
            "quantity": quantity,
            "variety": "STOPLOSS",
        }
        sl_resp = await self.client.place_order(**sl_params)
        sl_order_id = None
        if not self._is_order_success(sl_resp):
            logger.warning(
                f"Stoploss placement failed after entry filled: {self._extract_order_error(sl_resp)}"
            )
        else:
            sl_order_id = self._extract_orderid(sl_resp)
            logger.info(f"Stoploss placed: {sl_order_id}")

        # Target (LIMIT)
        target_params = {
            "symbol": symbol,
            "token": token,
            "transaction_type": exit_side,
            "exchange": exchange,
            "order_type": "LIMIT",
            "price": float(target_price),
            "product_type": "INTRADAY",
            "quantity": quantity,
            "variety": "NORMAL",
        }
        target_resp = await self.client.place_order(**target_params)
        target_order_id = None
        if not self._is_order_success(target_resp):
            logger.warning(
                f"Target placement failed after entry filled: {self._extract_order_error(target_resp)}"
            )
        else:
            target_order_id = self._extract_orderid(target_resp)
            logger.info(f"Target placed: {target_order_id}")

        return {
            "entry_order_id": entry_id,
            "sl_order_id": sl_order_id,
            "target_order_id": target_order_id,
            "notes": "Manual bracket placed after entry fill.",
        }

    # --------------------- helper: waiting for fill ---------------------
    async def _wait_for_fill(
        self, orderid: str, timeout: float = 15.0, poll_interval: float = 1.0
    ) -> bool:
        """
        Poll order status until it's filled or timeout. Returns True if filled.
        """
        logger.info(f"Waiting for fill for order {orderid} (timeout {timeout}s)")
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            try:
                status = await self.client.get_order_status(orderid)
                # status structure depends on API; adapt these keys if your client differs
                # typical fields: status = "COMPLETE" or "OPEN" ... data.filledquantity etc
                if self._is_order_filled(status):
                    logger.info(f"Order {orderid} filled.")
                    return True
                if self._is_order_cancelled_or_rejected(status):
                    logger.warning(f"Order {orderid} cancelled or rejected: {status}")
                    return False
            except Exception as e:
                logger.warning(f"Error checking order status for {orderid}: {e}")
            await asyncio.sleep(poll_interval)
        logger.info("Wait for fill timed out")
        return False

    # --------------------- helper: instrument info / ltp ---------------------
    async def _get_instrument_info(self, token: str, symbol: str) -> Dict[str, Any]:
        """
        Wrapper to read instrument metadata. Your client should implement this.
        Expected return keys (best-effort):
          - tick_size: decimal
          - supports_robo: bool
          - lower_circuit: Decimal or None
          - upper_circuit: Decimal or None
        If your client doesn't provide instrument metadata, this should return sensible defaults.
        """
        # If your client implements get_instrument_info, use it; otherwise fetch LTP and infer tick
        if hasattr(self.client, "get_instrument_info"):
            try:
                info = await self.client.get_instrument_info(token)
                return info or {}
            except Exception:
                logger.warning("get_instrument_info failed; falling back to defaults")
        # Fallback: provide reasonable defaults
        return {"tick_size": Decimal("0.05"), "supports_robo": True}

    async def _get_safe_limit_price(
        self, token: str, side: str, tick: Decimal, buffer_ticks: int = 2
    ) -> Decimal:
        """
        Derive a safe limit price from LTP:
          BUY -> LTP + buffer
          SELL -> LTP - buffer
        Buffer is max(0.5% of LTP, buffer_ticks * tick)
        """
        ltp_raw = await self.client.get_ltp(token)
        ltp = Decimal(str(ltp_raw))
        # compute buffer
        percent_buffer = (ltp * Decimal("0.005")).quantize(
            Decimal("0.0001"), rounding=ROUND_HALF_UP
        )
        tick_buffer = tick * Decimal(str(buffer_ticks))
        buffer_pts = max(percent_buffer, tick_buffer)
        if side == "BUY":
            safe = ltp + buffer_pts
        else:
            safe = ltp - buffer_pts
        # final rounding to 2 decimal places (or tick precision)
        return self._round_to_tick(safe, tick)

    # --------------------- helper: computations / rounding / validations ---------------------
    def _compute_sl_target(
        self, entry: Decimal, sl_points: Decimal, target_points: Decimal, side: str
    ) -> Tuple[Decimal, Decimal]:
        """
        Return (sl_price, target_price) absolute values based on side.
        For BUY: SL = entry - sl_points; Target = entry + target_points
        For SELL: SL = entry + sl_points; Target = entry - target_points
        """
        if side == "BUY":
            sl = entry - sl_points
            tg = entry + target_points
        else:
            sl = entry + sl_points
            tg = entry - target_points
        return sl, tg

    def _round_to_tick(self, price: Decimal, tick: Decimal) -> Decimal:
        """
        Round price to nearest tick (round half up).
        """
        if tick == 0:
            # fallback
            return price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        # compute factor = 1/tick
        tick = Decimal(str(tick))
        factor = (Decimal("1") / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        # number of ticks
        n = (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        rounded = (n * tick).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return rounded

    def _validate_price_within_limits(
        self,
        price: Decimal,
        tick: Decimal,
        lower_circuit: Optional[Decimal],
        upper_circuit: Optional[Decimal],
    ):
        """
        Basic validation against circuit limits (if provided)
        """
        if lower_circuit is not None and price < Decimal(str(lower_circuit)):
            raise RoboOrderError(f"Price {price} below lower circuit {lower_circuit}")
        if upper_circuit is not None and price > Decimal(str(upper_circuit)):
            raise RoboOrderError(f"Price {price} above upper circuit {upper_circuit}")

    # --------------------- helper: response parsers ---------------------
    def _is_order_success(self, resp: Dict[str, Any]) -> bool:
        """
        Determine whether place_order response is successful.
        Adjust according to your client response formats.
        """
        if not resp:
            return False
        # Many SmartAPI wrappers return {"status": True, "data": {...}} or {"status": "success"}
        status = resp.get("status")
        if isinstance(status, bool):
            return status
        if isinstance(status, str):
            return status.lower() in ("success", "ok", "true")
        # fallback: check data and orderid presence
        data = resp.get("data", {})
        if isinstance(data, dict) and data.get("orderid"):
            return True
        return False

    def _extract_orderid(self, resp: Dict[str, Any]) -> Optional[str]:
        if not resp:
            return None
        data = resp.get("data", {})
        return data.get("orderid") or resp.get("orderid")

    def _extract_order_error(self, resp: Dict[str, Any]) -> str:
        # Try to extract meaningful error message from broker response
        if not resp:
            return "empty response"
        for key in ("message", "error", "error_description", "data"):
            v = resp.get(key)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict) and v.get("message"):
                return v.get("message")
        # fallback to entire payload
        return str(resp)

    def _is_order_filled(self, status_resp: Dict[str, Any]) -> bool:
        # Adapt to your client's order status return structure
        # Example: status_resp.get("status") == "COMPLETE" or status_resp["data"]["filledquantity"] == requested
        st = status_resp.get("status") or status_resp.get("order_status") or ""
        if isinstance(st, str) and st.upper() in (
            "COMPLETE",
            "FILLED",
            "CANCELLED",
        ):  # CANCELLED is not filled but useful to short-circuit
            return st.upper() == "COMPLETE" or st.upper() == "FILLED"
        # If response includes filled quantity
        data = status_resp.get("data", {})
        filled_qty = (
            data.get("filledquantity")
            or data.get("filled_quantity")
            or data.get("filledQty")
        )
        if filled_qty:
            try:
                if int(filled_qty) > 0:
                    # if filled equals requested logic can be improved
                    return True
            except Exception:
                pass
        return False

    def _is_order_cancelled_or_rejected(self, status_resp: Dict[str, Any]) -> bool:
        st = status_resp.get("status") or status_resp.get("order_status") or ""
        if isinstance(st, str) and st.upper() in ("CANCELLED", "REJECTED", "FAILED"):
            return True
        return False
