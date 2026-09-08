from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from backend.app.db import get_db
from backend.app.models import Outreach, Contact, Job
from backend.app.schemas import OutreachOut, StatusUpdate

router = APIRouter(prefix="/outreach", tags=["outreach"])


@router.get("/facets")
def facets(db: Session = Depends(get_db)):
    """Counts per channel and per source, so each platform's outreach can be looked at on its own."""
    from sqlalchemy import func
    channels = {k: v for k, v in db.query(Outreach.channel, func.count(Outreach.id)).group_by(Outreach.channel).all()}
    waiting = {k: v for k, v in db.query(Outreach.channel, func.count(Outreach.id))
               .filter(Outreach.status == "pending_review").group_by(Outreach.channel).all()}
    sources = {(k or "unknown"): v for k, v in db.query(Job.source, func.count(Outreach.id))
               .select_from(Outreach).join(Job, Job.id == Outreach.job_id).group_by(Job.source).all()}
    statuses = {k: v for k, v in db.query(Outreach.status, func.count(Outreach.id)).group_by(Outreach.status).all()}
    return {"channel": channels, "pending_by_channel": waiting, "source": sources, "status": statuses}


@router.get("/people")
def people(db: Session = Depends(get_db), company: str | None = None, q: str | None = None,
           page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200)):
    """Everyone in outreach, one row per person, with what has actually reached them.

    Grouped by person rather than by message, because the question is usually "have I contacted this human yet", and one
    person can carry an email, an invitation and a follow-up.
    """
    from sqlalchemy import func
    contacts = db.query(Contact).join(Outreach, Outreach.contact_id == Contact.id)
    if company: contacts = contacts.filter(Contact.company == company)
    if q: contacts = contacts.filter((Contact.name.ilike(f"%{q}%")) | (Contact.company.ilike(f"%{q}%")) | (Contact.title.ilike(f"%{q}%")))
    contacts = contacts.group_by(Contact.id).order_by(Contact.company, Contact.name)
    total = contacts.count()
    rows = contacts.offset((page - 1) * size).limit(size).all()

    def state(messages, prefix):
        """What actually happened on this channel, best news first."""
        got = [o.status for o in messages if o.channel.startswith(prefix)]
        for status in ("replied", "sent", "sending", "submission_unverified", "approved", "pending_review", "bounced", "stopped", "skipped"):
            if status in got: return status
        return None

    items = []
    for c in rows:
        messages = db.query(Outreach).filter(Outreach.contact_id == c.id).all()
        jobs = {o.job_id for o in messages if o.job_id}
        titles = [j.title for j in db.query(Job).filter(Job.id.in_(jobs)).all()] if jobs else []
        items.append({
            "id": c.id, "name": c.name, "title": c.title, "company": c.company,
            "linkedin_url": c.linkedin_url, "email": c.email, "email_confidence": c.email_confidence,
            "linkedin_state": state(messages, "linkedin"), "email_state": state(messages, "email"),
            "x_state": state(messages, "x_"),
            "messages": len(messages), "roles": titles[:3],
            "last_sent": max([o.sent_at for o in messages if o.sent_at], default=None),
        })
    by_company = {k: v for k, v in db.query(Contact.company, func.count(func.distinct(Contact.id)))
                  .join(Outreach, Outreach.contact_id == Contact.id).group_by(Contact.company).all()}
    return {"total": total, "items": items, "by_company": by_company}


@router.get("")
def list_outreach(db: Session = Depends(get_db), status: str | None = None, channel: str | None = None,
                  source: str | None = None, page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=200)):
    qs = db.query(Outreach)
    if source: qs = qs.join(Job, Job.id == Outreach.job_id).filter(Job.source == source)
    if channel: qs = qs.filter(Outreach.channel == channel)
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
