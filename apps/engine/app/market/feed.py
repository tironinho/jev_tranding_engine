from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx
import websockets

from app.config import Settings
from app.market.parse import apply_market_message, parse_exchange_filters, parse_rest_klines, stream_symbol
from app.resilience.guards import CircuitBreaker, TokenBucket

log = logging.getLogger(__name__)

# Public /api/v3 from a cloud IP often returns 418 while the same client,
# signed with the account key, is accepted. Probe once and keep the route.
_SPOT_REST_CANDIDATES = (
    "https://api.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://data-api.binance.vision",
)
_MARKET_USER_AGENT = "Mozilla/5.0 (compatible; trading-engine/0.1)"
_SPOT_WS_API = "wss://ws-api.binance.com/ws-api/v3"


def _signed_params(secret: str, params: dict) -> dict:
    query = urlencode(params)
    signature = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return {**params, "signature": signature}


def _market_headers(api_key: str, mode: str) -> dict[str, str]:
    headers = {"User-Agent": _MARKET_USER_AGENT, "Accept": "application/json"}
    if api_key and mode in {"keyed", "signed"}:
        headers["X-MBX-APIKEY"] = api_key
    return headers


class MarketFeed:
    def __init__(self, settings: Settings, states: dict, on_trigger, on_ticker, on_price) -> None:
        self.settings = settings
        self.states = states
        self.on_trigger = on_trigger
        self.on_ticker = on_ticker
        self.on_price = on_price
        self.connected = False
        self.last_error: str | None = None
        self.reconnects = 0
        self.rules: dict[str, dict[str, float]] = {}
        self.breaker = CircuitBreaker("binance", failure_threshold=5, recovery_seconds=20)
        self.bucket = TokenBucket(rate=4, capacity=8)
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._last_ui: dict[str, float] = {}
        self.geo_blocked = False
        self._rest_mode = "plain"
        self._rest_base_override: str | None = None
        self._market_rest_blocked = False
        self._warned_418 = False
        self._ws_id = 0

    def _futures(self) -> bool:
        return self.settings.market_type == "futures"

    def ws_url(self) -> str:
        streams = []
        for symbol in self.settings.symbol_list:
            lower = symbol.lower()
            streams.extend(
                [
                    f"{lower}@kline_1m",
                    f"{lower}@kline_5m",
                    f"{lower}@kline_15m",
                    f"{lower}@aggTrade",
                    f"{lower}@bookTicker",
                    f"{lower}@depth20@100ms",
                ]
            )
            if self._futures():
                streams.append(f"{lower}@markPrice@1s")
        base = self.settings.binance_futures_ws_url if self._futures() else self.settings.binance_spot_ws_url
        return f"{base}?streams={'/'.join(streams)}"

    def rest_base(self) -> str:
        if self._rest_base_override:
            return self._rest_base_override
        if self._futures():
            return self.settings.binance_futures_rest_url
        return self.settings.binance_spot_rest_url

    async def start(self) -> None:
        self._tasks.append(asyncio.create_task(self._run(), name="market_data_worker"))
        if self._futures():
            self._tasks.append(asyncio.create_task(self._derivatives_loop(), name="derivatives_worker"))

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            if not self.breaker.allow():
                await asyncio.sleep(self.breaker.recovery_seconds)
                continue
            try:
                await self._bootstrap()
                await self._stream()
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = str(exc)
                self.reconnects += 1
                self.breaker.record_failure()
                log.warning("binance feed disconnected: %s", exc)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                    return
                except asyncio.TimeoutError:
                    backoff = min(backoff * 2, 60)

    async def _stream(self) -> None:
        async with websockets.connect(
            self.ws_url(),
            ping_interval=20,
            ping_timeout=20,
            max_queue=1024,
            open_timeout=15,
        ) as socket:
            self.connected = True
            self.last_error = None
            self.breaker.record_success()
            while not self._stop.is_set():
                raw = await asyncio.wait_for(socket.recv(), timeout=30)
                received = datetime.now(timezone.utc)
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                data = payload.get("data") if isinstance(payload, dict) else None
                symbol = stream_symbol(payload) if isinstance(payload, dict) else None
                if not isinstance(data, dict) or symbol not in self.states:
                    continue
                kind = apply_market_message(self.states[symbol], payload, received)
                if kind is None:
                    continue
                if kind == "kline_close_1m" and self.settings.snapshot_trigger == "1m_close":
                    log.info("1m candle closed %s", symbol)
                    await self.on_trigger(symbol, received, "kline_close_1m")
                if kind in {"book", "depth", "trade", "kline", "kline_close_1m"}:
                    await self.on_price(symbol, self.states[symbol].last_price, received)
                now_m = time.monotonic()
                if now_m - self._last_ui.get(symbol, 0.0) >= 0.5:
                    self._last_ui[symbol] = now_m
                    await self.on_ticker(symbol)

    async def _bootstrap(self) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            await self._prepare_market_rest(client)
            if self._market_rest_blocked:
                await self._load_ws_history()
                return
            await self._load_rules(client)
            for symbol in self.settings.symbol_list:
                for interval, limit in (("1m", 300), ("5m", 200), ("15m", 200)):
                    rows = await self._get(client, self._kline_path(), {"symbol": symbol, "interval": interval, "limit": limit})
                    if isinstance(rows, list):
                        for candle in parse_rest_klines(symbol, interval, rows):
                            self.states[symbol].upsert_candle(candle)
                book = await self._get(client, self._depth_path(), {"symbol": symbol, "limit": 20})
                if isinstance(book, dict):
                    self._apply_rest_depth(symbol, book)
                ticker = await self._get(client, self._book_path(), {"symbol": symbol})
                if isinstance(ticker, dict):
                    self._apply_rest_book(symbol, ticker)
                if self.states[symbol].last_price:
                    log.info("bootstrap snapshot %s", symbol)
                    await self.on_trigger(symbol, datetime.now(timezone.utc), "bootstrap")

    async def _derivatives_loop(self) -> None:
        while not self._stop.is_set():
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    for symbol in self.settings.symbol_list:
                        premium = await self._get(client, "/fapi/v1/premiumIndex", {"symbol": symbol})
                        oi = await self._get(client, "/fapi/v1/openInterest", {"symbol": symbol})
                        state = self.states[symbol]
                        if isinstance(premium, dict):
                            if premium.get("lastFundingRate") is not None:
                                new_rate = float(premium["lastFundingRate"])
                                if state.funding_rate is not None and new_rate != state.funding_rate:
                                    state.previous_funding_rate = state.funding_rate
                                state.funding_rate = new_rate
                            if premium.get("markPrice"):
                                state.mark_price = float(premium["markPrice"])
                            if premium.get("indexPrice"):
                                state.index_price = float(premium["indexPrice"])
                        if isinstance(oi, dict) and oi.get("openInterest") is not None:
                            new_oi = float(oi["openInterest"])
                            if state.open_interest is not None and new_oi != state.open_interest:
                                state.previous_open_interest = state.open_interest
                            state.open_interest = new_oi
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.info("derivatives poll failed: %s", exc)
            pause = 900 if self.geo_blocked else 30
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=pause)
                return
            except asyncio.TimeoutError:
                continue

    async def _load_rules(self, client: httpx.AsyncClient) -> None:
        path = "/fapi/v1/exchangeInfo" if self._futures() else "/api/v3/exchangeInfo"
        payload = await self._get(client, path, {})
        if isinstance(payload, dict):
            self.rules = parse_exchange_filters(payload, set(self.settings.symbol_list))

    async def _prepare_market_rest(self, client: httpx.AsyncClient) -> None:
        if self._futures() or not self.settings.symbol_list:
            return
        bases: list[str] = []
        for base in (self.settings.binance_spot_rest_url.rstrip("/"), *_SPOT_REST_CANDIDATES):
            if base and base not in bases:
                bases.append(base)
        modes: list[str] = []
        if self.settings.binance_api_key:
            modes.append("keyed")
        if self.settings.binance_api_key and self.settings.binance_api_secret:
            modes.append("signed")
        modes.append("plain")
        symbol = self.settings.symbol_list[0]
        for base in bases:
            for mode in modes:
                self._rest_mode = mode
                self._rest_base_override = base
                rows = await self._get(
                    client,
                    "/api/v3/klines",
                    {"symbol": symbol, "interval": "1m", "limit": 2},
                    probe=True,
                )
                if isinstance(rows, list) and rows:
                    log.info("market history host %s mode %s", base, mode)
                    return
        self._rest_mode = "plain"
        self._rest_base_override = None
        self._market_rest_blocked = True
        log.warning("market rest blocked; loading candle history through the websocket api")

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict, *, probe: bool = False):
        if not self.bucket.take():
            await asyncio.sleep(self.bucket.retry_after_s())
            if not self.bucket.take():
                return None
        query = dict(params)
        if self._rest_mode == "signed" and self.settings.binance_api_secret:
            query = _signed_params(
                self.settings.binance_api_secret,
                {**query, "timestamp": int(time.time() * 1000), "recvWindow": 5000},
            )
        response = await client.get(
            f"{self.rest_base()}{path}",
            params=query,
            headers=_market_headers(self.settings.binance_api_key, self._rest_mode),
        )
        if response.status_code == 451:
            if not self.geo_blocked:
                log.warning(
                    "Binance HTTP 451 on %s. This server region is blocked for futures REST. Funding and open interest stay empty until the service moves to Frankfurt or Singapore.",
                    path,
                )
                self.geo_blocked = True
            return None
        if response.status_code in {418, 429}:
            if not probe:
                self.breaker.record_failure()
            if response.status_code == 418 and not self._warned_418:
                self._warned_418 = True
                log.warning("Binance HTTP 418 on %s", path)
            return None
        if response.status_code >= 400:
            log.info("binance %s %s -> %s", path, params.get("symbol", ""), response.status_code)
            return None
        return response.json()

    async def _load_ws_history(self) -> None:
        try:
            async with websockets.connect(
                _SPOT_WS_API,
                ping_interval=20,
                open_timeout=15,
                max_queue=32,
            ) as socket:
                await self._fill_from_ws_api(socket)
        except Exception as exc:
            log.warning("websocket history failed: %s", type(exc).__name__)

    async def _fill_from_ws_api(self, socket) -> None:
        loaded = 0
        for symbol in self.settings.symbol_list:
            if symbol not in self.states:
                continue
            for interval, limit in (("1m", 300), ("5m", 200), ("15m", 200)):
                payload = await self._ws_call(socket, "klines", {"symbol": symbol, "interval": interval, "limit": limit})
                rows = payload.get("result") if isinstance(payload, dict) else None
                if not isinstance(rows, list):
                    continue
                for candle in parse_rest_klines(symbol, interval, rows):
                    self.states[symbol].upsert_candle(candle)
                    loaded += 1
            depth = await self._ws_call(socket, "depth", {"symbol": symbol, "limit": 20})
            book = depth.get("result") if isinstance(depth, dict) else None
            if isinstance(book, dict):
                self._apply_rest_depth(symbol, book)
            ticker = await self._ws_call(socket, "ticker.book", {"symbol": symbol})
            quote = ticker.get("result") if isinstance(ticker, dict) else None
            if isinstance(quote, dict):
                self._apply_rest_book(symbol, quote)
            if self.states[symbol].candles.get("1m") and self.on_trigger is not None:
                log.info("bootstrap snapshot %s", symbol)
                await self.on_trigger(symbol, datetime.now(timezone.utc), "bootstrap")
        log.info("market history via websocket api candles=%s", loaded)

    async def _ws_call(self, socket, method: str, params: dict) -> dict | None:
        self._ws_id += 1
        request_id = str(self._ws_id)
        await socket.send(json.dumps({"id": request_id, "method": method, "params": params}))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(socket.recv(), timeout=max(0.1, deadline - time.monotonic()))
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or str(payload.get("id")) != request_id:
                continue
            if payload.get("status") != 200:
                log.info("binance ws %s %s -> %s", method, params.get("symbol", ""), payload.get("status"))
                return None
            return payload
        return None

    def _kline_path(self) -> str:
        return "/fapi/v1/klines" if self._futures() else "/api/v3/klines"

    def _depth_path(self) -> str:
        return "/fapi/v1/depth" if self._futures() else "/api/v3/depth"

    def _book_path(self) -> str:
        return "/fapi/v1/ticker/bookTicker" if self._futures() else "/api/v3/ticker/bookTicker"

    def _apply_rest_depth(self, symbol: str, payload: dict) -> None:
        from app.market.parse import _levels

        bids = _levels(payload.get("bids"))
        asks = _levels(payload.get("asks"))
        if not bids or not asks:
            return
        now = datetime.now(timezone.utc)
        from app.market.state import OrderBook

        state = self.states[symbol]
        state.book = OrderBook(bids=bids, asks=asks, timestamp=now)
        state.best_bid = bids[0].price
        state.best_ask = asks[0].price
        state.last_book_at = now
        state.last_price = (bids[0].price + asks[0].price) / 2

    def _apply_rest_book(self, symbol: str, payload: dict) -> None:
        try:
            bid = float(payload["bidPrice"])
            ask = float(payload["askPrice"])
        except (KeyError, TypeError, ValueError):
            return
        if bid <= 0 or ask < bid:
            return
        state = self.states[symbol]
        now = datetime.now(timezone.utc)
        state.best_bid = bid
        state.best_ask = ask
        state.last_book_at = now
        state.last_price = (bid + ask) / 2
