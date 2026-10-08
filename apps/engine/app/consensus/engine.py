from __future__ import annotations

from app.domain.enums import Action, ConsensusLabel, SignalStatus
from app.domain.schemas import ConsensusResult, StrategyDecision


def consensus_from_decisions(decisions: list[StrategyDecision]) -> ConsensusResult | None:
    """Label agreement among strategies that actually voted.

    `skipped` is not a vote and is not a hole in a fixed panel of three.
    Two LONG votes with OpenAI on standby are 2/2, not 2/3.
    2/3 requires three votes, two of them in the same direction and none opposed.
    """
    usable = [item for item in decisions if item.signal_status.value != "skipped"]
    if not usable:
        return None
    actions = {item.strategy: item.action.value for item in usable}
    longs = sum(1 for value in actions.values() if value == "LONG")
    shorts = sum(1 for value in actions.values() if value == "SHORT")
    label = _label(longs, shorts, len(actions))
    head = usable[0]
    return ConsensusResult(
        opportunity_id=head.opportunity_id,
        snapshot_id=head.snapshot_id,
        symbol=head.symbol,
        timestamp=head.timestamp,
        label=label,
        actions=actions,
    )


def extra_open_slot(decisions: list[StrategyDecision], min_continuation: float) -> bool:
    """One position past the cap, only when both books want the same side.

    Continuation has to clear the extra-entry bar, which sits above the
    normal 0.55 confirm. Risk still refuses a symbol that is already open.
    """
    by_key = {item.strategy: item for item in decisions}
    baseline = by_key.get("baseline")
    jev = by_key.get("baseline_jev")
    if baseline is None or jev is None:
        return False
    if baseline.signal_status is not SignalStatus.VALID or jev.signal_status is not SignalStatus.VALID:
        return False
    if baseline.action not in (Action.LONG, Action.SHORT) or jev.action is not baseline.action:
        return False
    continuation = jev.metadata.get("jev_continuation")
    if not isinstance(continuation, (int, float)):
        return False
    return float(continuation) >= min_continuation


def _label(longs: int, shorts: int, voters: int) -> ConsensusLabel:
    if longs and shorts:
        return ConsensusLabel.CONFLICT
    agreed = longs or shorts
    long_side = longs > 0
    if voters >= 2 and agreed == voters:
        if voters == 3:
            return ConsensusLabel.LONG_3_3 if long_side else ConsensusLabel.SHORT_3_3
        if voters == 2:
            return ConsensusLabel.LONG_2_2 if long_side else ConsensusLabel.SHORT_2_2
    if voters == 3 and agreed == 2:
        return ConsensusLabel.LONG_2_3 if long_side else ConsensusLabel.SHORT_2_3
    return ConsensusLabel.NO_CONSENSUS
