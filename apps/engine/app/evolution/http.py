from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


def _engine(request: Request):
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(503, "engine not started")
    return engine


def _actor(request: Request) -> str:
    settings = request.app.state.settings
    secret = settings.engine_api_secret
    header = request.headers.get("authorization", "")
    if secret:
        if header != f"Bearer {secret}":
            raise HTTPException(401, "unauthorized")
        return "dashboard"
    if settings.environment != "development":
        raise HTTPException(401, "ENGINE_API_SECRET required")
    return "local-dev"


class AnalysisBody(BaseModel):
    confirm: str = ""


class PromoteBody(BaseModel):
    target: str
    confirm: str = ""


class RejectBody(BaseModel):
    reason: str
    confirm: str = ""


class ControlsBody(BaseModel):
    auto_research: bool | None = None
    auto_build: bool | None = None
    auto_shadow_promotion: bool | None = None
    auto_paper_promotion: bool | None = None
    confirm: str = ""


@router.get("/api/evolution/status")
async def evolution_status(request: Request) -> dict:
    _actor(request)
    return _engine(request).evolution.status()


@router.get("/api/evolution/champions")
async def champions(request: Request) -> dict:
    _actor(request)
    return {"rows": _engine(request).evolution.champion_rows()}


@router.get("/api/evolution/challengers")
async def challengers(request: Request) -> dict:
    _actor(request)
    return {"rows": _engine(request).evolution.challenger_rows()}


@router.get("/api/evolution/experiments")
async def experiments(request: Request) -> dict:
    _actor(request)
    return {"rows": _engine(request).evolution.experiment_rows()}


@router.get("/api/evolution/experiments/{experiment_id}")
async def experiment_detail(experiment_id: str, request: Request) -> dict:
    _actor(request)
    found = _engine(request).evolution.experiment_detail(experiment_id)
    if found is None:
        raise HTTPException(404, "NO DATA")
    return found


@router.get("/api/evolution/anomalies")
async def anomalies(request: Request) -> dict:
    _actor(request)
    rows = [item.model_dump(mode="json") for item in _engine(request).evolution.anomalies]
    return {"rows": rows}


@router.get("/api/evolution/tree")
async def tree(request: Request) -> dict:
    _actor(request)
    return {"nodes": _engine(request).evolution.tree()}


@router.post("/api/evolution/run-analysis")
async def run_analysis(body: AnalysisBody, request: Request) -> dict:
    actor = _actor(request)
    if body.confirm != "ANALYZE":
        raise HTTPException(400, "confirm ANALYZE")
    return await _engine(request).evolution.run_analysis(actor)


@router.post("/api/evolution/experiments/{experiment_id}/cancel")
async def cancel(experiment_id: str, body: AnalysisBody, request: Request) -> dict:
    _actor(request)
    if body.confirm != "CANCEL":
        raise HTTPException(400, "confirm CANCEL")
    return _engine(request).evolution.cancel(experiment_id)


@router.post("/api/evolution/experiments/{experiment_id}/promote")
async def promote(experiment_id: str, body: PromoteBody, request: Request) -> dict:
    actor = _actor(request)
    if body.confirm != "PROMOTE":
        raise HTTPException(400, "confirm PROMOTE")
    result = _engine(request).evolution.promote(experiment_id, body.target, actor, auto=False)
    if not result["accepted"] and result["reason"] == "LIVE_PROMOTION_DISABLED":
        raise HTTPException(403, result["reason"])
    if not result["accepted"]:
        raise HTTPException(409, result["reason"])
    return result


@router.post("/api/evolution/experiments/{experiment_id}/reject")
async def reject(experiment_id: str, body: RejectBody, request: Request) -> dict:
    actor = _actor(request)
    if body.confirm != "REJECT":
        raise HTTPException(400, "confirm REJECT")
    result = _engine(request).evolution.reject(experiment_id, body.reason, actor)
    if not result["accepted"]:
        raise HTTPException(404, result["reason"])
    return result


@router.patch("/api/evolution/controls")
async def controls(body: ControlsBody, request: Request) -> dict:
    actor = _actor(request)
    if body.confirm != "EVOLUTION":
        raise HTTPException(400, "confirm EVOLUTION")
    changes = body.model_dump(exclude_none=True)
    changes.pop("confirm", None)
    try:
        updated = _engine(request).evolution.update_controls(changes, actor)
    except KeyError as exc:
        raise HTTPException(400, f"field not editable: {exc}") from exc
    return updated
