from datetime import datetime, timezone

from app.domain.enums import SlippageModelName
from app.domain.schemas import BookLevel
from app.execution.slippage import SlippageConfig, book_from_levels, simulate_fill
from app.market.state import Level, OrderBook


def test_orderbook_walk_is_deterministic():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    book = book_from_levels(
        [BookLevel(price=99, quantity=1)],
        [BookLevel(price=100, quantity=1), BookLevel(price=101, quantity=2)],
        now,
    )
    fill = simulate_fill(
        side="BUY",
        quantity=2,
        best_bid=99,
        best_ask=100,
        book=book,
        config=SlippageConfig(model=SlippageModelName.ORDERBOOK_BASED),
    )
    assert fill.estimated_fill_price == 100.5
    assert fill.expected_price == 100
    assert fill.slippage_bps == 50
    assert fill.fully_filled


def test_partial_book_does_not_invent_size():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    book = OrderBook(bids=[Level(99, 1)], asks=[Level(100, 0.4)], timestamp=now)
    fill = simulate_fill(
        side="BUY",
        quantity=2,
        best_bid=99,
        best_ask=100,
        book=book,
        config=SlippageConfig(model=SlippageModelName.ORDERBOOK_BASED),
    )
    assert fill.filled_quantity == 0.4
    assert fill.fully_filled is False


def test_fixed_bps_moves_against_the_order():
    buy = simulate_fill(
        side="BUY",
        quantity=1,
        best_bid=100,
        best_ask=100,
        book=None,
        config=SlippageConfig(model=SlippageModelName.FIXED_BPS, fixed_bps=10),
    )
    sell = simulate_fill(
        side="SELL",
        quantity=1,
        best_bid=100,
        best_ask=100,
        book=None,
        config=SlippageConfig(model=SlippageModelName.FIXED_BPS, fixed_bps=10),
    )
    assert buy.estimated_fill_price == 100.1
    assert sell.estimated_fill_price == 99.9
