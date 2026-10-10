from fastapi.testclient import TestClient

from app.api.router import create_app
from tests.conftest import engine


class AuditPostgres:
    healthy = True

    async def recent_decision_audit(self, limit, strategy):
        assert limit == 25
        assert strategy == "baseline"
        return [
            {
                "decision": {"decision_id": "decision-1", "action": "NO_TRADE"},
                "snapshot": {"price": 100, "features": {"return_15m": -0.01}},
                "labels": {"future_return_15m": 0.02},
            },
            {
                "decision": {"decision_id": "decision-2", "action": "NO_TRADE"},
                "snapshot": {"price": 101},
                "labels": None,
            },
        ]


def test_decision_audit_is_authenticated_and_returns_market_labels():
    eng = engine(environment="production", engine_api_secret="audit-secret")
    eng.postgres = AuditPostgres()
    app = create_app(eng.settings, eng)
    app.state.settings = eng.settings
    app.state.engine = eng
    client = TestClient(app)

    assert client.get("/api/decisions/audit").status_code == 401
    response = client.get(
        "/api/decisions/audit?limit=25&strategy=baseline",
        headers={"authorization": "Bearer audit-secret"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["labelled"] == 1
    assert payload["unlabelled"] == 1
    assert payload["rows"][0]["labels"]["future_return_15m"] == 0.02


def test_decision_audit_rejects_unknown_strategy():
    eng = engine()
    app = create_app(eng.settings, eng)
    app.state.settings = eng.settings
    app.state.engine = eng
    response = TestClient(app).get("/api/decisions/audit?strategy=unknown")
    assert response.status_code == 400
