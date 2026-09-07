from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, func
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.models import Job, Application
from backend.app.schemas import JobOut, JobDetail, StatusUpdate

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("", response_model=dict)
def list_jobs(db: Session = Depends(get_db), q: str | None = None, source: str | None = None, status: str | None = None,
              eligible: bool | None = None, min_score: int | None = None, sponsor: bool | None = None, country: str | None = None,
              sort: str = Query("created_at", pattern="^(created_at|posted_at|fit_score|company|title|updated_at)$"), order: str = Query("desc", pattern="^(asc|desc)$"), page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200)):
    qs = db.query(Job)
    if q:
        like = f"%{q}%"
        qs = qs.filter(or_(Job.title.ilike(like), Job.company.ilike(like), Job.location.ilike(like)))
    if source: qs = qs.filter(Job.source == source)
    if status: qs = qs.filter(Job.status == status)
    if eligible is not None: qs = qs.filter(Job.eligible == eligible)
    if sponsor is not None: qs = qs.filter(Job.sponsor_flag == sponsor)
    if country: qs = qs.filter(Job.country == country)
    if min_score is not None: qs = qs.filter(Job.fit_score >= min_score)
    total = qs.count()
    col = getattr(Job, sort, Job.created_at)
    qs = qs.order_by(col.desc() if order == "desc" else col.asc()).offset((page - 1) * size).limit(size)
    return {"total": total, "items": [JobOut.model_validate(j).model_dump() for j in qs.all()]}


@router.get("/facets")
def facets(db: Session = Depends(get_db)):
    src = db.query(Job.source, func.count(Job.id)).group_by(Job.source).all()
    st = db.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    co = db.query(Job.country, func.count(Job.id)).filter(Job.eligible == True).group_by(Job.country).order_by(func.count(Job.id).desc()).all()  # noqa: E712
    return {"sources": dict(src), "statuses": dict(st), "countries": {k or "Unknown": v for k, v in co}}


@router.get("/{job_id}", response_model=JobDetail)
def get_job(job_id: int, db: Session = Depends(get_db)):
    j = db.get(Job, job_id)
    if not j: raise HTTPException(404)
    return j


@router.post("/{job_id}/status")
def set_status(job_id: int, body: StatusUpdate, db: Session = Depends(get_db)):
    j = db.get(Job, job_id)
    if not j: raise HTTPException(404)
    if body.status not in {"queued", "skipped", "new", "expired"}: raise HTTPException(422, "Invalid job transition")
    if body.status == "queued":
        if db.query(Application).filter(Application.job_id == job_id).first():
            raise HTTPException(409, "This job already has an application; open its review record")
        if not j.eligible: raise HTTPException(409, "Verify eligibility before queuing")
        j.status = "scored" if j.fit_score is not None else "new"
        db.commit()
        from backend.core.runner import enqueue
        rid = enqueue("pipeline", "prepare_selected", {"job_ids": [job_id]})
        return {"ok": True, "run_id": rid}
    else:
        j.status = body.status
        db.commit()
    return {"ok": True}
