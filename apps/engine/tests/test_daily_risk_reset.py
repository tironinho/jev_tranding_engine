from datetime import timedelta
from types import SimpleNamespace
import pytest
from app.service import _live_daily_risk_window
from tests.conftest import clock, settings


def test_fixed_reset_preserves_history_counts_new_losses_and_survives_restart():
    now=clock();cut=now-timedelta(minutes=5)
    trades=[SimpleNamespace(mode="live",closed_at=cut-timedelta(seconds=1),net_pnl=-1),
            SimpleNamespace(mode="live",closed_at=cut+timedelta(seconds=1),net_pnl=-.2),
            SimpleNamespace(mode="paper",closed_at=now,net_pnl=-3)]
    points=[dict(t=(now-timedelta(hours=2)).isoformat(),wallet=20)]
    assert _live_daily_risk_window(trades,points,now,19,cut,19)==(19,-.2)
    assert _live_daily_risk_window(trades,points,now,25,cut,19)==(19,-.2)
    assert len(trades)==3
    later=now+timedelta(days=1)
    assert _live_daily_risk_window(trades,points,later,18,cut,19)==(18,0)


def test_persisted_reset_requires_aware_past_cutoff_and_positive_equity():
    from app.config import DailyRiskReset
    with pytest.raises(ValueError): DailyRiskReset(reset_at="2026-01-01T12:00:00",equity=20)
    with pytest.raises(ValueError): DailyRiskReset(reset_at=clock(),equity=0)
    assert DailyRiskReset(reset_at=clock(),equity=20).equity==20


@pytest.mark.asyncio
async def test_reset_is_written_to_database_restored_and_preserved_by_limit_edits():
    from unittest.mock import AsyncMock
    from tests.conftest import engine
    eng=engine()
    saved=[]
    async def save(payload): saved.append(payload.copy())
    eng.postgres=SimpleNamespace(healthy=True,save_risk_limits=save,save_audit=AsyncMock())
    eng.balance.snapshot=AsyncMock(return_value=dict(status="ok",assets=[{"asset":"USDT","total":20}]))
    reset=await eng.reset_daily_risk("test")
    assert reset["max_daily_loss"]==.03
    restored=engine();restored.apply_runtime({"risk":saved[-1]})
    assert restored._daily_reset_payload()==saved[-1]["daily_reset"]
    await eng.update_risk({"max_daily_loss":.03},"test")
    assert saved[-1]["daily_reset"]==restored._daily_reset_payload()


@pytest.mark.asyncio
async def test_failed_persistence_does_not_reset_running_budget():
    from unittest.mock import AsyncMock
    from tests.conftest import engine
    eng=engine()
    async def fail(payload): eng.postgres.healthy=False
    eng.postgres=SimpleNamespace(healthy=True,save_risk_limits=fail)
    eng.balance.snapshot=AsyncMock(return_value=dict(status="ok",assets=[{"asset":"USDT","total":20}]))
    with pytest.raises(RuntimeError): await eng.reset_daily_risk("test")
    assert eng.daily_risk_reset_at is None
