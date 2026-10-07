from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID, uuid4

from app.domain.enums import Action, PositionStatus
from app.domain.schemas import PositionRecord, TradeRecord


def unrealized(position: PositionRecord, mark: float) -> float:
    if position.side is Action.LONG:
        return (mark - position.entry_price) * position.quantity
    return (position.entry_price - mark) * position.quantity


@dataclass
class VirtualAccount:
    strategy: str
    cash: float
    day_start_equity: float
    day_key: str
    realized_pnl_today: float = 0.0
    positions: dict[str, PositionRecord] = field(default_factory=dict)
    trades: list[TradeRecord] = field(default_factory=list)
    equity_points: list[dict] = field(default_factory=list)
    last_entry_at: datetime | None = None
    margin_locked: dict[str, float] = field(default_factory=dict)

    def equity(self, marks: dict[str, float]) -> float:
        marked = sum(
            unrealized(position, marks.get(symbol, position.entry_price))
            for symbol, position in self.positions.items()
        )
        return self.cash + sum(self.margin_locked.values()) + marked

    def exposure(self, marks: dict[str, float]) -> float:
        total = 0.0
        for symbol, position in self.positions.items():
            mark = marks.get(symbol, position.entry_price)
            total += abs(mark * position.quantity)
        return total

    def symbol_exposure(self, symbol: str, marks: dict[str, float]) -> float:
        position = self.positions.get(symbol)
        if position is None:
            return 0.0
        mark = marks.get(symbol, position.entry_price)
        return abs(mark * position.quantity)


class AccountBook:
    def __init__(self, strategies: list[str], initial_equity: float, now: datetime) -> None:
        day_key = now.astimezone(timezone.utc).date().isoformat()
        self.initial_equity = initial_equity
        self.accounts = {
            key: VirtualAccount(
                strategy=key,
                cash=initial_equity,
                day_start_equity=initial_equity,
                day_key=day_key,
                equity_points=[{"t": now.isoformat(), "equity": initial_equity, "mark": False}],
            )
            for key in strategies
        }

    def roll_day(self, strategy: str, now: datetime, marks: dict[str, float]) -> None:
        account = self.accounts[strategy]
        key = now.astimezone(timezone.utc).date().isoformat()
        if key != account.day_key:
            account.day_start_equity = account.equity(marks)
            account.realized_pnl_today = 0.0
            account.day_key = key

    def open_position(
        self,
        *,
        strategy: str,
        symbol: str,
        side: Action,
        quantity: float,
        entry_price: float,
        stop: float,
        target: float,
        opened_at: datetime,
        decision_id: UUID,
        entry_fee: float,
        initial_net_risk: float,
        margin: float,
    ) -> PositionRecord:
        account = self.accounts[strategy]
        account.cash -= entry_fee
        account.cash -= margin
        account.margin_locked[symbol] = margin
        account.realized_pnl_today -= entry_fee
        position = PositionRecord(
            strategy=strategy,
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            stop=stop,
            target=target,
            opened_at=opened_at,
            decision_id=decision_id,
            entry_fee=entry_fee,
            initial_net_risk=initial_net_risk,
        )
        account.positions[symbol] = position
        account.last_entry_at = opened_at
        return position

    def update_excursion(self, strategy: str, symbol: str, mark: float) -> None:
        position = self.accounts[strategy].positions.get(symbol)
        if position is None:
            return
        if position.side is Action.LONG:
            favorable = max(0.0, (mark - position.entry_price) * position.quantity)
            adverse = max(0.0, (position.entry_price - mark) * position.quantity)
        else:
            favorable = max(0.0, (position.entry_price - mark) * position.quantity)
            adverse = max(0.0, (mark - position.entry_price) * position.quantity)
        position.mfe = max(position.mfe, favorable)
        position.mae = max(position.mae, adverse)

    def close_position(
        self,
        *,
        strategy: str,
        symbol: str,
        exit_price: float,
        closed_at: datetime,
        exit_fee: float,
        slippage: float,
        funding: float,
        exit_reason: str,
        quantitative_regime: str | None,
        market_regime: str | None,
    ) -> TradeRecord:
        account = self.accounts[strategy]
        position = account.positions.pop(symbol)
        margin = account.margin_locked.pop(symbol, 0.0)
        if position.side is Action.LONG:
            gross = (exit_price - position.entry_price) * position.quantity
        else:
            gross = (position.entry_price - exit_price) * position.quantity
        net = gross - position.entry_fee - exit_fee + funding
        account.cash += margin
        account.cash += gross - exit_fee + funding
        account.realized_pnl_today += gross - exit_fee + funding
        r_multiple = (net / position.initial_net_risk) if position.initial_net_risk else None
        trade = TradeRecord(
            trade_id=uuid4(),
            strategy=strategy,
            symbol=symbol,
            side=position.side,
            quantity=position.quantity,
            entry_price=position.entry_price,
            exit_price=exit_price,
            stop=position.stop,
            target=position.target,
            opened_at=position.opened_at,
            closed_at=closed_at,
            gross_pnl=gross,
            fees=position.entry_fee + exit_fee,
            slippage=slippage,
            funding=funding,
            net_pnl=net,
            r_multiple=r_multiple,
            mfe=position.mfe,
            mae=position.mae,
            exit_reason=exit_reason,
            decision_id=position.decision_id,
            quantitative_regime=quantitative_regime,
            market_regime=market_regime,
        )
        position.status = PositionStatus.CLOSED
        position.closed_at = closed_at
        position.trade_id = trade.trade_id
        account.trades.append(trade)
        equity = account.equity({})
        account.equity_points.append({"t": closed_at.isoformat(), "equity": equity, "mark": False})
        return trade
