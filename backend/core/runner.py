"""Run bookkeeping: every collector/skill execution creates a Run row and streams Events. Also the in-process job runner."""
from __future__ import annotations
import threading, traceback
from datetime import datetime
from backend.app.db import session
from backend.app.models import Run, Event
from backend.core import config

_active: dict[int, threading.Event] = {}   # run_id -> stop flag


class RunContext:
    def __init__(self, kind: str, name: str):
        with session() as db:
            r = Run(kind=kind, name=name)
            db.add(r); db.flush()
            self.run_id = r.id
        self.stop_flag = threading.Event()
        _active[self.run_id] = self.stop_flag
        self.stats: dict = {}
        self._stats_lock = threading.Lock()

    @classmethod
    def resume(cls, run_id):
        obj = cls.__new__(cls)
        obj.run_id = run_id; obj.stop_flag = threading.Event(); obj._stats_lock = threading.Lock()
        with session() as db: obj.stats = dict(db.get(Run, run_id).stats or {})
        _active[run_id] = obj.stop_flag
        return obj

    def checkpoint_done(self, label):
        with session() as db: return label in (db.get(Run, self.run_id).checkpoints or [])

    def checkpoint(self, label):
        with session() as db:
            r = db.get(Run, self.run_id)
            r.checkpoints = list(dict.fromkeys([*(r.checkpoints or []), label]))

    def log(self, level: str, message: str, platform: str | None = None, data: dict | None = None, screenshot: str | None = None):
        with session() as db:
            db.add(Event(run_id=self.run_id, level=level, message=message, platform=platform, data=data, screenshot=screenshot))
        if level == "human":
            self.set_status("paused_for_human")

    def set_status(self, status: str):
        with session() as db:
            r = db.get(Run, self.run_id)
            r.status = status
            r.stats = dict(self.stats)

    def is_paused(self) -> bool:
        with session() as db:
            row = db.get(Run, self.run_id)
            return bool(row and row.status == "paused_for_human")

    def should_stop(self) -> bool:
        with session() as db:
            row = db.get(Run, self.run_id)
            return self.stop_flag.is_set() or bool(row and row.cancel_requested)

    def bump(self, key: str, n: int = 1):
        with self._stats_lock:
            self.stats[key] = self.stats.get(key, 0) + n
            snapshot = dict(self.stats)
            with session() as db: db.get(Run, self.run_id).stats = snapshot

    def finish(self, error: str | None = None):
        with session() as db:
            r = db.get(Run, self.run_id)
            r.status = ("stopped" if r.cancel_requested or self.stop_flag.is_set() else "failed" if error
                        else "paused_for_human" if r.status == "paused_for_human"
                        else "partially_completed" if any(self.stats.get(k, 0) for k in ("failed", "needs_human", "unverified", "not_found", "no_button"))
                        else "done")
            r.ended_at = datetime.utcnow()
            r.stats = dict(self.stats)
            r.error = error
        _active.pop(self.run_id, None)

    def screenshot(self, page, tag: str, full_page: bool = True) -> str:
        """Full page by default: when something is stuck, the part that matters is usually below the fold."""
        path = config.SCREENSHOTS / f"run{self.run_id}_{tag}_{datetime.utcnow().strftime('%H%M%S')}.png"
        try:
            try:
                page.screenshot(path=str(path), full_page=full_page)
            except Exception:
                page.screenshot(path=str(path), full_page=False)   # some pages refuse a full-page capture
            return str(path.relative_to(config.ROOT))
        except Exception:
            return ""


def stop_run(run_id: int) -> bool:
    with session() as db:
        row = db.get(Run, run_id)
        if not row or row.status not in ("queued", "running", "paused_for_human"): return False
        row.cancel_requested = True
        if row.status == "queued":
            row.status = "stopped"; row.ended_at = datetime.utcnow()
        elif row.status == "paused_for_human" and row.ended_at is not None: row.status = "stopped"
    if run_id in _active: _active[run_id].set()
    return True


def _enqueue_locked(db, kind, name, params, checkpoints=None):
    label = f"full:{','.join(params.get('platforms') or ['all'])}" if kind == "fullrun" else name
    spec = {"kind": kind, "name": name, "params": params}
    for existing in db.query(Run).filter(Run.kind == kind, Run.name == label, Run.status.in_(["queued", "running"])).all():
        if existing.spec == spec: return existing.id
    run = Run(kind=kind, name=label, status="queued", spec=spec, checkpoints=list(checkpoints or []))
    db.add(run); db.flush(); return run.id


def enqueue(kind: str, name: str, params=None, *, checkpoints=None) -> int:
    from sqlalchemy import text
    from backend.core.run_params import validate
    params = validate(kind, name, params if params is not None else {})
    with session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        return _enqueue_locked(db, kind, name, params, checkpoints)


def retry(run_id: int) -> int:
    from sqlalchemy import text
    from backend.core.run_params import validate
    with session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        old = db.get(Run, run_id)
        if not old or not old.spec or old.status not in ("interrupted", "failed", "stopped", "partially_completed", "paused_for_human"):
            raise ValueError("Only completed stopped/interrupted/failed runs can be retried")
        if old.ended_at is None:
            raise ValueError("This run still owns its work. Stop it and wait for it to finish before retrying.")
        spec = old.spec
        params = validate(spec["kind"], spec["name"], spec.get("params") or {})
        checkpoints = [c for c in (old.checkpoints or []) if not c.startswith("apply")]
        rid = _enqueue_locked(db, spec["kind"], spec["name"], params, checkpoints)
        old.status = "superseded"
        return rid


def start_in_thread(kind: str, name: str, fn, *args, **kwargs) -> int:
    """fn(ctx, *args, **kwargs). Returns run_id immediately."""
    ctx = RunContext(kind, name)

    def _target():
        try:
            fn(ctx, *args, **kwargs)
            ctx.finish()
        except Exception as e:  # noqa: BLE001
            ctx.log("error", f"{type(e).__name__}: {e}", data={"trace": traceback.format_exc()[-2000:]})
            ctx.finish(error=str(e))

    threading.Thread(target=_target, daemon=True, name=f"run-{ctx.run_id}-{name}").start()
    return ctx.run_id
