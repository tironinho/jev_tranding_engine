from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, Uuid
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models import Base


def _id() -> uuid.UUID:
    return uuid.uuid4()


class ExternalDataRawRow(Base):
    __tablename__ = "external_data_raw"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    metric: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)


class ExternalDataNormalizedRow(Base):
    __tablename__ = "external_data_normalized"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    raw_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    normalization_version: Mapped[str] = mapped_column(String(64), index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    symbol: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class ProviderHealthRow(Base):
    __tablename__ = "provider_health"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)


class ProviderCapabilityRow(Base):
    __tablename__ = "provider_capabilities"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    metric: Mapped[str] = mapped_column(String(128))
    asset: Mapped[str | None] = mapped_column(String(32), nullable=True)
    available: Mapped[bool] = mapped_column(Boolean)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)


class ProviderSymbolMappingRow(Base):
    __tablename__ = "provider_symbol_mappings"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=_id)
    internal_symbol: Mapped[str] = mapped_column(String(32), index=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    provider_symbol: Mapped[str] = mapped_column(String(64))
    exchange: Mapped[str | None] = mapped_column(String(32), nullable=True)
    market_type: Mapped[str | None] = mapped_column(String(16), nullable=True)


class IntelligenceSnapshotRow(Base):
    __tablename__ = "intelligence_snapshots"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    market_snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    symbol: Mapped[str] = mapped_column(String(32), index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    normalization_version: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
