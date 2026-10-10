from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient

from app.api.router import create_app
from app.domain.enums import Action
from app.domain.schemas import TradeRecord
from tests.conftest import clock, engine


def _live_trade(net_pnl: float = 8.5) -> TradeRecord:
    now = clock()
    return TradeRecord(
        strategy="baseline_jev",
        symbol="BTCUSDT",
        side=Action.LONG,
        quantity=0.1,
        entry_price=100,
        exit_price=101,
        stop=99,
        target=102,
        opened_at=now,
        closed_at=now + timedelta(minutes=5),
        gross_pnl=10,
        fees=1,
        slippage=0.5,
        funding=0,
        net_pnl=net_pnl,
        r_multiple=0.85,
        mfe=1.2,
        mae=0.2,
        exit_reason="TARGET",
        decision_id=uuid4(),
        mode="live",
    )


def test_jev_performance_is_authenticated_read_only_report():
    eng = engine(environment="production", engine_api_secret="report-secret")
    eng.accounts.accounts["baseline_jev"].trades.append(_live_trade())
    app = create_app(eng.settings, eng)
    app.state.settings = eng.settings
    app.state.engine = eng
    client = TestClient(app)

    assert client.get("/api/jev/performance").status_code == 401
    response = client.get(
        "/api/jev/performance?mode=live",
        headers={"authorization": "Bearer report-secret"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["closed_trades"] == 1
    assert payload["summary"]["wins"] == 1
    assert payload["summary"]["win_rate"] == 1
    assert payload["summary"]["realized_pnl_usdt"] == 8.5
    assert payload["closed"][0]["entry_price"] == 100
    assert payload["closed"][0]["exit_price"] == 101
    assert payload["closed"][0]["stop_loss"] == 99
    assert payload["closed"][0]["take_profit"] == 102


def test_jev_performance_rejects_unknown_scope():
    eng = engine()
    app = create_app(eng.settings, eng)
    app.state.settings = eng.settings
    app.state.engine = eng
    response = TestClient(app).get("/api/jev/performance?mode=unknown")
    assert response.status_code == 400
