from datetime import timedelta
import pytest
from app.config import RiskLimits
from app.domain.enums import Action
from app.strategies.context_policy import classify_entry
from app.risk.engine import FeeQuote
from tests.conftest import engine, long_snapshot
from tests.test_risk_limits import _context, _order


def facts(side=1):
    return dict(context_15m_slope=.002*side, price_vs_ema20_15m=.001*side,
                distance_to_vwap=.001*side, return_1m=.001*side,
                return_5m=.003*side, return_15m=.004*side,
                breakout_5m=side==1, breakdown_5m=side==-1,
                extension_above_resistance_5m_atr=.2, extension_below_support_5m_atr=.2,
                taker_flow_1m=.4*side, taker_flow_5m=.2*side)


@pytest.mark.parametrize("side,sign", [(Action.LONG,1),(Action.SHORT,-1)])
def test_continuation_and_reversal_have_different_evidence(side, sign):
    f=facts(sign)
    assert classify_entry(side,f)["eligible"]
    f["context_15m_slope"] *= -1
    assert not classify_entry(side,f)["eligible"]
    f["bullish_reclaim" if sign==1 else "bearish_reclaim"]=True
    assert not classify_entry(side,f)["eligible"]
    f["bullish_reclaim_5m" if sign==1 else "bearish_reclaim_5m"]=True
    f["reclaim_sustained_long_5m" if sign==1 else "reclaim_sustained_short_5m"]=True
    assert not classify_entry(side,f)["eligible"]
    f["reclaim_retest_long_5m" if sign==1 else "reclaim_retest_short_5m"]=True
    result=classify_entry(side,f)
    assert result["eligible"] and result["setup"]=="REVERSAL"


def test_missing_context_cannot_be_treated_as_alignment():
    assert not classify_entry(Action.LONG,{})["eligible"]
    f=facts(); f["context_15m_slope"]=float("nan")
    assert classify_entry(Action.LONG,f)["setup"]=="UNKNOWN"


@pytest.mark.parametrize("side,sign", [(Action.LONG,1),(Action.SHORT,-1)])
def test_context_requires_slope_and_ema_not_a_majority_of_scores(side,sign):
    f=facts(sign)
    f['price_vs_ema20_15m'] *= -1
    result=classify_entry(side,f)
    assert result['setup']=='REVERSAL'
    assert not result['eligible']
    assert 'REVERSAL_RETEST_REQUIRED' in result['reasons']


@pytest.mark.parametrize("side,sign", [(Action.LONG,1),(Action.SHORT,-1)])
def test_continuation_needs_five_minute_structure_even_with_strong_rsi(side,sign):
    f={**facts(sign),'breakout_5m':False,'breakdown_5m':False,
       'breakout':sign==1,'breakdown':sign==-1,'rsi':90 if sign==1 else 10}
    assert 'ENTRY_STRUCTURE_REQUIRED' in classify_entry(side,f)['reasons']
    f['pullback_resume_long_5m' if sign==1 else 'pullback_resume_short_5m']=True
    assert classify_entry(side,f)['eligible']
    f['taker_flow_5m'] *= -1
    assert 'ENTRY_FLOW_DIVERGENT' in classify_entry(side,f)['reasons']


@pytest.mark.parametrize("side,sign", [(Action.LONG,1),(Action.SHORT,-1)])
def test_extended_entry_waits_for_new_non_extended_retest(side,sign):
    f=facts(sign)
    extension='extension_above_resistance_5m_atr' if sign==1 else 'extension_below_support_5m_atr'
    f[extension]=1.2
    result=classify_entry(side,f)
    assert not result['eligible'] and 'ENTRY_EXTENDED_WAIT_RETEST' in result['reasons']
    assert result['timing']['preferred_entry']=='RETEST'
    # A subsequent snapshot needs a real event; losing extension alone is not enough.
    f.update(breakout_5m=False,breakdown_5m=False)
    f[extension]=.2
    assert not classify_entry(side,f)['eligible']
    f['retest_long_5m' if sign==1 else 'retest_short_5m']=True
    assert classify_entry(side,f)['eligible']
    f['return_1m']=-.001*sign
    assert 'ENTRY_FADING_WAIT_RESUMPTION' in classify_entry(side,f)['reasons']


@pytest.mark.parametrize('value',[None,True,float('nan'),float('inf')])
def test_missing_or_invalid_extension_is_not_a_clean_entry(value):
    result=classify_entry(Action.LONG,{**facts(),'extension_above_resistance_5m_atr':value})
    assert not result['eligible']
    assert 'ENTRY_TIMING_DATA_UNAVAILABLE' in result['reasons']


def test_confirmed_reversal_still_needs_both_flow_windows():
    f={**facts(),'context_15m_slope':-.002,'reclaim_retest_long_5m':True}
    assert classify_entry(Action.LONG,f)['eligible']
    f['taker_flow_5m']=-.2
    assert not classify_entry(Action.LONG,f)['eligible']


def test_shadow_timing_filter_records_v2_without_changing_target_or_risk():
    eng=engine()
    snapshot,book=long_snapshot(**{**facts(),'extension_above_resistance_5m_atr':1.2})
    risk=eng.risk.evaluate(_order(snapshot,Action.LONG),snapshot,_context(),FeeQuote(.0002,.0005,'test'),book)
    assert risk.accepted
    assert risk.details['entry_selection']['version']=='context_entry_v2'
    assert not risk.details['entry_selection']['would_accept']
    eng.risk.update_limits(entry_selection_mode='enforce')
    blocked=eng.risk.evaluate(_order(snapshot,Action.LONG),snapshot,_context(),FeeQuote(.0002,.0005,'test'),book)
    assert 'ENTRY_EXTENDED_WAIT_RETEST' in blocked.reject_reasons


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
