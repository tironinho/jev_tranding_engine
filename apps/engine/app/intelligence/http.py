from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from app.resilience.guards import CircuitBreaker, TokenBucket

log = logging.getLogger(__name__)


@dataclass
class FetchResult:
    status: str
    body: Any = None
    code: int | None = None
    latency_ms: float | None = None
    retry_after_s: float | None = None


async def fetch_json(client, breaker: CircuitBreaker, bucket: TokenBucket, url: str, params: dict, headers: dict) -> FetchResult:
    """GET JSON. The API key stays in the header and is never logged."""
    path = url.split("?", 1)[0]
    if not breaker.allow():
        log.info("provider=%s path=%s status=OPEN", breaker.name, path)
        return FetchResult(status="OPEN")
    if not bucket.take():
        log.info("provider=%s path=%s status=THROTTLED", breaker.name, path)
        return FetchResult(status="THROTTLED")
    started = time.perf_counter()
    try:
        response = await client.get(url, params=params, headers=headers, timeout=8)
    except Exception as exc:
        breaker.record_failure()
        latency = (time.perf_counter() - started) * 1000
        log.info("provider=%s path=%s status=ERROR latency_ms=%.0f detail=%s", breaker.name, path, latency, type(exc).__name__)
        return FetchResult(status="ERROR", latency_ms=latency, code=None)
    latency = (time.perf_counter() - started) * 1000
    code = response.status_code
    if code == 429:
        breaker.record_failure()
        retry = _retry_after(response.headers.get("Retry-After"))
        log.info("provider=%s path=%s status=429 latency_ms=%.0f", breaker.name, path, latency)
        return FetchResult(status="RATE_LIMIT", code=429, latency_ms=latency, retry_after_s=retry)
    if code == 451:
        log.info("provider=%s path=%s status=451 latency_ms=%.0f", breaker.name, path, latency)
        return FetchResult(status="GEO_BLOCKED", code=451, latency_ms=latency)
    if code >= 400:
        breaker.record_failure()
        log.info("provider=%s path=%s status=%s latency_ms=%.0f", breaker.name, path, code, latency)
        return FetchResult(status="ERROR", code=code, latency_ms=latency)
    breaker.record_success()
    try:
        body = response.json()
    except Exception:
        return FetchResult(status="ERROR", code=code, latency_ms=latency)
    log.info("provider=%s path=%s status=200 latency_ms=%.0f", breaker.name, path, latency)
    return FetchResult(status="ok", body=body, code=200, latency_ms=latency)


def _retry_after(header: str | None) -> float:
    if header is None:
        return 60.0
    try:
        return max(1.0, float(header))
    except ValueError:
        return 60.0
