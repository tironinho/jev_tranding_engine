from datetime import timedelta
from uuid import uuid4

import pytest

from app.db.models import MarketSnapshotRow
from app.db.postgres import _derive_audit_labels
from tests.conftest import clock


def _snapshot(seconds: int, price: float, identity=None):
    return MarketSnapshotRow(
        id=identity or uuid4(),
        symbol="BTCUSDT",
        market_type="margin",
        timestamp=clock() + timedelta(seconds=seconds),
        price=price,
        payload={},
    )


def test_audit_rebuilds_only_horizons_covered_by_durable_snapshots():
    identity = uuid4()
    origin = _snapshot(0, 100, identity)
    series = [origin, _snapshot(60, 101), _snapshot(180, 99), _snapshot(300, 102)]

    labels = _derive_audit_labels(origin, series)

    assert labels["snapshot_id"] == str(identity)
    assert labels["future_return_30s"] == pytest.approx(.01)
    assert labels["future_return_1m"] == pytest.approx(.01)
    assert labels["future_return_3m"] == pytest.approx(-.01)
    assert labels["future_return_5m"] == pytest.approx(.02)
    assert "future_return_15m" not in labels
