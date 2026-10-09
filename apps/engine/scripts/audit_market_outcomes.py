"""Reproduce a read-only audit from exported decisions and Binance klines.

Usage: python scripts/audit_market_outcomes.py EXPORT_DIRECTORY OUTPUT_JSON
Requires trades.json, <decision_id>.json and <decision_id>-klines.json.
Does not access exchange credentials or place orders.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.analytics.barriers import observed_barriers


def audit(directory):
    trades = json.loads((directory / 'trades.json').read_text(encoding='utf-8'))['rows']
    rows = []
    for trade in sorted(trades, key=lambda t: t['opened_at']):
        identity = trade['decision_id']
        detail = json.loads((directory / f'{identity}.json').read_text(encoding='utf-8'))
        bars = json.loads((directory / f'{identity}-klines.json').read_text(encoding='utf-8'))
        decision = detail['decision']
        metadata = decision.get('metadata') or {}
        economics = (detail.get('risk') or {}).get('economics') or {}
        opened = datetime.fromisoformat(trade['opened_at'].replace('Z','+00:00'))
        closed = datetime.fromisoformat(trade['closed_at'].replace('Z','+00:00'))
        start = opened.timestamp() * 1000
        rr = economics.get('net_rr')
        probability = metadata.get('jev_continuation')
        breakeven = 1 / (1 + rr) if rr and rr > 0 else None
        labels = detail.get('labels') or {}
        direction = 1 if trade['side'] == 'LONG' else -1
        rows.append({
            'decision_id': identity, 'symbol': trade['symbol'], 'side': trade['side'],
            'opened_at': trade['opened_at'], 'held_minutes': (closed-opened).total_seconds()/60,
            'net_pnl': trade['net_pnl'], 'recorded_exit': trade['exit_reason'],
            'jev_probability': probability, 'break_even_probability': breakeven,
            'below_economic_floor': probability < breakeven if probability is not None and breakeven is not None else None,
            'rule_version': metadata.get('combination_rule_version'),
            'directional_return_15m_label': direction * labels['future_return_15m'] if labels.get('future_return_15m') is not None else None,
            **observed_barriers(bars, start_ms=start, end_ms=start+3600000, entry=trade['entry_price'],
                stop=economics.get('stop',trade['stop']), target=economics.get('target',trade['target']), side=trade['side'])})
    return {'method': 'Next complete 1m bars within 60m, fixed original barriers. Not an execution backtest or calibration.',
            'gross': sum(t['gross_pnl'] for t in trades), 'fees': sum(t['fees'] for t in trades),
            'net': sum(t['net_pnl'] for t in trades), 'rows': rows}


if __name__ == '__main__':
    result = audit(Path(sys.argv[1]))
    Path(sys.argv[2]).write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f"Audited {len(result['rows'])} trades; output: {sys.argv[2]}")
