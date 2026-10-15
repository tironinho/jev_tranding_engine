from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.domain.enums import Action
from app.features.engine import closed_candles, five_minute_structure
from app.market.state import Candle
from app.strategies.context_policy import classify_entry
from tests.test_context_selection import facts


def bar(index, opening, high, low, close):
    start=datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(minutes=5*index)
    return Candle(start,start+timedelta(minutes=5)-timedelta(milliseconds=1),
                  opening,high,low,close,10,True,'5m')


def path(sign):
    bars=[bar(i,100.4,101,100,100.4) for i in range(20)]
    bars += [bar(20,99.9,100.3,99.8,100.1),bar(21,100.1,100.3,99.95,100.2)]
    if sign<0:
        bars=[replace(c,open=200-c.open,high=200-c.low,low=200-c.high,close=200-c.close) for c in bars]
    return bars


@pytest.mark.parametrize('side,sign',[(Action.LONG,1),(Action.SHORT,-1)])
def test_reversal_waits_for_reclaimed_level_retest_and_resumption(side,sign):
    candles=path(sign)
    key='reclaim_retest_long_5m' if sign==1 else 'reclaim_retest_short_5m'
    assert five_minute_structure(candles[:-1],1)[key] is False
    structure=five_minute_structure(candles,1)
    assert structure[key] is True
    f={**facts(sign),**structure,'context_15m_slope':-.002*sign}
    assert classify_entry(side,f)['eligible']


@pytest.mark.parametrize('sign',[1,-1])
def test_reclaim_retest_never_uses_forming_or_future_closed_bars(sign):
    candles=path(sign)
    key='reclaim_retest_long_5m' if sign==1 else 'reclaim_retest_short_5m'
    as_of=candles[-2].close_time
    assert five_minute_structure(closed_candles(candles,as_of),1)[key] is False
    forming=[*candles[:-1],replace(candles[-1],closed=False)]
    assert five_minute_structure(closed_candles(forming,candles[-1].close_time),1)[key] is False
    assert five_minute_structure(closed_candles(candles,candles[-1].close_time),1)[key] is True


@pytest.mark.parametrize('sign',[1,-1])
def test_retest_fails_after_reclaimed_level_was_lost(sign):
    candles=path(sign)
    invalid=bar(21,100.1,100.2,99.8,99.9)
    if sign<0:
        invalid=replace(invalid,open=200-invalid.open,high=200-invalid.low,low=200-invalid.high,close=200-invalid.close)
    final=replace(candles[-1],open_time=candles[-1].open_time+timedelta(minutes=5),
                  close_time=candles[-1].close_time+timedelta(minutes=5))
    key='reclaim_retest_long_5m' if sign==1 else 'reclaim_retest_short_5m'
    assert five_minute_structure([*candles[:-1],invalid,final],1)[key] is False
