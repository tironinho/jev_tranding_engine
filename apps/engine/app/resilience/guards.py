from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """Conservative request budget. Callers must not loop retries around this."""

    rate: float
    capacity: float
    tokens: float = field(init=False)
    updated: float = field(init=False)

    def __post_init__(self) -> None:
        self.tokens = self.capacity
        self.updated = time.monotonic()

    def take(self, amount: float = 1.0, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        elapsed = max(0.0, current - self.updated)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
        self.updated = current
        if self.tokens < amount:
            return False
        self.tokens -= amount
        return True

    def retry_after_s(self, amount: float = 1.0) -> float:
        if self.tokens >= amount:
            return 0.0
        missing = amount - self.tokens
        if self.rate <= 0:
            return 60.0
        return missing / self.rate


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = 5
    recovery_seconds: float = 30.0
    failures: int = 0
    opened_at: float | None = None
    state: str = "closed"

    def allow(self, now: float | None = None) -> bool:
        current = time.monotonic() if now is None else now
        if self.state == "open":
            if self.opened_at is not None and current - self.opened_at >= self.recovery_seconds:
                self.state = "half_open"
                return True
            return False
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.state = "closed"
        self.opened_at = None

    def record_failure(self, now: float | None = None) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold or self.state == "half_open":
            self.state = "open"
            self.opened_at = time.monotonic() if now is None else now
