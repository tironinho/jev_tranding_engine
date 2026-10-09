from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any
from urllib.parse import urlencode

from app.config import Settings

_EMPTY = {
    "asset": "USDT",
    "wallet": None,
    "available": None,
    "unrealized": None,
    "assets": [],
    "detail": None,
}


class BinanceBalanceProvider:
    """Read-only wallet snapshot. Never places or withdraws."""

    def __init__(self, settings: Settings, client: Any = None, ttl_s: float = 20) -> None:
        self.settings = settings
        self.client = client
        self.ttl_s = ttl_s
        self._cached: dict | None = None
        self._cached_at = 0.0

    async def snapshot(self, *, fresh: bool = False) -> dict:
        now = time.monotonic()
        if not fresh and self._cached is not None and now - self._cached_at < self.ttl_s:
            return self._cached
        payload = await self._fetch()
        self._cached = payload
        self._cached_at = now
        return payload

    async def _fetch(self) -> dict:
        settings = self.settings
        base = {"status": "UNAVAILABLE", "market_type": settings.market_type, **_EMPTY}
        if settings.balance_upstream_url:
            return await self._fetch_upstream(base)
        if not settings.binance_api_key or not settings.binance_api_secret:
            return {**base, "status": "NO_CREDENTIALS"}
        if self.client is None:
            return {**base, "status": "UNAVAILABLE", "detail": "client not started"}
        signed = _signed(
            settings.binance_api_secret,
            {"timestamp": int(time.time() * 1000), "recvWindow": 5000},
        )
        if settings.market_type == "margin":
            url = f"{settings.binance_account_rest_url.rstrip('/')}/sapi/v1/margin/account"
        elif settings.market_type == "spot":
            url = f"{settings.binance_spot_rest_url}/api/v3/account"
        else:
            url = f"{settings.binance_futures_rest_url}/fapi/v2/balance"
        try:
            response = await self.client.get(
                url,
                params=signed,
                headers={"X-MBX-APIKEY": settings.binance_api_key},
                timeout=5,
            )
        except Exception as exc:
            return {**base, "status": "ERROR", "detail": type(exc).__name__}
        if response.status_code != 200:
            return {**base, "status": "ERROR", "detail": f"HTTP {response.status_code}"}
        try:
            body = response.json()
        except Exception:
            return {**base, "status": "ERROR", "detail": "INVALID_JSON"}
        if settings.market_type == "margin":
            return _margin(body, settings.market_type)
        if settings.market_type == "spot":
            return _spot(body, settings.market_type)
        return _futures(body, settings.market_type)

    async def _fetch_upstream(self, base: dict) -> dict:
        settings = self.settings
        token = settings.balance_share_token or settings.engine_api_secret
        if not token:
            return {**base, "status": "NO_CREDENTIALS", "detail": "upstream token missing"}
        if self.client is None:
            return {**base, "status": "UNAVAILABLE", "detail": "client not started"}
        url = f"{settings.balance_upstream_url.rstrip('/')}/api/binance/balance"
        try:
            response = await self.client.get(
                url,
                headers={"authorization": f"Bearer {token}"},
                timeout=8,
            )
        except Exception as exc:
            return {**base, "status": "ERROR", "detail": type(exc).__name__}
        if response.status_code != 200:
            return {**base, "status": "ERROR", "detail": f"HTTP {response.status_code}"}
        try:
            body = response.json()
        except Exception:
            return {**base, "status": "ERROR", "detail": "INVALID_JSON"}
        if not isinstance(body, dict):
            return {**base, "status": "ERROR", "detail": "INVALID_JSON"}
        return body


def _signed(secret: str, params: dict) -> dict:
    query = urlencode(params)
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return {**params, "signature": signature}


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _spot(body: Any, market_type: str) -> dict:
    rows = body.get("balances") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return {"status": "ERROR", "market_type": market_type, **_EMPTY, "detail": "INVALID_ACCOUNT"}
    assets = []
    wallet = 0.0
    available = 0.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        free = _num(row.get("free"))
        locked = _num(row.get("locked"))
        total = free + locked
        if total <= 0:
            continue
        asset = str(row.get("asset") or "")
        assets.append({"asset": asset, "free": free, "locked": locked, "total": total})
        if asset == "USDT":
            wallet = total
            available = free
    assets.sort(key=lambda item: item["total"], reverse=True)
    return {
        "status": "ok",
        "market_type": market_type,
        "asset": "USDT",
        "wallet": wallet,
        "available": available,
        "unrealized": None,
        "assets": assets[:8],
        "detail": None,
    }


def _margin(body: Any, market_type: str) -> dict:
    rows = body.get("userAssets") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return {"status": "ERROR", "market_type": market_type, **_EMPTY, "detail": "INVALID_ACCOUNT"}
    assets = []
    wallet = 0.0
    available = 0.0
    found = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        free = _num(row.get("free"))
        locked = _num(row.get("locked"))
        borrowed = _num(row.get("borrowed"))
        net = _num(row.get("netAsset"))
        if net == 0 and free == 0 and locked == 0 and borrowed == 0:
            continue
        asset = str(row.get("asset") or "")
        assets.append({"asset": asset, "free": free, "locked": locked, "total": net})
        if asset == "USDT":
            found = True
            wallet = net
            available = free
    if not found:
        wallet = 0.0
        available = 0.0
    assets.sort(key=lambda item: abs(item["total"]), reverse=True)
    level = body.get("marginLevel")
    return {
        "status": "ok",
        "market_type": market_type,
        "asset": "USDT",
        "wallet": wallet,
        "available": available,
        "unrealized": None,
        "margin_level": _num(level) if level not in (None, "") else None,
        "assets": assets[:8],
        "detail": None,
    }


def _futures(body: Any, market_type: str) -> dict:
    if not isinstance(body, list):
        return {"status": "ERROR", "market_type": market_type, **_EMPTY, "detail": "INVALID_ACCOUNT"}
    assets = []
    wallet = 0.0
    available = 0.0
    unrealized = 0.0
    found = False
    for row in body:
        if not isinstance(row, dict):
            continue
        balance = _num(row.get("balance"))
        if balance == 0 and _num(row.get("crossUnPnl")) == 0:
            continue
        asset = str(row.get("asset") or "")
        assets.append(
            {
                "asset": asset,
                "free": _num(row.get("availableBalance")),
                "locked": None,
                "total": balance,
            }
        )
        if asset == "USDT":
            found = True
            wallet = balance
            available = _num(row.get("availableBalance"))
            unrealized = _num(row.get("crossUnPnl"))
    if not found:
        wallet = 0.0
        available = 0.0
        unrealized = 0.0
    assets.sort(key=lambda item: item["total"], reverse=True)
    return {
        "status": "ok",
        "market_type": market_type,
        "asset": "USDT",
        "wallet": wallet,
        "available": available,
        "unrealized": unrealized,
        "assets": assets[:8],
        "detail": None,
    }
