from datetime import timedelta

from app.service import FutureReturnLabeler
from tests.conftest import clock, long_snapshot


def test_labels_use_first_price_at_each_horizon_and_preserve_symbols():
    labeler = FutureReturnLabeler()
    snapshot, _ = long_snapshot()
    labeler.register(snapshot)
    other = snapshot.model_copy(update={"symbol": "ETHUSDT", "price": 200})
    labeler.register(other)
    for seconds, price in [(29, 110), (30, 101), (45, 109), (60, 102), (180, 103), (300, 104)]:
        assert labeler.on_price("BTCUSDT", clock()+timedelta(seconds=seconds), price) == []
    done = labeler.on_price("BTCUSDT", clock()+timedelta(seconds=900), 105)
    assert len(done) == 1
    labels = done[0]["labels"]
    assert abs(labels["future_return_30s"]-.01) < 1e-12
    assert abs(labels["future_return_1m"]-.02) < 1e-12
    assert abs(labels["future_return_15m"]-.05) < 1e-12
    assert len(labeler.pending) == 1
    eth_done = labeler.on_price("ETHUSDT", clock()+timedelta(seconds=901), 202)
    assert len(eth_done) == 1
    assert not labeler.pending


def test_ticks_before_next_deadline_do_not_scan_pending_signals():
    labeler = FutureReturnLabeler()
    snapshot, _ = long_snapshot()
    labeler.register(snapshot)

    class NotIterable(list):
        def __iter__(self):
            raise AssertionError("pending history was scanned before any label was due")

    labeler.pending = NotIterable(labeler.pending)
    assert labeler.on_price("BTCUSDT", clock()+timedelta(seconds=29), 100) == []
    assert labeler.on_price("XRPUSDT", clock()+timedelta(seconds=900), 100) == []
