"""Durable local worker. Run separately from uvicorn: python -m backend.core.worker."""
from __future__ import annotations
import fcntl, json, time, traceback
from datetime import datetime
from sqlalchemy import text
from backend.core import config
from backend.app.db import init_db, session
from backend.app.models import Run, Application, Outreach
from backend.core.runner import RunContext

HEARTBEAT = config.DATA / "worker.heartbeat.json"
HEARTBEAT_STALE_S = 30


_current_run = None


def _pulse():
    """A long stage (scoring a few hundred jobs) must not look like a hung worker, so the heartbeat is written on a timer
    rather than only between runs."""
    while True:
        heartbeat(_current_run)
        time.sleep(10)


def heartbeat(run_id=None):
    """Liveness record for the dashboard: written every loop, atomically, so a stale file means a dead worker."""
    import os, tempfile
    payload = {"ts": datetime.utcnow().isoformat(), "pid": os.getpid(), "run_id": run_id}
    try:
        fd, temp = tempfile.mkstemp(dir=str(config.DATA), prefix=".heartbeat-")
        with os.fdopen(fd, "w") as f: json.dump(payload, f)
        os.replace(temp, HEARTBEAT)
    except OSError:
        pass


def heartbeat_status() -> dict:
    """Read-only view used by the API: age in seconds and the run being worked on, or None when never written."""
    try:
        data = json.loads(HEARTBEAT.read_text())
        age = (datetime.utcnow() - datetime.fromisoformat(data["ts"])).total_seconds()
        return {"heartbeat_age_s": round(max(age, 0), 1), "run_id": data.get("run_id"), "pid": data.get("pid"), "fresh": age < HEARTBEAT_STALE_S}
    except (OSError, ValueError, KeyError):
        return {"heartbeat_age_s": None, "run_id": None, "pid": None, "fresh": False}


def dispatch(ctx, spec):
    from backend.core import registry, fullrun, browser
    from backend.core.run_params import validate
    kind, name = spec['kind'], spec['name']
    params = validate(kind, name, spec.get('params') or {})
    if kind == 'collector': return registry.COLLECTORS[name](ctx)
    if kind == 'collectors':
        for key, fn in registry.COLLECTORS.items():
            if ctx.should_stop(): break
            if ctx.checkpoint_done(key): continue
            try:
                fn(ctx); ctx.checkpoint(key)
            except Exception as e:
                ctx.bump('failed'); ctx.log('error', f'{key}: {e}')
        return
    if kind == 'pipeline': return registry.PIPELINES[name](ctx, **params)
    if kind == 'skill': return registry.SKILLS[name](ctx).run(**params)
    if kind == 'service': return registry.SERVICES[name](ctx, **params)
    if kind == 'fullrun':
        only = params.get('platforms')
        return fullrun.all_platforms(ctx, apply=bool(params.get('apply')), only=only)
    if kind == 'login':
        with browser.open_context(name, should_stop=ctx.should_stop) as context:
            page = context.new_page()
            ok = browser.ensure_login(page, name, ctx.log, wait_minutes=20, should_stop=ctx.should_stop, on_ready=lambda: ctx.set_status("running"))
            if not ok: ctx.set_status("paused_for_human")
            else: page.close()
        return
    raise ValueError(f'Unsupported run kind: {kind}')


def recover():
    """Never automatically replay a possibly submitted operation after a crash."""
    with session() as db:
        interrupted = db.query(Run).filter(Run.status.in_(['running', 'paused_for_human']), Run.ended_at.is_(None), Run.spec.isnot(None)).all()
        owned_ids = [run.id for run in interrupted]
        for run in interrupted:
            run.status = 'interrupted'; run.ended_at = datetime.utcnow()
            run.error = 'Worker interrupted. Inspect application evidence before explicitly retrying.'
        for app in db.query(Application).filter(Application.status == 'submitting', Application.claim_run_id.in_(owned_ids)).all():
            app.status = 'needs_human'
            app.error = 'Interrupted during submission; reconcile with employer before retrying.'


        for message in db.query(Outreach).filter(Outreach.status == 'sending', Outreach.claim_run_id.in_(owned_ids)).all():
            message.status = 'submission_unverified'
            message.error = 'Worker interrupted during send; check provider history before retrying.'


def run_one():
    with session() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        # A timed-out human handoff reserves browser work until explicitly stopped/retried.
        blocked = db.query(Run).filter(Run.status == 'paused_for_human', Run.spec.isnot(None)).first()
        row = next((r for r in db.query(Run).filter(Run.status == 'queued').order_by(Run.id).all()
                    if not blocked or r.kind not in ('skill', 'fullrun', 'login')), None)
        if not row: return False
        row.status = 'running'; row.started_at = datetime.utcnow()
        rid, spec = row.id, row.spec
    global _current_run
    ctx = RunContext.resume(rid)
    _current_run = rid
    heartbeat(rid)
    try:
        dispatch(ctx, spec); ctx.finish()
    except InterruptedError as e:
        # A cancelled wait (browser lock, launch) is a stop, not a failure.
        ctx.stop_flag.set(); ctx.log('info', f'stopped: {e}'); ctx.finish()
    except Exception as e:
        ctx.log('error', f'{type(e).__name__}: {e}', data={'trace': traceback.format_exc()[-2000:]})
        ctx.finish(str(e))
    finally:
        _current_run = None
        heartbeat(None)
    return True


def main():
    init_db()
    with (config.DATA / 'worker.lock').open('a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise SystemExit('A worker already owns this queue')
        recover()
        import threading
        threading.Thread(target=_pulse, daemon=True, name="heartbeat").start()
        from backend.core.scheduler import tick
        while True:
            if not run_one(): time.sleep(1)

if __name__ == '__main__': main()
