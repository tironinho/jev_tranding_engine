from app.db.models import Base


REQUIRED = {
    "symbols",
    "market_snapshots",
    "feature_snapshots",
    "strategy_configs",
    "strategy_decisions",
    "openai_calls",
    "jev_calls",
    "risk_decisions",
    "orders",
    "fills",
    "positions",
    "trades",
    "paper_accounts",
    "paper_balances",
    "performance_snapshots",
    "consensus_results",
    "future_labels",
    "engine_events",
    "audit_logs",
    "strategy_versions",
    "experiments",
    "experiment_hypotheses",
    "experiment_metrics",
    "experiment_runs",
    "experiment_artifacts",
    "anomalies",
    "research_reports",
    "promotion_events",
    "strategy_decay",
    "coding_agent_tasks",
}


def test_schema_covers_the_audit_chain():
    assert REQUIRED <= set(Base.metadata.tables)
    orders = Base.metadata.tables["orders"]
    assert "client_order_id" in orders.c
    decisions = Base.metadata.tables["strategy_decisions"]
    assert {"correlation_id", "opportunity_id", "snapshot_id"} <= set(decisions.c.keys())
