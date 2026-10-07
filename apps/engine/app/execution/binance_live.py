from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from urllib.parse import urlencode
from uuid import UUID

import httpx

from app.config import Settings
from app.domain.enums import LIVE_LOCKED, NO_CREDENTIALS, OrderStatus, OrderType
from app.domain.schemas import OrderRecord
from app.execution.paper import OrderIntent


class LiveExecutionBlocked(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class _Sent:
    order: OrderRecord


class BinanceExecutionProvider:
    """Real orders stay impossible unless both live flags are set.

    This class is the only place that knows Binance order endpoints.
    It never calls withdrawal endpoints.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self.client = client
        self._sent: dict[str, OrderRecord] = {}

    def _endpoint(self) -> str:
        if self.settings.market_type == "spot":
            return f"{self.settings.binance_spot_rest_url}/api/v3/order"
        return f"{self.settings.binance_futures_rest_url}/fapi/v1/order"

    async def submit(self, intent: OrderIntent) -> OrderRecord:
        client_id = intent.decision_id.hex
        existing = self._sent.get(client_id)
        if existing is not None:
            return existing
        if intent.mode != "live" or not (self.settings.trading_live_enabled and self.settings.allow_real_orders):
            raise LiveExecutionBlocked(LIVE_LOCKED)
        if not self.settings.binance_api_key or not self.settings.binance_api_secret:
            raise LiveExecutionBlocked(NO_CREDENTIALS)
        if self.client is None:
            raise LiveExecutionBlocked(NO_CREDENTIALS)
        params = {
            "symbol": intent.symbol,
            "side": intent.side,
            "type": intent.order_type.value,
            "quantity": f"{intent.quantity:.8f}".rstrip("0").rstrip("."),
            "newClientOrderId": client_id,
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000,
        }
        if intent.order_type is OrderType.LIMIT:
            params["timeInForce"] = "GTC"
            params["price"] = f"{(intent.limit_price or 0):.8f}"
        query = urlencode(params)
        signature = hmac.new(
            self.settings.binance_api_secret.encode(),
            query.encode(),
            hashlib.sha256,
        ).hexdigest()
        response = await self.client.post(
            self._endpoint(),
            params={**params, "signature": signature},
            headers={"X-MBX-APIKEY": self.settings.binance_api_key},
        )
        response.raise_for_status()
        payload = response.json()
        order = OrderRecord(
            client_order_id=str(payload.get("clientOrderId") or client_id),
            decision_id=intent.decision_id,
            risk_id=intent.risk_id,
            strategy=intent.strategy,
            symbol=intent.symbol,
            side=intent.side,  # type: ignore[arg-type]
            order_type=intent.order_type,
            status=OrderStatus.NEW,
            mode="live",
            quantity=intent.quantity,
            price=intent.limit_price,
            created_at=intent.created_at,
        )
        self._sent[client_id] = order
        return order

    def known(self, decision_id: UUID) -> OrderRecord | None:
        return self._sent.get(decision_id.hex)
