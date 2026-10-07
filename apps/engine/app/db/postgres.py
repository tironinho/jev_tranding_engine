from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.models import (
    AuditLogRow,
    Base,
    ConsensusRow,
    EngineEventRow,
    FeatureSnapshotRow,
    FutureLabelRow,
    JevCallRow,
    MarketSnapshotRow,
    OpenAICallRow,
    OrderRow,
    PaperAccountRow,
    RiskDecisionRow,
    StrategyConfigRow,
    StrategyDecisionRow,
    TradeRow,
)

log = logging.getLogger(__name__)


def to_async_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url[len("postgres://") :]
    if url.startswith("postgresql+psycopg://"):
        return "postgresql+asyncpg://" + url[len("postgresql+psycopg://") :]
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def to_sync_url(url: str) -> str:
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "+psycopg")
    if url.startswith("postgres://"):
        return "postgresql+psycopg://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


class PostgresMirror:
    """Best-effort durable copy. Reads stay on the in-memory store for the live API."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.healthy = False
        self.last_error: str | None = None
        self.factory: async_sessionmaker[AsyncSession] | None = None

    async def connect(self) -> bool:
        try:
            engine = create_async_engine(to_async_url(self.url), pool_pre_ping=True)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            self.factory = async_sessionmaker(engine, expire_on_commit=False)
            self.healthy = True
            self.last_error = None
            return True
        except Exception as exc:
            self.healthy = False
            self.last_error = str(exc)
            log.warning("postgres unavailable: %s", exc)
            return False

    async def _write(self, row) -> None:
        if not self.factory or not self.healthy:
            return
        try:
            async with self.factory() as session:
                session.add(row)
                await session.commit()
        except Exception as exc:
            self.healthy = False
            self.last_error = str(exc)
            log.warning("postgres write failed: %s", exc)

    async def save_snapshot(self, payload: dict) -> None:
        snapshot_id = uuid.UUID(payload["snapshot_id"])
        await self._write(
            MarketSnapshotRow(
                id=snapshot_id,
                symbol=payload["symbol"],
                market_type=payload["market_type"],
                timestamp=_dt(payload["timestamp"]),
                price=payload["price"],
                payload=payload,
            )
        )
        await self._write(FeatureSnapshotRow(snapshot_id=snapshot_id, payload=payload.get("features") or {}))

    async def save_decision(self, payload: dict) -> None:
        await self._write(
            StrategyDecisionRow(
                id=uuid.UUID(payload["decision_id"]),
                correlation_id=uuid.UUID(payload["correlation_id"]),
                opportunity_id=uuid.UUID(payload["opportunity_id"]),
                snapshot_id=uuid.UUID(payload["snapshot_id"]),
                strategy=payload["strategy"],
                symbol=payload["symbol"],
                timestamp=_dt(payload["timestamp"]),
                action=payload["action"],
                signal_status=payload["signal_status"],
                payload=payload,
            )
        )

    async def save_risk(self, payload: dict) -> None:
        await self._write(
            RiskDecisionRow(
                id=uuid.UUID(payload["risk_id"]),
                decision_id=uuid.UUID(payload["decision_id"]),
                correlation_id=uuid.UUID(payload["correlation_id"]),
                accepted=payload["accepted"],
                payload=payload,
            )
        )

    async def save_order(self, payload: dict) -> None:
        await self._write(
            OrderRow(
                id=uuid.UUID(payload["order_id"]),
                client_order_id=payload["client_order_id"],
                decision_id=uuid.UUID(payload["decision_id"]),
                strategy=payload["strategy"],
                symbol=payload["symbol"],
                status=payload["status"],
                mode=payload["mode"],
                payload=payload,
            )
        )

    async def save_trade(self, payload: dict) -> None:
        await self._write(
            TradeRow(
                id=uuid.UUID(payload["trade_id"]),
                strategy=payload["strategy"],
                symbol=payload["symbol"],
                side=payload["side"],
                net_pnl=payload["net_pnl"],
                gross_pnl=payload["gross_pnl"],
                closed_at=_dt(payload["closed_at"]),
                quantitative_regime=payload.get("quantitative_regime"),
                payload=payload,
            )
        )

    async def save_consensus(self, payload: dict) -> None:
        await self._write(
            ConsensusRow(
                opportunity_id=uuid.UUID(payload["opportunity_id"]),
                label=payload["label"],
                payload=payload,
            )
        )

    async def save_audit(self, payload: dict) -> None:
        await self._write(
            AuditLogRow(
                id=uuid.UUID(payload["audit_id"]),
                timestamp=_dt(payload["timestamp"]),
                actor=payload["actor"],
                action=payload["action"],
                payload=payload,
            )
        )

    async def save_event(self, kind: str, message: str, payload: dict) -> None:
        await self._write(
            EngineEventRow(
                kind=kind,
                message=message,
                timestamp=datetime.now(timezone.utc),
                payload=payload,
            )
        )

    async def save_label(self, snapshot_id: str, payload: dict) -> None:
        await self._write(FutureLabelRow(snapshot_id=uuid.UUID(snapshot_id), payload=payload))

    async def save_openai(self, decision_id: str, snapshot_id: str, artifact: dict) -> None:
        await self._write(
            OpenAICallRow(
                decision_id=uuid.UUID(decision_id),
                snapshot_id=uuid.UUID(snapshot_id),
                request_id=artifact.get("request_id"),
                prompt_version=artifact.get("prompt_version"),
                model=artifact.get("model"),
                latency_ms=artifact.get("latency_ms"),
                estimated_cost=artifact.get("estimated_cost"),
                error=artifact.get("error"),
                payload=artifact,
            )
        )

    async def save_jev(self, decision_id: str, snapshot_id: str, artifact: dict) -> None:
        await self._write(
            JevCallRow(
                decision_id=uuid.UUID(decision_id),
                snapshot_id=uuid.UUID(snapshot_id),
                provider_version=artifact.get("provider_version"),
                prompt_version=(artifact.get("response") or {}).get("prompt_version"),
                is_mock=bool(artifact.get("is_mock", True)),
                latency_ms=artifact.get("latency_ms"),
                error=artifact.get("error"),
                payload=artifact,
            )
        )

    async def save_strategy_config(self, key: str, enabled: bool, mode: str, config: dict) -> None:
        if not self.factory or not self.healthy:
            return
        try:
            async with self.factory() as session:
                result = await session.execute(select(StrategyConfigRow).where(StrategyConfigRow.strategy_key == key))
                row = result.scalar_one_or_none()
                if row is None:
                    session.add(
                        StrategyConfigRow(
                            strategy_key=key,
                            enabled=enabled,
                            mode=mode,
                            config=config,
                            updated_at=datetime.now(timezone.utc),
                        )
                    )
                else:
                    row.enabled = enabled
                    row.mode = mode
                    row.config = config
                    row.updated_at = datetime.now(timezone.utc)
                await session.commit()
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("strategy config persist failed: %s", exc)


def _dt(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
