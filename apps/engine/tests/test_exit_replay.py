from datetime import timedelta
import pytest
from app.analytics.exit_replay import compare_exits, ExitPolicy
from tests.conftest import clock


def entry(side='LONG'):
    return dict(side=side,opened_at=clock().isoformat(),entry_price=100,quantity=1,
                entry_fee=.1,exit_fee_rate=.001,borrow_hourly_rate=.0001,
                initial_net_risk=1.2,stop=99 if side=='LONG' else 101,
                target=103 if side=='LONG' else 97)


def quotes(prices):
    return [dict(timestamp=(clock()+timedelta(seconds=t)).isoformat(),bid=p-.01,ask=p+.01) for t,p in prices]


@pytest.mark.parametrize('side,prices',[('LONG',[(1,101.6),(2,100.9),(3,98)]),
                                     ('SHORT',[(1,98.4),(2,99.1),(3,102)])])
def test_profit_protection_uses_past_peak_and_executable_side(side,prices):
    r=compare_exits(entry(side),quotes(prices),source='bid_ask',slippage_bps=1,
        policies=[ExitPolicy('control'),ExitPolicy('protect',0,1,.5)])['results']
    assert r[0]['reason']=='STOP' and r[0]['net_pnl']<0
    assert r[1]['reason']=='PROFIT_PROTECTION' and r[1]['net_pnl']>0
    assert r[1]['held_minutes']==pytest.approx(2/60)
    assert r[1]['coverage_complete']


def test_gaps_and_proxy_cannot_become_verified_profit_and_censored_is_not_realized():
    result=compare_exits(entry(),quotes([(60,100.5)]),source='candle_close_proxy',slippage_bps=2)['results'][0]
    assert result['status']=='CENSORED' and result['net_pnl'] is None
    assert result['mark_net_pnl'] is not None and not result['coverage_complete']
    result=compare_exits(entry(),quotes([(60,104)]),source='bid_ask',slippage_bps=2)['results'][0]
    assert result['reason']=='TARGET' and not result['coverage_complete']


def test_time_exit_includes_fees_slippage_and_hourly_interest():
    e=entry()
    result=compare_exits(e,quotes([(1800,100)]),source='bid_ask',slippage_bps=1,
                         policies=[ExitPolicy('time',30)])['results'][0]
    execution=99.99*.9999
    assert result['net_pnl']==pytest.approx(execution-100-.1-execution*.001-.01)
    assert result['reason']=='TIME'


def test_out_of_order_quotes_and_unknown_costs_are_rejected():
    with pytest.raises(ValueError,match='out of order'):
        compare_exits(entry(),quotes([(2,100),(1,100)]),source='bid_ask',slippage_bps=0)
    e=entry();e['borrow_hourly_rate']=None
    with pytest.raises(ValueError,match='borrow_hourly_rate'):
        compare_exits(e,[],source='bid_ask',slippage_bps=0)
