from __future__ import annotations

import logging
import ssl
import uuid
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.intelligence.models  # noqa: F401  registers intelligence tables on Base.metadata

from app.db.models import (
    AccountSampleRow,
    AuditLogRow,
    Base,
    ConsensusRow,
    EngineEventRow,
    FeatureSnapshotRow,
    FillRow,
    FutureLabelRow,
    JevCallRow,
    MarketSnapshotRow,
    OpenAICallRow,
    OrderRow,
    PaperAccountRow,
    PaperBalanceRow,
    PositionRow,
    RiskDecisionRow,
    StrategyConfigRow,
    StrategyDecisionRow,
    TradeRow,
)

log = logging.getLogger(__name__)

_AUDIT_HORIZONS = {
    "future_return_30s": 30,
    "future_return_1m": 60,
    "future_return_3m": 180,
    "future_return_5m": 300,
    "future_return_15m": 900,
}


def _derive_audit_labels(snapshot: MarketSnapshotRow, series: list[MarketSnapshotRow]) -> dict | None:
    """Rebuild missing point-in-time labels from durable market snapshots."""
    if not series or snapshot.price <= 0:
        return None
    timestamps = [row.timestamp for row in series]
    labels: dict[str, float | str] = {"snapshot_id": str(snapshot.id)}
    for name, seconds in _AUDIT_HORIZONS.items():
        index = bisect_left(timestamps, snapshot.timestamp + timedelta(seconds=seconds))
        if index >= len(series):
            continue
        price = series[index].price
        if price and price > 0:
            labels[name] = price / snapshot.price - 1
    return labels if len(labels) > 1 else None


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


def prepare_asyncpg(url: str) -> tuple[str, dict]:
    """asyncpg rejects libpq parameters such as sslmode and channel_binding."""
    rewritten = to_async_url(url)
    parts = urlsplit(rewritten)
    query = parse_qsl(parts.query, keep_blank_values=True)
    ssl_on = False
    kept: list[tuple[str, str]] = []
    for key, value in query:
        if key in {"sslmode", "ssl"}:
            if value.lower() not in {"disable", "false", "allow"}:
                ssl_on = True
            continue
        if key == "channel_binding":
            continue
        kept.append((key, value))
    cleaned = urlunsplit(parts._replace(query=urlencode(kept)))
    connect_args: dict = {}
    if ssl_on:
        connect_args["ssl"] = ssl.create_default_context()
    host = parts.hostname or ""
    if "-pooler." in host:
        connect_args["statement_cache_size"] = 0
    return cleaned, connect_args


class PostgresMirror:
    """Best-effort durable copy. Reads stay on the in-memory store for the live API."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.healthy = False
        self.last_error: str | None = None
        self.factory: async_sessionmaker[AsyncSession] | None = None

    async def connect(self) -> bool:
        try:
            async_url, connect_args = prepare_asyncpg(self.url)
            engine = create_async_engine(async_url, pool_pre_ping=True, connect_args=connect_args)
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
            self.last_error = str(exc)
            self.healthy = False
            log.warning("postgres write failed: %s", exc)

    async def inspector(self, decision_id: str) -> dict | None:
        if not self.factory or not self.healthy:
            return None
        try:
            identity = uuid.UUID(decision_id)
        except ValueError:
            return None
        async with self.factory() as session:
            decision = await session.get(StrategyDecisionRow, identity)
            if decision is None:
                return None
            snapshot = await session.get(MarketSnapshotRow, decision.snapshot_id)
            async def rows(model, condition):
                return list((await session.execute(select(model).where(condition))).scalars().all())
            risks = await rows(RiskDecisionRow, RiskDecisionRow.decision_id == identity)
            orders = await rows(OrderRow, OrderRow.decision_id == identity)
            fills = await rows(FillRow, FillRow.decision_id == identity)
            artifacts = await rows(JevCallRow, JevCallRow.decision_id == identity)
            artifacts += await rows(OpenAICallRow, OpenAICallRow.decision_id == identity)
            consensus = await rows(ConsensusRow, ConsensusRow.opportunity_id == decision.opportunity_id)
            labels = await rows(FutureLabelRow, FutureLabelRow.snapshot_id == decision.snapshot_id)
            trades = await rows(TradeRow, TradeRow.payload["decision_id"].astext == decision_id)
            return {"decision": decision.payload, "snapshot": snapshot.payload if snapshot else None,
                    "features": snapshot.payload.get("features") if snapshot else None,
                    "artifacts": [row.payload for row in artifacts],
                    "risk": risks[-1].payload if risks else None,
                    "orders": [row.payload for row in orders], "fills": [row.payload for row in fills],
                    "trade": trades[-1].payload if trades else None,
                    "trades": [row.payload for row in trades],
                    "consensus": consensus[0].payload if consensus else None,
                    "labels": labels[-1].payload if labels else None, "source": "postgres"}

    async def recent_jev_calls(self, limit: int = 80) -> list[dict]:
        """Load recent Jev-strategy decisions, including ones that did not call Jev."""
        if not self.factory or not self.healthy:
            return []
        try:
            async with self.factory() as session:
                decisions = list((await session.execute(
                    select(StrategyDecisionRow)
                    .where(StrategyDecisionRow.strategy == "baseline_jev")
                    .order_by(StrategyDecisionRow.timestamp.desc())
                    .limit(max(1, min(limit, 240)))
                )).scalars().all())
                if not decisions:
                    return []
                identities = [row.id for row in decisions]
                calls = list((await session.execute(
                    select(JevCallRow).where(JevCallRow.decision_id.in_(identities))
                )).scalars().all())
                by_decision: dict[uuid.UUID, list[JevCallRow]] = defaultdict(list)
                for call in calls:
                    if call.decision_id is not None:
                        by_decision[call.decision_id].append(call)
                rows = []
                for decision in decisions:
                    decision_calls = by_decision.get(decision.id) or [None]
                    for call in decision_calls:
                        payload = {} if call is None else (call.payload or {})
                        rows.append({
                        "call": {
                            "decision_id": str(decision.id),
                            "snapshot_id": str(decision.snapshot_id),
                            **payload,
                        },
                        "decision": decision.payload or {},
                        })
                return rows
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("jev history load failed: %s", exc)
            return []

    async def recent_decision_audit(self, limit: int = 100, strategy: str = "baseline") -> list[dict]:
        """Return durable decisions with their recorded market snapshot and future label."""
        if not self.factory or not self.healthy:
            return []
        bounded = max(1, min(limit, 500))
        try:
            async with self.factory() as session:
                decisions = list(
                    (
                        await session.execute(
                            select(StrategyDecisionRow)
                            .where(StrategyDecisionRow.strategy == strategy)
                            .order_by(StrategyDecisionRow.timestamp.desc())
                            .limit(bounded)
                        )
                    ).scalars().all()
                )
                if not decisions:
                    return []
                snapshot_ids = [row.snapshot_id for row in decisions]
                snapshots = list(
                    (
                        await session.execute(
                            select(MarketSnapshotRow).where(MarketSnapshotRow.id.in_(snapshot_ids))
                        )
                    ).scalars().all()
                )
                labels = list(
                    (
                        await session.execute(
                            select(FutureLabelRow)
                            .where(FutureLabelRow.snapshot_id.in_(snapshot_ids))
                            .order_by(FutureLabelRow.snapshot_id, FutureLabelRow.id)
                        )
                    ).scalars().all()
                )
                snapshot_rows = {row.id: row for row in snapshots}
                symbols = sorted({row.symbol for row in snapshots})
                first_at = min(row.timestamp for row in snapshots)
                last_at = max(row.timestamp for row in snapshots) + timedelta(seconds=900)
                future_snapshots = list(
                    (
                        await session.execute(
                            select(MarketSnapshotRow)
                            .where(
                                MarketSnapshotRow.symbol.in_(symbols),
                                MarketSnapshotRow.timestamp >= first_at,
                                MarketSnapshotRow.timestamp <= last_at,
                            )
                            .order_by(MarketSnapshotRow.symbol, MarketSnapshotRow.timestamp)
                        )
                    ).scalars().all()
                )
                series_by_symbol: dict[str, list[MarketSnapshotRow]] = defaultdict(list)
                for market_row in future_snapshots:
                    series_by_symbol[market_row.symbol].append(market_row)
                snapshot_by_id = {row.id: row.payload for row in snapshots}
                label_by_snapshot = {row.snapshot_id: row.payload for row in labels}
                audited = []
                for row in decisions:
                    label = label_by_snapshot.get(row.snapshot_id)
                    source = "future_labels" if label is not None else None
                    snapshot_row = snapshot_rows.get(row.snapshot_id)
                    if label is None and snapshot_row is not None:
                        label = _derive_audit_labels(snapshot_row, series_by_symbol[snapshot_row.symbol])
                        source = "market_snapshots" if label is not None else None
                    audited.append({
                        "decision": row.payload,
                        "snapshot": snapshot_by_id.get(row.snapshot_id),
                        "labels": label,
                        "labels_source": source,
                    })
                return audited
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("decision audit history load failed: %s", exc)
            raise

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

    async def save_account_sample(self, sample: dict) -> None:
        equity = sample.get("equity")
        if not isinstance(equity, (int, float)):
            return
        wallet = sample.get("wallet")
        available = sample.get("available")
        level = sample.get("margin_level")
        await self._write(
            AccountSampleRow(
                timestamp=datetime.now(timezone.utc),
                equity=float(equity),
                wallet=float(wallet) if isinstance(wallet, (int, float)) else None,
                available=float(available) if isinstance(available, (int, float)) else None,
                margin_level=float(level) if isinstance(level, (int, float)) else None,
                payload=sample,
            )
        )

    async def load_balance_points(self) -> list[dict]:
        if not self.factory or not self.healthy:
            return []
        try:
            async with self.factory() as session:
                samples = (
                    await session.execute(
                        select(AccountSampleRow).order_by(AccountSampleRow.timestamp.desc()).limit(2000)
                    )
                ).scalars().all()
                events = (
                    await session.execute(
                        select(EngineEventRow)
                        .where(EngineEventRow.kind == "balance")
                        .order_by(EngineEventRow.timestamp.desc())
                        .limit(2000)
                    )
                ).scalars().all()
            points = []
            seen: set[str] = set()
            for row in samples:
                stamp = row.timestamp.isoformat()
                seen.add(stamp)
                points.append({"t": stamp, "wallet": float(row.equity)})
            for row in events:
                stamp = row.timestamp.isoformat()
                if stamp in seen:
                    continue
                equity = (row.payload or {}).get("equity")
                if isinstance(equity, (int, float)):
                    points.append({"t": stamp, "wallet": float(equity)})
            points.sort(key=lambda point: point["t"])
            return points[-2000:]
        except Exception as exc:
            self.last_error = str(exc)
            log.warning("balance history load failed: %s", exc)
            return []

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

    async def save_checkpoint(
        self,
        strategy: str,
        account: dict,
        positions: list[dict],
        orders: list[dict],
        fills: list[dict],
        trade: dict | None = None,
    ) -> None:
        """One commit for the book. The account payload is what boot reloads."""
        if not self.factory or not self.healthy:
            return
        try:
            async with self.factory() as session:
                await _upsert_account(session, strategy, account)
                for position in positions:
                    await _upsert_position(session, position)
                stored_ids: dict[str, str] = {}
                with session.no_autoflush:
                    for order in orders:
                        stored = await _upsert_order(session, order)
                        stored_ids[str(order["order_id"])] = str(stored)
                await session.flush()
                for fill in fills:
                    await _upsert_fill(session, _bind_fill_order(fill, stored_ids))
                if trade is not None:
                    await _upsert_trade(session, trade)
                session.add(
                    PaperBalanceRow(
                        strategy=strategy,
                        equity=float(account.get("equity") or 0),
                        timestamp=datetime.now(timezone.utc),
                        payload={"cash": account.get("cash"), "equity": account.get("equity")},
                    )
                )
                await session.commit()
        except Exception as exc:
            self.last_error = str(exc)
            self.healthy = False
            log.warning("postgres checkpoint failed: %s", exc)

    async def save_risk_limits(self, payload: dict) -> None:
        await self._write(
            EngineEventRow(
                kind="risk_limits",
                message="risk limits updated",
                timestamp=datetime.now(timezone.utc),
                payload=payload,
            )
        )

    async def load_runtime(self) -> dict:
        empty = {"accounts": {}, "strategies": [], "risk": None, "orphan_trades": {}}
        if not self.factory or not self.healthy:
            return empty
        try:
            async with self.factory() as session:
                accounts = (await session.execute(select(PaperAccountRow))).scalars().all()
                configs = (await session.execute(select(StrategyConfigRow))).scalars().all()
                risk = (
                    await session.execute(
                        select(EngineEventRow)
                        .where(EngineEventRow.kind == "risk_limits")
                        .order_by(EngineEventRow.timestamp.desc())
                        .limit(1)
                    )
                ).scalars().first()
                trades = (await session.execute(select(TradeRow))).scalars().all()
                entry_events = (await session.execute(select(EngineEventRow).where(
                    EngineEventRow.kind.in_(["live_entry_intent", "live_entry_resolved"])
                ).order_by(EngineEventRow.timestamp))).scalars().all()
            pending_entries = {}
            for event in entry_events:
                if event.kind == "live_entry_intent":
                    pending_entries[event.message] = event.payload
                else:
                    pending_entries.pop(event.message, None)
            saved = {row.strategy: row.payload for row in accounts}
            grouped: dict[str, list] = {}
            for row in trades:
                grouped.setdefault(row.strategy, []).append(row.payload)
            orphans = {key: value for key, value in grouped.items() if key not in saved}
            return {
                "accounts": saved,
                "pending_entries": pending_entries,
                "trades": grouped,
                "strategies": [
                    {
                        "strategy_key": row.strategy_key,
                        "enabled": row.enabled,
                        "mode": row.mode,
                        "config": row.config or {},
                    }
                    for row in configs
                ],
                "risk": None if risk is None else risk.payload,
                "orphan_trades": orphans,
            }
        except Exception as exc:
            self.healthy = False
            self.last_error = str(exc)
            log.warning("postgres load failed: %s", exc)
            return empty

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


async def _upsert_account(session: AsyncSession, strategy: str, account: dict) -> None:
    result = await session.execute(select(PaperAccountRow).where(PaperAccountRow.strategy == strategy))
    row = result.scalar_one_or_none()
    cash = float(account.get("cash") or 0)
    day_start = float(account.get("day_start_equity") or 0)
    if row is None:
        session.add(PaperAccountRow(strategy=strategy, cash=cash, day_start_equity=day_start, payload=account))
    else:
        row.cash = cash
        row.day_start_equity = day_start
        row.payload = account


async def _upsert_position(session: AsyncSession, position: dict) -> None:
    position_id = uuid.UUID(str(position["position_id"]))
    result = await session.execute(select(PositionRow).where(PositionRow.id == position_id))
    row = result.scalar_one_or_none()
    status = str(position.get("status") or "OPEN")
    if row is None:
        session.add(
            PositionRow(
                id=position_id,
                strategy=position["strategy"],
                symbol=position["symbol"],
                status=status,
                payload=position,
            )
        )
    else:
        row.status = status
        row.payload = position


async def _upsert_order(session: AsyncSession, order: dict) -> uuid.UUID:
    result = await session.execute(select(OrderRow).where(OrderRow.client_order_id == order["client_order_id"]))
    row = result.scalar_one_or_none()
    if row is None:
        stored = uuid.UUID(str(order["order_id"]))
        session.add(
            OrderRow(
                id=stored,
                client_order_id=order["client_order_id"],
                decision_id=uuid.UUID(str(order["decision_id"])),
                strategy=order["strategy"],
                symbol=order["symbol"],
                status=order["status"],
                mode=order["mode"],
                payload=order,
            )
        )
        return stored
    row.status = order["status"]
    row.payload = order
    return row.id


def _bind_fill_order(fill: dict, stored_ids: dict[str, str]) -> dict:
    """A restarted exit keeps the client order id and mints a new UUID.

    The fill has to reference the row that already exists, or the insert
    rolls the whole book back and the trade count never sticks.
    """
    incoming = str(fill.get("order_id") or "")
    stored = stored_ids.get(incoming)
    if not stored or stored == incoming:
        return fill
    return {**fill, "order_id": stored}


async def _upsert_fill(session: AsyncSession, fill: dict) -> None:
    fill_id = uuid.UUID(str(fill["fill_id"]))
    result = await session.execute(select(FillRow).where(FillRow.id == fill_id))
    if result.scalar_one_or_none() is not None:
        return
    session.add(
        FillRow(
            id=fill_id,
            order_id=uuid.UUID(str(fill["order_id"])),
            decision_id=uuid.UUID(str(fill["decision_id"])),
            price=float(fill["price"]),
            quantity=float(fill["quantity"]),
            fee=float(fill["fee"]),
            payload=fill,
        )
    )


async def _upsert_trade(session: AsyncSession, trade: dict) -> None:
    trade_id = uuid.UUID(str(trade["trade_id"]))
    result = await session.execute(select(TradeRow).where(TradeRow.id == trade_id))
    if result.scalar_one_or_none() is not None:
        return
    session.add(
        TradeRow(
            id=trade_id,
            strategy=trade["strategy"],
            symbol=trade["symbol"],
            side=trade["side"],
            net_pnl=float(trade["net_pnl"]),
            gross_pnl=float(trade["gross_pnl"]),
            closed_at=_dt(trade["closed_at"]),
            quantitative_regime=trade.get("quantitative_regime"),
            payload=trade,
        )
    )
