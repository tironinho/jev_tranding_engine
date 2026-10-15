import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from tests.conftest import clock, engine, long_snapshot
from tests.test_runtime import _open
from tests.test_risk_limits import _order
from app.domain.enums import Action, OperatingMode
from app.features.engine import five_minute_structure
from tests.test_role_baseline import _bar


@pytest.mark.asyncio
async def test_position_management_precedes_slow_label_persistence():
    eng=engine(); events=[]
    async def manage(*args,**kwargs): events.append("manage")
    async def labels(*args): events.append("labels")
    eng.manage_positions=manage
    eng.postgres=SimpleNamespace(healthy=True,save_label=labels)
    eng.labeler.on_price=lambda *args:[dict(snapshot_id=uuid4(),labels={},label_timing={})]
    eng._quotes["BTCUSDT"]=[dict(timestamp=clock(),price=100)]
    eng._quotes_dirty.add("BTCUSDT")
    await eng._drain_quotes("BTCUSDT")
    assert events==["manage","labels"]


@pytest.mark.asyncio
async def test_model_evaluates_while_snapshot_is_pending_but_orders_wait():
    eng=engine(); snapshot,book=long_snapshot()
    eng.strategy_settings["baseline_jev"].enabled=False
    entered=asyncio.Event(); release=asyncio.Event(); evaluated=asyncio.Event(); durable=[]
    async def save(payload):
        entered.set(); await release.wait(); durable.append(True)
    eng.postgres=SimpleNamespace(healthy=True,save_snapshot=save,save_decision=AsyncMock(),save_consensus=AsyncMock())
    async def evaluate(*args):
        evaluated.set(); return _order(snapshot,Action.LONG).model_copy(update=dict(strategy="baseline"))
    eng.strategies["baseline"]=SimpleNamespace(evaluate=evaluate)
    async def execute(*args): assert durable
    eng._execute_decisions=execute
    task=asyncio.create_task(eng.evaluate_snapshot(snapshot))
    try:
        await asyncio.wait_for(entered.wait(),1)
        await asyncio.wait_for(evaluated.wait(),1)
        assert not task.done()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_first_live_touch_is_kept_and_propagated_to_trade():
    eng=engine();position=_open(eng,mode="live")
    eng.manage_positions=AsyncMock()
    state=eng.states["BTCUSDT"];state.best_bid=121;state.best_ask=121.1
    await eng.on_price("BTCUSDT",121,clock()+timedelta(minutes=1))
    await asyncio.gather(*list(eng._background))
    first=dict(position.exit_observations["TARGET"])
    await eng.on_price("BTCUSDT",122,clock()+timedelta(minutes=2))
    await asyncio.gather(*list(eng._background))
    assert position.exit_observations["TARGET"]==first
    position.exit_timing={"reason":"TARGET","observed_touch":first}
    await eng._finish_exit("baseline","BTCUSDT",position,"TARGET",clock()+timedelta(minutes=2),state,122,2,.1,0,None,None)
    assert eng.accounts.accounts["baseline"].trades[-1].exit_timing["observed_touch"]==first


@pytest.mark.parametrize("side",[1,-1])
def test_retest_uses_level_before_break_not_rolling_extreme(side):
    quiet=[_bar(i,100) for i in range(20)]
    broke=_bar(20,100+side*2)
    retest=_bar(21,100+side*.2)
    result=five_minute_structure([*quiet,broke,retest],1)
    assert result["retest_long_5m" if side==1 else "retest_short_5m"] is True
    assert result["retest_long_level_5m" if side==1 else "retest_short_level_5m"]==100
    far=_bar(21,100+side*1.5)
    assert five_minute_structure([*quiet,broke,far],1)["retest_long_5m" if side==1 else "retest_short_5m"] is False


@pytest.mark.asyncio
async def test_scheduler_reports_queue_separately_from_provider_timeout():
    from app.providers.jev.scheduler import JevScheduler
    from tests.test_jev_scheduler import request
    scheduler=JevScheduler(active_timeout_s=.01)
    scheduler._active_slots=asyncio.Semaphore(0)
    class Provider:
        async def evaluate_market_state(self, req): await asyncio.Event().wait()
    telemetry={}
    task=asyncio.create_task(scheduler.evaluate(Provider(),request(),telemetry=telemetry))
    await asyncio.sleep(.02)
    scheduler._active_slots.release()
    with pytest.raises(TimeoutError): await task
    assert telemetry["queue_ms"] >= 10
    assert telemetry["provider_total_ms"] >= 5
    assert telemetry["timeout_s"]==.01
    assert telemetry["failure_type"]=="TimeoutError"


def test_reversal_variant_requires_sustained_recovery_and_remains_shadow():
    from app.strategies.role_baseline import assess_roles
    from tests.test_role_baseline import viable
    f=viable(context_15m_slope=-.002,price_vs_ema20_15m=-.001,
             distance_to_vwap=.001,breakout_5m=False,bullish_reclaim_5m=True,
             reclaim_sustained_long_5m=False)
    first=assess_roles(Action.LONG,f)
    assert not first["entry_variants"]["sustained_reversal"]["eligible"]
    f["reclaim_sustained_long_5m"]=True
    sustained=assess_roles(Action.LONG,f)
    assert sustained["entry_variants"]["sustained_reversal"]["eligible"]
    assert sustained["can_authorize"] is False
