from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.domain.enums import OperatingMode
from app.evolution.http import router as evolution_router
from app.service import TradingEngine

LABELS = {
    "baseline": "BASELINE",
    "baseline_jev": "BASELINE + JEV",
    "baseline_openai_jev": "BASELINE + OPENAI + JEV",
}

router = APIRouter()


def _engine(request: Request) -> TradingEngine:
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(503, "engine not started")
    return engine


def _actor(request: Request) -> str:
    settings: Settings = request.app.state.settings
    secret = settings.engine_api_secret
    header = request.headers.get("authorization", "")
    if secret:
        if header != f"Bearer {secret}":
            raise HTTPException(401, "unauthorized")
        return "dashboard"
    if settings.environment != "development":
        raise HTTPException(401, "ENGINE_API_SECRET required")
    return "local-dev"


class StrategyPatch(BaseModel):
    enabled: bool | None = None
    mode: OperatingMode | None = None
    call_model: bool | None = None
    confirm: str = ""


class RiskPatch(BaseModel):
    min_net_rr: float | None = None
    risk_per_trade: float | None = None
    max_risk_per_trade: float | None = None
    max_daily_loss: float | None = None
    max_daily_drawdown: float | None = None
    max_open_positions: int | None = None
    max_symbol_exposure: float | None = None
    max_total_exposure: float | None = None
    confirm: str = ""


class ConfirmBody(BaseModel):
    confirm: str = ""


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> dict:
    engine = getattr(request.app.state, "engine", None)
    return {"ready": engine is not None}


@router.get("/status")
async def status(request: Request) -> dict:
    _actor(request)
    return _engine(request).status()


@router.get("/metrics")
async def metrics(request: Request) -> dict:
    _actor(request)
    engine = _engine(request)
    return {
        "decisions": len(engine.store.decisions),
        "trades": len(engine.store.trades),
        "openai_calls": len(engine.store.openai_calls),
        "jev_calls": len(engine.store.jev_calls),
        "reconnects": engine.feed.reconnects,
        "strategy_errors": {key: value["errors"] for key, value in engine.health.items()},
        "background_tasks": len(engine._background),
    }


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    _actor(request)
    engine = _engine(request)
    queue = engine.bus.subscribe("*")

    async def generate():
        try:
            yield _sse("engine_status", engine.status())
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                yield _sse(event.topic, event.payload)
        finally:
            engine.bus.unsubscribe("*", queue)

    return StreamingResponse(generate(), media_type="text/event-stream")


def _sse(event: str, payload) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


@router.get("/api/engine/status")
async def engine_status(request: Request) -> dict:
    _actor(request)
    return _engine(request).status()


@router.get("/api/overview")
async def overview(request: Request) -> dict:
    _actor(request)
    engine = _engine(request)
    performance = engine.performance()
    cards = []
    for key, label in LABELS.items():
        stats = performance[key]
        cards.append({"key": key, "label": label, **stats})
    return {
        "status": engine.status(),
        "strategies": cards,
        "tickers": [engine.ticker(symbol) for symbol in engine.settings.symbol_list],
        "positions": engine.positions_payload(),
    }


@router.get("/api/strategies")
async def strategies(request: Request) -> dict:
    _actor(request)
    engine = _engine(request)
    return {
        "strategies": [
            {
                "key": key,
                "label": LABELS[key],
                "enabled": cfg.enabled,
                "mode": cfg.mode.value,
                "call_model": cfg.call_model,
                "max_signal_age_ms": cfg.max_signal_age_ms,
                "health": engine.health[key],
            }
            for key, cfg in engine.strategy_settings.items()
        ]
    }


@router.patch("/api/strategies/{strategy_id}")
async def patch_strategy(strategy_id: str, body: StrategyPatch, request: Request) -> dict:
    actor = _actor(request)
    engine = _engine(request)
    if strategy_id not in engine.strategy_settings:
        raise HTTPException(404, "unknown strategy")
    if body.mode is OperatingMode.LIVE and body.confirm != "LIVE":
        raise HTTPException(400, "confirm LIVE")
    if body.mode is not None and body.confirm not in {body.mode.value.upper(), "CONFIRM", "LIVE"}:
        raise HTTPException(400, "confirmation required")
    try:
        payload = await engine.update_strategy(
            strategy_id,
            enabled=body.enabled,
            mode=body.mode.value if body.mode else None,
            call_model=body.call_model,
            actor=actor,
        )
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return {"strategy": strategy_id, **payload, "live_armed": engine.settings.live_armed}


@router.get("/api/market")
async def market(request: Request) -> dict:
    _actor(request)
    engine = _engine(request)
    return {"market_type": engine.settings.market_type, "symbols": [engine.ticker(symbol) for symbol in engine.settings.symbol_list]}


@router.get("/api/market/{symbol}")
async def market_symbol(symbol: str, request: Request) -> dict:
    _actor(request)
    engine = _engine(request)
    symbol = symbol.upper()
    if symbol not in engine.states:
        raise HTTPException(404, "unknown symbol")
    latest = None
    for snapshot in reversed(list(engine.store.snapshots.values())):
        if snapshot["symbol"] == symbol:
            latest = snapshot
            break
    return {"ticker": engine.ticker(symbol), "snapshot": latest}


@router.get("/api/decisions")
async def decisions(request: Request, limit: int = 100, symbol: str | None = None) -> dict:
    _actor(request)
    engine = _engine(request)
    return {"rows": engine.store.opportunity_rows(limit=min(limit, 500), symbol=symbol.upper() if symbol else None)}


@router.get("/api/decisions/{decision_id}")
async def decision_detail(decision_id: str, request: Request) -> dict:
    _actor(request)
    found = _engine(request).store.inspector(decision_id)
    if found is None:
        raise HTTPException(404, "NO DATA")
    return found


@router.get("/api/trades")
async def trades(
    request: Request,
    strategy: str | None = None,
    symbol: str | None = None,
    side: str | None = None,
    result: str | None = None,
    regime: str | None = None,
) -> dict:
    _actor(request)
    rows = _engine(request).store.filter_trades(
        strategy=strategy,
        symbol=symbol.upper() if symbol else None,
        side=side,
        result=result,
        regime=regime,
    )
    return {"rows": rows}


@router.get("/api/positions")
async def positions(request: Request) -> dict:
    _actor(request)
    return {"rows": _engine(request).positions_payload()}


@router.get("/api/performance")
async def performance(request: Request) -> dict:
    _actor(request)
    return {"strategies": _engine(request).performance()}


@router.get("/api/performance/compare")
async def performance_compare(request: Request) -> dict:
    _actor(request)
    return _engine(request).comparison()


@router.get("/api/performance/differences")
async def performance_differences(request: Request) -> dict:
    _actor(request)
    report = _engine(request).comparison()
    return {
        "only_baseline": report["only_baseline"],
        "only_jev": report["only_jev"],
        "only_openai": report["only_openai"],
        "all_agreed": report["all_agreed"],
        "disagreed": report["disagreed"],
        "jev_eliminated_signals": report["jev_eliminated_signals"],
        "eliminated_trade_net_pnl": report["eliminated_trade_net_pnl"],
    }


@router.get("/api/performance/equity")
async def equity(request: Request) -> dict:
    _actor(request)
    curves = _engine(request).equity_curves()
    drawdown = {}
    for key, points in curves["series"].items():
        peak = None
        series = []
        for point in points:
            peak = point["equity"] if peak is None else max(peak, point["equity"])
            series.append({**point, "drawdown": (point["equity"] / peak - 1) if peak else 0})
        drawdown[key] = series
    curves["drawdown"] = drawdown
    return curves


@router.get("/api/risk/limits")
async def risk_limits(request: Request) -> dict:
    _actor(request)
    limits = _engine(request).risk.limits
    return {
        "min_net_rr": limits.min_net_rr,
        "risk_per_trade": limits.risk_per_trade,
        "max_risk_per_trade": limits.max_risk_per_trade,
        "max_daily_loss": limits.max_daily_loss,
        "max_daily_drawdown": limits.max_daily_drawdown,
        "max_open_positions": limits.max_open_positions,
        "max_symbol_exposure": limits.max_symbol_exposure,
        "max_total_exposure": limits.max_total_exposure,
        "target_mode": limits.target_mode.value,
        "max_leverage": limits.max_leverage,
    }


@router.patch("/api/risk/limits")
async def patch_risk(body: RiskPatch, request: Request) -> dict:
    actor = _actor(request)
    if body.confirm != "CONFIRM":
        raise HTTPException(400, "confirmation required")
    engine = _engine(request)
    changes = body.model_dump(exclude_none=True)
    changes.pop("confirm", None)
    try:
        engine.update_risk(changes, actor)
    except KeyError as exc:
        raise HTTPException(400, f"field not editable: {exc}") from exc
    await engine._audit(actor, "risk_update", changes)
    return await risk_limits(request)


@router.post("/api/engine/kill-switch")
async def kill_switch(body: ConfirmBody, request: Request) -> dict:
    actor = _actor(request)
    if body.confirm != "STOP":
        raise HTTPException(400, "confirm STOP")
    await _engine(request).kill_switch(actor)
    return {"trading_enabled": False}


@router.post("/api/engine/resume")
async def resume(body: ConfirmBody, request: Request) -> dict:
    actor = _actor(request)
    if body.confirm != "RESUME":
        raise HTTPException(400, "confirm RESUME")
    try:
        await _engine(request).resume(actor)
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    return {"trading_enabled": True}


@router.get("/api/audit")
async def audit(request: Request) -> dict:
    _actor(request)
    return {"rows": list(reversed(_engine(request).store.audit[-200:]))}


@router.get("/api/events")
async def events(request: Request) -> dict:
    _actor(request)
    engine = _engine(request)
    return {"rows": list(reversed(engine.logs.items))}


def create_app(settings: Settings | None = None, engine: TradingEngine | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.settings = settings
        if engine is not None:
            app.state.engine = engine
            yield
            return
        created = TradingEngine(settings)
        app.state.engine = created
        await created.start()
        try:
            yield
        finally:
            await created.stop()

    app = FastAPI(title="trading-engine", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.include_router(evolution_router)
    return app
