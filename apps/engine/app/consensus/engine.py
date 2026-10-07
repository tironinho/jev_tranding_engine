from __future__ import annotations

from app.domain.enums import ConsensusLabel
from app.domain.schemas import ConsensusResult, StrategyDecision


def consensus_from_decisions(decisions: list[StrategyDecision]) -> ConsensusResult | None:
    usable = [item for item in decisions if item.signal_status.value != "skipped"]
    if not usable:
        return None
    actions = {item.strategy: item.action.value for item in usable}
    longs = sum(1 for value in actions.values() if value == "LONG")
    shorts = sum(1 for value in actions.values() if value == "SHORT")
    if longs and shorts:
        label = ConsensusLabel.CONFLICT
    elif longs == 3 and shorts == 0:
        label = ConsensusLabel.LONG_3_3
    elif shorts == 3 and longs == 0:
        label = ConsensusLabel.SHORT_3_3
    elif longs == 2 and shorts == 0:
        label = ConsensusLabel.LONG_2_3
    elif shorts == 2 and longs == 0:
        label = ConsensusLabel.SHORT_2_3
    else:
        label = ConsensusLabel.NO_CONSENSUS
    head = usable[0]
    return ConsensusResult(
        opportunity_id=head.opportunity_id,
        snapshot_id=head.snapshot_id,
        symbol=head.symbol,
        timestamp=head.timestamp,
        label=label,
        actions=actions,
    )
