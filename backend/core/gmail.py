"""Gmail via API (OAuth once). send(): approved Outreach rows with channel=email. poll(): mark replies/rejections on threads we sent."""
from __future__ import annotations
import base64, re
from datetime import datetime
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
from backend.app.db import session
from backend.app.models import Outreach, Contact, Application, Job
from backend.core import config, google_auth, humanize, profile

REJECT_RX = re.compile(r"unfortunately|not (be )?moving forward|other candidates|decided not to|position has been filled|regret to inform", re.I)
INTERVIEW_RX = re.compile(r"interview|schedule a (call|chat|time)|calendly|next steps|would love to (chat|talk)|book a time", re.I)


def _svc():
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=google_auth.credentials(), cache_discovery=False)


def send(ctx, limit: int = 20):
    from backend.core.messages import claim
    if not google_auth.configured():
        ctx.log("warn", "gmail: client secret missing; skipping"); return
    svc = _svc(); me = profile.get("identity.email")
    with session() as db:
        ids = [o.id for o in db.query(Outreach).filter(Outreach.channel == "email", Outreach.status == "approved",
            (Outreach.scheduled_for.is_(None)) | (Outreach.scheduled_for <= datetime.utcnow())).order_by(Outreach.created_at).limit(limit).all()]
    for oid in ids:
        if ctx.should_stop(): break
        with session() as db:
            o = db.get(Outreach, oid); c = db.get(Contact, o.contact_id) if o.contact_id else None
            if not c or not c.email or c.email_confidence != "found":
                o.status = "stopped"; o.error = "No discovered recipient email"; continue
            address = c.email.strip().lower()
            related = [cid for (cid,) in db.query(Contact.id).filter(Contact.email.ilike(address)).all()]
            suppression = db.query(Outreach).filter(Outreach.contact_id.in_(related), Outreach.channel == "email",
                Outreach.id != oid, Outreach.status.in_(["bounced", "replied", "stopped"])).first()
            duplicate = db.query(Outreach).filter(Outreach.contact_id.in_(related), Outreach.channel == "email", Outreach.step == o.step,
                Outreach.id != oid, Outreach.status.in_(["sending", "sent", "replied", "submission_unverified"])).first()
            if suppression or duplicate:
                o.status = "stopped"; o.error = "Recipient suppression or duplicate message"; continue
            subject, body, tid = o.subject or "Quick note", o.body or "", o.thread_id
            app = db.query(Application).filter(Application.job_id == o.job_id).first() if o.job_id else None
            attachment = app.resume_path if app and o.step == 1 else None
        msg = MIMEMultipart(); msg["To"], msg["From"], msg["Subject"] = address, me, subject
        msg.attach(MIMEText(body, "plain"))
        if attachment and (config.ROOT / attachment).exists():
            part = MIMEApplication((config.ROOT / attachment).read_bytes(), _subtype="pdf")
            part.add_header("Content-Disposition", "attachment", filename="Resume.pdf"); msg.attach(part)
        payload = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}
        if tid:
            thread = svc.users().threads().get(userId="me", id=tid, format="metadata", metadataHeaders=["Message-ID"]).execute()
            headers = thread.get("messages", [{}])[-1].get("payload", {}).get("headers", [])
            mid = next((h["value"] for h in headers if h["name"].lower() == "message-id"), None)
            if mid: msg["In-Reply-To"] = mid; msg["References"] = mid
            payload = {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode(), "threadId": tid}
        # Complete local payload and read-only thread lookup before claiming a send.
        if ctx.should_stop(): break
        if not humanize.take("email", "cold", address): break
        if not claim(oid, ctx.run_id, expected=(address, subject, body, tid)): continue
        try:
            if ctx.should_stop():
                with session() as db:
                    db.get(Outreach, oid).status = "approved"
                    _email_application(db, oid, ctx.run_id, "approved")
                break
            res = svc.users().messages().send(userId="me", body=payload).execute()
            with session() as db:
                o = db.get(Outreach, oid); o.status = "sent"; o.sent_at = datetime.utcnow(); o.thread_id = res.get("threadId"); o.provider_message_id = res.get("id")
                _email_application(db, oid, ctx.run_id, "submitted", res.get("id"))
            ctx.bump("sent"); ctx.log("info", f"gmail: sent outreach #{oid}")
        except Exception as e:
            # A timeout may occur after Gmail accepted the email. Never blindly retry.
            with session() as db:
                o = db.get(Outreach, oid); o.status = "submission_unverified"; o.error = str(e)[:500]
                _email_application(db, oid, ctx.run_id, "needs_human")
            ctx.bump("unverified")
        humanize.pause("between_items_s")


def _email_application(db, oid, run_id, status, message_id=None):
    outreach = db.get(Outreach, oid)
    if not outreach or outreach.step != 1 or not outreach.job_id: return
    app = db.query(Application).filter_by(job_id=outreach.job_id, method="email", status="submitting", claim_run_id=run_id).first()
    if not app: return
    app.status = status
    if status == "submitted":
        app.submitted_at = datetime.utcnow(); app.job.status = "applied"
        app.confirmation_text = f"Gmail accepted application email; message {message_id or 'ID unavailable'}. This does not prove delivery."
    elif status == "needs_human": app.error = "Email send result is uncertain; check Sent before retrying"


def poll(ctx):
    """Check threads we sent for replies; classify interview / rejection / reply."""
    if not google_auth.configured(): return
    svc = _svc()
    with session() as db:
        rows = [(o.id, o.thread_id) for o in db.query(Outreach).filter(Outreach.status == "sent", Outreach.thread_id.isnot(None)).all()]
    hits = 0
    for oid, tid in rows:
        if ctx.should_stop(): break
        try:
            th = svc.users().threads().get(userId="me", id=tid, format="metadata", metadataHeaders=["From"]).execute()
        except Exception:  # noqa: BLE001
            continue
        msgs = th.get("messages", [])
        if len(msgs) < 2: continue
        last = msgs[-1]; snippet = last.get("snippet", "")
        frm = next((h["value"] for h in last["payload"]["headers"] if h["name"] == "From"), "")
        if profile.get("identity.email") in frm: continue
        if "mailer-daemon" in frm.lower() or "address not found" in snippet.lower() or "wasn't delivered" in snippet.lower():
            with session() as db:
                o = db.get(Outreach, oid); o.status = "bounced"; o.error = "delivery failed"
                if o.contact_id: db.get(Contact, o.contact_id).email_confidence = "bounced"
            continue
        with session() as db:
            o = db.get(Outreach, oid); o.status = "replied"; o.replied_at = datetime.utcnow()
            app = db.query(Application).filter(Application.job_id == o.job_id).first() if o.job_id else None
            # An email reply is not proof that an ATS application changed state.
            o.error = "Reply received; review the thread to classify interview/rejection."
            for follow in db.query(Outreach).filter(Outreach.contact_id == o.contact_id, Outreach.channel == "email", Outreach.status.in_(["pending_review", "approved"])).all():
                follow.status = "stopped"; follow.error = "Recipient replied"
        hits += 1
    ctx.log("info", f"gmail: {hits} new repl(ies)"); ctx.bump("replies", hits)


def draft_followups(ctx):
    """Draft one deterministic follow-up at a time; every step requires review."""
    from datetime import timedelta
    from sqlalchemy import text
    from backend.core.messages import recipient_ids
    with session() as db:
        originals = [o.id for o in db.query(Outreach).filter_by(channel="email", step=1, status="sent").all()]
    for original in originals:
        if ctx.should_stop(): break
        drafted = False
        with session() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            first = db.get(Outreach, original)
            if not first or first.status != "sent" or not first.sent_at or not first.thread_id: continue
            contact = db.get(Contact, first.contact_id) if first.contact_id else None
            if not contact or contact.email_confidence != "found": continue
            ids = recipient_ids(db, contact)
            rows = db.query(Outreach).filter(Outreach.channel == "email", Outreach.contact_id.in_(ids)).all()
            if any(o.status in ("replied", "bounced", "stopped", "pending_review", "approved", "sending", "submission_unverified") for o in rows): continue
            existing = {o.step: o for o in rows}
            step = 2 if 2 not in existing else 3
            if step in existing: continue
            previous = existing.get(step - 1)
            if not previous or previous.status != "sent" or not previous.sent_at: continue
            due = max(first.sent_at + timedelta(days=4 if step == 2 else 9), previous.sent_at + timedelta(days=4 if step == 2 else 5))
            if due > datetime.utcnow(): continue
            body = "Following up on my earlier note about this role. Is the team still hiring? I would be happy to answer any questions. If you prefer no further follow-ups, please let me know. Thank you."
            subject = first.subject or "Quick note"
            if not subject.lower().startswith("re:"): subject = "Re: " + subject
            db.add(Outreach(job_id=first.job_id, contact_id=first.contact_id, channel="email", step=step, subject=subject,
                body=body, status="pending_review", scheduled_for=due, thread_id=first.thread_id))
            drafted = True
        if drafted: ctx.bump("drafted")

SERVICES = {"gmail_send": send, "gmail_poll": poll, "gmail_followups": draft_followups}
