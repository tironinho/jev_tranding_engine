import pytest

from app.db.postgres import PostgresMirror, _bind_fill_order


def test_restarted_exit_fill_uses_the_stored_order_id():
    fill = {"fill_id": "new-fill", "order_id": "new-uuid", "price": 1}
    bound = _bind_fill_order(fill, {"new-uuid": "stored-uuid"})
    assert bound["order_id"] == "stored-uuid"
    assert fill["order_id"] == "new-uuid"
    assert _bind_fill_order(fill, {"new-uuid": "new-uuid"}) is fill


@pytest.mark.asyncio
async def test_a_failed_checkpoint_does_not_freeze_later_trades():
    mirror = PostgresMirror("postgresql://unused")
    mirror.healthy = True

    class Boom:
        async def __aenter__(self):
            raise RuntimeError("fills_order_id_fkey")

        async def __aexit__(self, *_args):
            return False

    mirror.factory = lambda: Boom()
    await mirror.save_checkpoint("baseline", {"cash": 1, "equity": 1}, [], [], [])
    assert mirror.healthy is True
    assert "fills_order_id_fkey" in (mirror.last_error or "")
