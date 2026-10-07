from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

import httpx
import websockets

from app.config import Settings
from app.market.parse import apply_market_message, parse_exchange_filters, parse_rest_klines, stream_symbol
from app.resilience.guards import CircuitBreaker, TokenBucket

log = logging.getLogger(__name__)


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

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict):
        if not self.bucket.take():
            await asyncio.sleep(self.bucket.retry_after_s())
            if not self.bucket.take():
                return None
        response = await client.get(f"{self.rest_base()}{path}", params=params)
        if response.status_code == 451:
            if not self.geo_blocked:
                log.warning(
                    "Binance HTTP 451 on %s. This server region is blocked for futures REST. Funding and open interest stay empty until the service moves to Frankfurt or Singapore.",
                    path,
                )
                self.geo_blocked = True
            return None
        if response.status_code in {418, 429}:
            self.breaker.record_failure()
            return None
        if response.status_code >= 400:
            log.info("binance %s %s -> %s", path, params.get("symbol", ""), response.status_code)
            return None
        return response.json()

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
