from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import UUID

from app.config import RiskLimits
from app.domain.enums import (
    ENGINE_DISABLED,
    ENTRY_THROTTLED,
    STOP_COOLDOWN,
    EXISTING_POSITION,
    EXCHANGE_RULES_UNAVAILABLE,
    INSUFFICIENT_LIQUIDITY,
    INSUFFICIENT_MARGIN,
    LIVE_LOCKED,
    MAX_DAILY_DRAWDOWN,
    MAX_DAILY_LOSS,
    MAX_OPEN_POSITIONS,
    MAX_RISK_PER_TRADE,
    MAX_SYMBOL_EXPOSURE,
    MAX_TOTAL_EXPOSURE,
    NET_RR_TOO_LOW,
    ORDER_BELOW_MIN_NOTIONAL,
    PERSISTENCE_UNAVAILABLE,
    RISK_REJECTED,
    SPOT_SHORT_NOT_SUPPORTED,
    Action,
    MarketType,
)
from app.domain.mathutil import round_down_to_step
from app.domain.schemas import MarketSnapshot, RiskDecision, StrategyDecision, TradeEconomics
from app.execution.slippage import SlippageConfig, simulate_fill
from app.market.state import OrderBook
from app.risk.economics import (
    compute_trade_economics,
    funding_cashflow,
    funding_periods,
    plan_geometry,
    rate_for,
    size_quantity,
)


@dataclass
class RiskContext:
    equity: float
    cash: float
    day_start_equity: float
    realized_pnl_today: float
    open_positions: int
    symbol_exposure_notional: float
    total_exposure_notional: float
    has_position_on_symbol: bool
    last_entry_at: datetime | None
    now: datetime
    trading_enabled: bool
    persistence_ok: bool
    live_requested: bool
    live_armed: bool
    market_type: str
    step_size: float | None = None
    rules_required: bool = False
    last_stop_at: datetime | None = None


@dataclass
class FeeQuote:
    maker: float
    taker: float
    source: str


class RiskEngine:
    def __init__(self, limits: RiskLimits, slippage: SlippageConfig) -> None:
        self.limits = limits
        self.slippage = slippage

    def update_limits(self, **changes: float | int) -> None:
        data = self.limits.__dict__.copy()
        for key, value in changes.items():
            if key not in data:
                raise KeyError(key)
            data[key] = value
        self.limits = RiskLimits(**data)

    def evaluate(
        self,
        decision: StrategyDecision,
        snapshot: MarketSnapshot,
        context: RiskContext,
        fees: FeeQuote,
        book: OrderBook | None,
        extra_slot: bool = False,
    ) -> RiskDecision:
        reasons: list[str] = []
        if decision.action is Action.NO_TRADE:
            return self._reject(decision, ["NO_TRADE"])
        if not context.trading_enabled:
            reasons.append(ENGINE_DISABLED)
        if decision.mode.value == "live" or context.live_requested:
            if not context.live_armed:
                reasons.append(LIVE_LOCKED)
            if not context.persistence_ok:
                reasons.append(PERSISTENCE_UNAVAILABLE)
            if context.rules_required and not context.step_size:
                reasons.append(EXCHANGE_RULES_UNAVAILABLE)
        elif decision.mode.value == "paper" and not context.persistence_ok:
            reasons.append(PERSISTENCE_UNAVAILABLE)
        if context.market_type == MarketType.SPOT.value and decision.action is Action.SHORT:
            reasons.append(SPOT_SHORT_NOT_SUPPORTED)
        if context.has_position_on_symbol and not self.limits.allow_pyramiding:
            reasons.append(EXISTING_POSITION)
        if context.last_entry_at is not None:
            elapsed = (context.now - context.last_entry_at).total_seconds()
            if elapsed < self.limits.min_entry_interval_seconds:
                reasons.append(ENTRY_THROTTLED)
        if context.last_stop_at is not None:
            since_stop = (context.now - context.last_stop_at).total_seconds()
            if since_stop < self.limits.stop_cooldown_minutes * 60:
                reasons.append(STOP_COOLDOWN)
        if context.day_start_equity > 0:
            drawdown = (context.day_start_equity - context.equity) / context.day_start_equity
            if drawdown >= self.limits.max_daily_drawdown:
                reasons.append(MAX_DAILY_DRAWDOWN)
            if context.realized_pnl_today <= -abs(self.limits.max_daily_loss) * context.day_start_equity:
                reasons.append(MAX_DAILY_LOSS)
        cap = self.limits.max_open_positions + (1 if extra_slot else 0)
        if context.open_positions >= cap:
            reasons.append(MAX_OPEN_POSITIONS)
        if reasons:
            return self._reject(decision, reasons)

        side_book = "BUY" if decision.action is Action.LONG else "SELL"
        probe = simulate_fill(
            side=side_book,
            quantity=1,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            book=book,
            config=self.slippage,
        )
        if probe.estimated_fill_price <= 0:
            return self._reject(decision, [INSUFFICIENT_LIQUIDITY])
        entry_guess = probe.estimated_fill_price
        geometry = plan_geometry(decision.action, entry_guess, snapshot.features, self.limits)
        if isinstance(geometry, str):
            return self._reject(decision, [geometry])

        entry_rate = rate_for(self._entry_liquidity(), fees.maker, fees.taker)
        exit_rate = rate_for(self._exit_liquidity(), fees.maker, fees.taker)
        exit_slip = entry_guess * (max(probe.slippage_bps, 0) / 10_000)
        ideal = size_quantity(
            equity=context.equity,
            risk_fraction=min(self.limits.risk_per_trade, self.limits.max_risk_per_trade),
            entry=entry_guess,
            stop=geometry.stop,
            entry_fee_rate=entry_rate,
            exit_fee_rate=exit_rate,
            exit_slippage_per_unit=exit_slip,
        )
        if context.step_size:
            ideal = round_down_to_step(ideal, context.step_size)
        if ideal <= 0:
            return self._reject(decision, [MAX_RISK_PER_TRADE])
        qty = self._fit_to_capital(ideal, entry_guess, context, entry_rate)
        if qty <= 0:
            return self._reject(decision, [self._capital_reason(context, entry_rate)])

        preview = self._fill(side_book, qty, snapshot, book)
        if preview is None:
            return self._reject(decision, [INSUFFICIENT_LIQUIDITY])
        entry = preview.estimated_fill_price
        refit = self._fit_to_capital(preview.filled_quantity, entry, context, entry_rate)
        if refit <= 0:
            return self._reject(decision, [self._capital_reason(context, entry_rate)])
        if refit + 1e-12 < preview.filled_quantity:
            preview = self._fill(side_book, refit, snapshot, book)
            if preview is None:
                return self._reject(decision, [INSUFFICIENT_LIQUIDITY])
            entry = preview.estimated_fill_price
        qty = preview.filled_quantity
        if entry * abs(qty) < self.limits.min_order_notional:
            return self._reject(decision, [ORDER_BELOW_MIN_NOTIONAL])
        # Re-plan geometry off the actual estimated entry so stop distance matches the fill.
        geometry = plan_geometry(decision.action, entry, snapshot.features, self.limits)
        if isinstance(geometry, str):
            return self._reject(decision, [geometry])

        notional = entry * qty
        periods = funding_periods(self.limits)
        funding_rate = snapshot.features.get("funding_rate")
        funding_rate_f = float(funding_rate) if isinstance(funding_rate, (int, float)) else None
        funding = funding_cashflow(decision.action, funding_rate_f, notional, periods)
        extra_spread = 0.0
        extra_slip = 0.0
        included = preview.model in {SlippageModelNameValue.ORDERBOOK, SlippageModelNameValue.SPREAD}
        if preview.model == "fixed_bps":
            included = False
        slip_per_unit = entry * max(preview.slippage_bps, 0) / 10_000
        economics = compute_trade_economics(
            side=decision.action,
            entry=entry,
            stop=geometry.stop,
            target=geometry.target,
            quantity=qty,
            entry_fee_rate=entry_rate,
            exit_fee_rate=exit_rate,
            exit_slippage_per_unit=slip_per_unit,
            funding_cashflow_total=funding,
            fee_source=fees.source,
            spread_cost=extra_spread,
            slippage_cost=extra_slip,
            spread_included_in_fill=included or preview.model != "fixed_bps",
        )
        if economics.net_rr is None or economics.net_rr < self.limits.min_net_rr:
            return self._reject(
                decision,
                [NET_RR_TOO_LOW],
                economics=economics,
                details={"geometry_reasons": list(geometry.reasons)},
            )
        if economics.net_risk > context.equity * self.limits.max_risk_per_trade + 1e-6:
            return self._reject(decision, [MAX_RISK_PER_TRADE], economics=economics)

        projected_symbol = context.symbol_exposure_notional + notional
        projected_total = context.total_exposure_notional + notional
        if context.equity <= 0 or self._leverage() <= 0:
            return self._reject(decision, [INSUFFICIENT_MARGIN], economics=economics)
        if projected_symbol > self._notional_ceiling(context.equity, self.limits.max_symbol_exposure) + 1e-6:
            return self._reject(decision, [MAX_SYMBOL_EXPOSURE], economics=economics)
        if projected_total > self._notional_ceiling(context.equity, self.limits.max_total_exposure) + 1e-6:
            return self._reject(decision, [MAX_TOTAL_EXPOSURE], economics=economics)
        if notional > context.cash * self._leverage() + 1e-6:
            return self._reject(decision, [INSUFFICIENT_MARGIN], economics=economics)

        return RiskDecision(
            decision_id=decision.decision_id,
            correlation_id=decision.correlation_id,
            accepted=True,
            reject_reasons=[],
            economics=economics,
            side=decision.action,
            details={
                "geometry_reasons": list(geometry.reasons),
                "extra_open_slot": bool(extra_slot and context.open_positions >= self.limits.max_open_positions),
                "quantity_capped": qty + 1e-12 < ideal,
                "slippage_model": preview.model,
                "expected_price": preview.expected_price,
                "slippage_bps": preview.slippage_bps,
                "warnings": preview.warnings,
                "preview_warnings": preview.warnings,
            },
        )

    def _fill(self, side: str, quantity: float, snapshot: MarketSnapshot, book: OrderBook | None):
        preview = simulate_fill(
            side=side,
            quantity=quantity,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            book=book,
            config=self.slippage,
        )
        if not preview.fully_filled and not self.limits.allow_partial_entry:
            return None
        if preview.filled_quantity <= 0 or preview.estimated_fill_price <= 0:
            return None
        return preview

    def _leverage(self) -> float:
        return max(self.limits.max_leverage, 0.0)

    def _notional_ceiling(self, equity: float, fraction: float) -> float:
        return equity * fraction * self._leverage()

    def _capital_room(self, context: RiskContext, entry_fee_rate: float) -> tuple[float, str]:
        if context.equity <= 0 or context.cash <= 0 or self._leverage() <= 0:
            return 0.0, INSUFFICIENT_MARGIN
        symbol_room = self._notional_ceiling(context.equity, self.limits.max_symbol_exposure) - context.symbol_exposure_notional
        total_room = self._notional_ceiling(context.equity, self.limits.max_total_exposure) - context.total_exposure_notional
        cash_room = context.cash * self._leverage() / (1 + max(entry_fee_rate, 0.0))
        room, reason = min(
            (
                (symbol_room, MAX_SYMBOL_EXPOSURE),
                (total_room, MAX_TOTAL_EXPOSURE),
                (cash_room, INSUFFICIENT_MARGIN),
            ),
            key=lambda item: item[0],
        )
        if room <= 0:
            return 0.0, reason
        return room, reason

    def _capital_reason(self, context: RiskContext, entry_fee_rate: float) -> str:
        return self._capital_room(context, entry_fee_rate)[1]

    def _fit_to_capital(self, qty: float, entry: float, context: RiskContext, entry_fee_rate: float) -> float:
        room, _reason = self._capital_room(context, entry_fee_rate)
        if entry <= 0 or room <= 0:
            return 0.0
        fitted = min(qty, room / entry)
        if context.step_size:
            fitted = round_down_to_step(fitted, context.step_size)
        return fitted

    def _entry_liquidity(self) -> str:
        return "taker" if self.limits.order_style == "market" else "maker"

    def _exit_liquidity(self) -> str:
        return "taker"

    def _reject(
        self,
        decision: StrategyDecision,
        reasons: list[str],
        economics: TradeEconomics | None = None,
        details: dict | None = None,
    ) -> RiskDecision:
        payload = {"risk_rejected": True}
        if details:
            payload.update(details)
        return RiskDecision(
            decision_id=decision.decision_id,
            correlation_id=decision.correlation_id,
            accepted=False,
            reject_reasons=[RISK_REJECTED, *reasons] if RISK_REJECTED not in reasons else reasons,
            economics=economics,
            side=decision.action,
            details=payload,
        )


class SlippageModelNameValue:
    ORDERBOOK = "orderbook_based"
    SPREAD = "spread_based"
