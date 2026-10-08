from datetime import datetime, timedelta, timezone

import pytest

from app.backtest.history import download_klines, kline_path
from app.backtest.runner import paper_scoreboard, replay_history
from app.market.state import Candle
from app.providers.jev.mock import MockJevProvider
from tests.conftest import engine


def _row(open_ms: int, taker: str = "3") -> list:
    return [open_ms, "100", "101", "99", "100", "10", open_ms + 59_999, "1000", 8, taker, "300"]


class _Response:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def json(self):
        return self._rows


class _Client:
    def __init__(self, pages: list[list]) -> None:
        self.pages = pages
        self.calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None):
        self.calls.append((url, params))
        return _Response(self.pages[len(self.calls) - 1])


def _minute(index: int, taker: float) -> Candle:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    return Candle(
        open_time=start,
        close_time=start + timedelta(minutes=1) - timedelta(milliseconds=1),
        open=100,
        high=100.4,
        low=99.6,
        close=100.2,
        volume=10,
        closed=True,
        timeframe="1m",
        taker_buy_volume=taker,
    )


@pytest.mark.asyncio
async def test_download_keeps_taker_volume_and_pages():
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    open_ms = int(start.timestamp() * 1000)
    first = _row(open_ms, "3")
    second = _row(open_ms + 60_000, "8")
    client = _Client([[first, second], [second]])
    candles = await download_klines(
        client,
        base_url="http://klines.test",
        symbol="BTCUSDT",
        interval="1m",
        start=start,
        end=start + timedelta(hours=1),
        market_type="margin",
        limit=2,
    )
    assert kline_path("margin") == "/api/v3/klines"
    assert client.calls[0][0] == "http://klines.test/api/v3/klines"
    assert [candle.taker_buy_volume for candle in candles] == [3, 8]
    assert len(client.calls) == 2


@pytest.mark.asyncio
async def test_replay_uses_mock_jev_and_the_paper_scoreboard():
    eng = engine(jev_provider="mock", market_type="spot")
    minutes = [_minute(index, 3) for index in range(3)]
    await replay_history(eng, "BTCUSDT", {"1m": minutes, "5m": [], "15m": []})
    assert isinstance(eng.jev, MockJevProvider)
    assert eng.states["BTCUSDT"].trades == []
    stored = list(eng.store.snapshots.values())[-1]["features"]
    assert stored["taker_flow_1m"] == -0.4
    assert stored["orderflow_delta_ratio"] is None
    board = paper_scoreboard(eng)
    for key in ("baseline", "baseline_jev"):
        assert set(board[key]) == {"trades", "wins", "losses", "win_rate", "profit_factor", "expectancy_r"}
        assert board[key]["trades"] == 0
