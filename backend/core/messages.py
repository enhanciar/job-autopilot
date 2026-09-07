"""Approval and reconciliation rules shared by messaging workers."""
from datetime import datetime
from sqlalchemy import text, func
from backend.app.db import session
from backend.app.models import Outreach, Contact, Application


def recipient_ids(db, contact):
    if not contact or not contact.email: return []
    address = contact.email.strip().lower()
    return [cid for (cid,) in db.query(Contact.id).filter(func.lower(func.trim(Contact.email)) == address).all()]


def claim(oid, run_id: int | None = None, expected=None):
    with session() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        o = db.get(Outreach, oid)
        if not o or o.status != "approved" or not (o.body or "").strip(): return False
        if o.scheduled_for and o.scheduled_for > datetime.utcnow(): return False
        c = db.get(Contact, o.contact_id) if o.contact_id else None
        ids = [o.contact_id] if o.contact_id else []
        if o.channel == "email":
            if not c or not c.email or c.email_confidence != "found": return False
            ids = recipient_ids(db, c)
            if db.query(Contact).filter(Contact.id.in_(ids), Contact.email_confidence == "bounced").first(): return False
            if db.query(Outreach).filter(Outreach.contact_id.in_(ids), Outreach.channel == "email", Outreach.id != oid,
                                        Outreach.status.in_(["bounced", "replied", "stopped"])).first(): return False
            if expected is not None and expected != (c.email.strip().lower(), o.subject or "Quick note", o.body or "", o.thread_id): return False
            if o.step > 1 and not db.query(Outreach).filter(Outreach.contact_id.in_(ids), Outreach.channel == "email",
                    Outreach.step == o.step - 1, Outreach.status == "sent", Outreach.thread_id == o.thread_id).first(): return False
        if ids and db.query(Outreach).filter(Outreach.contact_id.in_(ids), Outreach.channel == o.channel,
                Outreach.id != o.id, Outreach.status.in_(["sending", "sent", "replied", "submission_unverified"]), Outreach.step == o.step).first(): return False
        if o.channel == "email" and o.step == 1 and o.job_id:
            app = db.query(Application).filter_by(job_id=o.job_id, method="email").first()
            if app:
                from backend.core.submissions import approval_issue
                if app.status != "approved" or approval_issue(app): return False
                app.status = "submitting"; app.claim_run_id = run_id
        o.claim_run_id = run_id
        o.status = "sending"
    return True


def visible_message(page, body, selector):
    needle = " ".join((body or "").lower().split())
    if not needle: return False
    try:
        return any(needle in " ".join(t.lower().split()) for t in page.locator(selector).all_inner_texts())
    except Exception: return False


def set_body(o, body: str, why: str = "text changed after approval") -> bool:
    """Replace a draft's text. An approval covers the exact words the user read, so a changed approved message goes
    back to review instead of being sent with different content. Returns True when the approval was reset."""
    body = (body or "").strip()
    if body == (o.body or "").strip(): return False
    reset = o.status == "approved"
    o.body = body
    if reset:
        o.status = "pending_review"; o.error = f"Re-review required: {why}"
    return reset
