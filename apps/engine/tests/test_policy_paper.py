from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from app.analytics.policy_paper import PolicyPaperLab
from app.api.router import create_app
from app.providers.jev.scheduler import JevScheduler
from app.risk.engine import FeeQuote
from app.service import TradingEngine
from tests.conftest import clock, engine, settings
from tests.test_initial_live_profile import AssessmentProvider
from tests.test_market_range_plans import snapshot


def quote(book, now=None, bid=99.99, ask=100.01):
    return {"bid": bid, "ask": ask, "book_at": now or clock(), "book": book,
            "marks": {"BTCUSDT": (bid+ask)/2}}


async def sample(monkeypatch, probability=.3, span=2, execution_quote=None):
    monkeypatch.setattr("app.analytics.policy_paper.utcnow", clock)
    eng = TradingEngine(settings(initial_paper_equity=20))
    # Fail immediately if a comparison accidentally uses real execution.
    eng.live.submit_market = AsyncMock(side_effect=AssertionError("live order"))
    s, book = snapshot(span, span)
    provider = AssessmentProvider(probability)
    lab = eng.policy_paper
    await lab.sample(s, eng.weights, eng.combination, eng.risk.limits,
        FeeQuote(.00075,.00075,"test"), {}, provider, eng.jev_scheduler,
        lambda: execution_quote or quote(book), {"step_size": .001, "min_notional": 5})
    eng.live.submit_market.assert_not_awaited()
    assert not eng.accounts.accounts["baseline_jev"].positions
    assert eng.risk.limits.jev_expectancy_gate is True
    assert eng.risk.limits.min_net_rr == 1.5
    assert eng.risk.limits.min_stop_pct == .0025
    return eng, lab, provider, s, book


@pytest.mark.asyncio
async def test_paired_comparison_allows_only_paper_break_and_reuses_identical_question(monkeypatch):
    eng, lab, provider, s, book = await sample(monkeypatch)
    rows = {r["policy"]: r for r in lab.recent}
    assert "JEV_NON_POSITIVE_EXPECTANCY" in rows["control"]["reason_codes"]
    assert not rows["control"]["accepted"]
    assert rows["historical_break"]["accepted"]
    assert rows["combined"]["accepted"]
    assert lab.stats["control"]["rejects"] == {"JEV_NON_POSITIVE_EXPECTANCY": 1}
    assert lab.sample_count == 1
    assert lab.provider_calls == len(provider.requests) == 4
    assert lab.reused_calls == 1
    current, historical = rows["control"]["plan"], rows["historical_break"]["plan"]
    assert current["target"] == historical["target"]
    assert rows["control"]["probability"] == rows["historical_break"]["probability"]
    # A tighter geometry must be sent as a distinct question, not borrow p
    # from the original target/stop contract.
    assert rows["structural_stop"]["plan"]["stop"] > current["stop"]
    assert rows["structural_stop"]["plan"]["target"] != current["target"]
    p = next(iter(lab.book.accounts["historical_break"].positions.values()))
    assert p.mode == "paper"
    assert p.max_hold_minutes == 60
    assert p.initial_net_risk <= 20*eng.risk.limits.risk_per_trade
    assert not lab.due(s, eng.weights)


@pytest.mark.asyncio
async def test_reduced_geometry_reaches_model_when_control_target_does_not_fit_range(monkeypatch):
    _, lab, _, _, _ = await sample(monkeypatch, probability=.8, span=.7)
    rows = {r["policy"]: r for r in lab.recent}
    assert "MARKET_RANGE_BELOW_COST_TARGET" in rows["control"]["reason_codes"]
    assert rows["control"]["probability"] is None
    assert rows["net_rr_1"]["probability"] == .8
    assert rows["structural_stop"]["probability"] == .8
    assert rows["combined"]["probability"] == .8


@pytest.mark.asyncio
async def test_paper_loss_includes_both_fees_and_borrow_interest_and_survives_restart(monkeypatch):
    eng, lab, _, s, book = await sample(monkeypatch)
    account = lab.book.accounts["historical_break"]
    position = next(iter(account.positions.values()))
    later = clock()+timedelta(minutes=2)
    monkeypatch.setattr("app.analytics.policy_paper.utcnow", lambda: later)
    lab.observe(s.symbol, quote(None, later, bid=position.stop-.01, ask=position.stop+.01))
    assert not account.positions
    trade = account.trades[-1]
    assert trade.exit_reason == "STOP"
    assert trade.net_pnl < 0
    assert trade.fees > 0
    assert trade.funding < 0
    assert trade.net_pnl == pytest.approx(trade.gross_pnl-trade.fees+trade.funding)
    assert account.cash == pytest.approx(20+trade.net_pnl)
    stat = lab.stats["historical_break"]
    assert stat["net_pnl"] == trade.net_pnl
    assert stat["fees"] == trade.fees
    assert stat["interest"] == -trade.funding
    assert stat["closed"] == 1 and stat["wins"] == 0
    restored = PolicyPaperLab(eng.slippage, later)
    restored.restore(lab.export())
    assert restored.report(quote(book)["marks"]) == lab.report(quote(book)["marks"])
    assert restored.book.accounts["historical_break"].last_stop_at[s.symbol] == later


@pytest.mark.asyncio
async def test_paper_deadline_closes_winner_even_before_target(monkeypatch):
    _, lab, _, s, _ = await sample(monkeypatch)
    account = lab.book.accounts["historical_break"]
    p = next(iter(account.positions.values()))
    later = clock()+timedelta(minutes=61)
    monkeypatch.setattr("app.analytics.policy_paper.utcnow", lambda: later)
    lab.observe(s.symbol, quote(None,later,bid=p.entry_price+.3,ask=p.entry_price+.32))
    trade = account.trades[-1]
    assert trade.exit_reason == "TIME"
    assert trade.net_pnl > 0
    assert trade.funding == pytest.approx(-trade.quantity*trade.entry_price*.0001*2)


@pytest.mark.asyncio
async def test_stale_quote_cannot_create_simulated_profit_or_new_entry(monkeypatch):
    _, book = snapshot()
    stale = quote(book, clock()-timedelta(seconds=6))
    _, lab, _, _, _ = await sample(monkeypatch, execution_quote=stale)
    assert not any(a.positions for a in lab.book.accounts.values())
    assert "PAPER_STALE_EXECUTION_QUOTE" in lab.stats["historical_break"]["rejects"]


@pytest.mark.asyncio
async def test_checkpoint_failure_does_not_disable_live_and_is_retried(monkeypatch):
    eng, lab, _, _, _ = await sample(monkeypatch)
    class Database:
        healthy = True
        save_policy_paper = AsyncMock(side_effect=RuntimeError("temporary"))
    eng.postgres = Database()
    await eng._save_policy_paper()
    assert eng.postgres.healthy
    assert lab.dirty
    eng.postgres.save_policy_paper.side_effect = None
    await eng._save_policy_paper()
    assert not lab.dirty
    assert eng.postgres.save_policy_paper.await_count == 2


def test_comparison_endpoint_is_authenticated_and_separate_from_real_accounts():
    eng = engine(environment="production",engine_api_secret="test-secret")
    eng.combination = replace(eng.combination,paper_policy_comparison=True)
    app = create_app(eng.settings,eng)
    app.state.settings, app.state.engine = eng.settings, eng
    client = TestClient(app)
    assert client.get("/api/paper/policy-comparison").status_code == 401
    response = client.get("/api/paper/policy-comparison",headers={"authorization":"Bearer test-secret"})
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "PAPER_ONLY" and data["enabled"]
    assert len(data["policies"]) == 5
    assert all(p["equity"] == 20 and p["win_rate"] is None for p in data["policies"])
    assert list(eng.accounts.accounts) == ["baseline","baseline_jev"]


@pytest.mark.asyncio
async def test_gap_closure_remains_in_financial_pnl_but_is_excluded_from_observed_win_rate(monkeypatch):
    eng,lab,_,s,_ = await sample(monkeypatch)
    restored = PolicyPaperLab(eng.slippage,clock())
    restored.restore(lab.export())
    p = next(iter(restored.book.accounts['historical_break'].positions.values()))
    later = clock()+timedelta(minutes=115)
    monkeypatch.setattr('app.analytics.policy_paper.utcnow',lambda:later)
    restored.observe(s.symbol,quote(None,later,bid=p.stop-.01,ask=p.stop+.01))
    report = restored.report({s.symbol:p.stop})
    stat = next(r for r in report['policies'] if r['id']=='historical_break')
    assert stat['closed']==1 and stat['net_pnl']<0
    assert stat['verified_closed']==0 and stat['unverified_closed']==1
    assert stat['verified_win_rate'] is None
    trade = next(r for r in report['closed_records'] if r['strategy']=='historical_break')
    assert trade['observation_coverage']['complete'] is False
    assert trade['observation_coverage']['max_gap_seconds']==115*60


@pytest.mark.asyncio
async def test_continuously_observed_paper_trade_counts_in_verified_sample(monkeypatch):
    _,lab,_,s,_ = await sample(monkeypatch)
    p = next(iter(lab.book.accounts['historical_break'].positions.values()))
    for seconds in [60,120]:
        later = clock()+timedelta(seconds=seconds)
        monkeypatch.setattr('app.analytics.policy_paper.utcnow',lambda:later)
        bid = p.entry_price if seconds==60 else p.stop-.01
        lab.observe(s.symbol,quote(None,later,bid=bid,ask=bid+.02))
    stat = next(r for r in lab.report({s.symbol:p.stop})['policies'] if r['id']=='historical_break')
    assert stat['closed']==stat['verified_closed']==1
    assert stat['unverified_closed']==0 and stat['verified_win_rate']==0
