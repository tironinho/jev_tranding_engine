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


def test_reset_configuration_requires_pair_and_aware_past_cutoff():
    with pytest.raises(ValueError): settings(live_daily_risk_reset_at=clock())
    with pytest.raises(ValueError): settings(live_daily_risk_reset_equity=20)
    with pytest.raises(ValueError): settings(live_daily_risk_reset_at="2026-01-01T12:00:00",live_daily_risk_reset_equity=20)
    s=settings(live_daily_risk_reset_at=clock(),live_daily_risk_reset_equity=20)
    assert s.max_daily_loss==.03
