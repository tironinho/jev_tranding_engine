from __future__ import annotations

import hashlib
import hmac
import math
import time
from urllib.parse import urlencode
from uuid import UUID

import httpx

from app.config import Settings
from app.domain.enums import LIVE_LOCKED, MARGIN_PAPER_ONLY, NO_CREDENTIALS, OrderStatus, OrderType
from app.domain.schemas import OrderRecord
from app.execution.paper import OrderIntent


class LiveExecutionBlocked(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BinanceExecutionProvider:
    """Real orders stay impossible unless both live flags are set.

    This class is the only place that knows Binance order endpoints.
    It never calls withdrawal endpoints. A reduce-only close of a position
    this process already opened does not require the flags again: disarming
    stops new entries and still allows the open position to be flattened.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client
        self._sent: dict[str, OrderRecord] = {}

    def _endpoint(self) -> str:
        self._block_margin()
        if self.settings.market_type == "spot":
            return f"{self.settings.binance_spot_rest_url}/api/v3/order"
        return f"{self.settings.binance_futures_rest_url}/fapi/v1/order"

    def _block_margin(self) -> None:
        if self.settings.market_type == "margin":
            raise LiveExecutionBlocked(MARGIN_PAPER_ONLY)

    async def submit(self, intent: OrderIntent) -> OrderRecord:
        self._block_margin()
        client_id = intent.decision_id.hex
        existing = self._sent.get(client_id)
        if existing is not None:
            return existing
        self._require_entry(intent)
        params = {
            "symbol": intent.symbol,
            "side": intent.side,
            "type": intent.order_type.value,
            "quantity": _qty(intent.quantity),
            "newClientOrderId": client_id,
            "newOrderRespType": "RESULT",
        }
        if intent.order_type is OrderType.LIMIT:
            params["timeInForce"] = "GTC"
            params["price"] = _price(intent.limit_price or 0, None)
        payload = await self._signed("POST", params)
        order = _order_from_payload(intent, payload, client_id, intent.order_type)
        self._sent[client_id] = order
        return order

    async def submit_stop(self, intent: OrderIntent, stop_price: float, tick: float | None = None) -> OrderRecord:
        """Resting protective stop. Futures uses closePosition. Spot uses a stop-limit."""
        self._block_margin()
        client_id = _suffixed(intent.decision_id, "S")
        existing = self._sent.get(client_id)
        if existing is not None:
            return existing
        self._require_entry(intent)
        closing_side = "SELL" if intent.side == "BUY" else "BUY"
        aligned = _align_stop(stop_price, tick, direction=-1 if closing_side == "SELL" else 1)
        if self.settings.market_type == "spot":
            params = {
                "symbol": intent.symbol,
                "side": closing_side,
                "type": "STOP_LOSS_LIMIT",
                "quantity": _qty(intent.quantity),
                "stopPrice": _price(aligned, tick),
                "price": _price(_spot_limit(aligned, closing_side), tick),
                "timeInForce": "GTC",
                "newClientOrderId": client_id,
                "newOrderRespType": "RESULT",
            }
        else:
            params = {
                "symbol": intent.symbol,
                "side": closing_side,
                "type": "STOP_MARKET",
                "stopPrice": _price(aligned, tick),
                "closePosition": "true",
                "workingType": "CONTRACT_PRICE",
                "newClientOrderId": client_id,
                "newOrderRespType": "RESULT",
            }
        payload = await self._signed("POST", params)
        order = _order_from_payload(intent, payload, client_id, OrderType.STOP_MARKET)
        order.side = closing_side  # type: ignore[assignment]
        order.price = aligned
        self._sent[client_id] = order
        return order

    async def submit_close(self, intent: OrderIntent) -> OrderRecord:
        """Flatten a position this process opened. Does not require the arm flags."""
        self._block_margin()
        client_id = _suffixed(intent.decision_id, "C")
        existing = self._sent.get(client_id)
        if existing is not None and existing.status is OrderStatus.FILLED:
            return existing
        self._require_reduce(intent)
        params = {
            "symbol": intent.symbol,
            "side": intent.side,
            "type": "MARKET",
            "quantity": _qty(intent.quantity),
            "newClientOrderId": client_id,
            "newOrderRespType": "RESULT",
        }
        if self.settings.market_type != "spot":
            params["reduceOnly"] = "true"
        payload = await self._signed("POST", params)
        order = _order_from_payload(intent, payload, client_id, OrderType.MARKET)
        self._sent[client_id] = order
        return order

    async def cancel(self, symbol: str, client_order_id: str) -> None:
        self._block_margin()
        self._require_credentials()
        await self._signed("DELETE", {"symbol": symbol, "origClientOrderId": client_order_id})

    async def fetch(self, symbol: str, client_order_id: str) -> dict | None:
        try:
            self._require_credentials()
            return await self._signed("GET", {"symbol": symbol, "origClientOrderId": client_order_id})
        except Exception:
            return None

    def known(self, decision_id: UUID) -> OrderRecord | None:
        return self._sent.get(decision_id.hex)

    def _require_entry(self, intent: OrderIntent) -> None:
        if intent.mode != "live" or not (self.settings.trading_live_enabled and self.settings.allow_real_orders):
            raise LiveExecutionBlocked(LIVE_LOCKED)
        self._require_credentials()

    def _require_reduce(self, intent: OrderIntent) -> None:
        if intent.mode != "live":
            raise LiveExecutionBlocked(LIVE_LOCKED)
        self._require_credentials()

    def _require_credentials(self) -> None:
        if not self.settings.binance_api_key or not self.settings.binance_api_secret or self.client is None:
            raise LiveExecutionBlocked(NO_CREDENTIALS)

    async def _signed(self, method: str, params: dict) -> dict:
        self._require_credentials()
        body = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        query = urlencode(body)
        signature = hmac.new(self.settings.binance_api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        signed = {**body, "signature": signature}
        headers = {"X-MBX-APIKEY": self.settings.binance_api_key}
        url = self._endpoint()
        assert self.client is not None
        if method == "POST":
            response = await self.client.post(url, params=signed, headers=headers)
        elif method == "DELETE":
            response = await self.client.delete(url, params=signed, headers=headers)
        else:
            response = await self.client.get(url, params=signed, headers=headers)
        response.raise_for_status()
        return response.json()


def execution_of(payload: dict) -> tuple[float, float]:
    qty = float(payload.get("executedQty") or 0)
    raw_avg = payload.get("avgPrice")
    price = float(raw_avg) if raw_avg not in (None, "") else 0.0
    if price <= 0 and qty > 0:
        quote = payload.get("cumQuote") or payload.get("cummulativeQuoteQty")
        if quote not in (None, ""):
            price = float(quote) / qty
    fills = payload.get("fills")
    if price <= 0 and isinstance(fills, list) and fills:
        notional = 0.0
        filled = 0.0
        for item in fills:
            part = float(item.get("qty") or 0)
            part_price = float(item.get("price") or 0)
            notional += part * part_price
            filled += part
        if filled > 0:
            return filled, notional / filled
    return qty, price


def _suffixed(decision_id: UUID, suffix: str) -> str:
    return decision_id.hex[:31] + suffix


def _qty(quantity: float) -> str:
    return f"{quantity:.8f}".rstrip("0").rstrip(".")


def _decimals(tick: float) -> int:
    text = f"{tick:.10f}".rstrip("0")
    if "." not in text:
        return 0
    return len(text.split(".")[1])


def _price(price: float, tick: float | None) -> str:
    if tick and tick > 0:
        return f"{price:.{_decimals(tick)}f}"
    return f"{price:.8f}".rstrip("0").rstrip(".")


def _align_stop(price: float, tick: float | None, direction: int) -> float:
    if not tick or tick <= 0:
        return price
    steps = price / tick
    aligned = (math.floor(steps + 1e-9) if direction < 0 else math.ceil(steps - 1e-9)) * tick
    return aligned


def _spot_limit(stop_price: float, closing_side: str) -> float:
    if closing_side == "SELL":
        return stop_price * (1 - 0.002)
    return stop_price * (1 + 0.002)


def _order_from_payload(intent: OrderIntent, payload: dict, client_id: str, order_type: OrderType) -> OrderRecord:
    qty, price = execution_of(payload)
    raw_status = str(payload.get("status") or "NEW")
    try:
        status = OrderStatus(raw_status)
    except ValueError:
        status = OrderStatus.NEW
    return OrderRecord(
        client_order_id=str(payload.get("clientOrderId") or client_id),
        decision_id=intent.decision_id,
        risk_id=intent.risk_id,
        strategy=intent.strategy,
        symbol=intent.symbol,
        side=intent.side,  # type: ignore[arg-type]
        order_type=order_type,
        status=status,
        mode="live",
        quantity=intent.quantity,
        filled_quantity=qty,
        price=intent.limit_price,
        average_fill_price=price if qty > 0 and price > 0 else None,
        created_at=intent.created_at,
    )
