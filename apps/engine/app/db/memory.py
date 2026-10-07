from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from app.domain.schemas import (
    AuditRecord,
    ConsensusResult,
    MarketSnapshot,
    OrderRecord,
    RiskDecision,
    StrategyDecision,
    TradeRecord,
)


def _dump(model) -> dict:
    return json.loads(model.model_dump_json())


class MemoryStore:
    def __init__(self) -> None:
        self.snapshots: dict[str, dict] = {}
        self.decisions: list[dict] = []
        self.by_decision: dict[str, dict] = {}
        self.risks: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self.orders_by_client: dict[str, dict] = {}
        self.fills: list[dict] = []
        self.trades: list[dict] = []
        self.consensus: dict[str, dict] = {}
        self.openai_calls: list[dict] = []
        self.jev_calls: list[dict] = []
        self.audit: list[dict] = []
        self.events: list[dict] = []
        self.labels: dict[str, dict] = {}
        self.artifacts: dict[str, list[dict]] = {}

    def add_snapshot(self, snapshot: MarketSnapshot) -> None:
        self.snapshots[str(snapshot.snapshot_id)] = _dump(snapshot)

    def add_decision(self, decision: StrategyDecision, artifacts: list[dict] | None = None) -> dict:
        payload = _dump(decision)
        payload["artifacts"] = artifacts or []
        self.decisions.append(payload)
        self.by_decision[payload["decision_id"]] = payload
        self.artifacts[payload["decision_id"]] = artifacts or []
        for artifact in artifacts or []:
            kind = artifact.get("kind")
            record = {"decision_id": payload["decision_id"], "snapshot_id": payload["snapshot_id"], **artifact}
            if kind == "openai":
                self.openai_calls.append(record)
            elif kind == "jev":
                self.jev_calls.append(record)
        return payload

    def add_risk(self, risk: RiskDecision) -> dict:
        payload = _dump(risk)
        self.risks[payload["decision_id"]] = payload
        return payload

    def add_order(self, order: OrderRecord) -> dict:
        payload = _dump(order)
        self.orders[payload["order_id"]] = payload
        self.orders_by_client[payload["client_order_id"]] = payload
        return payload

    def add_fill(self, fill) -> dict:
        payload = _dump(fill)
        self.fills.append(payload)
        return payload

    def add_trade(self, trade: TradeRecord) -> dict:
        payload = _dump(trade)
        self.trades.append(payload)
        return payload

    def add_consensus(self, result: ConsensusResult) -> dict:
        payload = _dump(result)
        self.consensus[payload["opportunity_id"]] = payload
        return payload

    def add_audit(self, record: AuditRecord) -> dict:
        payload = _dump(record)
        self.audit.append(payload)
        return payload

    def add_event(self, kind: str, message: str, payload: dict | None = None, now: datetime | None = None) -> None:
        self.events.append(
            {
                "kind": kind,
                "message": message,
                "payload": payload or {},
                "timestamp": (now or datetime.now(timezone.utc)).isoformat(),
            }
        )
        if len(self.events) > 500:
            del self.events[:-500]

    def add_label(self, snapshot_id: UUID, labels: dict) -> None:
        self.labels[str(snapshot_id)] = labels

    def inspector(self, decision_id: str) -> dict | None:
        decision = self.by_decision.get(decision_id)
        if decision is None:
            return None
        snapshot = self.snapshots.get(decision["snapshot_id"])
        return {
            "decision": decision,
            "snapshot": snapshot,
            "features": None if snapshot is None else snapshot.get("features"),
            "artifacts": self.artifacts.get(decision_id, []),
            "risk": self.risks.get(decision_id),
            "orders": [order for order in self.orders.values() if order["decision_id"] == decision_id],
            "fills": [fill for fill in self.fills if fill["decision_id"] == decision_id],
            "trade": next((trade for trade in self.trades if trade["decision_id"] == decision_id), None),
            "consensus": self.consensus.get(decision["opportunity_id"]),
            "labels": self.labels.get(decision["snapshot_id"]),
        }

    def opportunity_rows(self, limit: int = 100, symbol: str | None = None) -> list[dict]:
        grouped: dict[str, list[dict]] = {}
        for decision in self.decisions:
            if symbol and decision["symbol"] != symbol:
                continue
            grouped.setdefault(decision["opportunity_id"], []).append(decision)
        rows = []
        for opportunity_id, items in grouped.items():
            by_strategy = {item["strategy"]: item for item in items}
            head = items[0]
            risks = {}
            for item in items:
                risk = self.risks.get(item["decision_id"])
                if risk is not None:
                    risks[item["strategy"]] = {
                        "accepted": risk["accepted"],
                        "reject_reasons": risk["reject_reasons"],
                    }
            rows.append(
                {
                    "opportunity_id": opportunity_id,
                    "snapshot_id": head["snapshot_id"],
                    "timestamp": head["timestamp"],
                    "symbol": head["symbol"],
                    "strategies": by_strategy,
                    "consensus": self.consensus.get(opportunity_id),
                    "risk": risks,
                }
            )
        rows.sort(key=lambda row: row["timestamp"], reverse=True)
        return rows[:limit]

    def filter_trades(
        self,
        *,
        strategy: str | None = None,
        symbol: str | None = None,
        side: str | None = None,
        result: str | None = None,
        regime: str | None = None,
    ) -> list[dict]:
        rows = []
        for trade in self.trades:
            if strategy and trade["strategy"] != strategy:
                continue
            if symbol and trade["symbol"] != symbol:
                continue
            if side and trade["side"] != side:
                continue
            if regime and trade.get("quantitative_regime") != regime:
                continue
            if result == "win" and trade["net_pnl"] <= 0:
                continue
            if result == "loss" and trade["net_pnl"] >= 0:
                continue
            rows.append(trade)
        rows.sort(key=lambda row: row["closed_at"], reverse=True)
        return rows
