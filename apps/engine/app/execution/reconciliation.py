"""Read-only comparison: balances are exposures, not invented strategy trades."""
from __future__ import annotations


def reconcile(balance: dict, positions: list[dict], marks: dict, rules: dict) -> dict:
    if balance.get("status") != "ok":
        return {"status": "UNAVAILABLE", "blocks_entry": True, "rows": [],
                "detail": balance.get("detail") or balance.get("status")}
    tracked: dict[str, float] = {}
    for position in positions:
        if position.get("mode") == "live":
            symbol = position["symbol"]
            tracked[symbol] = tracked.get(symbol, 0) + position["quantity"] * (1 if position["side"] == "LONG" else -1)
    assets = {row["asset"]: row for row in balance.get("assets", [])}
    for symbol in tracked:
        assets.setdefault(symbol.removesuffix("USDT"), {"asset": symbol.removesuffix("USDT"), "total": 0})
    rows = []
    for asset, row in assets.items():
        symbol = f"{asset}USDT"
        price = 1.0 if asset == "USDT" else marks.get(symbol)
        actual = float(row.get("total") or 0)
        expected = tracked.get(symbol, 0)
        # Interest is a liability separate from strategy principal.
        delta = actual + float(row.get("interest") or 0) - expected
        value = abs(delta * price) if price else None
        minimum = float((rules.get(symbol) or {}).get("min_notional") or 5)
        status = "CASH" if asset == "USDT" else (
            "UNPRICED" if price is None else
            "MATCHED" if abs(delta) <= 1e-12 else
            "RESIDUAL" if value < minimum else "MISMATCH")
        rows.append({**row, "symbol": symbol, "mark": price, "net_quantity": actual,
                     "value_usdt": actual * price if price else None,
                     "tracked_quantity": expected, "difference": delta, "status": status})
    blocked = any(row["status"] in {"MISMATCH", "UNPRICED"} for row in rows)
    return {"status": "MISMATCH" if blocked else "ok", "blocks_entry": blocked,
            "observed_at": balance.get("observed_at"), "rows": rows}
