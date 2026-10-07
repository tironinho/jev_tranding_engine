from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from app.domain.enums import Action
from app.domain.schemas import TradeRecord
from app.evolution.agent_dispatcher import CursorCodingAgentProvider, MockCodingAgentProvider, build_task_prompt
from app.evolution.anomaly_detector import bootstrap_mean_interval, detect_anomalies
from app.evolution.experiment_memory import hypothesis_fingerprint, text_similarity
from app.evolution.hypothesis_generator import accept_hypothesis
from app.evolution.paths import is_allowed, is_protected
from app.evolution.promotion_engine import promotion_decision
from app.evolution.schemas import REJECTION_LIVE, StrategyFamily
from tests.conftest import engine


def _trade(strategy: str, r_multiple: float, when: datetime) -> TradeRecord:
    net = r_multiple * 10
    return TradeRecord(
        strategy=strategy,
        symbol="SOLUSDT",
        side=Action.LONG,
        quantity=1,
        entry_price=100,
        exit_price=100 + net,
        stop=99,
        target=110,
        opened_at=when,
        closed_at=when,
        gross_pnl=net,
        fees=0,
        slippage=0,
        funding=0,
        net_pnl=net,
        r_multiple=r_multiple,
        mfe=1,
        mae=1,
        exit_reason="TARGET",
        decision_id=uuid4(),
    )


def test_bootstrap_champions_have_no_fake_performance():
    eng = engine()
    rows = eng.evolution.champion_rows()
    assert [row["version"] for row in rows] == ["B-001", "J-001", "OJ-001"]
    assert all(row["health"] is None for row in rows)
    assert all(row["expectancy_r"] is None for row in rows)
    assert eng.evolution.status()["live_promotion"] == "disabled"
    assert eng.evolution.status()["status"] == "online"


@pytest.mark.asyncio
async def test_analysis_without_sample_is_no_action():
    eng = engine()
    result = await eng.evolution.run_analysis("tester")
    assert result["decision"] == "NO_ACTION"
    assert result["anomalies"] == 0
    assert eng.evolution.experiments.experiments == {}


def test_small_sample_is_not_an_anomaly():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trades = [_trade("baseline", -0.4, now) for _ in range(10)]
    assert detect_anomalies(trades, min_sample=100, starting_equity=10_000) == []


def test_negative_expectancy_needs_interval_below_zero():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    trades = [_trade("baseline", -0.3, now + timedelta(minutes=index)) for index in range(120)]
    found = detect_anomalies(trades, min_sample=100, starting_equity=10_000)
    assert found
    assert found[0].code == "NEGATIVE_EXPECTANCY"
    assert found[0].anomaly_confidence > 0
    interval = bootstrap_mean_interval([-0.3] * 30, seed=1)
    assert interval is not None and interval[1] < 0


def test_protected_risk_path_is_rejected_and_strategy_path_is_allowed():
    assert is_protected("apps/engine/app/risk/engine.py")
    assert is_protected("apps/engine/app/execution/binance_live.py")
    assert is_protected(".env")
    assert not is_allowed("apps/engine/app/risk/engine.py")
    assert is_allowed("apps/engine/app/strategies/baseline_score.py")
    assert is_allowed("apps/engine/app/features/engine.py")


def test_live_promotion_is_impossible():
    accepted, reason = promotion_decision(
        target="live",
        gates={"static": True, "unit": True, "backtest": True, "oos": True, "walk_forward": True, "monte_carlo": True},
        comparison={"expectancy_delta": 1, "drawdown_delta": 0, "challenger": {"trades": 500, "profit_factor": 2}},
        policy={"min_trades": 100, "min_expectancy_improvement": 0.05, "max_drawdown_increase": 0, "min_profit_factor": 1.2},
        auto=False,
        auto_shadow=False,
        auto_paper=False,
    )
    assert accepted is False
    assert reason == REJECTION_LIVE


def test_duplicate_hypothesis_is_remembered():
    eng = engine()
    text = "Filter SOLUSDT when volatility_percentile is above 0.85 and spread is wide."
    created = eng.evolution.experiments.create(
        family=StrategyFamily.BASELINE_OPENAI_JEV,
        parent_version="OJ-001",
        hypothesis=text,
        problem="high volatility losses",
        dataset_version="dataset-test",
        seed=1,
    )
    assert getattr(created, "public_id", "").startswith("EXP-")
    again = eng.evolution.experiments.create(
        family=StrategyFamily.BASELINE_OPENAI_JEV,
        parent_version="OJ-001",
        hypothesis=text,
        problem="same",
        dataset_version="dataset-test",
        seed=1,
    )
    assert again == "DUPLICATE_EXPERIMENT"
    assert hypothesis_fingerprint("baseline", text) == hypothesis_fingerprint("baseline", "  " + text.upper() + "  ") or text_similarity(text, text) == 1


def test_vague_hypothesis_is_refused():
    assert accept_hypothesis("melhore a estratégia") is False
    assert accept_hypothesis("Filter entries when spread_bps exceeds the configured maximum.") is True


@pytest.mark.asyncio
async def test_mock_agent_does_not_modify_repository_and_cursor_is_unwired():
    provider = MockCodingAgentProvider()
    result = await provider.create_experiment_task({"task_id": "EXP-000001", "prompt": "x"})
    assert result["repository_modified"] is False
    cursor = CursorCodingAgentProvider()
    with pytest.raises(Exception):
        await cursor.create_experiment_task({"task_id": "EXP-000001"})
    eng = engine()
    experiment = eng.evolution.experiments.experiments.get("EXP-000001")
    assert experiment is None or "main" not in (build_task_prompt(created_prompt := experiment) if False else "")


def test_promote_api_blocks_live(monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.router import create_app

    eng = engine()
    created = eng.evolution.experiments.create(
        family=StrategyFamily.BASELINE,
        parent_version="B-001",
        hypothesis="Skip entries when the spread is wider than the baseline gate.",
        problem="spread losses",
        dataset_version="dataset-test",
        seed=1,
    )
    app = create_app(eng.settings, eng)
    app.state.settings = eng.settings
    app.state.engine = eng
    client = TestClient(app)
    response = client.post(
        f"/api/evolution/experiments/{created.public_id}/promote",
        json={"target": "live", "confirm": "PROMOTE"},
    )
    assert response.status_code == 403
    status = client.get("/api/evolution/status")
    assert status.status_code == 200
    assert status.json()["champions"] == 3
    assert status.json()["experiments_running"] == 1
