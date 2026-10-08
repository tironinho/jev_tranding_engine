from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx

from app.backtest.history import download_klines
from app.backtest.runner import paper_scoreboard, replay_history
from app.config import get_settings
from app.service import TradingEngine


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay the paper books on public klines. Jev stays mock.")
    parser.add_argument("--symbol", default="")
    parser.add_argument("--hours", type=float, default=6)
    return parser.parse_args()


async def _run() -> None:
    args = _args()
    settings = get_settings().model_copy(
        update={
            "jev_provider": "mock",
            "database_url": "",
            "trading_live_enabled": False,
            "allow_real_orders": False,
        }
    )
    symbol = (args.symbol or settings.symbol_list[0]).upper()
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start = end - timedelta(hours=args.hours)
    engine = TradingEngine(settings)
    async with httpx.AsyncClient(timeout=20) as client:
        series = {}
        for interval in ("1m", "5m", "15m"):
            series[interval] = await download_klines(
                client,
                base_url=settings.binance_spot_rest_url if settings.market_type != "futures" else settings.binance_futures_rest_url,
                symbol=symbol,
                interval=interval,
                start=start,
                end=end,
                market_type=settings.market_type,
            )
    await replay_history(engine, symbol, series)
    print(
        json.dumps(
            {
                "symbol": symbol,
                "jev": "mock",
                "bars": {interval: len(candles) for interval, candles in series.items()},
                "scoreboard": paper_scoreboard(engine),
            },
            default=str,
        )
    )


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
