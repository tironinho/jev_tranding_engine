from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.intelligence.http import fetch_json
from app.intelligence.schemas import ProviderHealth, RawExternalObservation
from app.resilience.guards import CircuitBreaker, TokenBucket

_WANTED = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}


class CoinalyzeProvider:
    """Aggregate derivatives. Symbols come from /v1/future-markets, never from a guessed name."""

    name = "coinalyze"

    def __init__(self, settings: Settings, client=None) -> None:
        self.settings = settings
        self.client = client
        rate = max(self.settings.coinalyze_requests_per_minute, 1) / 60
        self.breaker = CircuitBreaker("coinalyze", failure_threshold=4, recovery_seconds=90)
        self.bucket = TokenBucket(rate=rate, capacity=min(8, self.settings.coinalyze_requests_per_minute))
        self._markets: list[dict] = []
        self._status = "DISABLED"
        self._error: str | None = None

    async def health(self) -> ProviderHealth:
        return ProviderHealth(provider=self.name, status=self._status, last_error=self._error)

    def mappings(self) -> list[dict]:
        return list(self._markets)

    async def collect(self, symbols: list[str], now: datetime) -> list[RawExternalObservation]:
        if not self.settings.coinalyze_enabled or not self.settings.coinalyze_api_key:
            self._status = "DISABLED"
            self._error = None
            return []
        if self.client is None:
            self._status = "OFFLINE"
            self._error = "client not started"
            return []
        headers = {"api_key": self.settings.coinalyze_api_key}
        base = f"https://{self.settings.coinalyze_base_host.strip().removeprefix('https://').strip('/')}/v1"
        if not self._markets:
            listed = await fetch_json(self.client, self.breaker, self.bucket, f"{base}/future-markets", {}, headers)
            if listed.status == "RATE_LIMIT":
                self._status = "DEGRADED"
                self._error = "429"
                return []
            if listed.status != "ok" or not isinstance(listed.body, list):
                self._status = "ERROR" if listed.status == "ERROR" else "DEGRADED"
                self._error = listed.status
                return []
            self._markets = _select_markets(listed.body, symbols)
        chosen = self._markets[:12]
        if not chosen:
            self._status = "DEGRADED"
            self._error = "NO_MARKET_MAPPING"
            return []
        provider_symbols = ",".join(item["provider_symbol"] for item in chosen)
        rows: list[RawExternalObservation] = []
        current = (
            ("open-interest", "aggregate_open_interest_usd", {"symbols": provider_symbols, "convert_to_usd": "true"}),
            ("funding-rate", "funding_current", {"symbols": provider_symbols}),
            ("predicted-funding-rate", "predicted_funding", {"symbols": provider_symbols}),
        )
        for path, metric, params in current:
            result = await fetch_json(self.client, self.breaker, self.bucket, f"{base}/{path}", params, headers)
            if result.status == "RATE_LIMIT":
                self._status = "DEGRADED"
                self._error = "429"
                return rows
            if result.status != "ok":
                continue
            rows.extend(_current_rows(metric, result.body, chosen, now))
        start = int((now - timedelta(hours=2)).timestamp())
        end = int(now.timestamp())
        history_symbols = ",".join(item["provider_symbol"] for item in chosen if item["exchange"] == "A") or provider_symbols
        histories = (
            ("open-interest-history", _oi_history, {"convert_to_usd": "true"}),
            ("liquidation-history", _liquidation_history, {}),
            ("long-short-ratio-history", _ratio_history, {}),
            ("ohlcv-history", _ohlcv_history, {}),
        )
        for path, parser, extra in histories:
            params = {"symbols": history_symbols, "interval": "5min", "from": start, "to": end, **extra}
            result = await fetch_json(self.client, self.breaker, self.bucket, f"{base}/{path}", params, headers)
            if result.status == "RATE_LIMIT":
                self._status = "DEGRADED"
                self._error = "429"
                return rows
            if result.status != "ok":
                continue
            rows.extend(parser(result.body, chosen, now))
        self._status = "ONLINE" if rows else "DEGRADED"
        self._error = None if rows else "EMPTY"
        return rows


def _select_markets(body: list, symbols: list[str]) -> list[dict]:
    assets = {symbol.removesuffix("USDT") for symbol in symbols if symbol.removesuffix("USDT") in _WANTED}
    chosen = []
    for row in body:
        if not isinstance(row, dict) or not row.get("is_perpetual"):
            continue
        if row.get("quote_asset") != "USDT" or row.get("margined") != "STABLE":
            continue
        base = str(row.get("base_asset") or "")
        if base not in assets and base not in _WANTED:
            continue
        provider_symbol = row.get("symbol")
        if not provider_symbol:
            continue
        chosen.append(
            {
                "internal_symbol": _WANTED.get(base, f"{base}USDT"),
                "provider": "coinalyze",
                "provider_symbol": str(provider_symbol),
                "exchange": str(row.get("exchange") or ""),
                "market_type": "perpetual",
                "has_long_short_ratio_data": bool(row.get("has_long_short_ratio_data")),
                "has_buy_sell_data": bool(row.get("has_buy_sell_data")),
            }
        )
    chosen.sort(key=lambda item: (item["internal_symbol"], item["exchange"] != "A", item["exchange"]))
    return chosen


def _current_rows(metric: str, body, markets: list[dict], now: datetime) -> list[RawExternalObservation]:
    if not isinstance(body, list):
        return []
    by_symbol = {item["provider_symbol"]: item for item in markets}
    rows = []
    totals: dict[str, float] = {}
    for item in body:
        if not isinstance(item, dict) or item.get("value") is None:
            continue
        mapped = by_symbol.get(item.get("symbol"))
        if mapped is None:
            continue
        stamp = _seconds(item.get("update")) or now
        value = float(item["value"])
        rows.append(_obs("coinalyze", metric, mapped["internal_symbol"], value, "usd" if "open_interest" in metric else "rate", stamp, now, mapped["provider_symbol"]))
        if metric == "aggregate_open_interest_usd":
            totals[mapped["internal_symbol"]] = totals.get(mapped["internal_symbol"], 0.0) + value
    for symbol, total in totals.items():
        rows.append(_obs("coinalyze", "aggregate_open_interest_usd_sum", symbol, total, "usd", now, now, None))
    return rows


def _oi_history(body, markets, now) -> list[RawExternalObservation]:
    return _bars(body, markets, now, lambda bar, mapped: [
        _obs("coinalyze", "open_interest_usd", mapped["internal_symbol"], float(bar["c"]), "usd", _seconds(bar.get("t")) or now, now, mapped["provider_symbol"])
    ] if bar.get("c") is not None else [])


def _liquidation_history(body, markets, now) -> list[RawExternalObservation]:
    def emit(bar, mapped):
        stamp = _seconds(bar.get("t")) or now
        rows = []
        if bar.get("l") is not None:
            rows.append(_obs("coinalyze", "long_liquidations", mapped["internal_symbol"], float(bar["l"]), "contracts", stamp, now, mapped["provider_symbol"]))
        if bar.get("s") is not None:
            rows.append(_obs("coinalyze", "short_liquidations", mapped["internal_symbol"], float(bar["s"]), "contracts", stamp, now, mapped["provider_symbol"]))
        return rows

    return _bars(body, markets, now, emit)


def _ratio_history(body, markets, now) -> list[RawExternalObservation]:
    def emit(bar, mapped):
        stamp = _seconds(bar.get("t")) or now
        rows = []
        if bar.get("r") is not None:
            rows.append(_obs("coinalyze", "long_short_ratio", mapped["internal_symbol"], float(bar["r"]), "ratio", stamp, now, mapped["provider_symbol"]))
        if bar.get("l") is not None:
            rows.append(_obs("coinalyze", "long_pct", mapped["internal_symbol"], float(bar["l"]), "fraction", stamp, now, mapped["provider_symbol"]))
        if bar.get("s") is not None:
            rows.append(_obs("coinalyze", "short_pct", mapped["internal_symbol"], float(bar["s"]), "fraction", stamp, now, mapped["provider_symbol"]))
        return rows

    return _bars(body, markets, now, emit)


def _ohlcv_history(body, markets, now) -> list[RawExternalObservation]:
    def emit(bar, mapped):
        stamp = _seconds(bar.get("t")) or now
        rows = []
        if bar.get("c") is not None:
            rows.append(_obs("coinalyze", "close", mapped["internal_symbol"], float(bar["c"]), "price", stamp, now, mapped["provider_symbol"]))
        if bar.get("bv") is not None:
            rows.append(_obs("coinalyze", "buy_volume", mapped["internal_symbol"], float(bar["bv"]), "contracts", stamp, now, mapped["provider_symbol"]))
        if bar.get("v") is not None and bar.get("bv") is not None:
            rows.append(_obs("coinalyze", "sell_volume", mapped["internal_symbol"], float(bar["v"]) - float(bar["bv"]), "contracts", stamp, now, mapped["provider_symbol"]))
        return rows

    return _bars(body, markets, now, emit)


def _bars(body, markets, now, emit) -> list[RawExternalObservation]:
    if not isinstance(body, list):
        return []
    by_symbol = {item["provider_symbol"]: item for item in markets}
    rows = []
    for item in body:
        if not isinstance(item, dict):
            continue
        mapped = by_symbol.get(item.get("symbol"))
        history = item.get("history")
        if mapped is None or not isinstance(history, list):
            continue
        for bar in history:
            if isinstance(bar, dict):
                rows.extend(emit(bar, mapped))
    return rows


def _obs(provider, metric, symbol, value, unit, observed, received, provider_symbol) -> RawExternalObservation:
    return RawExternalObservation(
        provider=provider,
        metric=metric,
        symbol=symbol,
        asset=symbol.removesuffix("USDT"),
        provider_symbol=provider_symbol,
        value=value,
        unit=unit,
        observed_at=observed,
        received_at=received,
        provider_timestamp=observed,
        source_frequency_seconds=300,
    )


def _seconds(value) -> datetime | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number <= 0:
        return None
    return datetime.fromtimestamp(number, tz=timezone.utc)
