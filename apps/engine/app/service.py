from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.accounts import AccountBook
from app.analytics.comparison import compare_strategies
from app.analytics.performance import slice_performance, summarize_trades
from app.config import (
    Settings,
    baseline_from_file,
    combination_from_file,
    fees_from_file,
    load_file_config,
    risk_from_file,
    strategies_from_file,
)
from app.consensus.engine import consensus_from_decisions
from app.db.memory import MemoryStore
from app.db.postgres import PostgresMirror
from app.domain.enums import (
    EXPIRED_SIGNAL,
    STRATEGY_ERROR,
    Action,
    OperatingMode,
    OrderType,
    SignalStatus,
    SlippageModelName,
)
from app.domain.mathutil import utcnow
from app.domain.schemas import AuditRecord, MarketSnapshot, StrategyDecision
from app.events.bus import EngineLogBuffer, Event, EventBus
from app.execution.binance_live import BinanceExecutionProvider, LiveExecutionBlocked
from app.execution.paper import OrderIntent, PaperExecutionProvider, position_exit
from app.execution.slippage import SlippageConfig
from app.evolution.service import EvolutionService
from app.features.engine import build_snapshot
from app.market.feed import MarketFeed
from app.market.state import SymbolMarketState
from app.providers.fees import BinanceFeeProvider, ConfigFeeProvider
from app.providers.jev.factory import build_jev_provider
from app.providers.openai.provider import OpenAIProvider
from app.risk.economics import funding_cashflow, rate_for
from app.risk.engine import RiskContext, RiskEngine
from app.strategies.runners import BaselineJevStrategy, BaselineOpenAIJevStrategy, BaselineStrategy, StrategyContext

log = logging.getLogger(__name__)

STRATEGY_KEYS = ("baseline", "baseline_jev", "baseline_openai_jev")


class FutureReturnLabeler:
    HORIZONS = {
        "future_return_30s": 30,
        "future_return_1m": 60,
        "future_return_3m": 180,
        "future_return_5m": 300,
        "future_return_15m": 900,
    }

    def __init__(self) -> None:
        self.pending: list[dict] = []

    def register(self, snapshot: MarketSnapshot) -> None:
        self.pending.append(
            {
                "snapshot_id": snapshot.snapshot_id,
                "symbol": snapshot.symbol,
                "timestamp": snapshot.timestamp,
                "price": snapshot.price,
                "labels": {},
            }
        )

    def on_price(self, symbol: str, timestamp: datetime, price: float | None) -> list[dict]:
        if price is None or price <= 0:
            return []
        done: list[dict] = []
        still: list[dict] = []
        for item in self.pending:
            if item["symbol"] != symbol:
                still.append(item)
                continue
            for name, seconds in self.HORIZONS.items():
                if name in item["labels"]:
                    continue
                if timestamp >= item["timestamp"] + timedelta(seconds=seconds):
                    item["labels"][name] = price / item["price"] - 1
            if len(item["labels"]) < len(self.HORIZONS):
                still.append(item)
            else:
                done.append(item)
        self.pending = still[-5000:]
        return done


class TradingEngine:
    def __init__(self, settings: Settings) -> None:
        files = load_file_config()
        self.settings = settings
        self.weights = baseline_from_file(files["baseline"])
        self.combination = combination_from_file((files["strategies"] or {}).get("combination") or {})
        self.strategy_settings = strategies_from_file(files["strategies"])
        self.risk_limits = risk_from_file(files["risk"], settings)
        self.fee_config = fees_from_file(files["fees"], settings)
        now = utcnow()
        self.accounts = AccountBook(list(STRATEGY_KEYS), settings.initial_paper_equity, now)
        self.paper = PaperExecutionProvider(seed=settings.paper_seed)
        self.live = BinanceExecutionProvider(settings)
        self.slippage = SlippageConfig(
            model=SlippageModelName(settings.slippage_model),
            fixed_bps=settings.fixed_slippage_bps,
        )
        self.risk = RiskEngine(self.risk_limits, self.slippage)
        self.store = MemoryStore()
        self.postgres: PostgresMirror | None = PostgresMirror(settings.database_url) if settings.database_url else None
        self.bus = EventBus()
        self.logs = EngineLogBuffer()
        self.trading_enabled = settings.trading_engine_enabled
        self.states = {
            symbol: SymbolMarketState(symbol=symbol, market_type=settings.market_type) for symbol in settings.symbol_list
        }
        self.started_monotonic = time.monotonic()
        self.started_at = now
        self.persistence_mode = "memory"
        self.db_healthy = settings.database_url == ""
        self.db_error: str | None = None
        self.health = {
            key: {"errors": 0, "last_latency_ms": None, "last_decision_at": None, "status": "ok"} for key in STRATEGY_KEYS
        }
        self.labeler = FutureReturnLabeler()
        self.openai = OpenAIProvider(settings)
        self.jev = build_jev_provider(settings)
        self.fee_provider = BinanceFeeProvider(settings, self.fee_config) if settings.binance_api_key else ConfigFeeProvider(self.fee_config)
        self._locks = {key: asyncio.Lock() for key in STRATEGY_KEYS}
        self.strategies = {
            "baseline": BaselineStrategy(),
            "baseline_jev": BaselineJevStrategy(),
            "baseline_openai_jev": BaselineOpenAIJevStrategy(),
        }
        self.feed = MarketFeed(
            settings,
            self.states,
            on_trigger=self.on_snapshot_trigger,
            on_ticker=self.on_ticker,
            on_price=self.on_price,
        )
        self._background: set[asyncio.Task] = set()
        self.openai_cost_total = 0.0
        self.openai_cost_known = False
        self.evolution = EvolutionService(settings)
        self.evolution.bind_trades(self._all_trades)

    async def start(self) -> None:
        import httpx

        self._http = httpx.AsyncClient()
        self.openai.client = self._http
        self.live.client = self._http
        if getattr(self.jev, "provider_name", "") == "real":
            self.jev.client = self._http
        if isinstance(self.fee_provider, BinanceFeeProvider):
            self.fee_provider.client = self._http
        if self.postgres is not None:
            ok = await self.postgres.connect()
            self.persistence_mode = "postgres" if ok else "postgres_error"
            self.db_healthy = ok
            self.db_error = None if ok else self.postgres.last_error
        await self.feed.start()
        await self.evolution.start()
        self._log("engine", "engine started")

    async def stop(self) -> None:
        await self.evolution.stop()
        await self.feed.stop()
        for task in list(self._background):
            task.cancel()
        client = getattr(self, "_http", None)
        if client is not None:
            await client.aclose()

    def _all_trades(self) -> list:
        rows = []
        for account in self.accounts.accounts.values():
            rows.extend(account.trades)
        return rows

    def allows_new_paper(self) -> bool:
        if self.persistence_mode == "memory":
            return True
        return self.db_healthy and (self.postgres is None or self.postgres.healthy)

    def allows_new_live(self) -> bool:
        return self.persistence_mode == "postgres" and self.db_healthy and self.settings.live_armed

    async def kill_switch(self, actor: str) -> None:
        self.trading_enabled = False
        await self._audit(actor, "kill_switch", {"trading_enabled": False})
        self._log("kill_switch", "new entries stopped")
        await self.bus.publish(Event("engine_status", {"trading_enabled": False}))

    async def resume(self, actor: str) -> None:
        if not self.settings.trading_engine_enabled:
            raise PermissionError("ENGINE_DISABLED_BY_ENV")
        self.trading_enabled = True
        await self._audit(actor, "resume", {"trading_enabled": True})
        self._log("resume", "new entries enabled")
        await self.bus.publish(Event("engine_status", {"trading_enabled": True}))

    async def update_strategy(self, key: str, *, enabled: bool | None, mode: str | None, call_model: bool | None, actor: str) -> dict:
        current = self.strategy_settings[key]
        if mode == "live" and not self.settings.live_armed:
            raise PermissionError("LIVE_LOCKED")
        if mode is not None:
            current.mode = OperatingMode(mode)
        if enabled is not None:
            current.enabled = enabled
        if call_model is not None:
            current.call_model = call_model
        payload = {
            "enabled": current.enabled,
            "mode": current.mode.value,
            "call_model": current.call_model,
            "max_signal_age_ms": current.max_signal_age_ms,
        }
        await self._audit(actor, "strategy_update", {"strategy": key, **payload})
        if self.postgres and self.postgres.healthy:
            await self.postgres.save_strategy_config(key, current.enabled, current.mode.value, payload)
        return payload

    def update_risk(self, changes: dict, actor: str) -> None:
        allowed = {
            "min_net_rr",
            "risk_per_trade",
            "max_risk_per_trade",
            "max_daily_loss",
            "max_daily_drawdown",
            "max_open_positions",
            "max_symbol_exposure",
            "max_total_exposure",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise KeyError(",".join(sorted(unknown)))
        self.risk.update_limits(**changes)
        self.risk_limits = self.risk.limits

    async def on_ticker(self, symbol: str) -> None:
        await self.bus.publish(Event("market_update", self.ticker(symbol)))

    async def on_price(self, symbol: str, price: float | None, timestamp: datetime) -> None:
        finished = self.labeler.on_price(symbol, timestamp, price)
        for item in finished:
            labels = {"snapshot_id": str(item["snapshot_id"]), **item["labels"]}
            self.store.add_label(item["snapshot_id"], labels)
            if self.postgres and self.postgres.healthy:
                await self.postgres.save_label(str(item["snapshot_id"]), labels)
        await self.manage_positions(symbol)

    async def on_snapshot_trigger(self, symbol: str, as_of: datetime, trigger: str) -> None:
        task = asyncio.create_task(self.evaluate_symbol(symbol, as_of, trigger))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def evaluate_symbol(self, symbol: str, as_of: datetime, trigger: str) -> list[StrategyDecision]:
        state = self.states[symbol]
        snapshot = build_snapshot(
            state,
            as_of=as_of,
            trigger=trigger,
            stale_after_ms=self.settings.stale_after_ms,
        )
        if snapshot is None:
            return []
        return await self.evaluate_snapshot(snapshot)

    async def evaluate_snapshot(self, snapshot: MarketSnapshot) -> list[StrategyDecision]:
        self.store.add_snapshot(snapshot)
        self.labeler.register(snapshot)
        if self.postgres and self.postgres.healthy:
            await self.postgres.save_snapshot(self.store.snapshots[str(snapshot.snapshot_id)])
        await self.bus.publish(Event("snapshot_created", {"snapshot_id": str(snapshot.snapshot_id), "symbol": snapshot.symbol}))
        opportunity_id = uuid4()
        correlation_id = uuid4()
        active = []
        tasks = []
        for key in STRATEGY_KEYS:
            cfg = self.strategy_settings[key]
            if not cfg.enabled or cfg.mode is OperatingMode.DISABLED:
                self.health[key]["status"] = "off"
                continue
            active.append(key)
            tasks.append(asyncio.create_task(self._run_strategy(key, snapshot, opportunity_id, correlation_id)))
        raw = await asyncio.gather(*tasks, return_exceptions=True)
        decisions: list[StrategyDecision] = []
        for key, result in zip(active, raw, strict=True):
            if isinstance(result, Exception):
                self.health[key]["errors"] += 1
                self.health[key]["status"] = "error"
                log.exception("strategy %s failed", key, exc_info=result)
                decisions.append(
                    self._error_decision(key, snapshot, opportunity_id, correlation_id, STRATEGY_ERROR)
                )
            else:
                decisions.append(result)
        consensus = consensus_from_decisions(decisions)
        if consensus is not None:
            payload = self.store.add_consensus(consensus)
            if self.postgres and self.postgres.healthy:
                await self.postgres.save_consensus(payload)
        return decisions

    async def _run_strategy(self, key: str, snapshot: MarketSnapshot, opportunity_id, correlation_id) -> StrategyDecision:
        cfg = self.strategy_settings[key]
        started = time.perf_counter()
        async with self._locks[key]:
            waited_ms = (time.perf_counter() - started) * 1000
            context = StrategyContext(
                correlation_id=correlation_id,
                opportunity_id=opportunity_id,
                mode=cfg.mode,
                now=snapshot.timestamp,
                weights=self.weights,
                combination=self.combination,
                call_model=cfg.call_model,
                jev=self.jev,
                openai=self.openai,
                failure_policy=self.settings.jev_failure_policy or self.combination.failure_policy,
            )
            if waited_ms > cfg.max_signal_age_ms:
                decision = self._error_decision(key, snapshot, opportunity_id, correlation_id, EXPIRED_SIGNAL)
                decision.signal_status = SignalStatus.EXPIRED
            else:
                timeout = max(0.05, (cfg.max_signal_age_ms - waited_ms) / 1000)
                try:
                    decision = await asyncio.wait_for(
                        self.strategies[key].evaluate(snapshot, snapshot.features, context),
                        timeout=timeout,
                    )
                except TimeoutError:
                    decision = self._error_decision(key, snapshot, opportunity_id, correlation_id, EXPIRED_SIGNAL)
                    decision.signal_status = SignalStatus.EXPIRED
                    context.artifacts.append({"kind": "timeout"})
                except Exception as exc:
                    self.health[key]["errors"] += 1
                    self.health[key]["status"] = "error"
                    decision = self._error_decision(key, snapshot, opportunity_id, correlation_id, STRATEGY_ERROR)
                    context.artifacts.append({"kind": "error", "error": str(exc)})
            elapsed = (time.perf_counter() - started) * 1000
            if decision.signal_status is SignalStatus.VALID and elapsed > cfg.max_signal_age_ms:
                decision.signal_status = SignalStatus.EXPIRED
                if EXPIRED_SIGNAL not in decision.reason_codes:
                    decision.reason_codes.append(EXPIRED_SIGNAL)
            self.health[key]["last_latency_ms"] = elapsed
            self.health[key]["last_decision_at"] = snapshot.timestamp.isoformat()
            if self.health[key]["status"] != "error":
                self.health[key]["status"] = "ok"
            stored = self.store.add_decision(decision, context.artifacts)
            for artifact in context.artifacts:
                if artifact.get("kind") == "openai" and artifact.get("estimated_cost") is not None:
                    self.openai_cost_total += float(artifact["estimated_cost"])
                    self.openai_cost_known = True
                if self.postgres and self.postgres.healthy:
                    if artifact.get("kind") == "openai":
                        await self.postgres.save_openai(stored["decision_id"], stored["snapshot_id"], artifact)
                    elif artifact.get("kind") == "jev":
                        await self.postgres.save_jev(stored["decision_id"], stored["snapshot_id"], artifact)
            if self.postgres and self.postgres.healthy:
                await self.postgres.save_decision(stored)
            await self.bus.publish(Event("decision", {"decision_id": stored["decision_id"], "strategy": key, "action": decision.action.value}))
            if decision.action is Action.NO_TRADE or decision.signal_status is not SignalStatus.VALID:
                return decision
            if cfg.mode is OperatingMode.SHADOW:
                return decision
            if cfg.mode is OperatingMode.PAPER:
                await self._execute_paper(decision, snapshot)
            elif cfg.mode is OperatingMode.LIVE:
                await self._execute_live(decision, snapshot)
            return decision

    async def _execute_paper(self, decision: StrategyDecision, snapshot: MarketSnapshot) -> None:
        fees = await self.fee_provider.get_fees(snapshot.symbol)
        context = self._risk_context(decision.strategy, snapshot, live=False)
        risk = self.risk.evaluate(decision, snapshot, context, fees, self.states[snapshot.symbol].book)
        payload = self.store.add_risk(risk)
        if self.postgres and self.postgres.healthy:
            await self.postgres.save_risk(payload)
        await self.bus.publish(Event("risk", {"decision_id": str(decision.decision_id), "accepted": risk.accepted}))
        if not risk.accepted or risk.economics is None:
            return
        econ = risk.economics
        side = "BUY" if decision.action is Action.LONG else "SELL"
        client_id = self.paper.client_order_id(decision.decision_id)
        if self.paper.get(client_id) is not None:
            return
        intent = OrderIntent(
            decision_id=decision.decision_id,
            risk_id=risk.risk_id,
            strategy=decision.strategy,
            symbol=snapshot.symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=econ.quantity,
            limit_price=None,
            mode="paper",
            created_at=snapshot.timestamp,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            book=self.states[snapshot.symbol].book,
            fee_rate=econ.costs.fee_rate_entry,
        )
        order, fills = self.paper.submit_market(intent, self.slippage)
        order_payload = self.store.add_order(order)
        if self.postgres and self.postgres.healthy:
            await self.postgres.save_order(order_payload)
        for fill in fills:
            self.store.add_fill(fill)
        if not fills:
            return
        fill = fills[0]
        margin = (fill.price * fill.quantity) / max(self.risk.limits.max_leverage, 1e-9)
        self.accounts.open_position(
            strategy=decision.strategy,
            symbol=snapshot.symbol,
            side=decision.action,
            quantity=fill.quantity,
            entry_price=fill.price,
            stop=econ.stop,
            target=econ.target,
            opened_at=snapshot.timestamp,
            decision_id=decision.decision_id,
            entry_fee=fill.fee,
            initial_net_risk=econ.net_risk,
            margin=margin,
        )
        await self.bus.publish(Event("position", {"strategy": decision.strategy, "symbol": snapshot.symbol, "status": "OPEN"}))

    async def _execute_live(self, decision: StrategyDecision, snapshot: MarketSnapshot) -> None:
        if not self.allows_new_live():
            self._log("live_blocked", "LIVE_LOCKED")
            self.store.add_event("live_blocked", "LIVE_LOCKED", {"decision_id": str(decision.decision_id)})
            return
        fees = await self.fee_provider.get_fees(snapshot.symbol)
        context = self._risk_context(decision.strategy, snapshot, live=True)
        risk = self.risk.evaluate(decision, snapshot, context, fees, self.states[snapshot.symbol].book)
        self.store.add_risk(risk)
        if not risk.accepted or risk.economics is None:
            return
        intent = OrderIntent(
            decision_id=decision.decision_id,
            risk_id=risk.risk_id,
            strategy=decision.strategy,
            symbol=snapshot.symbol,
            side="BUY" if decision.action is Action.LONG else "SELL",
            order_type=OrderType.MARKET,
            quantity=risk.economics.quantity,
            limit_price=None,
            mode="live",
            created_at=snapshot.timestamp,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            book=self.states[snapshot.symbol].book,
            fee_rate=risk.economics.costs.fee_rate_entry,
        )
        try:
            order = await self.live.submit(intent)
        except LiveExecutionBlocked as exc:
            self._log("live_blocked", exc.reason)
            return
        self.store.add_order(order)

    async def manage_positions(self, symbol: str) -> None:
        state = self.states.get(symbol)
        if state is None or state.last_price is None:
            return
        for key in STRATEGY_KEYS:
            async with self._locks[key]:
                await self._manage_one(key, symbol, state)

    async def _manage_one(self, key: str, symbol: str, state) -> None:
        account = self.accounts.accounts[key]
        position = account.positions.get(symbol)
        if position is None or state.last_price is None:
            return
        self.accounts.update_excursion(key, symbol, state.last_price)
        hold_minutes = (utcnow() - position.opened_at).total_seconds() / 60
        reason = position_exit(
            position.side,
            state.best_bid,
            state.best_ask,
            position.stop,
            position.target,
            hold_minutes,
            self.risk.limits.max_hold_minutes,
        )
        if reason is None:
            return
        side = "SELL" if position.side is Action.LONG else "BUY"
        from app.execution.slippage import simulate_fill

        preview = simulate_fill(
            side=side,
            quantity=position.quantity,
            best_bid=state.best_bid,
            best_ask=state.best_ask,
            book=state.book,
            config=self.slippage,
        )
        if preview.filled_quantity <= 0:
            return
        exit_rate = rate_for("taker", self.fee_config.maker_fee_rate, self.fee_config.taker_fee_rate)
        exit_fee = preview.estimated_fill_price * preview.filled_quantity * exit_rate
        hold_minutes = (utcnow() - position.opened_at).total_seconds() / 60
        periods = 0.0
        if self.risk.limits.apply_funding and hold_minutes >= self.risk.limits.funding_interval_minutes:
            periods = hold_minutes / self.risk.limits.funding_interval_minutes
        funding_rate = state.funding_rate
        funding = funding_cashflow(
            position.side,
            funding_rate,
            preview.estimated_fill_price * preview.filled_quantity,
            periods,
        )
        touch = preview.expected_price or preview.estimated_fill_price
        slippage_quote = abs(preview.estimated_fill_price - touch) * preview.filled_quantity
        trade = self.accounts.close_position(
            strategy=key,
            symbol=symbol,
            exit_price=preview.estimated_fill_price,
            closed_at=utcnow(),
            exit_fee=exit_fee,
            slippage=slippage_quote,
            funding=funding,
            exit_reason=reason,
            quantitative_regime=None,
            market_regime=None,
        )
        payload = self.store.add_trade(trade)
        if self.postgres and self.postgres.healthy:
            await self.postgres.save_trade(payload)
        await self.bus.publish(Event("trade", payload))
        self._log("trade", f"{key} {symbol} {reason} net {trade.net_pnl:.4f}")

    def _risk_context(self, strategy: str, snapshot: MarketSnapshot, live: bool) -> RiskContext:
        account = self.accounts.accounts[strategy]
        marks = {snapshot.symbol: snapshot.price}
        self.accounts.roll_day(strategy, snapshot.timestamp, marks)
        step = self.feed.rules.get(snapshot.symbol, {}).get("step_size")
        return RiskContext(
            equity=account.equity(marks),
            cash=account.cash,
            day_start_equity=account.day_start_equity,
            realized_pnl_today=account.realized_pnl_today,
            open_positions=len(account.positions),
            symbol_exposure_notional=account.symbol_exposure(snapshot.symbol, marks),
            total_exposure_notional=account.exposure(marks),
            has_position_on_symbol=snapshot.symbol in account.positions,
            last_entry_at=account.last_entry_at,
            now=snapshot.timestamp,
            trading_enabled=self.trading_enabled,
            persistence_ok=self.allows_new_live() if live else self.allows_new_paper(),
            live_requested=live,
            live_armed=self.settings.live_armed,
            market_type=self.settings.market_type,
            step_size=step,
            rules_required=live,
        )

    def _error_decision(self, strategy: str, snapshot: MarketSnapshot, opportunity_id, correlation_id, reason: str) -> StrategyDecision:
        cfg = self.strategy_settings[strategy]
        return StrategyDecision(
            correlation_id=correlation_id,
            opportunity_id=opportunity_id,
            snapshot_id=snapshot.snapshot_id,
            strategy=strategy,
            symbol=snapshot.symbol,
            timestamp=snapshot.timestamp,
            action=Action.NO_TRADE,
            confidence=0,
            reason_codes=[reason],
            metadata={},
            mode=cfg.mode,
            signal_status=SignalStatus.VALID if reason == STRATEGY_ERROR else SignalStatus.EXPIRED,
        )

    def ticker(self, symbol: str) -> dict:
        state = self.states.get(symbol)
        if state is None or state.last_price is None:
            return {"symbol": symbol, "price": None, "status": "NO DATA"}
        spread_bps = None
        if state.best_bid and state.best_ask and state.best_ask >= state.best_bid:
            mid = (state.best_bid + state.best_ask) / 2
            spread_bps = (state.best_ask - state.best_bid) / mid * 10_000 if mid else None
        stale = True
        if state.last_book_at is not None:
            stale = (utcnow() - state.last_book_at).total_seconds() * 1000 > self.settings.stale_after_ms
        return {
            "symbol": symbol,
            "market_type": state.market_type,
            "price": state.last_price,
            "best_bid": state.best_bid,
            "best_ask": state.best_ask,
            "spread_bps": spread_bps,
            "mark_price": state.mark_price,
            "funding_rate": state.funding_rate,
            "open_interest": state.open_interest,
            "last_event_at": state.last_event_at.isoformat() if state.last_event_at else None,
            "stale": stale,
            "status": "STALE" if stale else "LIVE",
        }

    def performance(self) -> dict:
        reports = {}
        for key in STRATEGY_KEYS:
            trades = [trade for trade in self.accounts.accounts[key].trades]
            reports[key] = summarize_trades(trades, self.settings.initial_paper_equity)
            reports[key]["by_regime"] = slice_performance(trades, self.settings.initial_paper_equity)
            reports[key]["equity"] = self.accounts.accounts[key].equity(self._marks())
            reports[key]["mode"] = self.strategy_settings[key].mode.value
            reports[key]["enabled"] = self.strategy_settings[key].enabled
        return reports

    def comparison(self) -> dict:
        cost = self.openai_cost_total if self.openai_cost_known else None
        return compare_strategies(
            baseline=self.accounts.accounts["baseline"].trades,
            baseline_jev=self.accounts.accounts["baseline_jev"].trades,
            openai_jev=self.accounts.accounts["baseline_openai_jev"].trades,
            decisions=self.store.decisions,
            openai_cost=cost,
            starting_equity=self.settings.initial_paper_equity,
        )

    def equity_curves(self) -> dict:
        series = {}
        start = self.settings.initial_paper_equity
        for key in STRATEGY_KEYS:
            points = list(self.accounts.accounts[key].equity_points)
            current = self.accounts.accounts[key].equity(self._marks())
            points.append({"t": utcnow().isoformat(), "equity": current, "mark": True})
            series[key] = [
                {
                    **point,
                    "indexed": (point["equity"] / start * 100) if start else None,
                }
                for point in points
            ]
        return {"starting_equity": start, "series": series}

    def positions_payload(self) -> list[dict]:
        rows = []
        marks = self._marks()
        for key, account in self.accounts.accounts.items():
            for symbol, position in account.positions.items():
                mark = marks.get(symbol, position.entry_price)
                rows.append(
                    {
                        "strategy": key,
                        "symbol": symbol,
                        "side": position.side.value,
                        "quantity": position.quantity,
                        "entry": position.entry_price,
                        "stop": position.stop,
                        "target": position.target,
                        "unrealized": (mark - position.entry_price) * position.quantity
                        if position.side is Action.LONG
                        else (position.entry_price - mark) * position.quantity,
                        "mfe": position.mfe,
                        "mae": position.mae,
                        "opened_at": position.opened_at.isoformat(),
                    }
                )
        return rows

    def status(self) -> dict:
        jev_name = getattr(self.jev, "provider_name", "unknown")
        openai_status = "not_configured"
        if self.openai.configured():
            openai_status = "open_circuit" if self.openai.breaker.state == "open" else "configured"
        if not self.strategy_settings["baseline_openai_jev"].call_model:
            openai_status = f"{openai_status}|calls_off"
        return {
            "engine": "online",
            "uptime_s": time.monotonic() - self.started_monotonic,
            "started_at": self.started_at.isoformat(),
            "trading_enabled": self.trading_enabled,
            "live_armed": self.settings.live_armed,
            "market_type": self.settings.market_type,
            "symbols": self.settings.symbol_list,
            "binance": {
                "status": "connected" if self.feed.connected else "disconnected",
                "last_error": self.feed.last_error,
                "reconnects": self.feed.reconnects,
                "circuit": self.feed.breaker.state,
            },
            "openai": {"status": openai_status, "circuit": self.openai.breaker.state, "model": self.settings.openai_model},
            "jev": {
                "status": jev_name,
                "provider_version": getattr(self.jev, "provider_version", None),
                "is_mock": jev_name == "mock",
            },
            "database": {"mode": self.persistence_mode, "healthy": self.db_healthy if self.persistence_mode != "memory" else True, "error": self.db_error},
            "strategies": {
                key: {
                    **self.health[key],
                    "mode": self.strategy_settings[key].mode.value,
                    "enabled": self.strategy_settings[key].enabled,
                    "call_model": self.strategy_settings[key].call_model,
                }
                for key in STRATEGY_KEYS
            },
            "queues": {"background_tasks": len(self._background)},
            "paper_seed": self.settings.paper_seed,
        }

    def _marks(self) -> dict[str, float]:
        return {symbol: state.last_price for symbol, state in self.states.items() if state.last_price is not None}

    async def _audit(self, actor: str, action: str, payload: dict) -> None:
        record = AuditRecord(timestamp=utcnow(), actor=actor, action=action, payload=payload)
        dumped = self.store.add_audit(record)
        if self.postgres and self.postgres.healthy:
            await self.postgres.save_audit(dumped)

    def _log(self, kind: str, message: str) -> None:
        item = {"kind": kind, "message": message, "timestamp": utcnow().isoformat()}
        self.logs.add(item)
        self.store.add_event(kind, message)
        log.info("%s %s", kind, message)
