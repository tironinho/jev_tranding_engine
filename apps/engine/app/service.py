from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.accounts import AccountBook, unrealized
from app.analytics.comparison import compare_strategies
from app.analytics.performance import slice_performance, summarize_trades
from app.config import (
    Settings,
    as_paper_margin,
    baseline_from_file,
    combination_from_file,
    fees_from_file,
    load_file_config,
    risk_from_file,
    strategies_from_file,
)
from app.consensus.engine import consensus_from_decisions, extra_open_slot
from app.db.memory import MemoryStore
from app.db.postgres import PostgresMirror
from app.domain.enums import (
    EXPIRED_SIGNAL,
    QUEUE_SATURATED,
    RISK_REJECTED,
    STRATEGY_ERROR,
    VOTES_NOT_ARRIVED,
    Action,
    OperatingMode,
    OrderStatus,
    OrderType,
    SignalStatus,
    SlippageModelName,
)
from app.domain.mathutil import utcnow
from app.domain.schemas import AuditRecord, FillRecord, MarketSnapshot, OrderRecord, RiskDecision, StrategyDecision
from app.events.bus import EngineLogBuffer, Event, EventBus
from app.intelligence.runner import IntelligenceRunner
from app.execution.binance_live import BinanceExecutionProvider, LiveExecutionBlocked, execution_of
from app.execution.paper import OrderIntent, PaperExecutionProvider, position_exit_observed, stepped_stop
from app.execution.slippage import SlippageConfig
from app.evolution.service import EvolutionService
from app.features.engine import build_snapshot
from app.market.feed import MarketFeed
from app.market.state import SymbolMarketState
from app.providers.binance_account import BinanceBalanceProvider
from app.providers.fees import BinanceFeeProvider, ConfigFeeProvider
from app.providers.jev.factory import build_jev_provider
from app.providers.openai.provider import OpenAIProvider
from app.risk.economics import funding_cashflow, rate_for
from app.risk.engine import RiskContext, RiskEngine
from app.strategies.runners import BaselineJevStrategy, BaselineStrategy, StrategyContext

log = logging.getLogger(__name__)

STRATEGY_KEYS = ("baseline", "baseline_jev")
EDITABLE_RISK = (
    "min_net_rr",
    "risk_per_trade",
    "max_risk_per_trade",
    "max_daily_loss",
    "max_daily_drawdown",
    "max_open_positions",
    "max_symbol_exposure",
    "max_total_exposure",
)


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
        settings = as_paper_margin(settings)
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
        self.balance = BinanceBalanceProvider(settings)
        self.intelligence = IntelligenceRunner(settings)
        self._locks = {key: asyncio.Lock() for key in STRATEGY_KEYS}
        self.strategies = {
            "baseline": BaselineStrategy(),
            "baseline_jev": BaselineJevStrategy(),
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
        self._evaluating: set[str] = set()
        self._quotes: dict[str, list[dict]] = {}
        self._quotes_dirty: set[str] = set()
        self._quote_tasks: dict[str, asyncio.Task] = {}

    async def start(self) -> None:
        import httpx

        self._http = httpx.AsyncClient()
        self.openai.client = self._http
        self.live.client = self._http
        if getattr(self.jev, "provider_name", "") == "real":
            self.jev.client = self._http
        if isinstance(self.fee_provider, BinanceFeeProvider):
            self.fee_provider.client = self._http
        self.balance.client = self._http
        self.intelligence.bind(self._http)
        await self.intelligence.start()
        if self.postgres is not None:
            ok = await self.postgres.connect()
            self.persistence_mode = "postgres" if ok else "postgres_error"
            self.db_healthy = ok
            self.db_error = None if ok else self.postgres.last_error
            if ok:
                runtime = await self.postgres.load_runtime()
                self.db_healthy = self.postgres.healthy
                self.db_error = self.postgres.last_error
                if self.db_healthy:
                    self.apply_runtime(runtime)
                    self._log("restore", "book restored from postgres")
        await self.feed.start()
        self.evolution.bind_research_client(self._http)
        if self.postgres is not None and self.postgres.factory is not None and self.db_healthy:
            await self.evolution.attach_postgres(self.postgres.factory)
        await self.evolution.start()
        self._log("engine", "engine started")

    async def stop(self) -> None:
        await self.intelligence.stop()
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

    async def update_risk(self, changes: dict, actor: str) -> None:
        unknown = set(changes) - set(EDITABLE_RISK)
        if unknown:
            raise KeyError(",".join(sorted(unknown)))
        cleaned = _coerce_risk(changes)
        self.risk.update_limits(**cleaned)
        self.risk_limits = self.risk.limits
        if self.postgres and self.postgres.healthy:
            payload = {key: getattr(self.risk.limits, key) for key in EDITABLE_RISK}
            await self.postgres.save_risk_limits(payload)

    async def on_ticker(self, symbol: str) -> None:
        await self.bus.publish(Event("market_update", self.ticker(symbol)))

    async def on_price(self, symbol: str, price: float | None, timestamp: datetime) -> None:
        """Schedule label and exit work. The market socket does not wait for it."""
        state = self.states.get(symbol)
        bucket = self._quotes.setdefault(symbol, [])
        bucket.append(
            {
                "price": price,
                "timestamp": timestamp,
                "bid": None if state is None else state.best_bid,
                "ask": None if state is None else state.best_ask,
            }
        )
        if len(bucket) > 500:
            del bucket[:-500]
        self._quotes_dirty.add(symbol)
        current = self._quote_tasks.get(symbol)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._drain_quotes(symbol))
        self._quote_tasks[symbol] = task
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _drain_quotes(self, symbol: str) -> None:
        while symbol in self._quotes_dirty:
            self._quotes_dirty.discard(symbol)
            batch = self._quotes.pop(symbol, [])
            if not batch:
                continue
            for point in batch:
                finished = self.labeler.on_price(symbol, point["timestamp"], point["price"])
                for item in finished:
                    labels = {"snapshot_id": str(item["snapshot_id"]), **item["labels"]}
                    self.store.add_label(item["snapshot_id"], labels)
                    if self.postgres and self.postgres.healthy:
                        await self.postgres.save_label(str(item["snapshot_id"]), labels)
            last = batch[-1]
            await self.manage_positions(symbol, as_of=last["timestamp"], extreme=_extreme(batch))

    async def on_snapshot_trigger(self, symbol: str, as_of: datetime, trigger: str) -> None:
        if symbol in self._evaluating:
            self.store.add_event("queue", QUEUE_SATURATED, {"symbol": symbol, "trigger": trigger})
            self._log("queue", f"{symbol} {QUEUE_SATURATED}")
            return
        self._evaluating.add(symbol)
        task = asyncio.create_task(self.evaluate_symbol(symbol, as_of, trigger))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def evaluate_symbol(self, symbol: str, as_of: datetime, trigger: str) -> list[StrategyDecision]:
        try:
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
        finally:
            self._evaluating.discard(symbol)

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
        await self._execute_decisions(decisions, snapshot)
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
                risk=self.risk.limits,
                round_trip_fee=self.fee_config.taker_fee_rate * 2,
            )
            context.intelligence = self.intelligence.context_for(snapshot.symbol, snapshot.features, snapshot.timestamp)
            budget_ms = cfg.max_signal_age_ms
            if key == "baseline_jev":
                budget_ms = max(budget_ms, 12_000)
            if key == "baseline_openai_jev":
                budget_ms = max(budget_ms, int((self.settings.openai_timeout_s + 4) * 1000))
            if waited_ms > budget_ms:
                decision = self._error_decision(key, snapshot, opportunity_id, correlation_id, EXPIRED_SIGNAL)
                decision.signal_status = SignalStatus.EXPIRED
            else:
                timeout = max(0.05, (budget_ms - waited_ms) / 1000)
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
            if decision.signal_status is SignalStatus.VALID and elapsed > budget_ms:
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
            return decision

    def _votes_missing(self, decisions: list[StrategyDecision]) -> bool:
        by_key = {item.strategy: item for item in decisions}
        for key in STRATEGY_KEYS:
            cfg = self.strategy_settings[key]
            if not cfg.enabled or cfg.mode is OperatingMode.DISABLED:
                continue
            decision = by_key.get(key)
            if decision is None or decision.signal_status is SignalStatus.EXPIRED:
                return True
        return False

    def _can_execute(self, decision: StrategyDecision) -> bool:
        cfg = self.strategy_settings.get(decision.strategy)
        if cfg is None or not cfg.enabled or cfg.mode not in (OperatingMode.PAPER, OperatingMode.LIVE):
            return False
        return decision.action is not Action.NO_TRADE and decision.signal_status is SignalStatus.VALID

    async def _execute_decisions(self, decisions: list[StrategyDecision], snapshot: MarketSnapshot) -> None:
        if self._votes_missing(decisions):
            for decision in decisions:
                if not self._can_execute(decision):
                    continue
                await self._record_risk(
                    RiskDecision(
                        decision_id=decision.decision_id,
                        correlation_id=decision.correlation_id,
                        accepted=False,
                        reject_reasons=[RISK_REJECTED, VOTES_NOT_ARRIVED],
                        side=decision.action,
                        details={"risk_rejected": True},
                    )
                )
            return
        slot = extra_open_slot(decisions, self.combination.extra_entry_min_continuation)
        for decision in decisions:
            if not self._can_execute(decision):
                continue
            cfg = self.strategy_settings[decision.strategy]
            if cfg.mode is OperatingMode.PAPER:
                await self._execute_paper(decision, snapshot, extra_slot=slot)
            elif cfg.mode is OperatingMode.LIVE:
                await self._execute_live(decision, snapshot, extra_slot=slot)

    async def _execute_paper(self, decision: StrategyDecision, snapshot: MarketSnapshot, extra_slot: bool = False) -> None:
        fees = await self.fee_provider.get_fees(snapshot.symbol)
        context = self._risk_context(decision.strategy, snapshot, live=False)
        risk = self.risk.evaluate(decision, snapshot, context, fees, self.states[snapshot.symbol].book, extra_slot=extra_slot)
        await self._record_risk(risk)
        if not risk.accepted or risk.economics is None:
            return
        econ = risk.economics
        side = "BUY" if decision.action is Action.LONG else "SELL"
        client_id = self.paper.client_order_id(decision.decision_id)
        if self.paper.get(client_id) is not None:
            return
        intent = self._intent(decision, snapshot, risk, side, "paper")
        order, fills = self.paper.submit_market(intent, self.slippage)
        order_payload = self.store.add_order(order)
        fill_payloads = [self.store.add_fill(fill) for fill in fills]
        if not fills:
            await self._checkpoint(decision.strategy, positions=[], orders=[order_payload], fills=[])
            return
        fill = fills[0]
        position = self._open_from_fill(decision, snapshot, econ, fill.price, fill.quantity, fill.fee, "paper", None)
        await self._checkpoint(decision.strategy, positions=[position], orders=[order_payload], fills=fill_payloads)
        await self.bus.publish(Event("position", {"strategy": decision.strategy, "symbol": snapshot.symbol, "status": "OPEN"}))

    async def _execute_live(self, decision: StrategyDecision, snapshot: MarketSnapshot, extra_slot: bool = False) -> None:
        if not self.allows_new_live():
            self._log("live_blocked", "LIVE_LOCKED")
            self.store.add_event("live_blocked", "LIVE_LOCKED", {"decision_id": str(decision.decision_id)})
            return
        fees = await self.fee_provider.get_fees(snapshot.symbol)
        context = self._risk_context(decision.strategy, snapshot, live=True)
        risk = self.risk.evaluate(decision, snapshot, context, fees, self.states[snapshot.symbol].book, extra_slot=extra_slot)
        await self._record_risk(risk)
        if not risk.accepted or risk.economics is None:
            return
        econ = risk.economics
        side = "BUY" if decision.action is Action.LONG else "SELL"
        intent = self._intent(decision, snapshot, risk, side, "live")
        try:
            order = await self.live.submit(intent)
        except LiveExecutionBlocked as exc:
            self._log("live_blocked", exc.reason)
            return
        except Exception as exc:
            self._log("live_error", str(exc))
            return
        order_payload = self.store.add_order(order)
        if order.filled_quantity <= 0 or not order.average_fill_price:
            self._log("live_unfilled", order.client_order_id)
            await self._checkpoint(decision.strategy, positions=[], orders=[order_payload], fills=[])
            return
        fee = order.average_fill_price * order.filled_quantity * econ.costs.fee_rate_entry
        position = self._open_from_fill(
            decision,
            snapshot,
            econ,
            order.average_fill_price,
            order.filled_quantity,
            fee,
            "live",
            None,
        )
        fill = FillRecord(
            order_id=order.order_id,
            decision_id=decision.decision_id,
            price=order.average_fill_price,
            quantity=order.filled_quantity,
            fee=fee,
            slippage_bps=0,
            liquidity="taker",
            filled_at=snapshot.timestamp,
        )
        fill_payload = self.store.add_fill(fill)
        tick = self.feed.rules.get(snapshot.symbol, {}).get("tick_size")
        intent.quantity = order.filled_quantity
        try:
            stop = await self.live.submit_stop(intent, position.stop, tick=tick)
        except Exception as exc:
            self._log("live_stop", str(exc))
            stop = None
        orders = [order_payload]
        if stop is not None:
            position.stop_client_order_id = stop.client_order_id
            orders.append(self.store.add_order(stop))
        await self._checkpoint(decision.strategy, positions=[position], orders=orders, fills=[fill_payload])
        await self.bus.publish(Event("position", {"strategy": decision.strategy, "symbol": snapshot.symbol, "status": "OPEN"}))

    async def manage_positions(self, symbol: str, as_of: datetime | None = None, extreme: dict | None = None) -> None:
        state = self.states.get(symbol)
        if state is None or state.last_price is None:
            return
        moment = as_of or utcnow()
        for key in STRATEGY_KEYS:
            async with self._locks[key]:
                await self._manage_one(key, symbol, state, moment, extreme)

    async def _manage_one(self, key: str, symbol: str, state, as_of: datetime, extreme: dict | None) -> None:
        account = self.accounts.accounts[key]
        if state.last_price is None:
            return
        positions = [position for position in list(account.positions.values()) if position.symbol == symbol]
        for position in positions:
            await self._manage_position(key, symbol, position, state, as_of, extreme)

    async def _manage_position(self, key: str, symbol: str, position, state, as_of: datetime, extreme: dict | None) -> None:
        observed = _observed_quotes(state, extreme)
        position_id = str(position.position_id)
        self.accounts.update_excursion(key, position_id, observed["min_bid"] or state.last_price)
        self.accounts.update_excursion(key, position_id, observed["max_bid"] or state.last_price)
        if position.side is Action.SHORT:
            self.accounts.update_excursion(key, position_id, observed["min_ask"] or state.last_price)
            self.accounts.update_excursion(key, position_id, observed["max_ask"] or state.last_price)
        hold_minutes = max(0.0, (as_of - position.opened_at).total_seconds() / 60)
        reason = position_exit_observed(
            position.side,
            min_bid=observed["min_bid"],
            max_bid=observed["max_bid"],
            min_ask=observed["min_ask"],
            max_ask=observed["max_ask"],
            stop=position.stop,
            target=position.target,
            hold_minutes=hold_minutes,
            max_hold_minutes=self.risk.limits.max_hold_minutes,
            entry=position.entry_price,
            bid=state.best_bid,
            ask=state.best_ask,
        )
        if reason is None:
            await self._step_stop(key, symbol, position, observed)
            return
        if position.mode == "live":
            await self._exit_live(key, symbol, position, reason, as_of, state)
            return
        from app.execution.slippage import simulate_fill

        side = "SELL" if position.side is Action.LONG else "BUY"
        bid, ask, book = _exit_touch(position, reason, state)
        preview = simulate_fill(
            side=side,
            quantity=position.quantity,
            best_bid=bid,
            best_ask=ask,
            book=book,
            config=self.slippage,
        )
        if preview.filled_quantity <= 0:
            return
        exit_price = preview.estimated_fill_price
        exit_fee = _exit_fee(position, exit_price, preview.filled_quantity, self.fee_config)
        order = OrderRecord(
            client_order_id=position.decision_id.hex[:31] + "X",
            decision_id=position.decision_id,
            strategy=position.strategy,
            symbol=symbol,
            side=side,  # type: ignore[arg-type]
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
            mode="paper",
            quantity=position.quantity,
            filled_quantity=preview.filled_quantity,
            average_fill_price=exit_price,
            expected_price=preview.expected_price,
            created_at=as_of,
        )
        fill = FillRecord(
            order_id=order.order_id,
            decision_id=position.decision_id,
            price=exit_price,
            quantity=preview.filled_quantity,
            fee=exit_fee,
            slippage_bps=preview.slippage_bps,
            liquidity="taker",
            filled_at=as_of,
        )
        touch = preview.expected_price or exit_price
        slippage_quote = abs(exit_price - touch) * preview.filled_quantity
        await self._finish_exit(
            key,
            symbol,
            position,
            reason,
            as_of,
            state,
            exit_price,
            preview.filled_quantity,
            exit_fee,
            slippage_quote,
            order,
            fill,
        )

    async def _exit_live(self, key: str, symbol: str, position, reason: str, as_of: datetime, state) -> None:
        if reason == "STOP" and position.stop_client_order_id:
            found = await self.live.fetch(symbol, position.stop_client_order_id)
            if found and str(found.get("status")) == "FILLED":
                qty, price = execution_of(found)
                if qty > 0 and price > 0:
                    exit_fee = _exit_fee(position, price, qty, self.fee_config)
                    await self._finish_exit(key, symbol, position, reason, as_of, state, price, qty, exit_fee, 0.0, None, None)
                    return
        if position.stop_client_order_id:
            try:
                await self.live.cancel(symbol, position.stop_client_order_id)
            except Exception as exc:
                self._log("live_stop_cancel", str(exc))
        side = "SELL" if position.side is Action.LONG else "BUY"
        intent = OrderIntent(
            decision_id=position.decision_id,
            risk_id=None,
            strategy=position.strategy,
            symbol=symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=position.quantity,
            limit_price=None,
            mode="live",
            created_at=as_of,
            best_bid=state.best_bid,
            best_ask=state.best_ask,
            book=state.book,
            fee_rate=position.exit_fee_rate,
        )
        try:
            order = await self.live.submit_close(intent)
        except Exception as exc:
            self._log("live_exit", str(exc))
            return
        if order.filled_quantity <= 0 or not order.average_fill_price:
            self.store.add_order(order)
            self._log("live_exit", "unfilled")
            return
        exit_price = order.average_fill_price
        exit_fee = _exit_fee(position, exit_price, order.filled_quantity, self.fee_config)
        fill = FillRecord(
            order_id=order.order_id,
            decision_id=position.decision_id,
            price=exit_price,
            quantity=order.filled_quantity,
            fee=exit_fee,
            slippage_bps=0,
            liquidity="taker",
            filled_at=as_of,
        )
        touch = position.stop if reason == "STOP" else position.target if reason == "TARGET" else exit_price
        slippage_quote = abs(exit_price - touch) * order.filled_quantity
        await self._finish_exit(
            key,
            symbol,
            position,
            reason,
            as_of,
            state,
            exit_price,
            order.filled_quantity,
            exit_fee,
            slippage_quote,
            order,
            fill,
        )

    async def _step_stop(self, key: str, symbol: str, position, observed: dict) -> None:
        favorable = observed["max_bid"] if position.side is Action.LONG else observed["min_ask"]
        updated = stepped_stop(position, favorable)
        if updated is None:
            return
        if position.initial_stop is None:
            position.initial_stop = position.stop
        position.stop = updated
        await self._checkpoint(key, positions=[position], orders=[], fills=[])
        self._log("stop", f"{key} {symbol} {updated:.8f}")

    async def _finish_exit(
        self,
        key: str,
        symbol: str,
        position,
        reason: str,
        as_of: datetime,
        state,
        exit_price: float,
        quantity: float,
        exit_fee: float,
        slippage_quote: float,
        order,
        fill,
    ) -> None:
        hold_minutes = max(0.0, (as_of - position.opened_at).total_seconds() / 60)
        periods = 0.0
        if self.risk.limits.apply_funding and hold_minutes >= self.risk.limits.funding_interval_minutes:
            periods = hold_minutes / self.risk.limits.funding_interval_minutes
        funding = funding_cashflow(position.side, state.funding_rate, exit_price * quantity, periods)
        trade = self.accounts.close_position(
            strategy=key,
            position_id=str(position.position_id),
            exit_price=exit_price,
            closed_at=as_of,
            exit_fee=exit_fee,
            slippage=slippage_quote,
            funding=funding,
            exit_reason=reason,
            quantitative_regime=position.quantitative_regime,
            market_regime=position.market_regime,
        )
        trade_payload = self.store.add_trade(trade)
        orders = []
        fills = []
        if order is not None:
            orders.append(self.store.add_order(order))
        if fill is not None:
            fills.append(self.store.add_fill(fill))
        await self._checkpoint(key, positions=[position], orders=orders, fills=fills, trade=trade_payload)
        await self.bus.publish(Event("trade", trade_payload))
        self._log("trade", f"{key} {symbol} {reason} net {trade.net_pnl:.4f}")

    def _intent(self, decision: StrategyDecision, snapshot: MarketSnapshot, risk, side: str, mode: str) -> OrderIntent:
        econ = risk.economics
        return OrderIntent(
            decision_id=decision.decision_id,
            risk_id=risk.risk_id,
            strategy=decision.strategy,
            symbol=snapshot.symbol,
            side=side,
            order_type=OrderType.MARKET,
            quantity=econ.quantity,
            limit_price=None,
            mode=mode,
            created_at=snapshot.timestamp,
            best_bid=snapshot.best_bid,
            best_ask=snapshot.best_ask,
            book=self.states[snapshot.symbol].book,
            fee_rate=econ.costs.fee_rate_entry,
        )

    def _open_from_fill(self, decision, snapshot, econ, price: float, quantity: float, fee: float, mode: str, stop_client_order_id: str | None):
        margin = (price * quantity) / max(self.risk.limits.max_leverage, 1e-9)
        regime = decision.metadata.get("openai_regime")
        return self.accounts.open_position(
            strategy=decision.strategy,
            symbol=snapshot.symbol,
            side=decision.action,
            quantity=quantity,
            entry_price=price,
            stop=econ.stop,
            target=econ.target,
            opened_at=snapshot.timestamp,
            decision_id=decision.decision_id,
            entry_fee=fee,
            initial_net_risk=econ.net_risk,
            margin=margin,
            exit_fee_rate=econ.costs.fee_rate_exit,
            fee_source=econ.costs.fee_source,
            quantitative_regime=snapshot.quantitative_regime,
            market_regime=regime if isinstance(regime, str) else None,
            mode=mode,
            stop_client_order_id=stop_client_order_id,
        )

    async def _record_risk(self, risk) -> None:
        payload = self.store.add_risk(risk)
        if self.postgres and self.postgres.healthy:
            await self.postgres.save_risk(payload)
        await self.bus.publish(Event("risk", {"decision_id": str(risk.decision_id), "accepted": risk.accepted}))

    async def _checkpoint(
        self,
        strategy: str,
        positions: list,
        orders: list[dict],
        fills: list[dict],
        trade: dict | None = None,
    ) -> None:
        if self.postgres is None or not self.postgres.healthy:
            return
        dumped = [item if isinstance(item, dict) else item.model_dump(mode="json") for item in positions]
        await self.postgres.save_checkpoint(
            strategy,
            self.accounts.export_strategy(strategy, self._marks()),
            dumped,
            orders,
            fills,
            trade,
        )

    def apply_runtime(self, runtime: dict) -> None:
        saved_accounts = set()
        accounts = runtime.get("accounts") or {}
        if accounts:
            self.accounts.restore_state(accounts)
            saved_accounts = set(accounts)
        for strategy, rows in (runtime.get("orphan_trades") or {}).items():
            if strategy in saved_accounts or strategy not in self.accounts.accounts:
                continue
            self.accounts.adopt_trades(strategy, rows)
        self.store.trades = []
        for account in self.accounts.accounts.values():
            for trade in account.trades:
                self.store.add_trade(trade)
        for item in runtime.get("strategies") or []:
            current = self.strategy_settings.get(item.get("strategy_key"))
            if current is None:
                continue
            current.enabled = bool(item.get("enabled", current.enabled))
            config = item.get("config") or {}
            if "call_model" in config:
                current.call_model = bool(config["call_model"])
            if "max_signal_age_ms" in config:
                current.max_signal_age_ms = int(config["max_signal_age_ms"])
            mode = item.get("mode")
            if mode == "live" and not self.settings.live_armed:
                self._log("restore", f"{current.key} live config left disarmed")
            elif mode:
                current.mode = OperatingMode(mode)
        risk = runtime.get("risk") or {}
        editable = {key: risk[key] for key in EDITABLE_RISK if key in risk}
        if editable:
            self.risk.update_limits(**_coerce_risk(editable))
            self.risk_limits = self.risk.limits

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
            has_position_on_symbol=any(position.symbol == snapshot.symbol for position in account.positions.values()),
            last_entry_at=account.last_entry_by_symbol.get(snapshot.symbol),
            last_stop_at=account.last_stop_at.get(snapshot.symbol),
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
            account = self.accounts.accounts[key]
            trades = list(account.trades)
            marked = account.equity(self._marks())
            reports[key] = summarize_trades(trades, self.settings.initial_paper_equity, mark=marked)
            reports[key]["marked_pnl"] = marked - self.settings.initial_paper_equity
            reports[key]["by_regime"] = slice_performance(trades, self.settings.initial_paper_equity)
            reports[key]["equity"] = marked
            reports[key]["mode"] = self.strategy_settings[key].mode.value
            reports[key]["enabled"] = self.strategy_settings[key].enabled
        return reports

    def comparison(self) -> dict:
        cost = self.openai_cost_total if self.openai_cost_known else None
        return compare_strategies(
            baseline=self.accounts.accounts["baseline"].trades,
            baseline_jev=self.accounts.accounts["baseline_jev"].trades,
            openai_jev=[],
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
            for position in account.positions.values():
                mark = marks.get(position.symbol, position.entry_price)
                notional = abs(mark * position.quantity)
                margin = account.margin_locked.get(str(position.position_id))
                if margin is None:
                    margin = account.margin_locked.get(position.symbol)
                rows.append(
                    {
                        "position_id": str(position.position_id),
                        "strategy": key,
                        "symbol": position.symbol,
                        "side": position.side.value,
                        "quantity": position.quantity,
                        "entry": position.entry_price,
                        "stop": position.stop,
                        "target": position.target,
                        "mark": mark,
                        "notional": notional,
                        "margin": margin,
                        "leverage": (notional / margin) if margin and margin > 0 else None,
                        "unrealized": unrealized(position, mark),
                        "target_pnl": unrealized(position, position.target),
                        "stop_pnl": unrealized(position, position.stop),
                        "mfe": position.mfe,
                        "mae": position.mae,
                        "opened_at": position.opened_at.isoformat(),
                    }
                )
        return rows

    def paper_book(self) -> dict:
        marks = self._marks()
        start = self.settings.initial_paper_equity
        accounts = []
        for key in STRATEGY_KEYS:
            account = self.accounts.accounts[key]
            locked = sum(account.margin_locked.values())
            equity = account.equity(marks)
            accounts.append(
                {
                    "strategy": key,
                    "starting_equity": start,
                    "cash": account.cash,
                    "margin": locked,
                    "equity": equity,
                    "unrealized": equity - account.cash - locked,
                    "realized_today": account.realized_pnl_today,
                    "net_pnl": equity - start,
                    "open_positions": len(account.positions),
                }
            )
        return {"starting_equity": start, "quote": "USDT", "leverage": self.risk.limits.max_leverage, "accounts": accounts}

    def status(self) -> dict:
        jev_name = getattr(self.jev, "provider_name", "unknown")
        openai_status = "not_configured"
        if self.openai.configured():
            openai_status = "open_circuit" if self.openai.breaker.state == "open" else "configured"
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


def _extreme(batch: list[dict]) -> dict:
    bids = [point["bid"] for point in batch if point.get("bid") is not None]
    asks = [point["ask"] for point in batch if point.get("ask") is not None]
    return {
        "min_bid": min(bids) if bids else None,
        "max_bid": max(bids) if bids else None,
        "min_ask": min(asks) if asks else None,
        "max_ask": max(asks) if asks else None,
    }


def _observed_quotes(state, extreme: dict | None) -> dict:
    if not extreme:
        return {
            "min_bid": state.best_bid,
            "max_bid": state.best_bid,
            "min_ask": state.best_ask,
            "max_ask": state.best_ask,
        }
    return {
        "min_bid": extreme["min_bid"] if extreme.get("min_bid") is not None else state.best_bid,
        "max_bid": extreme["max_bid"] if extreme.get("max_bid") is not None else state.best_bid,
        "min_ask": extreme["min_ask"] if extreme.get("min_ask") is not None else state.best_ask,
        "max_ask": extreme["max_ask"] if extreme.get("max_ask") is not None else state.best_ask,
    }


def _exit_touch(position, reason: str, state) -> tuple[float | None, float | None, object | None]:
    if reason == "STOP" and position.side is Action.LONG:
        return position.stop, state.best_ask, None
    if reason == "TARGET" and position.side is Action.LONG:
        return position.target, state.best_ask, None
    if reason == "STOP" and position.side is Action.SHORT:
        return state.best_bid, position.stop, None
    if reason == "TARGET" and position.side is Action.SHORT:
        return state.best_bid, position.target, None
    return state.best_bid, state.best_ask, state.book


def _exit_fee(position, exit_price: float, quantity: float, fee_config) -> float:
    rate = position.exit_fee_rate
    if rate <= 0:
        rate = rate_for("taker", fee_config.maker_fee_rate, fee_config.taker_fee_rate)
    return exit_price * quantity * rate


def _coerce_risk(changes: dict) -> dict:
    cleaned = dict(changes)
    if "max_open_positions" in cleaned:
        cleaned["max_open_positions"] = int(cleaned["max_open_positions"])
    return cleaned
