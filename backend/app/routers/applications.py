from __future__ import annotations
from datetime import datetime
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import func
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.models import Application, Job
from backend.app.schemas import ApplicationOut, StatusUpdate

router = APIRouter(prefix="/applications", tags=["applications"])


def _out(a: Application) -> dict:
    from backend.core.transitions import APPLICATION_TRANSITIONS
    d = ApplicationOut.model_validate(a).model_dump()
    from backend.core.submissions import approval_issue
    d["status_options"] = sorted(APPLICATION_TRANSITIONS.get(a.status, set()))
    d["preparation_issue"] = approval_issue(a) if a.status in ("pending_review", "approved", "needs_human", "failed") else None
    if d["preparation_issue"] and "approved" in d["status_options"]: d["status_options"].remove("approved")
    d["can_regenerate"] = a.status in ("pending_review", "needs_human", "failed", "rejected_by_user")
    if a.job:
        d.update(company=a.job.company, title=a.job.title, url=a.job.url, country=a.job.country, location=a.job.location, fit_score=a.job.fit_score, source=a.job.source)
    return d


def _filtered(db: Session, status=None, country=None, platform=None, method=None, q=None, source=None):
    qs = db.query(Application).join(Job)
    if status: qs = qs.filter(Application.status == status)
    if country: qs = qs.filter(Job.country == country)
    if platform: qs = qs.filter(Application.platform == platform)
    if method: qs = qs.filter(Application.method == method)
    if source: qs = qs.filter(Job.source == source)
    if q: qs = qs.filter((Job.company.ilike(f"%{q}%")) | (Job.title.ilike(f"%{q}%")))
    return qs


@router.get("")
def list_apps(db: Session = Depends(get_db), status: str | None = None, country: str | None = None, platform: str | None = None,
              method: str | None = None, q: str | None = None, source: str | None = None, page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200)):
    qs = _filtered(db, status, country, platform, method, q, source)
    total = qs.count()
    rows = qs.order_by(Application.created_at.desc()).offset((page - 1) * size).limit(size).all()
    return {"total": total, "items": [_out(a) for a in rows]}


@router.get("/facets")
def facets(db: Session = Depends(get_db)):
    base = db.query(Application).join(Job)
    def grp(col): return {k or "Unknown": v for k, v in db.query(col, func.count(Application.id)).select_from(Application).join(Job).group_by(col).all()}
    submitted = {k or "Unknown": v for k, v in db.query(Job.country, func.count(Application.id)).select_from(Application).join(Job)
                 .filter(Application.status.in_(["submitted", "replied", "interview", "offer", "rejected"])).group_by(Job.country).all()}
    return {"country": grp(Job.country), "country_submitted": submitted, "status": grp(Application.status), "platform": grp(Application.platform), "method": grp(Application.method), "source": grp(Job.source)}


@router.get("/review")
def review_queue(db: Session = Depends(get_db), country: str | None = None, platform: str | None = None, page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200)):
    qs = _filtered(db, country=country, platform=platform).filter(Application.status.in_(["pending_review", "needs_human"]))
    total = qs.count()
    rows = qs.order_by(Job.fit_score.desc().nullslast(), Application.created_at.asc(), Application.id.asc()).offset((page - 1) * size).limit(size).all()
    return {"total": total, "items": [_out(a) for a in rows]}


@router.post("/approve-all")
def approve_all(body: dict = Body(default={}), db: Session = Depends(get_db)):
    """Approve every application currently waiting for review, honouring the filters shown on screen.

    Only touches pending_review: anything stopped on a CAPTCHA or an unanswered question still needs you to look at it
    and say why retrying is safe. Applications whose documents no longer match your profile, or whose fact-check did not
    pass, are reported back rather than approved.
    """
    from sqlalchemy import text
    from backend.core.submissions import approval_issue
    db.execute(text("BEGIN IMMEDIATE"))
    qs = _filtered(db, country=body.get("country"), platform=body.get("platform")).filter(Application.status == "pending_review")
    approved, blocked = [], []
    for a in qs.all():
        issue = approval_issue(a)
        if issue:
            blocked.append({"id": a.id, "company": a.job.company if a.job else None, "reason": issue}); continue
        a.status = "approved"; a.error = None
        approved.append(a.id)
    db.commit()
    return {"approved": len(approved), "blocked": blocked[:20], "blocked_total": len(blocked)}


@router.post("/{app_id}/status")
def set_status(app_id: int, body: StatusUpdate, db: Session = Depends(get_db)):
    from sqlalchemy import text
    db.execute(text("BEGIN IMMEDIATE"))
    a = db.get(Application, app_id)
    if not a: raise HTTPException(404)
    from backend.core.transitions import application_transition
    try: application_transition(a, body.status, body.note)
    except ValueError as e: raise HTTPException(409, str(e))
    a.status = body.status
    if body.note:
        if body.status == "submitted": a.confirmation_text = body.note
        else: a.answers = {**(a.answers or {}), "review_note": body.note}
    if body.status in ("submitted", "replied", "interview", "offer", "rejected"): a.job.status = "applied"
    if body.status == "approved": a.error = None
    if body.status == "submitted" and not a.submitted_at:
        a.submitted_at = datetime.utcnow()
    db.commit()
    return {"ok": True}
