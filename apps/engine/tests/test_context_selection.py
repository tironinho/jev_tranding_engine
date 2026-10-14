from datetime import timedelta
import pytest
from app.config import RiskLimits
from app.domain.enums import Action
from app.strategies.context_policy import classify_entry
from app.risk.engine import FeeQuote
from tests.conftest import engine, long_snapshot
from tests.test_risk_limits import _context, _order


def facts(side=1):
    return dict(context_15m_slope=.002*side, distance_to_vwap=.001*side,
                return_5m=.003*side, return_15m=.004*side,
                taker_flow_1m=.4*side, taker_flow_5m=.2*side)


@pytest.mark.parametrize("side,sign", [(Action.LONG,1),(Action.SHORT,-1)])
def test_continuation_and_reversal_have_different_evidence(side, sign):
    f=facts(sign)
    assert classify_entry(side,f)["eligible"]
    f["context_15m_slope"] *= -1
    assert not classify_entry(side,f)["eligible"]
    f["bullish_reclaim" if sign==1 else "bearish_reclaim"]=True
    result=classify_entry(side,f)
    assert result["eligible"] and result["setup"]=="REVERSAL"


def test_missing_context_cannot_be_treated_as_alignment():
    assert not classify_entry(Action.LONG,{})["eligible"]
    f=facts(); f["context_15m_slope"]=float("nan")
    assert classify_entry(Action.LONG,f)["setup"]=="UNKNOWN"


def test_shadow_records_rejection_and_enforce_blocks_same_trade():
    eng=engine(); snapshot,book=long_snapshot(**{**facts(),"context_15m_slope":-.002})
    decision=_order(snapshot,Action.LONG)
    risk=eng.risk.evaluate(decision,snapshot,_context(),FeeQuote(.0002,.0005,"test"),book)
    assert risk.accepted
    assert not risk.details["entry_selection"]["would_accept"]
    eng.risk.update_limits(entry_selection_mode="enforce")
    blocked=eng.risk.evaluate(_order(snapshot,Action.LONG),snapshot,_context(),FeeQuote(.0002,.0005,"test"),book)
    assert "REVERSAL_RECLAIM_REQUIRED" in blocked.reject_reasons


@pytest.mark.parametrize("event,elapsed,entry,allowed", [(False,10,99,False),(True,2,99,False),(True,10,100,False),(True,10,99,True)])
def test_repeat_entry_needs_event_time_and_price_progress(event,elapsed,entry,allowed):
    eng=engine(); eng.risk.update_limits(entry_selection_mode="enforce",allow_pyramiding=True)
    snapshot,book=long_snapshot(**facts(),atr=.1,breakout=event)
    ctx=_context(has_position_on_symbol=True,position_sides_on_symbol=(Action.LONG,),
                 same_side_entry_prices=(entry,),last_entry_at=snapshot.timestamp-timedelta(minutes=elapsed))
    risk=eng.risk.evaluate(_order(snapshot,Action.LONG),snapshot,ctx,FeeQuote(.0002,.0005,"test"),book)
    assert ("REPEATED_ENTRY_WITHOUT_NEW_SETUP" not in risk.reject_reasons)==allowed


def test_invalid_mode_fails_closed_at_configuration_load():
    with pytest.raises(ValueError): RiskLimits(entry_selection_mode="enforc")


def test_review_excludes_emergency_exits_and_separates_versions():
    from app.analytics.selection_review import selection_review
    def row(commit, reason):
        return dict(decision=dict(prompt_version="test",metadata=dict(build_commit=commit,
            jev_continuation=.7,jev_trade_plan=dict(target=103,stop=99))),
            risk=dict(details=dict(entry_selection=dict(version="v1",mode="shadow",setup="CONTINUATION",would_accept=True))),
            trade=dict(side="LONG",exit_reason=reason,target=103,stop=99,net_pnl=.1,fees=.01))
    result=selection_review([row("a","TARGET"),row("a","PROTECTION_FAILED"),row("b","STOP"),None])
    assert len(result["cohorts"])==2
    assert result["cohorts"][0]["calibration_n"]==1
    assert result["cohorts"][0]["brier"]==pytest.approx(.09)
    assert result["excluded"]["DETAIL_OR_TRADE_MISSING"]==1
