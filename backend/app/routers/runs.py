from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.models import Run, Event
from backend.app.schemas import RunOut, EventOut, TriggerRun
from backend.core import runner, registry

router = APIRouter(prefix="/runs", tags=["runs"])


def _enqueue(kind, name, params=None):
    try: return runner.enqueue(kind, name, params)
    except ValueError as e: raise HTTPException(422, str(e))


@router.get("/health")
def worker_health(db: Session = Depends(get_db)):
    from backend.core import browser, config
    from backend.core.worker import heartbeat_status
    hb = heartbeat_status()
    return {"worker_active": browser.lock_busy(config.DATA / "worker.lock"),
            "worker_heartbeat_age_s": hb["heartbeat_age_s"], "worker_heartbeat_fresh": hb["fresh"], "worker_run_id": hb["run_id"],
            "queued": db.query(Run).filter(Run.status == "queued").count(),
            "running": [r.id for r in db.query(Run).filter(Run.status == "running").all()],
            "browser_handoffs": [r.id for r in db.query(Run).filter(Run.status == "paused_for_human", Run.spec.isnot(None)).all()],
            "browser": browser.health()}


@router.get("")
def list_runs(db: Session = Depends(get_db), limit: int = 50):
    rows = db.query(Run).order_by(Run.started_at.desc()).limit(limit).all()
    return [RunOut.model_validate(r).model_dump() for r in rows]


@router.get("/events")
def events(db: Session = Depends(get_db), run_id: int | None = None, after_id: int = 0, level: str | None = None, limit: int = 200, latest: bool = False):
    qs = db.query(Event).filter(Event.id > after_id)
    if run_id: qs = qs.filter(Event.run_id == run_id)
    if level: qs = qs.filter(Event.level == level)
    rows = qs.order_by(Event.id.desc() if latest else Event.id.asc()).limit(min(max(limit, 1), 500)).all()
    if latest: rows.reverse()
    return [EventOut.model_validate(e).model_dump() for e in rows]


@router.post("/collector/{name}")
def run_collector(name: str, body: TriggerRun | None = None):
    fn = registry.COLLECTORS.get(name)
    if not fn: raise HTTPException(404, f"unknown collector {name}")
    rid = _enqueue("collector", name)
    return {"run_id": rid}


@router.post("/collectors/all")
def run_all_collectors():
    def _all(ctx):
        for name, fn in registry.COLLECTORS.items():
            if ctx.should_stop(): break
            try:
                fn(ctx)
            except Exception as e:  # noqa: BLE001
                ctx.log("error", f"{name}: {e}", platform=name)
    return {"run_id": _enqueue("collectors", "collect_all")}


@router.post("/pipeline/{name}")
def run_pipeline(name: str, body: TriggerRun | None = None):
    fn = registry.PIPELINES.get(name)
    if not fn: raise HTTPException(404, f"unknown pipeline {name}")
    params = (body.params if body else {}) or {}
    return {"run_id": _enqueue("pipeline", name, params)}


@router.post("/service/{name}")
def run_service(name: str, body: TriggerRun | None = None):
    fn = registry.SERVICES.get(name)
    if not fn: raise HTTPException(404, f"unknown service {name}")
    return {"run_id": _enqueue("service", name, (body.params if body else {}) or {})}


@router.post("/skill/{name}")
def run_skill(name: str, body: TriggerRun | None = None):
    cls = registry.SKILLS.get(name)
    if not cls: raise HTTPException(404, f"skill {name} not implemented yet")
    params = (body.params if body else {}) or {}

    def _go(ctx):
        cls(ctx).run(**params)
    return {"run_id": _enqueue("skill", name, params)}


@router.post("/{run_id}/stop")
def stop(run_id: int):
    return {"stopped": runner.stop_run(run_id)}

@router.get("/fullrun/platforms")
def fullrun_platforms():
    """Platforms the 'run the system' button can target, in the order a full run would visit them."""
    from backend.core import fullrun
    names = {p["key"]: p["name"] for p in registry.PLATFORMS}
    return [{"key": k, "name": names.get(k, k), "needs_login": k in fullrun.SKILL_DISCOVER_MODE} for k in fullrun._platform_keys()]


@router.post("/fullrun")
def start_fullrun(body: TriggerRun | None = None):
    """Run the whole chain (discover -> hydrate -> score -> prepare -> optional apply).
    params: {"platforms": ["linkedin", ...] | null for all, "apply": false}"""
    from backend.core import fullrun
    params = (body.params if body else {}) or {}
    if set(params) - {"platforms", "apply"}: raise HTTPException(422, "Unsupported fullrun parameters")
    plats = params.get("platforms")
    apply_too = params.get("apply", False)
    if plats and (not isinstance(plats, list) or any(p not in fullrun._platform_keys() for p in plats)):
        raise HTTPException(422, "Unknown platform selection")
    return {"run_id": _enqueue("fullrun", "all", {"platforms": plats, "apply": apply_too})}


@router.post("/{run_id}/retry")
def retry_run(run_id: int):
    try: return {"run_id": runner.retry(run_id)}
    except ValueError as e: raise HTTPException(409, str(e))
