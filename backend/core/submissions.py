"""Submission invariants shared by API and workers."""
from backend.app.db import session
from backend.app.models import Application, Job
from backend.core import config, profile
from sqlalchemy import text


def approval_issue(app) -> str | None:
    answers = app.answers or {}
    check = answers.get("factcheck") or {}
    if check.get("ok") is not True or check.get("violations"):
        return "Application must pass fact-check before approval"
    if answers.get("profile_hash") != profile.fingerprint():
        return "Your profile facts changed since these documents were written (or this is a legacy document). Regenerate them and review again."
    return None


def claim(aid: int, run_id: int | None = None) -> bool:
    cfg = config.load()
    with session() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        app = db.get(Application, aid)
        if not app: return False
        issue = approval_issue(app)
        if issue:
            app.error = issue
            return False
        allowed = app.status == 'approved' or (not cfg.get('review_mode', True) and app.status == 'pending_review' and (app.job.fit_score or 0) >= cfg.get('auto_submit_min_score', 75))
        if not allowed: return False
        app.claim_run_id = run_id
        app.status = 'submitting'; app.error = None
        return True


def confirmation(body: str, phrase: str | None = None) -> bool:
    body = body.lower()
    specific = ('application received', "we've received your application", 'successfully submitted', 'application submitted', 'thanks for applying', 'thank you for applying', 'your application has been received')
    # A model-generated phrase is not independent evidence of a successful submission.
    return any(p in body for p in specific)


def claim_assistance(aid: int, run_id: int) -> bool:
    """Explicit manual assistance may fill an uncertain form; it never auto-submits."""
    with session() as db:
        db.execute(text('BEGIN IMMEDIATE'))
        app = db.get(Application, aid)
        if not app or app.status not in ('needs_human', 'failed', 'approved'): return False
        issue = approval_issue(app)
        if issue:
            app.error = issue
            return False
        app.status = 'assisting'; app.claim_run_id = run_id
        return True
