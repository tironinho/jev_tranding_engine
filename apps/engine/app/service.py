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
    STRATEGY_ERROR,
    Action,
    OperatingMode,
    OrderStatus,
    OrderType,
    SignalStatus,
    SlippageModelName,
)
from app.domain.mathutil import utcnow
from app.domain.schemas import AuditRecord, FillRecord, MarketSnapshot, OrderRecord, StrategyDecision
from app.events.bus import EngineLogBuffer, Event, EventBus
from app.intelligence.runner import IntelligenceRunner
from app.execution.binance_live import (
    BinanceExecutionProvider,
    LiveExecutionBlocked,
    base_asset,
    execution_of,
    held_quantity,
    lot_quantity,
    sellable_quantity,
)
from app.execution.paper import OrderIntent, PaperExecutionProvider, position_exit_observed, stepped_stop
from app.execution.slippage import SlippageConfig
from app.execution.reconciliation import reconcile
from app.evolution.service import EvolutionService
from app.features.engine import build_snapshot
from app.market.feed import MarketFeed
from app.market.state import SymbolMarketState
from app.providers.binance_account import BinanceBalanceProvider
from app.providers.fees import BinanceFeeProvider, ConfigFeeProvider
from app.providers.jev.factory import build_jev_provider
from app.providers.openai.provider import OpenAIProvider
from app.risk.economics import funding_cashflow, rate_for
from app.risk.engine import RiskContext, RiskEngine, margin_borrow_room
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
        self.pending_entries: dict[str, dict] = {}
        self._real_day = None
        self._real_day_start: float | None = None
        self._stop_checked: dict[str, float] = {}
        self._repaired_longs: set[str] = set()
        self.balance_points: list[dict] = []
        self._last_wallet: float | None = None
        self._close_batch: list[tuple[str, datetime, str]] = []
        self._close_flush: asyncio.Task | None = None
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
        if self.settings.service_role == "account":
            self._log("engine", "account gateway started")
            return
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
                    self.balance_points = await self.postgres.load_balance_points()
                    if self.balance_points:
                        self._last_wallet = self.balance_points[-1]["wallet"]
                    self._log("restore", "book restored from postgres")
        await self.feed.start()
        self.evolution.bind_research_client(self._http)
        if self.postgres is not None and self.postgres.factory is not None and self.db_healthy:
            await self.evolution.attach_postgres(self.postgres.factory)
        await self.evolution.start()
        self._arm_live_strategy()
        task = asyncio.create_task(self._balance_loop(), name="balance_samples")
        self._background.add(task)
        task.add_done_callback(self._background.discard)
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
        return self.persistence_mode == "postgres" and self.db_healthy and (self.postgres is None or self.postgres.healthy) and self.settings.live_armed

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
        self._close_batch.append((symbol, as_of, trigger))
        if self._close_flush is None or self._close_flush.done():
            self._close_flush = asyncio.create_task(self._flush_closes())
            self._background.add(self._close_flush)
            self._close_flush.add_done_callback(self._background.discard)

    async def _flush_closes(self) -> None:
        """Candle closes of the same minute arrive a few messages apart. Start them together."""
        await asyncio.sleep(0.3)
        while self._close_batch:
            batch = self._close_batch
            self._close_batch = []
            await asyncio.gather(
                *(self.evaluate_symbol(symbol, as_of, trigger) for symbol, as_of, trigger in batch),
                return_exceptions=True,
            )

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
        started = time.perf_counter()
        decision, context, _budget_ms = await self._decide(key, snapshot, opportunity_id, correlation_id, started, waited_ms=0)
        async with self._locks[key]:
            return await self._persist_strategy(key, snapshot, decision, context, started)

    async def _decide(self, key: str, snapshot: MarketSnapshot, opportunity_id, correlation_id, started: float, waited_ms: float):
        cfg = self.strategy_settings[key]
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
        self.intelligence.note_marks(self._marks(), snapshot.timestamp)
        context.intelligence = self.intelligence.context_for(snapshot.symbol, snapshot.features, snapshot.timestamp)
        budget_ms = cfg.max_signal_age_ms
        if cfg.mode is OperatingMode.LIVE:
            waited_ms = max(waited_ms, (utcnow() - snapshot.timestamp).total_seconds() * 1000)
        if key == "baseline_openai_jev":
            budget_ms = max(budget_ms, int((self.settings.openai_timeout_s + 4) * 1000))
        if waited_ms > budget_ms:
            decision = self._error_decision(key, snapshot, opportunity_id, correlation_id, EXPIRED_SIGNAL)
            decision.signal_status = SignalStatus.EXPIRED
            return decision, context, budget_ms
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
        if cfg.mode is OperatingMode.LIVE:
            decision.metadata["snapshot_at"] = snapshot.timestamp.isoformat()
            decision.metadata["decided_at"] = utcnow().isoformat()
            decision.timestamp = utcnow()
        return decision, context, budget_ms

    async def _persist_strategy(self, key: str, snapshot: MarketSnapshot, decision, context, started: float):
        elapsed = (time.perf_counter() - started) * 1000
        self.health[key]["last_latency_ms"] = elapsed
        self.health[key]["last_decision_at"] = snapshot.timestamp.isoformat()
        if decision.action is Action.NO_TRADE:
            reasons = ",".join(decision.reason_codes[:4]) or "NO_REASON"
            self._log("decision", f"{key} {snapshot.symbol} NO_TRADE {decision.confidence:.3f} {reasons}")
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

    def _can_execute(self, decision: StrategyDecision) -> bool:
        cfg = self.strategy_settings.get(decision.strategy)
        if cfg is None or not cfg.enabled or cfg.mode not in (OperatingMode.PAPER, OperatingMode.LIVE):
            return False
        return decision.action is not Action.NO_TRADE and decision.signal_status is SignalStatus.VALID

    async def _execute_decisions(self, decisions: list[StrategyDecision], snapshot: MarketSnapshot) -> None:
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
        async with self._locks[decision.strategy]:
            await self._execute_paper_locked(decision, snapshot, fees, extra_slot)

    async def _execute_paper_locked(self, decision: StrategyDecision, snapshot: MarketSnapshot, fees, extra_slot: bool = False) -> None:
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

    def _live_signal_fresh(self, decision, snapshot) -> bool:
        age = (utcnow() - snapshot.timestamp).total_seconds() * 1000
        state = self.states[snapshot.symbol]
        price = state.best_ask if decision.action is Action.LONG else state.best_bid
        reference = snapshot.best_ask if decision.action is Action.LONG else snapshot.best_bid
        book_age = (utcnow() - state.last_book_at).total_seconds() * 1000 if state.last_book_at else float("inf")
        return (0 <= age <= self.strategy_settings[decision.strategy].max_signal_age_ms
                and 0 <= book_age <= self.settings.stale_after_ms
                and price is not None and reference is not None
                and abs(price / reference - 1) <= 0.001)

    def _order_fee(self, order, estimate: float) -> float:
        if not order.commissions:
            return estimate
        total = 0.0
        for asset, quantity in order.commissions.items():
            mark = 1 if asset == "USDT" else self._marks().get(f"{asset}USDT")
            if mark is None:
                return estimate
            total += quantity * mark
        return total

    async def _execute_live(self, decision: StrategyDecision, snapshot: MarketSnapshot, extra_slot: bool = False) -> None:
        fees = await self.fee_provider.get_fees(snapshot.symbol)
        async with self._locks[decision.strategy]:
            await self._execute_live_locked(decision, snapshot, fees, extra_slot)

    async def _execute_live_locked(self, decision: StrategyDecision, snapshot: MarketSnapshot, fees, extra_slot: bool = False) -> None:
        if any(p.mode == "live" and p.protection_status not in {"PROTECTED", "RESIDUAL"}
               for a in self.accounts.accounts.values() for p in a.positions.values()):
            await self._record_risk(self.risk._reject(decision, ["UNPROTECTED_POSITION"]))
            return
        if self.pending_entries:
            await self._record_risk(self.risk._reject(decision, ["PENDING_ENTRY_RECONCILIATION"]))
            return
        if not self.allows_new_live():
            self._log("live_blocked", "LIVE_LOCKED")
            self.store.add_event("live_blocked", "LIVE_LOCKED", {"decision_id": str(decision.decision_id)})
            return
        context = self._risk_context(decision.strategy, snapshot, live=True)
        if self.settings.market_type == "margin":
            balance = self.mark_account(await self.balance.snapshot(fresh=True))
            equity = balance.get("equity_usdt")
            wallet = balance.get("wallet")
            if balance.get("status") != "ok":
                self._log("live_blocked", "NO_REAL_EQUITY")
                return
            # USDT net is the borrow. The collateral is the marked account, and the order borrows against it.
            if not isinstance(equity, (int, float)) or equity <= 0:
                if isinstance(wallet, (int, float)) and wallet > 0 and not self._holds_coin(balance):
                    equity = float(wallet)
                else:
                    self._log("live_blocked", "NO_REAL_EQUITY")
                    return
            level = balance.get("margin_level")
            level_value = float(level) if isinstance(level, (int, float)) else None
            room = margin_borrow_room(float(equity), level_value, self.risk.limits.min_margin_level)
            leverage = self.risk.limits.max_leverage
            context.equity = float(equity)
            context.cash = room / leverage if leverage > 0 else 0.0
            context.margin_level = level_value
            day = snapshot.timestamp.astimezone(timezone.utc).date()
            if self._real_day != day or self._real_day_start is None:
                self._real_day = day
                self._real_day_start = float(equity)
            context.day_start_equity = self._real_day_start
            points = [point for point in self.balance_points if datetime.fromisoformat(point["t"]).astimezone(timezone.utc).date() == day]
            if points:
                self._real_day_start = float(points[0]["wallet"])
            context.day_start_equity = self._real_day_start
            context.realized_pnl_today = sum(
                trade.net_pnl for account in self.accounts.accounts.values() for trade in account.trades
                if trade.mode == "live" and trade.closed_at.astimezone(timezone.utc).date() == day)
            report = reconcile(balance, self.positions_payload(), self._marks(), self.feed.rules)
            if report["blocks_entry"]:
                await self._record_risk(self.risk._reject(decision, ["ACCOUNT_RECONCILIATION_REQUIRED"], details=report))
                return
            exposures = [row for row in report["rows"] if row["status"] != "CASH"]
            context.total_exposure_notional = sum(abs(row["value_usdt"] or 0) for row in exposures)
            context.symbol_exposure_notional = sum(abs(row["value_usdt"] or 0) for row in exposures if row["symbol"] == snapshot.symbol)
            await self.note_balance(balance)
        risk = self.risk.evaluate(decision, snapshot, context, fees, self.states[snapshot.symbol].book, extra_slot=extra_slot)
        await self._record_risk(risk)
        if not risk.accepted or risk.economics is None:
            return
        econ = risk.economics
        side = "BUY" if decision.action is Action.LONG else "SELL"
        if not self._live_signal_fresh(decision, snapshot):
            await self._record_risk(self.risk._reject(decision, [EXPIRED_SIGNAL]))
            return
        intent = self._intent(decision, snapshot, risk, side, "live")
        pending = {"decision": decision.model_dump(mode="json"), "snapshot": snapshot.model_dump(mode="json"),
                   "risk": risk.model_dump(mode="json"), "created_at": utcnow().isoformat()}
        self.pending_entries[str(decision.decision_id)] = pending
        if self.postgres:
            await self.postgres.save_event("live_entry_intent", str(decision.decision_id), pending)
            if not self.postgres.healthy:
                self._log("live_blocked", "ENTRY_INTENT_NOT_DURABLE")
                return
        if not self._live_signal_fresh(decision, snapshot):
            await self._resolve_entry(str(decision.decision_id))
            await self._record_risk(self.risk._reject(decision, [EXPIRED_SIGNAL]))
            return
        try:
            order = await self.live.submit(intent)
        except LiveExecutionBlocked as exc:
            self._log("live_blocked", exc.reason)
            return
        except Exception as exc:
            self._log("live_error", str(exc))
            return
        await self._record_live_entry(decision, snapshot, risk, order)

    async def _resolve_entry(self, decision_id: str) -> None:
        if self.postgres:
            await self.postgres.save_event("live_entry_resolved", decision_id, {"decision_id": decision_id})
            if not self.postgres.healthy:
                return
        self.pending_entries.pop(decision_id, None)

    async def _recover_pending_entries(self) -> None:
        from app.domain.schemas import RiskDecision
        from app.execution.binance_live import _order_from_payload
        for identity, pending in list(self.pending_entries.items()):
            decision = StrategyDecision.model_validate(pending["decision"])
            async with self._locks[decision.strategy]:
                existing = any(str(p.decision_id) == identity for a in self.accounts.accounts.values() for p in a.positions.values())
                closed = any(str(t.decision_id) == identity for a in self.accounts.accounts.values() for t in a.trades)
                if existing or closed:
                    await self._resolve_entry(identity)
                    continue
                try:
                    found = await self.live.fetch_verified(decision.symbol, decision.decision_id.hex)
                except Exception as exc:
                    self._log("live_recovery", f"{decision.symbol} decision={identity} {exc}")
                    continue
                if found.get("status") == "NOT_FOUND":
                    # Do not clear a very recent ambiguous request while the exchange may still process it.
                    age = (utcnow() - datetime.fromisoformat(pending["created_at"])).total_seconds()
                    if age > 60:
                        await self._resolve_entry(identity)
                    continue
                if found.get("status") not in {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}:
                    continue
                snapshot = MarketSnapshot.model_validate(pending["snapshot"])
                risk = RiskDecision.model_validate(pending["risk"])
                intent = self._intent(decision, snapshot, risk, "BUY" if decision.action is Action.LONG else "SELL", "live")
                order = _order_from_payload(intent, found, decision.decision_id.hex, OrderType.MARKET)
                await self._record_live_entry(decision, snapshot, risk, order)

    async def _record_live_entry(self, decision, snapshot, risk, order) -> None:
        econ = risk.economics
        order_payload = self.store.add_order(order)
        if order.filled_quantity <= 0 or not order.average_fill_price:
            self._log("live_unfilled", order.client_order_id)
            await self._checkpoint(decision.strategy, positions=[], orders=[order_payload], fills=[])
            if order.status in {OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED}:
                await self._resolve_entry(str(decision.decision_id))
            return
        step = self.feed.rules.get(snapshot.symbol, {}).get("step_size")
        held = lot_quantity(order.filled_quantity, step)
        if held <= 0:
            held = order.filled_quantity
        fee = self._order_fee(order, order.average_fill_price * held * econ.costs.fee_rate_entry)
        position = self._open_from_fill(
            decision,
            snapshot,
            econ,
            order.average_fill_price,
            held,
            fee,
            "live",
            None,
        )
        fill = FillRecord(
            order_id=order.order_id,
            decision_id=decision.decision_id,
            price=order.average_fill_price,
            quantity=held,
            fee=fee,
            slippage_bps=0,
            liquidity="taker",
            filled_at=order.exchange_at or order.received_at or utcnow(),
        )
        position.opened_at = fill.filled_at
        fill_payload = self.store.add_fill(fill)
        position.protection_status = "UNPROTECTED"
        position.stop_client_order_id = decision.decision_id.hex[:31] + "S"
        # Persist the filled position before attempting exchange protection.
        await self._checkpoint(decision.strategy, positions=[position], orders=[order_payload], fills=[fill_payload])
        tick = self.feed.rules.get(snapshot.symbol, {}).get("tick_size")
        intent = self._intent(decision, snapshot, risk, "BUY" if decision.action is Action.LONG else "SELL", "live")
        intent.quantity = held
        if decision.action is Action.LONG and self.settings.market_type == "margin":
            intent.quantity = sellable_quantity(held, await self._free_base(snapshot.symbol, fresh=True), step)
        if intent.quantity <= 0:
            await self._exit_live(decision.strategy, snapshot.symbol, position, "PROTECTION_FAILED", utcnow(), self.states[snapshot.symbol])
            return
        position.stop_client_order_id = decision.decision_id.hex[:31] + "S"
        try:
            stop = await self.live.submit_stop(intent, position.stop, tick=tick)
        except Exception as exc:
            self._log("live_stop", f"{snapshot.symbol} decision={decision.decision_id} {exc}")
            stop = None
        orders = [order_payload]
        if stop is not None:
            position.protection_status = "PROTECTED"
            position.stop_client_order_id = stop.client_order_id
            orders.append(self.store.add_order(stop))
        await self._checkpoint(decision.strategy, positions=[position], orders=orders, fills=[fill_payload])
        await self._resolve_entry(str(decision.decision_id))
        if stop is None:
            position.protection_status = "UNPROTECTED"
            await self._checkpoint(decision.strategy, positions=[position], orders=[], fills=[])
            await self._exit_live(decision.strategy, snapshot.symbol, position, "PROTECTION_FAILED", utcnow(), self.states[snapshot.symbol])
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
        if position.mode == "live":
            if position.protection_status == "RESIDUAL":
                return
            if position.protection_status == "UNPROTECTED" or not position.stop_client_order_id:
                await self._exit_live(key, symbol, position, "PROTECTION_FAILED", as_of, state)
                return
            identity = str(position.position_id)
            if time.monotonic() - self._stop_checked.get(identity, 0) >= 5:
                self._stop_checked[identity] = time.monotonic()
                try:
                    found = await self.live.fetch_verified(symbol, position.stop_client_order_id)
                except Exception:
                    position.protection_status = "UNKNOWN"
                    found = None
                if found:
                    if found.get("status") in {"FILLED", "PARTIALLY_FILLED"}:
                        await self._exit_live(key, symbol, position, "STOP", as_of, state)
                        return
                    if found.get("status") == "NEW":
                        position.protection_status = "PROTECTED"
                    else:
                        position.protection_status = "UNPROTECTED"
                        await self._exit_live(key, symbol, position, "PROTECTION_FAILED", as_of, state)
                        return
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
        tag = f"{symbol} decision={position.decision_id} position={position.position_id}"
        # Reconcile the stop on EVERY exit path, including time and target exits.
        if position.stop_client_order_id:
            client_id = position.stop_client_order_id
            try:
                found = await self.live.fetch_verified(symbol, client_id)
                if found.get("status") in {"NEW", "PARTIALLY_FILLED"}:
                    try:
                        await self.live.cancel(symbol, client_id)
                    except Exception:
                        pass  # A cancellation race must be resolved by a subsequent read.
                    found = await self.live.fetch_verified(symbol, client_id)
                if found.get("status") not in {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "NOT_FOUND"}:
                    self._log("live_exit_blocked", f"{tag} STOP_STATUS_UNKNOWN")
                    return
            except Exception as exc:
                self._log("live_exit_blocked", f"{tag} STOP_RECONCILIATION {exc}")
                return
            qty, price = execution_of(found)
            already = position.exit_processed.get(client_id, 0)
            quantity = max(0, qty - already)
            position.stop_client_order_id = None
            position.protection_status = "UNPROTECTED"
            # Persist the exchange order lifecycle, preserving its identity.
            updated = []
            for stored in self.store.orders.values():
                if stored.get("client_order_id") == client_id and found.get("status") != "NOT_FOUND":
                    stored.update(status=found["status"], filled_quantity=qty, average_fill_price=price or None)
                    updated.append(stored)
            if quantity > 0 and price > 0:
                position.exit_processed[client_id] = qty
                await self._finish_exit(key, symbol, position, "STOP", utcnow(), state, price,
                                        min(quantity, position.quantity), _exit_fee(position, price, quantity, self.fee_config), 0, None, None)
                await self._checkpoint(key, positions=[position], orders=updated, fills=[])
                if str(position.position_id) not in self.accounts.accounts[key].positions:
                    return
            else:
                await self._checkpoint(key, positions=[position], orders=updated, fills=[])
        quantity = position.quantity
        if self.settings.market_type == "margin":
            balance = await self.balance.snapshot(fresh=True)
            if balance.get("status") != "ok":
                self._log("live_exit_blocked", f"{tag} ACCOUNT_UNAVAILABLE")
                return
            asset = next((r for r in balance.get("assets", []) if r.get("asset") == base_asset(symbol)), {})
            if position.side is Action.LONG:
                quantity = sellable_quantity(quantity, float(asset.get("free") or 0), self.feed.rules.get(symbol, {}).get("step_size"))
            else:
                # Never buy the original short again after an external/stop close.
                debt_exposure = max(0, -float(asset.get("total") or 0))
                quantity = lot_quantity(min(quantity, debt_exposure), self.feed.rules.get(symbol, {}).get("step_size"))
        if quantity <= 0:
            self._log("live_exit_blocked", f"{tag} NO_CLOSABLE_BALANCE_RECONCILE")
            return
        intent = OrderIntent(
            decision_id=position.decision_id, risk_id=None, strategy=position.strategy,
            symbol=symbol, side="SELL" if position.side is Action.LONG else "BUY",
            order_type=OrderType.MARKET, quantity=quantity, limit_price=None, mode="live",
            created_at=utcnow(), best_bid=state.best_bid, best_ask=state.best_ask,
            book=state.book, fee_rate=position.exit_fee_rate, close_sequence=position.close_sequence)
        # The sequence is checkpointed with the position; ambiguous retries retain the ID.
        await self._checkpoint(key, positions=[position], orders=[], fills=[])
        try:
            order = await self.live.submit_close(intent)
        except Exception as exc:
            self._log("live_exit", f"{tag} {exc}")
            return
        order_payload = self.store.add_order(order)
        if order.status not in {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.REJECTED}:
            await self._checkpoint(key, positions=[position], orders=[order_payload], fills=[])
            return
        already = position.exit_processed.get(order.client_order_id, 0)
        quantity = min(position.quantity, max(0, order.filled_quantity - already))
        position.close_sequence += 1
        if quantity <= 0 or not order.average_fill_price:
            await self._checkpoint(key, positions=[position], orders=[order_payload], fills=[])
            return
        position.exit_processed[order.client_order_id] = order.filled_quantity
        exit_price = order.average_fill_price
        exit_fee = self._order_fee(order, _exit_fee(position, exit_price, quantity, self.fee_config))
        moment = order.exchange_at or order.received_at or utcnow()
        fill = FillRecord(order_id=order.order_id, decision_id=position.decision_id,
                          price=exit_price, quantity=quantity, fee=exit_fee, slippage_bps=0,
                          liquidity="taker", filled_at=moment)
        touch = position.stop if reason == "STOP" else position.target if reason == "TARGET" else exit_price
        await self._finish_exit(key, symbol, position, reason, moment, state, exit_price,
                                quantity, exit_fee, abs(exit_price - touch) * quantity, order, fill)

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
            quantity=quantity,
        )
        remaining = self.accounts.accounts[key].positions.get(str(position.position_id))
        if remaining is not None and remaining.mode == "live":
            minimum = float(self.feed.rules.get(symbol, {}).get("min_notional") or self.risk.limits.min_order_notional)
            if remaining.quantity * exit_price < minimum:
                remaining.protection_status = "RESIDUAL"
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
            created_at=utcnow() if mode == "live" else snapshot.timestamp,
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
            opened_at=utcnow() if mode == "live" else snapshot.timestamp,
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

    def _arm_live_strategy(self) -> None:
        if not self.settings.live_armed:
            return
        jev = self.strategy_settings.get("baseline_jev")
        if jev is None:
            return
        jev.mode = OperatingMode.LIVE
        self._log("live", "baseline_jev live")

    def apply_runtime(self, runtime: dict) -> None:
        self.pending_entries = runtime.get("pending_entries") or {}
        saved_accounts = set()
        accounts = runtime.get("accounts") or {}
        if accounts:
            self.accounts.restore_state(accounts)
            saved_accounts = set(accounts)
        for strategy, rows in (runtime.get("trades") or {}).items():
            if strategy in saved_accounts:
                self.accounts.merge_trades(strategy, rows)
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
            if current.key == "baseline" and mode == "live":
                current.mode = OperatingMode.PAPER
                self._log("restore", "baseline comparison stays paper")
            elif mode == "live" and not self.settings.live_armed:
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
        rules = self.feed.rules.get(snapshot.symbol, {})
        step = rules.get("step_size")
        min_notional = rules.get("min_notional")
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
            min_notional=min_notional,
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
            start = self.settings.initial_paper_equity
            live_book = key == "baseline_jev" and self.settings.live_armed and self._last_wallet is not None
            if live_book:
                trades = [trade for trade in trades if trade.mode == "live"]
                start = self.balance_points[0]["wallet"] if self.balance_points else self._last_wallet
                marked = self._last_wallet
            reports[key] = summarize_trades(trades, start, mark=marked)
            reports[key]["marked_pnl"] = marked - start
            reports[key]["by_regime"] = slice_performance(trades, start)
            reports[key]["equity"] = marked
            reports[key]["starting_equity"] = start
            reports[key]["mode"] = self.strategy_settings[key].mode.value
            reports[key]["enabled"] = self.strategy_settings[key].enabled
            if live_book:
                peak = max(point["wallet"] for point in self.balance_points) if self.balance_points else marked
                reports[key]["max_drawdown"] = ((peak - marked) / peak) if peak else 0.0
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
        if self.balance_points:
            first = self.balance_points[0]["wallet"] or 1
            series["conta"] = [
                {
                    "t": point["t"],
                    "equity": point["wallet"],
                    "indexed": (point["wallet"] / first * 100) if first else None,
                    "mark": False,
                }
                for point in self.balance_points
            ]
        return {"starting_equity": start, "series": series, "account": self.account_curve()}

    def account_curve(self) -> dict:
        points = [{"t": point["t"], "equity": point["wallet"]} for point in self.balance_points]
        current = self._last_wallet if self._last_wallet is not None else (points[-1]["equity"] if points else None)
        started = points[0]["equity"] if points else None
        return {
            "started": started,
            "started_at": points[0]["t"] if points else None,
            "current": current,
            "change": (current - started) if current is not None and started is not None else None,
            "points": points,
        }

    def mark_account(self, payload: dict) -> dict:
        """USDT net is cash after the borrow. Equity marks every debt at the last price."""
        if payload.get("status") != "ok":
            return payload
        equity = 0.0
        for asset in payload.get("assets") or []:
            net = float(asset.get("total") or 0)
            name = str(asset.get("asset") or "")
            if abs(net) < 1e-8:
                continue
            if name == "USDT":
                equity += net
                continue
            state = self.states.get(f"{name}USDT")
            price = state.last_price if state is not None else None
            if price is None:
                return payload
            equity += net * price
        return {**payload, "equity_usdt": equity}

    def _holds_coin(self, payload: dict) -> bool:
        for asset in payload.get("assets") or []:
            if str(asset.get("asset") or "") == "USDT":
                continue
            if abs(float(asset.get("total") or 0)) >= 1e-4:
                return True
        return False

    async def note_balance(self, payload: dict) -> None:
        if payload.get("status") != "ok":
            return
        equity = payload.get("equity_usdt")
        wallet = payload.get("wallet")
        # Cash after a short is not the account. Skip until the coins are marked.
        if not isinstance(equity, (int, float)):
            if self._holds_coin(payload):
                return
            equity = wallet
        value = equity
        if not isinstance(value, (int, float)):
            return
        now = utcnow()
        previous = self.balance_points[-1] if self.balance_points else None
        if previous is not None:
            then = datetime.fromisoformat(previous["t"])
            if then.tzinfo is None:
                then = then.replace(tzinfo=timezone.utc)
            same = abs(float(previous["wallet"]) - float(value)) < 0.005
            if same and (now - then).total_seconds() < 60:
                self._last_wallet = float(value)
                return
        point = {"t": now.isoformat(), "wallet": float(value)}
        self.balance_points.append(point)
        self.balance_points = self.balance_points[-2000:]
        self._last_wallet = float(value)
        if self.postgres and self.postgres.healthy:
            await self.postgres.save_account_sample(self.account_sample(payload, float(value)))

    def account_sample(self, payload: dict, equity: float) -> dict:
        """Everything needed to rebuild this reading later. No secrets."""
        assets = []
        marks: dict[str, float] = {}
        for asset in payload.get("assets") or []:
            name = str(asset.get("asset") or "")
            total = float(asset.get("total") or 0)
            free = asset.get("free")
            free_value = float(free) if isinstance(free, (int, float)) else None
            if abs(total) < 1e-8 and (free_value is None or abs(free_value) < 1e-8):
                continue
            assets.append({"asset": name, "free": free_value, "total": total})
            if name != "USDT" and abs(total) >= 1e-8:
                state = self.states.get(f"{name}USDT")
                if state is not None and isinstance(state.last_price, (int, float)):
                    marks[f"{name}USDT"] = float(state.last_price)
        positions = []
        for row in self.positions_payload():
            positions.append(
                {
                    "strategy": row.get("strategy"),
                    "symbol": row.get("symbol"),
                    "side": row.get("side"),
                    "quantity": row.get("quantity"),
                    "entry": row.get("entry"),
                    "mark": row.get("mark"),
                    "notional": row.get("notional"),
                    "margin": row.get("margin"),
                    "leverage": row.get("leverage"),
                    "mode": row.get("mode"),
                    "unrealized": row.get("unrealized"),
                    "target_pnl": row.get("target_pnl"),
                    "stop_pnl": row.get("stop_pnl"),
                    "opened_at": row.get("opened_at"),
                }
            )
        return {
            "equity": equity,
            "wallet": payload.get("wallet"),
            "available": payload.get("available"),
            "unrealized": payload.get("unrealized"),
            "margin_level": payload.get("margin_level"),
            "market_type": payload.get("market_type"),
            "assets": assets,
            "marks": marks,
            "positions": positions,
        }

    async def _balance_loop(self) -> None:
        while True:
            try:
                await self._recover_pending_entries()
                await self.note_balance(self.mark_account(await self.balance.snapshot()))
                for symbol in self.states:
                    await self.manage_positions(symbol)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log("balance", type(exc).__name__)
            await asyncio.sleep(60)

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
                        "mode": position.mode,
                        "unrealized": _marked_net(position, mark, self.fee_config),
                        "target_pnl": _plan_net(position, position.target, self.fee_config),
                        "stop_pnl": _plan_net(position, position.stop, self.fee_config),
                        "mfe": position.mfe,
                        "mae": position.mae,
                        "opened_at": position.opened_at.isoformat(),
                        "protection_status": position.protection_status,
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

    def jev_reviews(self, limit: int = 40) -> list[dict]:
        from app.providers.jev.real import _normalized_state

        rows = []
        for call in reversed(self.store.jev_calls):
            decision = self.store.by_decision.get(str(call.get("decision_id"))) or {}
            request = call.get("request") if isinstance(call.get("request"), dict) else {}
            response = call.get("response") if isinstance(call.get("response"), dict) else {}
            meta = decision.get("metadata") or {}
            rows.append(
                {
                    "decision_id": str(call.get("decision_id")),
                    "symbol": decision.get("symbol") or request.get("symbol"),
                    "timestamp": decision.get("timestamp"),
                    "action": decision.get("action"),
                    "confidence": decision.get("confidence"),
                    "effect": meta.get("jev_effect"),
                    "required_continuation": meta.get("jev_required_continuation"),
                    "size_scale": meta.get("size_scale"),
                    "reason_codes": decision.get("reason_codes") or [],
                    "state": response.get("sent_request", {}).get("state") if response.get("sent_request") else None,
                    "request_recorded": bool(response.get("sent_request")),
                    "response": {
                        "trend_continuation_probability": response.get("trend_continuation_probability"),
                        "reversal_probability": response.get("reversal_probability"),
                        "false_breakout_probability": response.get("false_breakout_probability"),
                        "model": response.get("model"),
                        "latency_ms": response.get("latency_ms") if response.get("latency_ms") is not None else call.get("latency_ms"),
                        "is_mock": response.get("is_mock") if response.get("is_mock") is not None else call.get("is_mock"),
                        "error": response.get("error") or call.get("error"),
                    },
                }
            )
            if len(rows) >= limit:
                break
        return rows

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

    async def _repair_live_long(self, key: str, position) -> None:
        """A long is the filled order. An older free balance must not stay as the size."""
        if position.mode != "live" or position.side is not Action.LONG or self.settings.market_type != "margin":
            return
        if str(position.position_id) in self._repaired_longs:
            return
        try:
            found = await self.live.fetch(position.symbol, position.decision_id.hex)
        except Exception as exc:
            self._log("position_size", str(exc))
            return
        self._repaired_longs.add(str(position.position_id))
        if not found:
            return
        executed, _price = execution_of(found)
        filled = held_quantity("BUY", position.symbol, executed, found)
        step = self.feed.rules.get(position.symbol, {}).get("step_size")
        size = lot_quantity(filled, step)
        if size <= position.quantity + 1e-12:
            return
        ratio = size / position.quantity if position.quantity > 0 else 1.0
        position.quantity = size
        position.entry_fee *= ratio
        locked = self.accounts.accounts[key].margin_locked.get(str(position.position_id))
        if locked:
            self.accounts.accounts[key].margin_locked[str(position.position_id)] = locked * ratio
        self._log("position_size", f"{position.symbol} {size:.8f}")
        await self._replace_short_stop(position)
        await self._checkpoint(key, positions=[position], orders=[], fills=[])

    async def _replace_short_stop(self, position) -> None:
        suffix = "S"
        if position.stop_client_order_id:
            resting = await self.live.fetch(position.symbol, position.stop_client_order_id)
            covered = 0.0
            if resting:
                try:
                    covered = float(resting.get("origQty") or 0)
                except (TypeError, ValueError):
                    covered = 0.0
            if covered >= position.quantity * 0.99:
                return
            try:
                await self.live.cancel(position.symbol, position.stop_client_order_id)
            except Exception as exc:
                self._log("live_stop_cancel", str(exc))
            suffix = "S2"
        tick = self.feed.rules.get(position.symbol, {}).get("tick_size")
        intent = OrderIntent(
            decision_id=position.decision_id,
            risk_id=None,
            strategy=position.strategy,
            symbol=position.symbol,
            side="BUY",
            order_type=OrderType.MARKET,
            quantity=position.quantity,
            limit_price=None,
            mode="live",
            created_at=utcnow(),
            best_bid=None,
            best_ask=None,
            book=None,
            fee_rate=position.exit_fee_rate,
        )
        try:
            stop = await self.live.submit_stop(intent, position.stop, tick=tick, suffix=suffix)
        except Exception as exc:
            self._log("live_stop", str(exc))
            return
        position.stop_client_order_id = stop.client_order_id

    async def _free_base(self, symbol: str, *, fresh: bool = False) -> float | None:
        """Free base on the margin account. None when the snapshot cannot be read."""
        try:
            balance = await self.balance.snapshot(fresh=fresh)
        except Exception:
            return None
        if balance.get("status") != "ok":
            return None
        name = base_asset(symbol)
        for asset in balance.get("assets") or []:
            if str(asset.get("asset") or "") != name:
                continue
            free = asset.get("free")
            if isinstance(free, (int, float)):
                return float(free)
            return 0.0
        return 0.0

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


def _marked_net(position, price: float, fee_config) -> float:
    """Price result if closed here, after the exit fee still to be paid."""
    return unrealized(position, price) - _exit_fee(position, price, position.quantity, fee_config)


def _plan_net(position, price: float, fee_config) -> float:
    """Trade result at this price, after the entry fee already paid and the exit fee."""
    return unrealized(position, price) - position.entry_fee - _exit_fee(position, price, position.quantity, fee_config)


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
