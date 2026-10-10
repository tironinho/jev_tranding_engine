from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.enums import Action, OrderStatus, OrderType
from app.domain.schemas import FillRecord, OrderRecord
from app.execution.slippage import SlippageConfig, simulate_fill
from app.market.state import OrderBook


@dataclass
class OrderIntent:
    decision_id: UUID
    risk_id: UUID | None
    strategy: str
    symbol: str
    side: str
    order_type: OrderType
    quantity: float
    limit_price: float | None
    mode: str
    created_at: datetime
    best_bid: float | None
    best_ask: float | None
    book: OrderBook | None
    fee_rate: float
    close_sequence: int = 0


class PaperExecutionProvider:
    """Deterministic paper fills. `seed` is recorded for reproducibility; the default path does not use RNG."""

    def __init__(self, seed: int = 1) -> None:
        self.seed = seed
        self._orders: dict[str, OrderRecord] = {}
        self._fills: dict[str, list[FillRecord]] = {}

    @staticmethod
    def client_order_id(decision_id: UUID) -> str:
        return decision_id.hex

    def get(self, client_order_id: str) -> OrderRecord | None:
        return self._orders.get(client_order_id)

    def submit_market(self, intent: OrderIntent, slippage: SlippageConfig) -> tuple[OrderRecord, list[FillRecord]]:
        client_id = self.client_order_id(intent.decision_id)
        existing = self._orders.get(client_id)
        if existing is not None:
            return existing, self._fills.get(client_id, [])
        preview = simulate_fill(
            side=intent.side,
            quantity=intent.quantity,
            best_bid=intent.best_bid,
            best_ask=intent.best_ask,
            book=intent.book,
            config=slippage,
        )
        status = OrderStatus.FILLED if preview.fully_filled else OrderStatus.PARTIALLY_FILLED
        if preview.filled_quantity <= 0:
            status = OrderStatus.REJECTED
        order = OrderRecord(
            client_order_id=client_id,
            decision_id=intent.decision_id,
            risk_id=intent.risk_id,
            strategy=intent.strategy,
            symbol=intent.symbol,
            side=intent.side,  # type: ignore[arg-type]
            order_type=OrderType.MARKET,
            status=status,
            mode="paper",
            quantity=intent.quantity,
            filled_quantity=preview.filled_quantity,
            price=None,
            expected_price=preview.expected_price,
            average_fill_price=preview.estimated_fill_price if preview.filled_quantity else None,
            created_at=intent.created_at,
        )
        fills: list[FillRecord] = []
        if preview.filled_quantity > 0:
            fills.append(
                FillRecord(
                    order_id=order.order_id,
                    decision_id=intent.decision_id,
                    price=preview.estimated_fill_price,
                    quantity=preview.filled_quantity,
                    fee=preview.estimated_fill_price * preview.filled_quantity * intent.fee_rate,
                    slippage_bps=preview.slippage_bps,
                    liquidity="taker",
                    filled_at=intent.created_at,
                )
            )
        self._orders[client_id] = order
        self._fills[client_id] = fills
        return order, fills

    def submit_limit(self, intent: OrderIntent, now: datetime) -> tuple[OrderRecord, list[FillRecord]]:
        """A limit fills only when the opposite touch is through the limit. Otherwise it rests, then expires as MISSED."""
        # Limit orders use a distinct id so they do not collide with a market entry of the same decision.
        client_id = f"{intent.decision_id.hex[:31]}L"
        existing = self._orders.get(client_id)
        if existing is not None and existing.status in {OrderStatus.FILLED, OrderStatus.MISSED, OrderStatus.CANCELED}:
            return existing, self._fills.get(client_id, [])
        limit = intent.limit_price or 0
        crossed = False
        if intent.side == "BUY" and intent.best_ask is not None and intent.best_ask <= limit:
            crossed = True
        if intent.side == "SELL" and intent.best_bid is not None and intent.best_bid >= limit:
            crossed = True
        if not crossed:
            order = OrderRecord(
                client_order_id=client_id,
                decision_id=intent.decision_id,
                risk_id=intent.risk_id,
                strategy=intent.strategy,
                symbol=intent.symbol,
                side=intent.side,  # type: ignore[arg-type]
                order_type=OrderType.LIMIT,
                status=OrderStatus.MISSED,
                mode="paper",
                quantity=intent.quantity,
                price=limit,
                expected_price=limit,
                created_at=now,
            )
            self._orders[client_id] = order
            self._fills[client_id] = []
            return order, []
        fill_price = limit
        order = OrderRecord(
            client_order_id=client_id,
            decision_id=intent.decision_id,
            risk_id=intent.risk_id,
            strategy=intent.strategy,
            symbol=intent.symbol,
            side=intent.side,  # type: ignore[arg-type]
            order_type=OrderType.LIMIT,
            status=OrderStatus.FILLED,
            mode="paper",
            quantity=intent.quantity,
            filled_quantity=intent.quantity,
            price=limit,
            expected_price=limit,
            average_fill_price=fill_price,
            created_at=now,
        )
        fills = [
            FillRecord(
                order_id=order.order_id,
                decision_id=intent.decision_id,
                price=fill_price,
                quantity=intent.quantity,
                fee=fill_price * intent.quantity * intent.fee_rate,
                slippage_bps=0,
                liquidity="maker",
                filled_at=now,
            )
        ]
        self._orders[client_id] = order
        self._fills[client_id] = fills
        return order, fills


def exit_reason(side: Action, bid: float | None, ask: float | None, stop: float, target: float) -> str | None:
    if side is Action.LONG and bid is not None:
        if bid <= stop:
            return "STOP"
        if bid >= target:
            return "TARGET"
    if side is Action.SHORT and ask is not None:
        if ask >= stop:
            return "STOP"
        if ask <= target:
            return "TARGET"
    return None


def _clock_closes_a_loser(side: Action, bid: float | None, ask: float | None, entry: float | None) -> bool:
    """A flat or losing mark may leave on the clock. A winner stays for the target or the stop."""
    if entry is None:
        return True
    if side is Action.LONG:
        return bid is not None and bid <= entry
    return ask is not None and ask >= entry


def position_exit(
    side: Action,
    bid: float | None,
    ask: float | None,
    stop: float,
    target: float,
    hold_minutes: float,
    max_hold_minutes: int,
    entry: float | None = None,
) -> str | None:
    """Price exits win over the clock. Time exit closes a loser, not a winner."""
    reason = exit_reason(side, bid, ask, stop, target)
    if reason is not None:
        return reason
    if (max_hold_minutes > 0 and hold_minutes >= max_hold_minutes
            and _clock_closes_a_loser(side, bid, ask, entry)):
        return "TIME"
    return None


def stepped_stop(position, favorable: float | None, *, enabled: bool = True) -> float | None:
    """Tighten the stop after +1R and +2R. The first lock is entry plus round-trip fees.

    R is the original stop distance. A print inside the first R does not move the stop.
    +1R moves it to net breakeven. +2R moves it to +1R. The target is left alone.
    Pass enabled=False to keep the original stop unchanged.
    """
    if not enabled:
        return None
    if favorable is None or position.quantity == 0:
        return None
    initial = position.initial_stop if position.initial_stop is not None else position.stop
    risk = abs(position.entry_price - initial)
    if risk <= 0:
        return None
    entry = position.entry_price
    entry_per = position.entry_fee / abs(position.quantity)
    rate = max(position.exit_fee_rate, 0.0)
    if position.side is Action.LONG:
        progress = favorable - entry
        if progress < risk:
            return None
        breakeven = entry + entry_per if rate >= 1 else (entry + entry_per) / (1 - rate)
        locked = entry + risk if progress >= 2 * risk else breakeven
        if locked <= position.stop or locked >= position.target:
            return None
        return locked
    progress = entry - favorable
    if progress < risk:
        return None
    breakeven = (entry - entry_per) / (1 + rate)
    locked = entry - risk if progress >= 2 * risk else breakeven
    if locked >= position.stop or locked <= position.target:
        return None
    return locked


def position_exit_observed(
    side: Action,
    *,
    min_bid: float | None,
    max_bid: float | None,
    min_ask: float | None,
    max_ask: float | None,
    stop: float,
    target: float,
    hold_minutes: float,
    max_hold_minutes: int,
    entry: float | None = None,
    bid: float | None = None,
    ask: float | None = None,
) -> str | None:
    """Stop uses the worst touch since the last check. Target uses the best. Clock closes a loser."""
    if side is Action.LONG:
        if min_bid is not None and min_bid <= stop:
            return "STOP"
        if max_bid is not None and max_bid >= target:
            return "TARGET"
    if side is Action.SHORT:
        if max_ask is not None and max_ask >= stop:
            return "STOP"
        if min_ask is not None and min_ask <= target:
            return "TARGET"
    mark_bid = bid if bid is not None else max_bid
    mark_ask = ask if ask is not None else min_ask
    if (max_hold_minutes > 0 and hold_minutes >= max_hold_minutes
            and _clock_closes_a_loser(side, mark_bid, mark_ask, entry)):
        return "TIME"
    return None
