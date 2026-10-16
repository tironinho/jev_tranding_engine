"""Offline only: python scripts/compare_exit_policies.py INPUT.json OUTPUT.json.

Input: {source: "bid_ask"|"candle_close_proxy", slippage_bps: number,
        entries: [{entry: {...}, quotes: [{timestamp,bid,ask}, ...]}]}.
Required entry costs: entry_fee, exit_fee_rate, borrow_hourly_rate, initial_net_risk.
Proxy results are always unverified; never use them to select production parameters.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.analytics.exit_replay import compare_exits


def run(payload):
    if payload['source'] not in ('bid_ask', 'candle_close_proxy'):
        raise ValueError('unsupported quote source')
    rows = [{"decision_id": item['entry'].get('decision_id'),
             **compare_exits(item['entry'], item['quotes'], source=payload['source'],
                 slippage_bps=payload['slippage_bps'])} for item in payload['entries']]
    return dict(rows=rows, warning='Research only. Quotes do not guarantee full executable size. '
        'Candle proxies cannot validate missed targets or historical fills. '
        'Do not combine censored marks with simulated closed PNL; compare paired complete paths.')


if __name__ == '__main__':
    data = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8-sig'))
    Path(sys.argv[2]).write_text(json.dumps(run(data), ensure_ascii=False, indent=2), encoding='utf-8')
