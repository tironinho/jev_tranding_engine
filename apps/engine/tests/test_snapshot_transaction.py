import pytest

from app.db.postgres import PostgresMirror
from app.db.models import MarketSnapshotRow, FeatureSnapshotRow
from tests.conftest import long_snapshot


@pytest.mark.asyncio
async def test_snapshot_and_features_share_one_commit_with_parent_flushed_first():
    events = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def add(self, row):
            events.append(type(row))

        async def flush(self):
            events.append("flush")

        def add_all(self, rows):
            events.extend(type(row) for row in rows)

        async def commit(self):
            events.append("commit")

    mirror = PostgresMirror("postgresql://unused")
    mirror.healthy = True
    mirror.factory = Session
    snapshot, _ = long_snapshot()
    await mirror.save_snapshot(snapshot.model_dump(mode="json"))
    assert events == [MarketSnapshotRow, "flush", FeatureSnapshotRow, "commit"]
