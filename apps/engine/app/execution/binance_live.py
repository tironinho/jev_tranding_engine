from __future__ import annotations

import hashlib
import hmac
import math
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from uuid import UUID

import httpx

from app.config import Settings
from app.domain.enums import LIVE_LOCKED, NO_CREDENTIALS, OrderStatus, OrderType
from app.domain.mathutil import round_down_to_step
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
        self._close_attempts: dict[UUID, int] = {}

    def _margin(self) -> bool:
        return self.settings.market_type == "margin"

    def _relays(self) -> bool:
        return self._margin() and bool(self.settings.balance_upstream_url)

    def _endpoint(self) -> str:
        if self._margin():
            return f"{self.settings.binance_account_rest_url.rstrip('/')}/sapi/v1/margin/order"
        if self.settings.market_type == "spot":
            return f"{self.settings.binance_spot_rest_url}/api/v3/order"
        return f"{self.settings.binance_futures_rest_url}/fapi/v1/order"

    async def submit(self, intent: OrderIntent) -> OrderRecord:
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
            "newOrderRespType": "FULL",
        }
        if self._margin():
            params["sideEffectType"] = "AUTO_BORROW_REPAY"
            params["isIsolated"] = "FALSE"
        if intent.order_type is OrderType.LIMIT:
            params["timeInForce"] = "GTC"
            params["price"] = _price(intent.limit_price or 0, None)
        payload = await self._signed("POST", params)
        order = _order_from_payload(intent, payload, client_id, intent.order_type)
        self._sent[client_id] = order
        return order

    async def submit_stop(self, intent: OrderIntent, stop_price: float, tick: float | None = None, suffix: str = "S") -> OrderRecord:
        """Resting protective stop. Futures uses closePosition. Margin and spot use a stop-limit that repays debt."""
        client_id = _suffixed(intent.decision_id, suffix)
        existing = self._sent.get(client_id)
        if existing is not None:
            return existing
        self._require_reduce(intent)
        closing_side = "SELL" if intent.side == "BUY" else "BUY"
        aligned = _align_stop(stop_price, tick, direction=-1 if closing_side == "SELL" else 1)
        if self._margin() or self.settings.market_type == "spot":
            params = {
                "symbol": intent.symbol,
                "side": closing_side,
                "type": "STOP_LOSS_LIMIT",
                "quantity": _qty(intent.quantity),
                "stopPrice": _price(aligned, tick),
                "price": _price(_spot_limit(aligned, closing_side), tick),
                "timeInForce": "GTC",
                "newClientOrderId": client_id,
                "newOrderRespType": "FULL",
            }
            if self._margin():
                params["sideEffectType"] = "AUTO_REPAY"
                params["isIsolated"] = "FALSE"
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
        actual_type = OrderType.STOP_LOSS_LIMIT if self._margin() or self.settings.market_type == "spot" else OrderType.STOP_MARKET
        order = _order_from_payload(intent, payload, client_id, actual_type)
        order.side = closing_side  # type: ignore[assignment]
        order.price = aligned
        self._sent[client_id] = order
        return order

    async def submit_close(self, intent: OrderIntent) -> OrderRecord:
        """Flatten a position this process opened. Does not require the arm flags."""
        attempt = intent.close_sequence
        client_id = _close_client_id(intent.decision_id, attempt)
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
            "newOrderRespType": "FULL" if self._margin() or self.settings.market_type == "spot" else "RESULT",
        }
        if self._margin():
            params["sideEffectType"] = "AUTO_REPAY"
            params["isIsolated"] = "FALSE"
        elif self.settings.market_type != "spot":
            params["reduceOnly"] = "true"
        # Same durable client ID across timeouts/restarts. Unknown status is never a new order.
        try:
            payload = await self._signed("GET", {"symbol": intent.symbol, "origClientOrderId": client_id})
        except LiveExecutionBlocked as exc:
            if "-2013" not in str(exc):
                raise
            payload = await self._signed("POST", params)
        order = _order_from_payload(intent, payload, client_id, OrderType.MARKET)
        self._sent[client_id] = order
        return order

    async def cancel(self, symbol: str, client_order_id: str) -> dict:
        self._require_credentials()
        result = await self._signed("DELETE", {"symbol": symbol, "origClientOrderId": client_order_id})
        self._sent.pop(client_order_id, None)
        return result

    async def fetch(self, symbol: str, client_order_id: str) -> dict | None:
        try:
            self._require_credentials()
            return await self._signed("GET", {"symbol": symbol, "origClientOrderId": client_order_id})
        except Exception:
            return None

    async def fetch_verified(self, symbol: str, client_order_id: str) -> dict:
        try:
            return await self._signed("GET", {"symbol": symbol, "origClientOrderId": client_order_id})
        except LiveExecutionBlocked as exc:
            if "-2013" in str(exc):
                return {"status": "NOT_FOUND", "executedQty": "0"}
            raise

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
        if self.client is None:
            raise LiveExecutionBlocked(NO_CREDENTIALS)
        if self._relays():
            if not (self.settings.balance_share_token or self.settings.engine_api_secret):
                raise LiveExecutionBlocked(NO_CREDENTIALS)
            return
        if not self.settings.binance_api_key or not self.settings.binance_api_secret:
            raise LiveExecutionBlocked(NO_CREDENTIALS)

    async def _signed(self, method: str, params: dict) -> dict:
        self._require_credentials()
        if self._relays():
            return await self._relay(method, params)
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
        if response.status_code >= 400:
            raise LiveExecutionBlocked(_http_failure(response))
        return response.json()

    async def _relay(self, method: str, params: dict) -> dict:
        token = self.settings.balance_share_token or self.settings.engine_api_secret
        url = f"{self.settings.balance_upstream_url.rstrip('/')}/api/binance/order"
        assert self.client is not None
        response = await self.client.post(
            url,
            json={"method": method, "params": params},
            headers={"authorization": f"Bearer {token}"},
            timeout=15,
        )
        if response.status_code >= 400:
            raise LiveExecutionBlocked(_http_failure(response))
        body = response.json()
        if not isinstance(body, dict):
            raise LiveExecutionBlocked("INVALID_JSON")
        return body


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


def base_asset(symbol: str) -> str:
    if symbol.endswith("USDT"):
        return symbol[:-4]
    return symbol


def held_quantity(side: str, symbol: str, executed: float, payload: dict) -> float:
    """A buy paid in the base asset holds less than the executed quantity."""
    if side != "BUY" or executed <= 0:
        return executed
    base = base_asset(symbol)
    paid = 0.0
    fills = payload.get("fills")
    if isinstance(fills, list):
        for item in fills:
            if not isinstance(item, dict) or str(item.get("commissionAsset") or "") != base:
                continue
            try:
                paid += float(item.get("commission") or 0)
            except (TypeError, ValueError):
                continue
    net = executed - paid
    return net if net > 0 else executed


def sellable_quantity(quantity: float, free: float | None, step: float | None) -> float:
    """Step a sell down only when the free base is the same fill minus the fee.

    A much smaller free balance is an older snapshot, not the position.
    """
    capped = min(quantity, max(0.0, free)) if free is not None else 0.0
    if step and step > 0:
        capped = round_down_to_step(capped, step)
    return capped if capped > 0 else 0.0


def lot_quantity(quantity: float, step: float | None) -> float:
    """The filled size, on the exchange step. The account snapshot does not choose it."""
    if quantity <= 0:
        return 0.0
    if step and step > 0:
        return round_down_to_step(quantity, step)
    return quantity


def _close_client_id(decision_id: UUID, attempt: int) -> str:
    suffix = "C" if attempt <= 0 else f"C{attempt}"
    return decision_id.hex[: 36 - len(suffix)] + suffix


def _http_failure(response) -> str:
    detail = " ".join(response.text.split())[:160]
    if not detail:
        return f"HTTP {response.status_code}"
    return f"HTTP {response.status_code} {detail}"


def _order_from_payload(intent: OrderIntent, payload: dict, client_id: str, order_type: OrderType) -> OrderRecord:
    qty, price = execution_of(payload)
    commissions = {}
    for item in payload.get("fills") or []:
        asset = str(item.get("commissionAsset") or "")
        commissions[asset] = commissions.get(asset, 0) + float(item.get("commission") or 0)
    stamp = payload.get("transactTime") or payload.get("updateTime") or payload.get("time")
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
        exchange_at=datetime.fromtimestamp(float(stamp) / 1000, timezone.utc) if stamp else None,
        received_at=datetime.now(timezone.utc),
        commissions=commissions,
    )
