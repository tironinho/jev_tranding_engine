from __future__ import annotations

import math
from collections import defaultdict

from app.domain.schemas import TradeRecord


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _stdev(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    avg = sum(values) / len(values)
    var = sum((item - avg) ** 2 for item in values) / (len(values) - 1)
    return math.sqrt(var)


def max_drawdown(equity: list[float]) -> float | None:
    if not equity:
        return None
    peak = equity[0]
    worst = 0.0
    for value in equity:
        peak = max(peak, value)
        if peak > 0:
            worst = max(worst, (peak - value) / peak)
    return worst


def sharpe(returns: list[float]) -> float | None:
    if len(returns) < 3:
        return None
    avg = _mean(returns)
    dev = _stdev(returns)
    if avg is None or dev is None or dev == 0:
        return None
    return (avg / dev) * math.sqrt(len(returns))


def sortino(returns: list[float]) -> float | None:
    if len(returns) < 3:
        return None
    avg = _mean(returns)
    downside = [min(0.0, item) ** 2 for item in returns]
    if avg is None or not downside:
        return None
    dev = math.sqrt(sum(downside) / len(downside))
    if dev == 0:
        return None
    return (avg / dev) * math.sqrt(len(returns))


def summarize_trades(trades: list[TradeRecord], starting_equity: float) -> dict:
    closed = list(trades)
    count = len(closed)
    if count == 0:
        return {
            "trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": None,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "net_profit": 0.0,
            "average_win": None,
            "average_loss": None,
            "average_r": None,
            "expectancy_r": None,
            "expectancy": None,
            "profit_factor": None,
            "max_drawdown": 0.0,
            "sharpe": None,
            "sortino": None,
            "average_trade_duration_s": None,
            "fees_total": 0.0,
            "slippage_total": 0.0,
            "funding_total": 0.0,
            "gross_pnl": 0.0,
            "net_pnl": 0.0,
            "break_even_win_rate": None,
        }
    wins = [trade for trade in closed if trade.net_pnl > 0]
    losses = [trade for trade in closed if trade.net_pnl < 0]
    gross_profit = sum(trade.gross_pnl for trade in closed if trade.gross_pnl > 0)
    gross_loss = sum(trade.gross_pnl for trade in closed if trade.gross_pnl < 0)
    net_profit = sum(trade.net_pnl for trade in closed)
    gross_pnl = sum(trade.gross_pnl for trade in closed)
    avg_win = _mean([trade.net_pnl for trade in wins])
    avg_loss = _mean([abs(trade.net_pnl) for trade in losses])
    r_values = [trade.r_multiple for trade in closed if trade.r_multiple is not None]
    win_rate = len(wins) / count
    loss_rate = len(losses) / count
    expectancy = None
    if avg_win is not None and avg_loss is not None:
        expectancy = win_rate * avg_win - loss_rate * avg_loss
    elif avg_win is not None and not losses:
        expectancy = win_rate * avg_win
    elif avg_loss is not None and not wins:
        expectancy = -loss_rate * avg_loss
    break_even = None
    if avg_win and avg_loss:
        break_even = avg_loss / (avg_win + avg_loss)
    profit_factor = None
    loss_abs = abs(sum(trade.net_pnl for trade in losses))
    win_sum = sum(trade.net_pnl for trade in wins)
    if loss_abs > 0:
        profit_factor = win_sum / loss_abs
    elif win_sum > 0:
        profit_factor = None
    equity = [starting_equity]
    for trade in closed:
        equity.append(equity[-1] + trade.net_pnl)
    returns = [(equity[i] / equity[i - 1] - 1) for i in range(1, len(equity)) if equity[i - 1]]
    durations = [(trade.closed_at - trade.opened_at).total_seconds() for trade in closed]
    return {
        "trades": count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_profit": net_profit,
        "average_win": avg_win,
        "average_loss": avg_loss,
        "average_r": _mean(r_values),
        "expectancy_r": _mean(r_values),
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "max_drawdown": max_drawdown(equity),
        "sharpe": sharpe(returns),
        "sortino": sortino(returns),
        "average_trade_duration_s": _mean(durations),
        "fees_total": sum(trade.fees for trade in closed),
        "slippage_total": sum(trade.slippage for trade in closed),
        "funding_total": sum(trade.funding for trade in closed),
        "gross_pnl": gross_pnl,
        "net_pnl": net_profit,
        "break_even_win_rate": break_even,
    }


def slice_performance(trades: list[TradeRecord], starting_equity: float) -> dict:
    grouped: dict[str, list[TradeRecord]] = defaultdict(list)
    for trade in trades:
        grouped[trade.quantitative_regime or "unknown"].append(trade)
    return {key: summarize_trades(value, starting_equity) for key, value in grouped.items()}
