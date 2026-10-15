from __future__ import annotations

from dataclasses import dataclass, field
import math
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
    INSUFFICIENT_PROTECTION_BALANCE,
    LIVE_LONG_DISABLED,
    LIVE_LOCKED,
    MAX_CONSECUTIVE_LIVE_LOSSES,
    MAX_DAILY_DRAWDOWN,
    MAX_DAILY_LOSS,
    MAX_OPEN_POSITIONS,
    MAX_RISK_PER_TRADE,
    MAX_SYMBOL_EXPOSURE,
    MAX_TOTAL_EXPOSURE,
    MARGIN_LEVEL,
    NET_RR_TOO_LOW,
    ORDER_BELOW_MIN_NOTIONAL,
    PERSISTENCE_UNAVAILABLE,
    RISK_REJECTED,
    SPOT_SHORT_NOT_SUPPORTED,
    Action,
    MarketType,
)
from app.domain.mathutil import round_down_to_step, round_up_to_step
from app.risk.executable import minimum_executable_quantity, minimum_trade_room
from app.domain.schemas import MarketSnapshot, RiskDecision, StrategyDecision, TradeEconomics
from app.execution.slippage import SlippageConfig, simulate_fill
from app.market.state import OrderBook
from app.risk.economics import (
    compute_trade_economics,
    funding_cashflow,
    funding_periods,
    plan_geometry,
    Geometry,
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
    min_notional: float | None = None
    margin_level: float | None = None
    rules_required: bool = False
    last_stop_at: datetime | None = None
    open_stop_risk: float = 0.0
    quote_free: float | None = None
    consecutive_live_losses: int = 0
    position_sides_on_symbol: tuple[Action, ...] = ()
    same_side_entry_prices: tuple[float, ...] = ()


@dataclass
class FeeQuote:
    maker: float
    taker: float
    source: str


def margin_borrow_room(equity: float, margin_level: float | None, min_level: float) -> float:
    """Quote still borrowable on cross margin without crossing the level floor.

    Equity is assets minus liabilities. A full new borrow of this size lands on min_level.
    USDT net can be negative while the coins are the collateral; that net is not the room.
    """
    if equity <= 0 or min_level <= 1:
        return 0.0
    if margin_level is None:
        return equity / (min_level - 1)
    if margin_level <= min_level:
        return 0.0
    span = margin_level - 1
    assets = equity * margin_level / span
    liabilities = equity / span
    return max(0.0, (assets - min_level * liabilities) / (min_level - 1))


def _size_scale(decision) -> float:
    """1 keeps the planned risk. A break with soft continuation passes a smaller fraction."""
    raw = (getattr(decision, "metadata", None) or {}).get("size_scale")
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return 1.0
    if not math.isfinite(raw) or raw <= 0:
        return 0.0
    return min(float(raw), 1.0)


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
            if (
                decision.strategy == "baseline_jev"
                and decision.action is Action.LONG
                and not self.limits.live_longs_enabled
            ):
                reasons.append(LIVE_LONG_DISABLED)
            if not context.live_armed:
                reasons.append(LIVE_LOCKED)
            if not context.persistence_ok:
                reasons.append(PERSISTENCE_UNAVAILABLE)
            if context.rules_required and not context.step_size:
                reasons.append(EXCHANGE_RULES_UNAVAILABLE)
            if context.margin_level is not None and context.margin_level < self.limits.min_margin_level:
                reasons.append(MARGIN_LEVEL)
            if context.consecutive_live_losses >= self.limits.max_consecutive_live_losses:
                reasons.append(MAX_CONSECUTIVE_LIVE_LOSSES)
        elif decision.mode.value == "paper" and not context.persistence_ok:
            reasons.append(PERSISTENCE_UNAVAILABLE)
        if context.market_type == MarketType.SPOT.value and decision.action is Action.SHORT:
            reasons.append(SPOT_SHORT_NOT_SUPPORTED)
        if context.has_position_on_symbol and not self.limits.allow_pyramiding:
            reasons.append(EXISTING_POSITION)
        elif context.has_position_on_symbol and (decision.mode.value == "live" or context.live_requested):
            # The current futures adapter uses close-all stops. Margin auto
            # repayment would net opposite legs instead of funding an owned
            # independent position. Same-side margin entries remain independent.
            if context.market_type == "futures":
                reasons.append("FUTURES_CLOSE_ALL_POSITION")
            elif any(side is not decision.action for side in context.position_sides_on_symbol):
                reasons.append("OPPOSITE_POSITION_NETTING")
        from app.strategies.context_policy import classify_entry
        selection = classify_entry(decision.action, snapshot.features)
        repeat_reasons = []
        if context.has_position_on_symbol:
            direction = 1 if decision.action is Action.LONG else -1
            atr = snapshot.features.get("atr")
            elapsed = ((context.now - context.last_entry_at).total_seconds() / 60
                       if context.last_entry_at else 0)
            event = (snapshot.features.get("breakout" if direction == 1 else "breakdown") is True
                     or snapshot.features.get("bullish_reclaim" if direction == 1 else "bearish_reclaim") is True)
            progressed = (isinstance(atr, (float, int)) and math.isfinite(atr) and atr > 0
                          and bool(context.same_side_entry_prices)
                          and all(direction * (snapshot.price - price) >= atr * self.limits.repeat_entry_atr_distance
                                  for price in context.same_side_entry_prices))
            if not event or not progressed or elapsed < self.limits.repeat_entry_interval_minutes:
                repeat_reasons.append("REPEATED_ENTRY_WITHOUT_NEW_SETUP")
        selection.update(mode=self.limits.entry_selection_mode, repeat_reasons=repeat_reasons,
                         would_accept=selection["eligible"] and not repeat_reasons)
        decision.metadata["entry_selection"] = selection
        if self.limits.entry_selection_mode == "enforce":
            reasons.extend(selection["reasons"])
            reasons.extend(repeat_reasons)
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
        if self.limits.max_open_positions > 0:
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
        plan = decision.metadata.get("jev_trade_plan")
        if plan:
            stop, target = float(plan["stop"]), float(plan["target"])
            candidate_entry = entry_guess
            valid = stop < candidate_entry < target if decision.action is Action.LONG else target < candidate_entry < stop
            if not valid or abs(candidate_entry / float(plan["entry"]) - 1) > 0.001:
                return self._reject(decision, ["JEV_PLAN_CHANGED"], details={
                    "assessed_entry": plan["entry"], "estimated_entry": candidate_entry,
                    "price_drift": abs(candidate_entry / float(plan["entry"]) - 1),
                    "max_price_drift": 0.001, "stop": stop, "target": target,
                })
            geometry = Geometry(stop=stop, target=target, reasons=("JEV_ASSESSED_PLAN",))

        entry_rate = rate_for(self._entry_liquidity(), fees.maker, fees.taker)
        exit_rate = rate_for(self._exit_liquidity(), fees.maker, fees.taker)
        stress_bps = float((plan or {}).get("stress_bps", 0))
        interest_per_unit = float((plan or {}).get("interest_estimate_per_unit", 0))
        exit_slip = entry_guess * (max(probe.slippage_bps, stress_bps, 0) / 10_000) + interest_per_unit
        ideal = size_quantity(
            equity=context.equity,
            risk_fraction=min(self.limits.risk_per_trade, self.limits.max_risk_per_trade) * _size_scale(decision),
            entry=entry_guess,
            stop=geometry.stop,
            entry_fee_rate=entry_rate,
            exit_fee_rate=exit_rate,
            exit_slippage_per_unit=exit_slip,
        )
        if context.step_size:
            ideal = round_down_to_step(ideal, context.step_size)
        protection_cap = self._protection_quantity_cap(
            decision.action, entry_guess, geometry.stop, exit_rate, context
        )
        if protection_cap is not None:
            if protection_cap <= 0:
                return self._reject(
                    decision,
                    [INSUFFICIENT_PROTECTION_BALANCE],
                    details=self._protection_details(context, entry_guess, geometry.stop, exit_rate, 0),
                )
            ideal = min(ideal, protection_cap)
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
        protection_cap = self._protection_quantity_cap(
            decision.action, entry, geometry.stop, exit_rate, context
        )
        if protection_cap is not None:
            refit = min(refit, protection_cap)
            if context.step_size:
                refit = round_down_to_step(refit, context.step_size)
        if refit <= 0:
            return self._reject(decision, [self._capital_reason(context, entry_rate)])
        if refit + 1e-12 < preview.filled_quantity:
            preview = self._fill(side_book, refit, snapshot, book)
            if preview is None:
                return self._reject(decision, [INSUFFICIENT_LIQUIDITY])
            entry = preview.estimated_fill_price
        qty = preview.filled_quantity
        floor = self._min_notional(context)
        needed = self._needed_quantity(entry, geometry, decision.action, floor, entry_rate, context)
        if needed is not None and qty + 1e-12 < needed:
            # The per-trade fraction may rise to this exchange minimum. The
            # stop-risk limit, portfolio budget, symbol cap and cash do not.
            blocked = self._reject_unfunded_minimum(
                decision, context, needed, qty, entry, geometry.stop, floor, entry_rate, exit_rate, protection_cap
            )
            if blocked is not None:
                return blocked
            lifted = self._lift_to_notional(needed, entry, geometry.stop, context, entry_rate, exit_rate, side_book, snapshot, book)
            if lifted is None:
                room, limiting_reason = self._capital_room(
                    context, entry_rate, minimum_trade_notional=entry * needed
                )
                return self._reject(decision, [ORDER_BELOW_MIN_NOTIONAL], details={
                    "order_notional": entry * abs(qty), "required_notional": entry * needed,
                    "available_notional": room, "capital_limiting_reason": limiting_reason,
                })
            preview, entry = lifted
            qty = preview.filled_quantity
        # Re-plan geometry off the actual estimated entry so stop distance matches the fill.
        geometry = plan_geometry(decision.action, entry, snapshot.features, self.limits)
        if isinstance(geometry, str):
            return self._reject(decision, [geometry])
        plan = decision.metadata.get("jev_trade_plan")
        if plan:
            stop, target = float(plan["stop"]), float(plan["target"])
            candidate_entry = entry
            valid = stop < candidate_entry < target if decision.action is Action.LONG else target < candidate_entry < stop
            if not valid or abs(candidate_entry / float(plan["entry"]) - 1) > 0.001:
                return self._reject(decision, ["JEV_PLAN_CHANGED"], details={
                    "assessed_entry": plan["entry"], "estimated_entry": candidate_entry,
                    "price_drift": abs(candidate_entry / float(plan["entry"]) - 1),
                    "max_price_drift": 0.001, "stop": stop, "target": target,
                })
            geometry = Geometry(stop=stop, target=target, reasons=("JEV_ASSESSED_PLAN",))

        notional = entry * qty
        if context.live_requested and context.market_type in {MarketType.MARGIN.value, "spot"}:
            # Entry alone passing NOTIONAL is insufficient: the exit and the
            # resting stop-limit must remain executable after fees and rounding.
            exit_quantity = qty * (1 - entry_rate if decision.action is Action.LONG else 1)
            if context.step_size:
                exit_quantity = round_down_to_step(exit_quantity, context.step_size)
            lowest_price = min(entry, geometry.target, geometry.stop * 0.998)
            exit_notional = exit_quantity * lowest_price
            if exit_notional + 1e-9 < floor * 1.01:
                needed = minimum_executable_quantity(
                    entry, geometry.stop, geometry.target, decision.action, floor, entry_rate, context.step_size
                )
                if needed is not None and qty + 1e-12 < needed:
                    blocked = self._reject_unfunded_minimum(
                        decision, context, needed, qty, entry, geometry.stop, floor, entry_rate, exit_rate, protection_cap
                    )
                    if blocked is not None:
                        return blocked
                    lifted = self._lift_to_notional(
                        needed, entry, geometry.stop, context, entry_rate, exit_rate, side_book, snapshot, book
                    )
                    if lifted is not None:
                        preview, candidate_entry = lifted
                        plan = decision.metadata.get("jev_trade_plan")
                        if plan and abs(candidate_entry / float(plan["entry"]) - 1) > 0.001:
                            return self._reject(decision, ["JEV_PLAN_CHANGED"], details={
                                "assessed_entry": plan["entry"], "estimated_entry": candidate_entry,
                                "price_drift": abs(candidate_entry / float(plan["entry"]) - 1),
                                "max_price_drift": 0.001, "stop": geometry.stop, "target": geometry.target,
                            })
                        entry, qty = candidate_entry, preview.filled_quantity
                        exit_quantity = qty * (1 - entry_rate if decision.action is Action.LONG else 1)
                        if context.step_size:
                            exit_quantity = round_down_to_step(exit_quantity, context.step_size)
                        lowest_price = min(entry, geometry.target, geometry.stop * 0.998)
                        exit_notional = exit_quantity * lowest_price
                if exit_notional + 1e-9 < floor * 1.01:
                    return self._reject(decision, ["EXIT_BELOW_MIN_NOTIONAL"], details={
                        "exit_notional": exit_notional, "required_exit_notional": floor * 1.01,
                    })
        # A larger minimum order can fill at a different book level. Recheck
        # budgets at that price and use its final notional in all economics.
        notional = entry * qty
        required_qty = self._needed_quantity(entry, geometry, decision.action, floor, entry_rate, context)
        minimum_notional = entry * required_qty if required_qty is not None else 0.0
        room, limiting_reason = self._capital_room(context, entry_rate, minimum_notional)
        if notional > room + 1e-6:
            return self._reject(decision, [limiting_reason], details={
                "order_notional": notional, "available_notional": room,
                "required_notional": minimum_notional,
            })
        periods = funding_periods(self.limits)
        funding_rate = snapshot.features.get("funding_rate")
        funding_rate_f = float(funding_rate) if isinstance(funding_rate, (int, float)) else None
        funding = funding_cashflow(decision.action, funding_rate_f, notional, periods)
        if context.market_type == MarketType.MARGIN.value:
            funding = -interest_per_unit * qty
        extra_spread = 0.0
        extra_slip = 0.0
        included = preview.model in {SlippageModelNameValue.ORDERBOOK, SlippageModelNameValue.SPREAD}
        if preview.model == "fixed_bps":
            included = False
        slip_per_unit = entry * max(preview.slippage_bps, stress_bps, 0) / 10_000
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
        if self.limits.min_net_rr > 0 and (economics.net_rr is None or economics.net_rr + 1e-12 < self.limits.min_net_rr):
            return self._reject(decision, [NET_RR_TOO_LOW], economics=economics,
                                details={"geometry_reasons": list(geometry.reasons)})
        net_target_return = economics.net_reward / notional if notional > 0 else 0.0
        if net_target_return + 1e-12 < self.limits.min_net_target_return:
            return self._reject(decision, ["NET_TARGET_RETURN_TOO_LOW"], economics=economics,
                                details={"net_target_return": net_target_return,
                                         "required_net_target_return": self.limits.min_net_target_return})
        if economics.net_risk > context.equity * self.limits.max_risk_per_trade + 1e-6:
            return self._reject(decision, [MAX_RISK_PER_TRADE], economics=economics)
        if context.open_stop_risk + economics.net_risk > context.equity * self.limits.max_portfolio_stop_risk + 1e-6:
            return self._reject(decision, ["MAX_PORTFOLIO_STOP_RISK"], economics=economics,
                                details={"open_stop_risk": context.open_stop_risk})

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
                "entry_selection": selection,
                "role_baseline": decision.metadata.get("role_baseline"),
                "geometry_reasons": list(geometry.reasons),
                "extra_open_slot": bool(self.limits.max_open_positions > 0 and extra_slot
                    and context.open_positions >= self.limits.max_open_positions),
                "quantity_capped": qty + 1e-12 < ideal,
                "notional_fraction": notional / context.equity,
                "minimum_allocation_used": (
                    self.limits.max_trade_notional_fraction > 0
                    and notional > self._buying_power(context.equity) * self.limits.max_trade_notional_fraction + 1e-6
                ),
                "minimum_executable_notional": minimum_notional,
                "max_minimum_trade_notional_fraction": self.limits.max_minimum_trade_notional_fraction,
                "net_target_return": net_target_return,
                "capital_limiting_reason": self._capital_reason(context, entry_rate),
                "slippage_model": preview.model,
                "expected_price": preview.expected_price,
                "slippage_bps": preview.slippage_bps,
                "warnings": preview.warnings,
                "preview_warnings": preview.warnings,
                "protection_quote": self._protection_details(
                    context, entry, geometry.stop, exit_rate, qty
                ) if protection_cap is not None else None,
            },
        )

    def _protection_quantity_cap(
        self,
        action: Action,
        entry: float,
        stop: float,
        exit_fee_rate: float,
        context: RiskContext,
    ) -> float | None:
        """Maximum margin short whose resting buy-stop can reserve real quote.

        A short sale credits quote at the entry price. The stop-limit reserves
        quote at 20 bps above its trigger. Only that price gap, exit fee and a
        fixed cash reserve may consume quote that existed before the entry.
        """
        if (
            not context.live_requested
            or context.market_type != MarketType.MARGIN.value
            or action is not Action.SHORT
        ):
            return None
        if context.quote_free is None:
            return 0.0
        available = max(0.0, context.quote_free - self.limits.min_protection_quote_reserve)
        if available <= 0:
            return 0.0
        stop_limit = stop * 1.002
        stressed_stop = stop_limit * (
            1 + max(exit_fee_rate, 0.0) + self.limits.plan_stress_bps / 10_000
        )
        gap = stressed_stop - entry
        if gap <= 0:
            return float("inf")
        return available / gap

    def _protection_details(
        self,
        context: RiskContext,
        entry: float,
        stop: float,
        exit_fee_rate: float,
        quantity: float,
    ) -> dict:
        stop_limit = stop * 1.002
        required = quantity * stop_limit * (
            1 + max(exit_fee_rate, 0.0) + self.limits.plan_stress_bps / 10_000
        )
        proceeds = quantity * entry
        return {
            "quote_free_before_entry": context.quote_free,
            "minimum_quote_reserve": self.limits.min_protection_quote_reserve,
            "estimated_entry_proceeds": proceeds,
            "estimated_stop_quote": required,
            "estimated_quote_after_stop": (context.quote_free or 0.0) + proceeds - required,
        }

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

    def _min_notional(self, context: RiskContext) -> float:
        floor = self.limits.min_order_notional
        if context.min_notional and context.min_notional > floor:
            floor = context.min_notional
        return floor

    def _needed_quantity(self, entry, geometry, side, floor, entry_rate, context):
        if entry <= 0:
            return None
        executable = None
        if context.live_requested and context.market_type in {MarketType.MARGIN.value, "spot"}:
            executable = minimum_executable_quantity(
                entry, geometry.stop, geometry.target, side, floor, entry_rate, context.step_size
            )
        if executable is not None:
            return executable
        needed = floor / entry
        if context.step_size:
            needed = round_up_to_step(needed, context.step_size)
        return needed

    def _planned_stop_cash(self, qty, entry, stop, entry_rate, exit_rate) -> float:
        return abs(entry - stop) * qty + entry * qty * entry_rate + abs(stop) * qty * exit_rate

    def _reject_unfunded_minimum(self, decision, context, needed, qty, entry, stop, floor, entry_rate, exit_rate, protection_cap):
        # A minimum-sized entry without a fundable stop is not an executable trade.
        if protection_cap is not None and protection_cap + 1e-12 < needed:
            return self._reject(
                decision,
                [INSUFFICIENT_PROTECTION_BALANCE],
                details=self._protection_details(context, entry, stop, exit_rate, protection_cap),
            )
        stop_cash = self._planned_stop_cash(needed, entry, stop, entry_rate, exit_rate)
        limit = context.equity * self.limits.max_risk_per_trade
        if stop_cash > limit + 1e-6:
            return self._reject(decision, [MAX_RISK_PER_TRADE], details={
                "order_notional": entry * abs(qty), "required_notional": entry * needed,
                "stop_risk": stop_cash, "max_stop_risk": limit,
            })
        return None

    def _lift_to_notional(self, needed, entry, stop, context, entry_rate, exit_rate, side, snapshot, book):
        if entry <= 0 or needed <= 0:
            return None
        lifted = self._fit_to_capital(
            needed, entry, context, entry_rate, minimum_trade_notional=entry * needed
        )
        if lifted + 1e-12 < needed:
            return None
        if self._planned_stop_cash(lifted, entry, stop, entry_rate, exit_rate) > context.equity * self.limits.max_risk_per_trade + 1e-6:
            return None
        preview = self._fill(side, lifted, snapshot, book)
        if preview is None or preview.filled_quantity + 1e-12 < needed:
            return None
        return preview, preview.estimated_fill_price

    def _leverage(self) -> float:
        return max(self.limits.max_leverage, 0.0)

    def _buying_power(self, equity: float) -> float:
        """Cross-margin capacity. The loss caps stay on equity."""
        return equity * self._leverage()

    def _notional_ceiling(self, equity: float, fraction: float) -> float:
        return self._buying_power(equity) * fraction

    def _capital_room(self, context: RiskContext, entry_fee_rate: float, minimum_trade_notional: float = 0.0) -> tuple[float, str]:
        if context.equity <= 0 or context.cash <= 0 or self._leverage() <= 0:
            return 0.0, INSUFFICIENT_MARGIN
        symbol_room = self._notional_ceiling(context.equity, self.limits.max_symbol_exposure) - context.symbol_exposure_notional
        total_room = self._notional_ceiling(context.equity, self.limits.max_total_exposure) - context.total_exposure_notional
        cash_room = context.cash * self._leverage() / (1 + max(entry_fee_rate, 0.0))
        power = self._buying_power(context.equity)
        trade_room = minimum_trade_room(
            power, self.limits.max_trade_notional_fraction,
            self.limits.max_minimum_trade_notional_fraction,
            context.symbol_exposure_notional, minimum_trade_notional,
        )
        portfolio_room = (power * self.limits.max_portfolio_notional_fraction
                          - context.total_exposure_notional
                          if self.limits.max_portfolio_notional_fraction > 0 else float("inf"))
        room, reason = min(
            (
                (trade_room, "MAX_TRADE_NOTIONAL"),
                (portfolio_room, "MAX_PORTFOLIO_NOTIONAL"),
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

    def _fit_to_capital(self, qty: float, entry: float, context: RiskContext, entry_fee_rate: float, minimum_trade_notional: float = 0.0) -> float:
        room, _reason = self._capital_room(context, entry_fee_rate, minimum_trade_notional)
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
        payload = {"risk_rejected": True, "entry_selection": decision.metadata.get("entry_selection"),
                   "role_baseline": decision.metadata.get("role_baseline")}
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
