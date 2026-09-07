from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.models import Outreach, Contact, Job
from backend.app.schemas import OutreachOut, StatusUpdate

router = APIRouter(prefix="/outreach", tags=["outreach"])


@router.get("")
def list_outreach(db: Session = Depends(get_db), status: str | None = None, page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200)):
    qs = db.query(Outreach)
    if status: qs = qs.filter(Outreach.status == status)
    total = qs.count()
    rows = qs.order_by(Outreach.created_at.desc()).offset((page - 1) * size).limit(size).all()
    items = []
    for o in rows:
        d = OutreachOut.model_validate(o).model_dump()
        c = db.get(Contact, o.contact_id) if o.contact_id else None
        j = db.get(Job, o.job_id) if o.job_id else None
        d.update(contact_name=c.name if c else None, contact_title=c.title if c else None, company=(j.company if j else (c.company if c else None)))
        items.append(d)
    return {"total": total, "items": items}


@router.post("/{oid}/status")
def set_status(oid: int, body: StatusUpdate, db: Session = Depends(get_db)):
    from sqlalchemy import text
    db.execute(text("BEGIN IMMEDIATE"))
    o = db.get(Outreach, oid)
    if not o: raise HTTPException(404)
    if body.status not in ("approved", "stopped", "pending_review"): raise HTTPException(422, "Invalid outreach status")
    if o.status not in ("pending_review", "approved", "failed", "stopped"): raise HTTPException(409, "Sent or claimed messages cannot be reapproved")
    if body.status == "approved" and not (o.body or "").strip(): raise HTTPException(409, "A message body is required")
    o.status = body.status
    db.commit()
    return {"ok": True}
