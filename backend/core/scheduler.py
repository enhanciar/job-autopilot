"""Tiny in-process scheduler (no external deps). Reads config.yaml -> schedule and starts pipeline/collector/skill runs in threads.
Only one run of a given name at a time; browser skills only inside business hours."""
from __future__ import annotations
import threading, time
from datetime import datetime
from backend.core import config, registry, runner, humanize
from backend.app.db import session
from backend.app.models import Run

DEFAULT = {
    "enabled": False,
    "jobs": [
        {"name": "collect_all", "kind": "collectors", "every_minutes": 180},
        {"name": "daily", "kind": "pipeline", "every_minutes": 60, "business_hours_only": False},
        {"name": "ats_apply", "kind": "skill", "every_minutes": 45, "business_hours_only": True, "params": {"limit": 8}},
        {"name": "sheets_sync", "kind": "service", "every_minutes": 30},
    ],
}
_last: dict[str, float] = {}
_thread: threading.Thread | None = None


def _running(name: str) -> bool:
    with session() as db:
        return db.query(Run).filter(Run.name == name, Run.status.in_(["queued", "running", "paused_for_human"])).count() > 0


def _start(job: dict):
    return runner.enqueue(job["kind"], job["name"], job.get("params") or {})


def tick():
    sched = {**DEFAULT, **(config.load().get("schedule") or {})}
    if not sched.get("enabled"): return
    now = time.time()
    for job in sched.get("jobs", DEFAULT["jobs"]):
        name = job["name"]
        if job.get("business_hours_only") and not humanize.in_business_hours(): continue
        if now - _last.get(name, 0) < job.get("every_minutes", 60) * 60: continue
        if _running(name): continue
        _last[name] = now
        try: _start(job)
        except Exception:  # noqa: BLE001
            pass


def start():
    global _thread
    if _thread: return
    def loop():
        time.sleep(20)
        while True:
            try: tick()
            except Exception: pass  # noqa: BLE001
            time.sleep(60)
    _thread = threading.Thread(target=loop, daemon=True, name="scheduler"); _thread.start()
