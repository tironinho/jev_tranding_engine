from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.intelligence.http import fetch_json
from app.intelligence.schemas import ProviderCapability, ProviderHealth, RawExternalObservation
from app.resilience.guards import CircuitBreaker, TokenBucket

# Asked only after /discovery/endpoints lists them. No guessed path.
_FLOW = "/btc/exchange-flows/netflow"
_RESERVE = "/stablecoin/exchange-flows/reserve"
_SUPPLY = "/stablecoin/network-data/supply"


class CryptoQuantProvider:
    """Exchange netflow and stablecoin reserve. A metric is called only if this key's catalog lists it."""

    name = "cryptoquant"

    def __init__(self, settings: Settings, client=None) -> None:
        self.settings = settings
        self.client = client
        self.capabilities: list[ProviderCapability] = []
        self.breaker = CircuitBreaker("cryptoquant", failure_threshold=4, recovery_seconds=120)
        self.bucket = TokenBucket(rate=0.2, capacity=3)
        self._paths: set[str] = set()
        self._status = "DISABLED"
        self._error: str | None = None
        self._metrics_at: datetime | None = None

    async def health(self) -> ProviderHealth:
        if not self.settings.cryptoquant_enabled or not self.settings.cryptoquant_api_key:
            return ProviderHealth(provider=self.name, status="DISABLED")
        return ProviderHealth(provider=self.name, status=self._status, last_error=self._error)

    async def collect(self, symbols: list[str], now: datetime) -> list[RawExternalObservation]:
        if not self.settings.cryptoquant_enabled or not self.settings.cryptoquant_api_key:
            self._status = "DISABLED"
            self._error = None
            return []
        if self.client is None:
            self._status = "OFFLINE"
            self._error = "client not started"
            return []
        moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        if self._metrics_at is not None and moment - self._metrics_at < timedelta(minutes=30) and self._paths:
            return []
        headers = {"Authorization": f"Bearer {self.settings.cryptoquant_api_key}"}
        base = f"https://{self.settings.cryptoquant_base_host.strip().removeprefix('https://').strip('/')}/v1"
        if not self._paths:
            listed = await fetch_json(self.client, self.breaker, self.bucket, f"{base}/discovery/endpoints", {}, headers)
            if listed.status == "RATE_LIMIT":
                self._status = "DEGRADED"
                self._error = "429"
                return []
            if listed.status != "ok":
                self._status = "ERROR" if listed.status == "ERROR" else "DEGRADED"
                self._error = listed.status
                return []
            self._paths = _catalog_paths(listed.body)
            self.capabilities = [
                ProviderCapability(provider=self.name, metric=path, available=True, last_checked_at=moment)
                for path in sorted(self._paths)
            ]
            if not self._paths:
                self._status = "DEGRADED"
                self._error = "EMPTY_CATALOG"
                return []
        rows: list[RawExternalObservation] = []
        if _listed(self._paths, _FLOW):
            body = await self._metric(f"{base}{_FLOW}", {"exchange": "all_exchange", "window": "day", "limit": 30}, headers)
            rows.extend(_series(body, "exchange_netflow", "BTC", "btc", "netflow_total", moment))
        reserve_ok = False
        if _listed(self._paths, _RESERVE):
            body = await self._metric(
                f"{base}{_RESERVE}",
                {"token": "all_token", "exchange": "all_exchange", "window": "day", "limit": 30},
                headers,
            )
            reserve = _series(body, "stablecoin_reserve", "USD", "usd", "reserve", moment)
            rows.extend(reserve)
            reserve_ok = bool(reserve)
        if not reserve_ok and _listed(self._paths, _SUPPLY):
            body = await self._metric(f"{base}{_SUPPLY}", {"token": "all_token", "window": "day", "limit": 30}, headers)
            rows.extend(_series(body, "stablecoin_supply", "USD", "usd", "supply_circulating", moment) or _series(body, "stablecoin_supply", "USD", "usd", "supply_total", moment))
        if rows:
            self._metrics_at = moment
            self._status = "ONLINE"
            self._error = None
        elif not any(_listed(self._paths, path) for path in (_FLOW, _RESERVE, _SUPPLY)):
            self._status = "DEGRADED"
            self._error = "CATALOG_MISSING"
        else:
            self._status = "DEGRADED"
            self._error = self._error or "EMPTY"
        return rows

    async def _metric(self, url: str, params: dict, headers: dict):
        result = await fetch_json(self.client, self.breaker, self.bucket, url, params, headers)
        if result.status == "RATE_LIMIT":
            self._error = "429"
            return None
        if result.status != "ok":
            self._error = result.status
            return None
        body = result.body
        status = body.get("status") if isinstance(body, dict) else None
        code = status.get("code") if isinstance(status, dict) else None
        if code not in (None, 200, "200"):
            self._error = f"HTTP {code}"
            return None
        return body


def _listed(paths: set[str], suffix: str) -> bool:
    return any(suffix in path for path in paths)


def _catalog_paths(body) -> set[str]:
    found: set[str] = set()

    def walk(node) -> None:
        if isinstance(node, str):
            if node.startswith("/") or "cryptoquant.com/" in node:
                found.add(node.split("?", 1)[0])
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
            return
        if isinstance(node, list):
            for value in node:
                walk(value)

    walk(body)
    return found


def _series(body, metric: str, asset: str, unit: str, field: str, now: datetime) -> list[RawExternalObservation]:
    result = body.get("result") if isinstance(body, dict) else None
    data = result.get("data") if isinstance(result, dict) else None
    if not isinstance(data, list):
        return []
    rows = []
    for item in data:
        if not isinstance(item, dict) or item.get(field) is None:
            continue
        stamp = _when(item.get("datetime") or item.get("date"))
        if stamp is None:
            continue
        try:
            value = float(item[field])
        except (TypeError, ValueError):
            continue
        rows.append(
            RawExternalObservation(
                provider="cryptoquant",
                metric=metric,
                symbol=None,
                asset=asset,
                value=value,
                unit=unit,
                observed_at=stamp,
                received_at=now,
                provider_timestamp=stamp,
                source_frequency_seconds=86_400,
            )
        )
    return rows


def _when(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "")
    parsed = None
    if " " in text:
        try:
            parsed = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            parsed = None
    elif "-" in text:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            parsed = None
    elif len(text) >= 8 and text[:8].isdigit():
        try:
            parsed = datetime.strptime(text[:8], "%Y%m%d")
        except ValueError:
            parsed = None
    if parsed is None:
        return None
    return parsed.replace(tzinfo=timezone.utc)


# Community series. Requested only when catalog-v2 marks that asset/metric community (or the paid key is set).
_CM_FLOW = ("FlowInExUSD", "FlowOutExUSD")
_CM_CHAIN = ("SplyExUSD", "CapMVRVCur", "AdrActCnt", "TxCnt", "FeeTotNtv")
_CM_SUPPLY = "SplyCur"
_CM_ASSETS = "btc,eth,xrp,usdt,usdc"
_CM_CHAIN_ASSETS = ("btc", "eth", "xrp")
_CM_PAIRS = {"btc": "BTCUSDT", "eth": "ETHUSDT", "xrp": "XRPUSDT"}
_CM_FIELDS = (
    ("exchange_supply", "SplyExUSD", "usd"),
    ("mvrv", "CapMVRVCur", "ratio"),
    ("active_addresses", "AdrActCnt", "count"),
    ("tx_count", "TxCnt", "count"),
    ("fee_native", "FeeTotNtv", "native"),
)


class CoinMetricsProvider:
    """Community on-chain series. An empty key is enough. Price and reference rate are not requested."""

    name = "coinmetrics"

    def __init__(self, settings: Settings, client=None) -> None:
        self.settings = settings
        self.client = client
        self.capabilities: list[ProviderCapability] = []
        self.breaker = CircuitBreaker("coinmetrics", failure_threshold=4, recovery_seconds=120)
        self.bucket = TokenBucket(rate=0.2, capacity=3)
        self._allowed: dict[str, set[str]] = {}
        self._status = "DISABLED"
        self._error: str | None = None
        self._metrics_at: datetime | None = None

    def _ready(self) -> bool:
        if self.settings.coinmetrics_api_key:
            return bool(self.settings.coinmetrics_enabled)
        return bool(self.settings.coinmetrics_community)

    async def health(self) -> ProviderHealth:
        if not self._ready():
            return ProviderHealth(provider=self.name, status="DISABLED")
        return ProviderHealth(provider=self.name, status=self._status, last_error=self._error)

    async def collect(self, symbols: list[str], now: datetime) -> list[RawExternalObservation]:
        if not self._ready():
            self._status = "DISABLED"
            self._error = None
            return []
        if self.client is None:
            self._status = "OFFLINE"
            self._error = "client not started"
            return []
        moment = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
        if self._metrics_at is not None and moment - self._metrics_at < timedelta(minutes=30) and self._allowed:
            return []
        host = self.settings.coinmetrics_base_host.strip().removeprefix("https://").strip("/")
        base = f"https://{host}/v4"
        paid = bool(self.settings.coinmetrics_api_key)
        auth = {"api_key": self.settings.coinmetrics_api_key} if paid else {}
        if not self._allowed:
            listed = await fetch_json(
                self.client,
                self.breaker,
                self.bucket,
                f"{base}/catalog-v2/asset-metrics",
                {"assets": _CM_ASSETS, "metrics": ",".join((*_CM_FLOW, *_CM_CHAIN, _CM_SUPPLY)), **auth},
                {},
            )
            if listed.status == "RATE_LIMIT":
                self._status = "DEGRADED"
                self._error = "429"
                return []
            if listed.status != "ok":
                self._status = "ERROR" if listed.status == "ERROR" else "DEGRADED"
                self._error = listed.status
                return []
            self._allowed = _community_metrics(listed.body, require_community=not paid)
            self.capabilities = [
                ProviderCapability(provider=self.name, metric=metric, asset=asset.upper(), frequency="1d", available=True, last_checked_at=moment)
                for asset, metrics in sorted(self._allowed.items())
                for metric in sorted(metrics)
            ]
            if not self._allowed:
                self._status = "DEGRADED"
                self._error = "CATALOG_MISSING"
                return []
        start = (moment - timedelta(days=90)).date().isoformat()
        rows: list[RawExternalObservation] = []
        chain_assets = [asset for asset in _CM_CHAIN_ASSETS if self._allowed.get(asset, set()) & set(_CM_FLOW + _CM_CHAIN)]
        chain_metrics = sorted(set().union(*(self._allowed.get(asset, set()) & set(_CM_FLOW + _CM_CHAIN) for asset in chain_assets)))
        if chain_assets and chain_metrics:
            body = await self._series(base, ",".join(chain_assets), chain_metrics, start, auth)
            rows.extend(_chain_points(body, moment))
        stables = sorted(asset for asset, metrics in self._allowed.items() if asset in {"usdt", "usdc"} and _CM_SUPPLY in metrics)
        if stables:
            body = await self._series(base, ",".join(stables), [_CM_SUPPLY], start, auth)
            rows.extend(_stable_supply(body, set(stables), moment))
        if rows:
            self._metrics_at = moment
            self._status = "ONLINE"
            self._error = None
        elif chain_metrics or stables:
            self._status = "DEGRADED"
            self._error = self._error or "EMPTY"
        else:
            self._status = "DEGRADED"
            self._error = "CATALOG_MISSING"
        return rows

    async def _series(self, base: str, assets: str, metrics: list[str], start: str, auth: dict):
        result = await fetch_json(
            self.client,
            self.breaker,
            self.bucket,
            f"{base}/timeseries/asset-metrics",
            {"assets": assets, "metrics": ",".join(metrics), "frequency": "1d", "page_size": 100, "start_time": start, **auth},
            {},
        )
        if result.status == "RATE_LIMIT":
            self._error = "429"
            return None
        if result.status != "ok":
            self._error = result.status
            return None
        return result.body


def _community_metrics(body, require_community: bool) -> dict[str, set[str]]:
    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        return {}
    found: dict[str, set[str]] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        asset = str(item.get("asset") or "").lower()
        metrics = item.get("metrics")
        if not asset or not isinstance(metrics, list):
            continue
        for metric in metrics:
            if not isinstance(metric, dict) or not metric.get("metric"):
                continue
            frequencies = metric.get("frequencies")
            if not isinstance(frequencies, list):
                continue
            usable = [
                freq
                for freq in frequencies
                if isinstance(freq, dict) and freq.get("frequency") == "1d" and (freq.get("community") is True or not require_community)
            ]
            if usable:
                found.setdefault(asset, set()).add(str(metric["metric"]))
    return found


def _chain_points(body, now: datetime) -> list[RawExternalObservation]:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return []
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        asset = str(item.get("asset") or "").lower()
        symbol = _CM_PAIRS.get(asset)
        stamp = _cm_stamp(item.get("time"))
        if symbol is None or stamp is None:
            continue
        inflow = _number(item.get("FlowInExUSD"))
        outflow = _number(item.get("FlowOutExUSD"))
        if inflow is not None and outflow is not None:
            rows.append(_point("exchange_netflow", symbol, asset.upper(), inflow - outflow, "usd", stamp, now))
        for metric, field, unit in _CM_FIELDS:
            value = _number(item.get(field))
            if value is None:
                continue
            rows.append(_point(metric, symbol, asset.upper(), value, unit, stamp, now))
    return rows


def _point(metric: str, symbol: str, asset: str, value: float, unit: str, stamp: datetime, now: datetime) -> RawExternalObservation:
    return RawExternalObservation(
        provider="coinmetrics",
        metric=metric,
        symbol=symbol,
        asset=asset,
        value=value,
        unit=unit,
        observed_at=stamp,
        received_at=now,
        provider_timestamp=stamp,
        source_frequency_seconds=86_400,
    )


def _stable_supply(body, assets: set[str], now: datetime) -> list[RawExternalObservation]:
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or not assets:
        return []
    bucket: dict[datetime, dict[str, float]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        asset = str(item.get("asset") or "").lower()
        value = _number(item.get("SplyCur"))
        stamp = _cm_stamp(item.get("time"))
        if asset not in assets or value is None or stamp is None:
            continue
        bucket.setdefault(stamp, {})[asset] = value
    rows = []
    for stamp, parts in sorted(bucket.items()):
        if set(parts) != assets:
            continue
        rows.append(
            RawExternalObservation(
                provider="coinmetrics",
                metric="stablecoin_supply",
                symbol=None,
                asset="USD",
                value=sum(parts.values()),
                unit="usd",
                observed_at=stamp,
                received_at=now,
                provider_timestamp=stamp,
                source_frequency_seconds=86_400,
            )
        )
    return rows


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _cm_stamp(raw) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    if "." in text:
        head, tail = text.split(".", 1)
        digits = []
        rest = ""
        for index, char in enumerate(tail):
            if char.isdigit():
                digits.append(char)
            else:
                rest = tail[index:]
                break
        text = f"{head}.{''.join(digits[:6])}{rest}" if digits else f"{head}{rest}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
