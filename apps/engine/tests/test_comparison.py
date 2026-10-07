from datetime import datetime, timezone
from uuid import uuid4

import pytest

from app.analytics.comparison import compare_strategies
from app.analytics.performance import summarize_trades
from app.consensus.engine import consensus_from_decisions
from app.domain.enums import Action, ConsensusLabel, OperatingMode, SignalStatus
from app.domain.schemas import StrategyDecision, TradeRecord


def _decision(
    strategy: str,
    action: Action,
    opportunity,
    status: SignalStatus = SignalStatus.VALID,
) -> StrategyDecision:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return StrategyDecision(
        correlation_id=opportunity,
        opportunity_id=opportunity,
        snapshot_id=opportunity,
        strategy=strategy,
        symbol="BTCUSDT",
        timestamp=now,
        action=action,
        confidence=0.6,
        reason_codes=[],
        signal_status=status,
        mode=OperatingMode.PAPER,
    )


def _trade(strategy: str, net: float, gross: float, decision_id) -> TradeRecord:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return TradeRecord(
        strategy=strategy,
        symbol="BTCUSDT",
        side=Action.LONG,
        quantity=1,
        entry_price=100,
        exit_price=100 + gross,
        stop=99,
        target=110,
        opened_at=now,
        closed_at=now,
        gross_pnl=gross,
        fees=gross - net,
        slippage=0,
        funding=0,
        net_pnl=net,
        r_multiple=net / 10,
        mfe=1,
        mae=1,
        exit_reason="TARGET",
        decision_id=decision_id,
        quantitative_regime="bull_trend|normal_volatility",
    )


def test_consensus_labels():
    opportunity = uuid4()
    three = [
        _decision("baseline", Action.LONG, opportunity),
        _decision("baseline_jev", Action.LONG, opportunity),
        _decision("baseline_openai_jev", Action.LONG, opportunity),
    ]
    assert consensus_from_decisions(three).label is ConsensusLabel.LONG_3_3
    mixed = [
        _decision("baseline", Action.LONG, opportunity),
        _decision("baseline_jev", Action.NO_TRADE, opportunity),
        _decision("baseline_openai_jev", Action.LONG, opportunity),
    ]
    assert consensus_from_decisions(mixed).label is ConsensusLabel.LONG_2_3
    conflict = [
        _decision("baseline", Action.LONG, opportunity),
        _decision("baseline_jev", Action.SHORT, opportunity),
        _decision("baseline_openai_jev", Action.NO_TRADE, opportunity),
    ]
    assert consensus_from_decisions(conflict).label is ConsensusLabel.CONFLICT
    quiet = [
        _decision("baseline", Action.NO_TRADE, opportunity),
        _decision("baseline_jev", Action.NO_TRADE, opportunity),
        _decision("baseline_openai_jev", Action.NO_TRADE, opportunity),
    ]
    assert consensus_from_decisions(quiet).label is ConsensusLabel.NO_CONSENSUS
    standby = [
        _decision("baseline", Action.LONG, opportunity),
        _decision("baseline_jev", Action.LONG, opportunity),
        _decision("baseline_openai_jev", Action.NO_TRADE, opportunity, SignalStatus.SKIPPED),
    ]
    agreed = consensus_from_decisions(standby)
    assert agreed.label is ConsensusLabel.LONG_2_2
    assert "baseline_openai_jev" not in agreed.actions
    partial = [
        _decision("baseline", Action.LONG, opportunity),
        _decision("baseline_jev", Action.NO_TRADE, opportunity),
        _decision("baseline_openai_jev", Action.NO_TRADE, opportunity, SignalStatus.SKIPPED),
    ]
    assert consensus_from_decisions(partial).label is ConsensusLabel.NO_CONSENSUS
    standby_short = [
        _decision("baseline", Action.SHORT, opportunity),
        _decision("baseline_jev", Action.SHORT, opportunity),
        _decision("baseline_openai_jev", Action.NO_TRADE, opportunity, SignalStatus.SKIPPED),
    ]
    assert consensus_from_decisions(standby_short).label is ConsensusLabel.SHORT_2_2


def test_net_pnl_is_not_gross_and_expectancy_matches_definition():
    decision_id = uuid4()
    trades = [
        _trade("baseline", net=30, gross=40, decision_id=decision_id),
        _trade("baseline", net=-10, gross=-8, decision_id=uuid4()),
    ]
    summary = summarize_trades(trades, 10_000)
    assert summary["gross_pnl"] == 32
    assert summary["net_pnl"] == 20
    assert summary["net_pnl"] != summary["gross_pnl"]
    win_rate = 0.5
    expectancy = win_rate * 30 - 0.5 * 10
    assert summary["expectancy"] == pytest.approx(expectancy)
    assert summary["profit_factor"] == pytest.approx(3)
    assert summary["break_even_win_rate"] == pytest.approx(10 / 40)


def test_comparison_counts_eliminated_baseline_trades():
    kept = uuid4()
    dropped = uuid4()
    opportunity_kept = uuid4()
    opportunity_dropped = uuid4()
    decisions = [
        {
            "opportunity_id": str(opportunity_kept),
            "strategy": "baseline",
            "action": "LONG",
            "signal_status": "valid",
            "decision_id": str(kept),
        },
        {
            "opportunity_id": str(opportunity_kept),
            "strategy": "baseline_jev",
            "action": "LONG",
            "signal_status": "valid",
            "decision_id": str(uuid4()),
        },
        {
            "opportunity_id": str(opportunity_dropped),
            "strategy": "baseline",
            "action": "LONG",
            "signal_status": "valid",
            "decision_id": str(dropped),
        },
        {
            "opportunity_id": str(opportunity_dropped),
            "strategy": "baseline_jev",
            "action": "NO_TRADE",
            "signal_status": "valid",
            "decision_id": str(uuid4()),
        },
    ]
    report = compare_strategies(
        baseline=[_trade("baseline", 15, 20, dropped), _trade("baseline", 5, 8, kept)],
        baseline_jev=[_trade("baseline_jev", 5, 8, uuid4())],
        openai_jev=[],
        decisions=decisions,
        openai_cost=18,
        starting_equity=10_000,
    )
    assert report["jev_eliminated_signals"] == 1
    assert report["eliminated_trade_net_pnl"] == 15
    assert report["ai_cost"] == 18
    assert report["ai_roi"] == pytest.approx((0 - 5) / 18)


def test_ai_roi_stays_null_without_a_real_price():
    report = compare_strategies(
        baseline=[],
        baseline_jev=[],
        openai_jev=[],
        decisions=[],
        openai_cost=None,
        starting_equity=10_000,
    )
    assert report["ai_cost"] is None
    assert report["ai_roi"] is None
