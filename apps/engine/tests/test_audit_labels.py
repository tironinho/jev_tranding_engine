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


def test_audit_labels_do_not_call_a_price_115_minutes_later_a_15_minute_return():
    origin = _snapshot(0,100)
    labels = _derive_audit_labels(origin,[origin,_snapshot(60,101),_snapshot(115*60,110)])
    assert labels['future_return_1m'] == pytest.approx(.01)
    assert labels['future_return_15m'] is None
    assert labels['timing']['future_return_15m'] == {
        'observed_at':_snapshot(115*60,110).timestamp.isoformat(),'elapsed_seconds':6900,
        'lateness_seconds':6000,'status':'OBSERVATION_GAP'}
    assert labels['label_version'] == 'future_returns_v2'


def test_all_overdue_labels_are_invalid_instead_of_a_labelled_sample():
    from app.analytics.labels import has_valid_return
    origin = _snapshot(0,100)
    labels = _derive_audit_labels(origin,[origin,_snapshot(101*60,110)])
    assert not has_valid_return(labels)
    assert labels['timing']['future_return_15m']['status'] == 'OBSERVATION_GAP'


def test_snapshot_cadence_lateness_is_explicit_and_bounded():
    origin = _snapshot(0,100)
    labels = _derive_audit_labels(origin,[origin,_snapshot(950,101)])
    assert labels['future_return_15m'] == pytest.approx(.01)
    assert labels['timing']['future_return_15m']['elapsed_seconds'] == 950
    assert labels['timing']['future_return_15m']['lateness_seconds'] == 50
    assert _derive_audit_labels(origin,[origin,_snapshot(961,101)])['future_return_15m'] is None
