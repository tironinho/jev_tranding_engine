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
    last_entry_by_symbol: dict[str, datetime] = field(default_factory=dict)
    last_stop_at: dict[str, datetime] = field(default_factory=dict)
    margin_locked: dict[str, float] = field(default_factory=dict)

    def equity(self, marks: dict[str, float]) -> float:
        marked = sum(
            unrealized(position, marks.get(position.symbol, position.entry_price))
            for position in self.positions.values()
        )
        return self.cash + sum(self.margin_locked.values()) + marked

    def exposure(self, marks: dict[str, float]) -> float:
        total = 0.0
        for position in self.positions.values():
            mark = marks.get(position.symbol, position.entry_price)
            total += abs(mark * position.quantity)
        return total

    def symbol_exposure(self, symbol: str, marks: dict[str, float]) -> float:
        total = 0.0
        for position in self.positions.values():
            if position.symbol != symbol:
                continue
            mark = marks.get(symbol, position.entry_price)
            total += abs(mark * position.quantity)
        return total

    def sole(self, symbol: str) -> PositionRecord:
        matches = [position for position in self.positions.values() if position.symbol == symbol]
        if len(matches) != 1:
            raise KeyError(symbol)
        return matches[0]


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
        initial_stop: float | None = None,
        target: float,
        opened_at: datetime,
        decision_id: UUID,
        entry_fee: float,
        initial_net_risk: float,
        margin: float,
        exit_fee_rate: float = 0,
        fee_source: str = "config",
        quantitative_regime: str | None = None,
        market_regime: str | None = None,
        mode: str = "paper",
        stop_client_order_id: str | None = None,
    ) -> PositionRecord:
        account = self.accounts[strategy]
        account.cash -= entry_fee
        account.cash -= margin
        account.realized_pnl_today -= entry_fee
        position = PositionRecord(
            strategy=strategy,
            symbol=symbol,
            side=side,
            quantity=quantity,
            entry_price=entry_price,
            stop=stop,
            initial_stop=stop if initial_stop is None else initial_stop,
            target=target,
            opened_at=opened_at,
            decision_id=decision_id,
            entry_fee=entry_fee,
            initial_net_risk=initial_net_risk,
            exit_fee_rate=exit_fee_rate,
            fee_source=fee_source,
            quantitative_regime=quantitative_regime,
            market_regime=market_regime,
            mode=mode,  # type: ignore[arg-type]
            stop_client_order_id=stop_client_order_id,
        )
        key = str(position.position_id)
        account.margin_locked[key] = margin
        account.positions[key] = position
        account.last_entry_at = opened_at
        account.last_entry_by_symbol[symbol] = opened_at
        return position

    def update_excursion(self, strategy: str, position_id: str, mark: float) -> None:
        position = self.accounts[strategy].positions.get(position_id)
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
        position_id: str,
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
        position = account.positions.pop(position_id)
        margin = account.margin_locked.pop(position_id, None)
        if margin is None:
            margin = account.margin_locked.pop(position.symbol, 0.0)
        if position.side is Action.LONG:
            gross = (exit_price - position.entry_price) * position.quantity
        else:
            gross = (position.entry_price - exit_price) * position.quantity
        net = gross - position.entry_fee - exit_fee + funding
        account.cash += margin
        account.cash += gross - exit_fee + funding
        if exit_reason == "STOP":
            account.last_stop_at[position.symbol] = closed_at
        account.realized_pnl_today += gross - exit_fee + funding
        r_multiple = (net / position.initial_net_risk) if position.initial_net_risk else None
        trade = TradeRecord(
            trade_id=uuid4(),
            strategy=strategy,
            symbol=position.symbol,
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
            mode=position.mode,
        )
        position.status = PositionStatus.CLOSED
        position.closed_at = closed_at
        position.trade_id = trade.trade_id
        account.trades.append(trade)
        equity = account.equity({})
        account.equity_points.append({"t": closed_at.isoformat(), "equity": equity, "mark": False})
        return trade

    def export_state(self, marks: dict[str, float] | None = None) -> dict[str, dict]:
        marked = marks or {}
        return {key: self.export_strategy(key, marked) for key in self.accounts}

    def export_strategy(self, strategy: str, marks: dict[str, float] | None = None) -> dict:
        account = self.accounts[strategy]
        marked = marks or {}
        return {
            "strategy": strategy,
            "cash": account.cash,
            "day_start_equity": account.day_start_equity,
            "day_key": account.day_key,
            "realized_pnl_today": account.realized_pnl_today,
            "last_entry_at": account.last_entry_at.isoformat() if account.last_entry_at else None,
            "last_entry_by_symbol": {
                symbol: moment.isoformat() for symbol, moment in account.last_entry_by_symbol.items()
            },
            "last_stop_at": {symbol: moment.isoformat() for symbol, moment in account.last_stop_at.items()},
            "margin_locked": dict(account.margin_locked),
            "equity_points": list(account.equity_points[-2000:]),
            "equity": account.equity(marked),
            "positions": [position.model_dump(mode="json") for position in account.positions.values()],
            "trades": [trade.model_dump(mode="json") for trade in account.trades],
        }

    def restore_state(self, payload: dict[str, dict]) -> None:
        for key, item in payload.items():
            if key in self.accounts and isinstance(item, dict):
                self._restore_strategy(item)

    def _restore_strategy(self, payload: dict) -> None:
        account = self.accounts[payload["strategy"]]
        account.cash = float(payload["cash"])
        account.day_start_equity = float(payload["day_start_equity"])
        account.day_key = str(payload["day_key"])
        account.realized_pnl_today = float(payload.get("realized_pnl_today") or 0)
        last_entry = payload.get("last_entry_at")
        account.last_entry_at = datetime.fromisoformat(last_entry) if last_entry else None
        account.last_entry_by_symbol = {
            symbol: datetime.fromisoformat(moment)
            for symbol, moment in (payload.get("last_entry_by_symbol") or {}).items()
            if moment
        }
        account.last_stop_at = {
            symbol: datetime.fromisoformat(moment)
            for symbol, moment in (payload.get("last_stop_at") or {}).items()
            if moment
        }
        account.margin_locked = {symbol: float(amount) for symbol, amount in (payload.get("margin_locked") or {}).items()}
        account.equity_points = list(payload.get("equity_points") or [])
        account.positions = {}
        for raw in payload.get("positions") or []:
            if raw.get("status") not in (None, "OPEN"):
                continue
            position = PositionRecord.model_validate(raw)
            if position.status is not PositionStatus.OPEN:
                continue
            account.positions[str(position.position_id)] = position
        account.margin_locked = _margin_by_position(account.positions, account.margin_locked)
        account.trades = [TradeRecord.model_validate(raw) for raw in payload.get("trades") or []]
        for position in account.positions.values():
            account.last_entry_by_symbol.setdefault(position.symbol, position.opened_at)
        for trade in account.trades:
            account.last_entry_by_symbol.setdefault(trade.symbol, trade.opened_at)

    def adopt_trades(self, strategy: str, rows: list[dict]) -> None:
        """Closed trades written before the account snapshot existed. Cash follows their net sum."""
        account = self.accounts[strategy]
        if account.trades or account.positions:
            return
        trades = [TradeRecord.model_validate(raw) for raw in rows]
        if not trades:
            return
        account.trades = trades
        account.cash = self.initial_equity + sum(trade.net_pnl for trade in trades)
        account.realized_pnl_today = 0.0
        last = max(trade.closed_at for trade in trades)
        account.equity_points.append({"t": last.isoformat(), "equity": account.cash, "mark": False})


def _margin_by_position(positions: dict[str, PositionRecord], locked: dict[str, float]) -> dict[str, float]:
    """Keep margin next to the position. Older snapshots keyed it by symbol."""
    by_symbol: dict[str, list[PositionRecord]] = {}
    for position in positions.values():
        by_symbol.setdefault(position.symbol, []).append(position)
    remapped: dict[str, float] = {}
    for key, amount in locked.items():
        matches = by_symbol.get(key)
        if matches is not None and len(matches) == 1:
            remapped[str(matches[0].position_id)] = float(amount)
        else:
            remapped[key] = float(amount)
    return remapped
