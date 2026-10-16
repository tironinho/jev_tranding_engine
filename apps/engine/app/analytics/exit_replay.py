"""Paired exit research on ordered bid/ask observations. No live execution imports."""
from dataclasses import dataclass
from datetime import datetime
import math


@dataclass(frozen=True)
class ExitPolicy:
    name: str
    max_minutes: float = 0
    protect_at_r: float | None = None
    trail_r: float | None = None


POLICIES = (ExitPolicy("control"), ExitPolicy("time_30m", 30), ExitPolicy("time_60m", 60),
            ExitPolicy("protect_1r_time_60m", 60, 1.0, .5))


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    if not isinstance(result, datetime) or result.tzinfo is None:
        raise ValueError("timezone-aware timestamp required")
    return result


def number(value, name, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"invalid {name}")
    if value < 0 or (positive and value == 0):
        raise ValueError(f"invalid {name}")
    return value


def compare_exits(entry, quotes, *, source, slippage_bps, max_gap_seconds=5, policies=POLICIES):
    """Use executable side, entry/exit fees and hourly interest; gaps never become verified fills.

    Research assumes full size at top of book plus explicit adverse slippage.
    It does not infer quotes from candle highs/lows or claim depth/latency simulation.
    Censored paths retain mark PNL separately from realized simulated results.
    """
    opened = timestamp(entry['opened_at'])
    if entry['side'] not in ('LONG', 'SHORT'):
        raise ValueError('invalid side')
    sign = 1 if entry['side'] == 'LONG' else -1
    price = number(entry['entry_price'], 'entry_price', True)
    qty = number(entry['quantity'], 'quantity', True)
    fee = number(entry['entry_fee'], 'entry_fee')
    rate = number(entry['exit_fee_rate'], 'exit_fee_rate')
    borrow = number(entry['borrow_hourly_rate'], 'borrow_hourly_rate')
    risk = number(entry['initial_net_risk'], 'initial_net_risk', True)
    stop = number(entry['stop'], 'stop', True)
    target = number(entry['target'], 'target', True)
    if sign * (price-stop) <= 0 or sign * (target-price) <= 0 or rate >= 1:
        raise ValueError('invalid geometry or fee rate')
    slip = number(slippage_bps, 'slippage_bps') / 10000
    number(max_gap_seconds, 'max_gap_seconds', True)
    if slip >= 1:
        raise ValueError('slippage must be below 10000 bps')
    points = []
    previous = opened
    gap = 0.
    for row in quotes:
        when = timestamp(row['timestamp'])
        bid, ask = number(row['bid'], 'bid', True), number(row['ask'], 'ask', True)
        if bid > ask:
            raise ValueError('crossed quote')
        if when <= opened:
            continue
        if when < previous:
            raise ValueError('quotes out of order')
        gap = max(gap, (when-previous).total_seconds())
        points.append((when, bid if sign == 1 else ask, gap))
        previous = when

    def net(quote, minutes):
        execution = quote*(1-sign*slip)
        interest = price*qty*borrow*max(1, math.ceil(minutes/60))
        return sign*(execution-price)*qty-fee-execution*qty*rate-interest

    rows = []
    for policy in policies:
        if policy.max_minutes < 0 or (policy.protect_at_r is not None and policy.protect_at_r < 0):
            raise ValueError('invalid policy')
        if policy.trail_r is not None and (policy.protect_at_r is None or policy.trail_r < 0):
            raise ValueError('trailing requires protection trigger')
        peak = float('-inf')
        floor = None
        result = dict(policy=policy.name, status='CENSORED', reason=None, net_pnl=None,
                      mark_net_pnl=None, held_minutes=None, exit_at=None, max_gap_seconds=0,
                      coverage_complete=False, peak_net_pnl=None, giveback=None)
        for when, quote, observed_gap in points:
            minutes = (when-opened).total_seconds()/60
            value = net(quote, minutes)
            reason = ('STOP' if sign*(quote-stop) <= 0 else
                      'TARGET' if sign*(quote-target) >= 0 else
                      'PROFIT_PROTECTION' if floor is not None and value <= floor else
                      'TIME' if policy.max_minutes and minutes >= policy.max_minutes else None)
            peak = max(peak, value)
            result.update(mark_net_pnl=value, held_minutes=minutes, max_gap_seconds=observed_gap,
                          coverage_complete=source=='bid_ask' and observed_gap <= max_gap_seconds,
                          peak_net_pnl=peak, giveback=peak-value)
            if reason:
                result.update(status='SIMULATED_EXIT', reason=reason, net_pnl=value, exit_at=when.isoformat())
                break
            # The new floor can only trigger on a subsequent observation.
            if policy.protect_at_r is not None and peak >= policy.protect_at_r*risk:
                floor = max(floor or 0, 0 if policy.trail_r is None else peak-policy.trail_r*risk)
        rows.append(result)
    return dict(source=source, assumptions=dict(slippage_bps=slippage_bps,
        max_gap_seconds=max_gap_seconds, depth_simulated=False, latency_simulated=False,
        interest='entry_notional_hourly_rounded_up', paired_entries=True), results=rows)
