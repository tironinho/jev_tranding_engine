from copy import deepcopy

from app.analytics.setup_review import classify_setup, review_setups


def candidate():
    return {"decision": {"decision_id": "case", "timestamp": "2026-10-12T11:23:00Z",
        "symbol": "ETHUSDT", "action": "NO_TRADE", "reason_codes": ["JEV_NON_POSITIVE_EXPECTANCY"],
        "metadata": {"baseline_action": "SHORT", "jev_continuation": .27,
                     "jev_trade_plan": {"horizon_minutes": 60}}},
        "snapshot": {"features": {"breakdown": True, "ema_alignment": -1,
            "return_5m": -.001, "return_15m": -.002, "return_60m": -.003,
            "context_15m_slope": -.001, "volume_ratio": 1.4,
            "taker_flow_1m": -.4, "taker_flow_5m": -.3, "imbalance_10": .6}},
        "labels": {"future_return_15m": -.002}}


def test_matching_setup_does_not_override_veto_or_require_book_agreement():
    row = candidate()
    setup = classify_setup(row["decision"], row["snapshot"])
    assert setup["matches"]
    assert setup["can_override_veto"] is False
    assert row["decision"]["action"] == "NO_TRADE"


def test_missing_or_conflicting_context_does_not_match():
    row = candidate()
    row["snapshot"]["features"]["context_15m_slope"] = .001
    assert not classify_setup(row["decision"], row["snapshot"])["matches"]
    row["snapshot"]["features"].pop("taker_flow_5m")
    setup = classify_setup(row["decision"], row["snapshot"])
    assert "taker_flow_5m" in setup["missing_features"]
    assert not setup["matches"]


def test_review_counts_winners_and_losers_and_distinguishes_forecast_horizon():
    win = candidate()
    loss = deepcopy(win)
    loss["decision"]["decision_id"] = "loss"
    for row, pnl in [(win, .2), (loss, -.3)]:
        row["trades"] = [{"net_pnl": pnl, "opened_at": "2026-10-12T11:23:00Z",
                         "closed_at": "2026-10-13T12:42:00Z"}]
    unexecuted = candidate()
    result = review_setups([win, loss, unexecuted])
    matching = result["cohorts"]["matching"]
    assert matching["decisions"] == 3
    assert matching["closed_trade_records"] == 2
    assert abs(matching["net_pnl"] + .1) < 1e-10
    assert matching["win_rate"] == .5
    assert matching["closures_beyond_forecast_horizon"] == 2
    assert len(result["rows"]) == 3
    assert result["cohorts"]["other"]["net_pnl"] is None


def test_setup_review_api_requires_auth_and_forwards_history_cursor():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from fastapi.testclient import TestClient
    from app.api.router import create_app
    from tests.conftest import engine
    eng = engine(environment="production", engine_api_secret="test-secret")
    loader = AsyncMock(return_value=[candidate()])
    eng.postgres = SimpleNamespace(healthy=True, recent_decision_audit=loader)
    app = create_app(eng.settings, eng)
    app.state.settings = eng.settings
    app.state.engine = eng
    client = TestClient(app)
    assert client.get("/api/jev/setup-review").status_code == 401
    result = client.get("/api/jev/setup-review?limit=1&before=2026-10-13T00:00:00Z",
        headers={"authorization": "Bearer test-secret"})
    assert result.status_code == 200
    data = result.json()
    assert data["cohorts"]["matching"]["decisions"] == 1
    assert data["next_before"] == candidate()["decision"]["timestamp"]
    assert loader.call_args.kwargs["before"].year == 2026
