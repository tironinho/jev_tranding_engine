from __future__ import annotations

from dataclasses import dataclass, field

from app.domain.enums import SlippageModelName
from app.domain.schemas import BookLevel, ExecutionPreview
from app.market.state import OrderBook


@dataclass
class SlippageConfig:
    model: SlippageModelName = SlippageModelName.ORDERBOOK_BASED
    fixed_bps: float = 1.0


def _walk(levels: list, quantity: float) -> tuple[float, float]:
    remaining = quantity
    notional = 0.0
    filled = 0.0
    for level in levels:
        if remaining <= 1e-12:
            break
        take = min(remaining, level.quantity)
        notional += take * level.price
        filled += take
        remaining -= take
    if filled <= 0:
        return 0.0, 0.0
    return notional / filled, filled


def simulate_fill(
    *,
    side: str,
    quantity: float,
    best_bid: float | None,
    best_ask: float | None,
    book: OrderBook | None,
    config: SlippageConfig,
) -> ExecutionPreview:
    """side is BUY or SELL. expected_price is the touch. Fill may be worse."""
    warnings: list[str] = []
    model = config.model
    if side == "BUY":
        touch = best_ask
        levels = book.asks if book else []
    else:
        touch = best_bid
        levels = book.bids if book else []
    if touch is None or touch <= 0:
        if book and levels:
            touch = levels[0].price
        else:
            return ExecutionPreview(
                model=model.value,
                expected_price=0,
                estimated_fill_price=0,
                slippage_bps=0,
                fully_filled=False,
                filled_quantity=0,
                warnings=["NO_TOUCH"],
            )

    if model is SlippageModelName.ORDERBOOK_BASED and levels:
        price, filled = _walk(levels, quantity)
        if filled <= 0:
            model = SlippageModelName.SPREAD_BASED
            warnings.append("SLIPPAGE_BOOK_FALLBACK")
        else:
            slippage = (price - touch) / touch * 10_000 if side == "BUY" else (touch - price) / touch * 10_000
            return ExecutionPreview(
                model=SlippageModelName.ORDERBOOK_BASED.value,
                expected_price=touch,
                estimated_fill_price=price,
                slippage_bps=slippage,
                fully_filled=filled + 1e-9 >= quantity,
                filled_quantity=filled,
                warnings=warnings,
            )

    if model is SlippageModelName.ORDERBOOK_BASED:
        model = SlippageModelName.SPREAD_BASED
        warnings.append("SLIPPAGE_BOOK_FALLBACK")

    if model is SlippageModelName.SPREAD_BASED:
        return ExecutionPreview(
            model=SlippageModelName.SPREAD_BASED.value,
            expected_price=touch,
            estimated_fill_price=touch,
            slippage_bps=0.0,
            fully_filled=True,
            filled_quantity=quantity,
            warnings=warnings,
        )

    bump = config.fixed_bps / 10_000
    fill = touch * (1 + bump) if side == "BUY" else touch * (1 - bump)
    return ExecutionPreview(
        model=SlippageModelName.FIXED_BPS.value,
        expected_price=touch,
        estimated_fill_price=fill,
        slippage_bps=config.fixed_bps,
        fully_filled=True,
        filled_quantity=quantity,
        warnings=warnings,
    )


def levels_from_pairs(pairs: list[tuple[float, float]]) -> list:
    return [type("L", (), {"price": price, "quantity": qty}) for price, qty in pairs]


def book_from_levels(bids: list[BookLevel], asks: list[BookLevel], timestamp) -> OrderBook:
    from app.market.state import Level

    return OrderBook(
        bids=[Level(price=level.price, quantity=level.quantity) for level in bids],
        asks=[Level(price=level.price, quantity=level.quantity) for level in asks],
        timestamp=timestamp,
    )
