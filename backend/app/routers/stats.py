from __future__ import annotations
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.models import Job, Application, Outreach, Run, PlatformSession
from backend.core import config, humanize

router = APIRouter(prefix="/stats", tags=["stats"])


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    cfg = config.load()
    apps_today = db.query(func.count(Application.id)).filter(Application.submitted_at >= today).scalar() or 0
    out_today = db.query(func.count(Outreach.id)).filter(Outreach.sent_at >= today).scalar() or 0
    days = []
    for i in range(13, -1, -1):
        d0 = today - timedelta(days=i); d1 = d0 + timedelta(days=1)
        days.append({"day": d0.strftime("%b %d"),
                     "found": db.query(func.count(Job.id)).filter(Job.created_at >= d0, Job.created_at < d1).scalar() or 0,
                     "applied": db.query(func.count(Application.id)).filter(Application.submitted_at >= d0, Application.submitted_at < d1).scalar() or 0,
                     "outreach": db.query(func.count(Outreach.id)).filter(Outreach.sent_at >= d0, Outreach.sent_at < d1).scalar() or 0})
    return {
        "target_per_day": cfg.get("target_per_day", 100),
        "review_mode": cfg.get("review_mode", True),
        "today": {"applied": apps_today, "outreach": out_today, "touches": apps_today + out_today,
                  "jobs_found": db.query(func.count(Job.id)).filter(Job.created_at >= today).scalar() or 0},
        "totals": {
            "jobs": db.query(func.count(Job.id)).scalar() or 0,
            "eligible": db.query(func.count(Job.id)).filter(Job.eligible == True).scalar() or 0,  # noqa: E712
            "queued": db.query(func.count(Job.id)).filter(Job.status == "queued").scalar() or 0,
            "pending_review": db.query(func.count(Application.id)).filter(Application.status.in_(["pending_review", "needs_human"])).scalar() or 0,
            "applied": db.query(func.count(Application.id)).filter(Application.status.in_(["submitted", "replied", "interview", "rejected", "offer"])).scalar() or 0,
            "replies": db.query(func.count(Application.id)).filter(Application.status.in_(["replied", "interview", "offer"])).scalar() or 0,
            "interviews": db.query(func.count(Application.id)).filter(Application.status.in_(["interview", "offer"])).scalar() or 0,
            "outreach_sent": db.query(func.count(Outreach.id)).filter(Outreach.status.in_(["sent", "replied"])).scalar() or 0,
        },
        "by_source": dict(db.query(Job.source, func.count(Job.id)).group_by(Job.source).all()),
        "eligible_by_source": dict(db.query(Job.source, func.count(Job.id)).filter(Job.eligible == True).group_by(Job.source).all()),  # noqa: E712
        "days": days,
        "active_runs": db.query(func.count(Run.id)).filter(Run.status.in_(["running", "paused_for_human"])).scalar() or 0,
        "paused_runs": db.query(func.count(Run.id)).filter(Run.status == "paused_for_human").scalar() or 0,
        "caps": humanize.caps_snapshot(),
        "sessions": [{"platform": s.platform, "logged_in": s.logged_in, "last_checked": s.last_checked, "note": s.note} for s in db.query(PlatformSession).all()],
    }
