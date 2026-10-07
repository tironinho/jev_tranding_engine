from datetime import timedelta

from app.domain.enums import (
    ENGINE_DISABLED,
    MAX_DAILY_DRAWDOWN,
    MAX_DAILY_LOSS,
    MAX_OPEN_POSITIONS,
    NET_RR_TOO_LOW,
    SPOT_SHORT_NOT_SUPPORTED,
    Action,
)
from app.execution.slippage import SlippageConfig
from app.risk.engine import FeeQuote, RiskContext, RiskEngine
from app.strategies.baseline_score import score_baseline
from tests.conftest import attach_book, clock, engine, long_snapshot


def _context(**overrides) -> RiskContext:
    data = dict(
        equity=10_000,
        cash=10_000,
        day_start_equity=10_000,
        realized_pnl_today=0,
        open_positions=0,
        symbol_exposure_notional=0,
        total_exposure_notional=0,
        has_position_on_symbol=False,
        last_entry_at=None,
        now=clock(),
        trading_enabled=True,
        persistence_ok=True,
        live_requested=False,
        live_armed=False,
        market_type="futures",
    )
    data.update(overrides)
    return RiskContext(**data)


def _decision_from_snapshot(eng, snapshot):
    import asyncio

    decisions = asyncio.get_event_loop().run_until_complete(eng.evaluate_snapshot(snapshot)) if False else None
    return decisions


def test_drawdown_and_daily_loss_reject():
    eng = engine()
    snapshot, book = long_snapshot()
    result = score_baseline(snapshot, eng.weights)
    assert result.action is Action.LONG
    from app.domain.schemas import StrategyDecision
    from app.domain.enums import OperatingMode

    decision = StrategyDecision(
        correlation_id=snapshot.snapshot_id,
        opportunity_id=snapshot.snapshot_id,
        snapshot_id=snapshot.snapshot_id,
        strategy="baseline",
        symbol="BTCUSDT",
        timestamp=snapshot.timestamp,
        action=Action.LONG,
        confidence=0.8,
        reason_codes=["TREND_UP"],
        mode=OperatingMode.PAPER,
    )
    risk = eng.risk
    rejected = risk.evaluate(
        decision,
        snapshot,
        _context(equity=9_699, day_start_equity=10_000),
        FeeQuote(0.0002, 0.0005, "config"),
        book,
    )
    assert MAX_DAILY_DRAWDOWN in rejected.reject_reasons
    loss = risk.evaluate(
        decision,
        snapshot,
        _context(realized_pnl_today=-300),
        FeeQuote(0.0002, 0.0005, "config"),
        book,
    )
    assert MAX_DAILY_LOSS in loss.reject_reasons


def test_kill_switch_blocks_new_risk():
    eng = engine()
    snapshot, book = long_snapshot()
    from app.domain.enums import OperatingMode
    from app.domain.schemas import StrategyDecision

    decision = StrategyDecision(
        correlation_id=snapshot.snapshot_id,
        opportunity_id=snapshot.snapshot_id,
        snapshot_id=snapshot.snapshot_id,
        strategy="baseline",
        symbol="BTCUSDT",
        timestamp=snapshot.timestamp,
        action=Action.LONG,
        confidence=0.8,
        reason_codes=[],
        mode=OperatingMode.PAPER,
    )
    rejected = eng.risk.evaluate(
        decision,
        snapshot,
        _context(trading_enabled=False),
        FeeQuote(0.0002, 0.0005, "config"),
        book,
    )
    assert ENGINE_DISABLED in rejected.reject_reasons


def test_position_cap_and_spot_short():
    eng = engine()
    snapshot, book = long_snapshot()
    from app.domain.enums import OperatingMode
    from app.domain.schemas import StrategyDecision

    decision = StrategyDecision(
        correlation_id=snapshot.snapshot_id,
        opportunity_id=snapshot.snapshot_id,
        snapshot_id=snapshot.snapshot_id,
        strategy="baseline",
        symbol="BTCUSDT",
        timestamp=snapshot.timestamp,
        action=Action.LONG,
        confidence=0.8,
        reason_codes=[],
        mode=OperatingMode.PAPER,
    )
    capped = eng.risk.evaluate(
        decision,
        snapshot,
        _context(open_positions=eng.risk.limits.max_open_positions),
        FeeQuote(0.0002, 0.0005, "config"),
        book,
    )
    assert MAX_OPEN_POSITIONS in capped.reject_reasons
    short = decision.model_copy(update={"action": Action.SHORT})
    blocked = eng.risk.evaluate(
        short,
        snapshot,
        _context(market_type="spot"),
        FeeQuote(0.0002, 0.0005, "config"),
        book,
    )
    assert SPOT_SHORT_NOT_SUPPORTED in blocked.reject_reasons


def test_low_net_rr_rejects_without_moving_the_target():
    eng = engine(min_net_rr=50)
    snapshot, book = long_snapshot()
    from app.domain.enums import OperatingMode
    from app.domain.schemas import StrategyDecision

    decision = StrategyDecision(
        correlation_id=snapshot.snapshot_id,
        opportunity_id=snapshot.snapshot_id,
        snapshot_id=snapshot.snapshot_id,
        strategy="baseline",
        symbol="BTCUSDT",
        timestamp=snapshot.timestamp,
        action=Action.LONG,
        confidence=0.8,
        reason_codes=[],
        mode=OperatingMode.PAPER,
    )
    rejected = eng.risk.evaluate(decision, snapshot, _context(), FeeQuote(0.0002, 0.0005, "config"), book)
    assert not rejected.accepted
    assert NET_RR_TOO_LOW in rejected.reject_reasons
    assert rejected.economics is not None
    assert rejected.economics.target == 108


def test_entry_throttle():
    from app.domain.enums import ENTRY_THROTTLED, OperatingMode
    from app.domain.schemas import StrategyDecision

    eng = engine()
    snapshot, book = long_snapshot()
    decision = StrategyDecision(
        correlation_id=snapshot.snapshot_id,
        opportunity_id=snapshot.snapshot_id,
        snapshot_id=snapshot.snapshot_id,
        strategy="baseline",
        symbol="BTCUSDT",
        timestamp=snapshot.timestamp,
        action=Action.LONG,
        confidence=0.8,
        reason_codes=[],
        mode=OperatingMode.PAPER,
    )
    rejected = eng.risk.evaluate(
        decision,
        snapshot,
        _context(last_entry_at=snapshot.timestamp - timedelta(seconds=5)),
        FeeQuote(0.0002, 0.0005, "config"),
        book,
    )
    assert ENTRY_THROTTLED in rejected.reject_reasons
