from __future__ import annotations

from app.domain.enums import ConsensusLabel
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
