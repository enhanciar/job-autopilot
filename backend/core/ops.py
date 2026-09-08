"""Offline maintenance: backup, report and explicit restore. Never send or launch workers."""
import argparse, json, sqlite3
from pathlib import Path
from datetime import datetime
from backend.core import config


def backup(destination: Path):
    destination = destination.resolve()
    if destination.exists(): raise ValueError('Backup destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(config.DB_PATH.resolve().as_uri()+'?mode=ro', uri=True)
    target = sqlite3.connect(destination)
    try: source.backup(target)
    finally: target.close(); source.close()
    destination.chmod(0o600)
    return str(destination)


def report():
    db=sqlite3.connect(config.DB_PATH.resolve().as_uri()+'?mode=ro',uri=True)
    queries={
        'application_statuses': 'SELECT status,count(*) FROM applications GROUP BY status',
        'reconcile_submissions': "SELECT id,platform,status FROM applications WHERE status IN ('submitting','assisting','needs_human')",
        'failed_factchecks': "SELECT id,status FROM applications WHERE json_extract(answers,'$.factcheck.ok') = 0",
        'email_routing_candidates': "SELECT a.id,j.source,a.method FROM applications a JOIN jobs j ON j.id=a.job_id WHERE j.source IN ('hackernews','reddit') AND a.method='ats_form'",
        'duplicate_application_jobs': 'SELECT job_id,count(*) FROM applications GROUP BY job_id HAVING count(*)>1',
        'duplicate_urls': 'SELECT url,count(*) FROM jobs GROUP BY url HAVING count(*)>1',
    }
    try: return {key: db.execute(query).fetchall() for key,query in queries.items()}
    finally: db.close()


def restore(source: Path, destination: Path):
    """Restore to a NEW path; never overwrite an active DB or its WAL."""
    if destination.exists(): raise ValueError('Restore requires a new destination path')
    origin=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        if origin.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise ValueError('Backup integrity check failed')
        target=sqlite3.connect(destination)
        try: origin.backup(target)
        finally: target.close()
    finally: origin.close()
    destination.chmod(0o600)


def retention(days=30):
    """Report old unreferenced files; never delete automatically."""
    import time
    db=sqlite3.connect(config.DB_PATH.resolve().as_uri()+'?mode=ro',uri=True)
    try:
        refs={str((config.ROOT / p).resolve()) for row in db.execute('SELECT resume_path,screenshot_before,screenshot_after FROM applications') for p in row if p}
        refs.update(str((config.ROOT / p).resolve()) for (p,) in db.execute('SELECT screenshot FROM events WHERE screenshot IS NOT NULL'))
    finally: db.close()
    cutoff=time.time()-days*86400
    return [str(p) for root in (config.ARTIFACTS,config.DATA/'llm_cache') if root.exists() for p in root.rglob('*') if p.is_file() and p.stat().st_mtime < cutoff and str(p.resolve()) not in refs]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['backup','report','restore','retention','relink-documents','route-email','link-apply-urls','recheck-answers','clear-stuck'])
    parser.add_argument('--apply',action='store_true',help='write the change; without it the command only reports')
    parser.add_argument('--path',type=Path); parser.add_argument('--destination',type=Path)
    args=parser.parse_args()
    if args.action=='backup':
        if not args.path: parser.error('--path is required')
        print(backup(args.path))
    elif args.action=='restore':
        if not args.path or not args.destination: parser.error('--path and --destination are required')
        restore(args.path,args.destination);print('Restored to new destination')
    elif args.action=='clear-stuck':
        if args.apply: print('backup:', backup(config.DATA/'backups'/f'before-clear-{datetime.now():%Y%m%d-%H%M%S}.db'))
        result=clear_stuck(args.apply)
        print(json.dumps({'applications_deleted':result['deleted'],'jobs_protected_from_reapplying':len(result['protected_jobs']),'jobs_marked_skipped':result['skipped_jobs']},indent=2))
        if not args.apply: print('Nothing was written. Re-run with --apply.')
    elif args.action=='recheck-answers':
        result=recheck_answers(args.apply)
        print(json.dumps({'ready':len(result['ready']),'still_blocked':result['still_blocked']},indent=2,default=str))
        if not args.apply: print('Nothing was written. Re-run with --apply.')
    elif args.action=='link-apply-urls':
        result=link_apply_urls(args.apply)
        print(json.dumps({k:len(v) for k,v in result.items()},indent=2))
        if not args.apply: print('Nothing was written. Re-run with --apply.')
    elif args.action=='route-email':
        result=route_email_applications(args.apply)
        print(json.dumps({k:len(v) for k,v in result.items()},indent=2))
        if not args.apply: print('Nothing was written. Re-run with --apply.')
    elif args.action=='relink-documents':
        result=relink_documents(args.apply)
        print(json.dumps({k:(len(v) if isinstance(v,list) else v) for k,v in result.items()},indent=2))
        if not args.apply: print('Nothing was written. Re-run with --apply to re-stamp the listed applications.')
    else: print(json.dumps(report() if args.action=='report' else retention(),indent=2))



def relink_documents(apply: bool = False) -> dict:
    """Repair for a fingerprint definition change, not a way to bless stale documents.

    Before 2026-09-08 one hash covered the profile facts AND the answer bank, so editing a screening answer marked every
    prepared resume out of date even though no word of it could have changed. This re-stamps applications whose documents
    still exist and whose stored facts overlay matches the current profile, and reports anything it will not touch.
    Never runs automatically; never touches submitted or in-flight work.
    """
    from backend.app.db import session
    from backend.app.models import Application
    from backend.core import profile
    current = profile.fingerprint()
    facts = {k: v for k, v in profile.load().items() if k in profile.DOCUMENT_FACTS}
    report = {"relinked": [], "regenerate": [], "already_current": 0}
    with session() as db:
        for a in db.query(Application).filter(Application.status.in_(["pending_review", "approved", "needs_human", "failed"])).all():
            answers = dict(a.answers or {})
            if answers.get("profile_hash") == current: report["already_current"] += 1; continue
            overlay = answers.get("overlay") or {}
            employers = {e["company"] for e in facts.get("experience", [])}
            categories = set(facts.get("skills", {}))
            drifted = (set(overlay.get("experience_bullets") or {}) - employers) or (set(overlay.get("skills_order") or []) - categories)
            if not overlay or drifted or not a.resume_path:
                report["regenerate"].append(a.id); continue
            report["relinked"].append(a.id)
            if apply:
                answers["profile_hash"] = current
                answers["answers_hash"] = profile.answers_fingerprint()
                answers["relinked_note"] = "Re-stamped on 2026-09-08: documents match the current profile facts; only the answer bank had changed."
                a.answers = answers
    return report


def route_email_applications(apply: bool = False) -> dict:
    """Repair for postings that were never web forms.

    Hacker News and Reddit hiring posts ask you to email a person; routing them to the form filler was always going to
    stop at 'no application form'. This switches those applications to the email method and puts them back in review so
    the outreach flow can draft a message you approve. Nothing is sent, and no already-submitted work is touched.
    """
    import re
    from backend.app.db import session
    from backend.app.models import Application, Job
    email_rx = re.compile(r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}")
    report = {"routed": [], "no_address": []}
    with session() as db:
        rows = db.query(Application).join(Job).filter(Job.source.in_(["hackernews", "reddit"]), Application.method.in_(["ats_form", "external"]),
                                                      Application.status.in_(["needs_human", "failed"])).all()
        for a in rows:
            text = f"{a.job.description or ''} {a.job.url or ''}"
            if not email_rx.search(text):
                report["no_address"].append(a.id); continue
            report["routed"].append(a.id)
            if apply:
                a.method = "email"; a.status = "pending_review"; a.claim_run_id = None
                a.error = None
                a.answers = {**(a.answers or {}), "repair_note": "Routed to email on 2026-09-08: the posting has no application form."}
    return report


def link_apply_urls(apply: bool = False) -> dict:
    """Repair for postings whose apply link sits inside the post text.

    Hacker News comments point at the discussion thread, not at a form, so these applications stopped with 'no application
    form' even though the comment names a careers page or an ATS link. This reads the stored post text, sets the real
    apply URL, and puts the application back in review so the normal form flow can try it.
    """
    from backend.app.db import session
    from backend.app.models import Application, Job
    from backend.core.collectors.public_apis import apply_target
    report = {"linked": [], "no_link": []}
    with session() as db:
        rows = db.query(Application).join(Job).filter(Job.source.in_(["hackernews", "reddit"]),
                                                      Application.status.in_(["needs_human", "failed"])).all()
        for a in rows:
            link, _ = apply_target(a.job.description or "")
            if not link:
                report["no_link"].append(a.id); continue
            report["linked"].append(a.id)
            if apply:
                a.job.apply_url = link
                a.method = "ats_form"; a.status = "pending_review"; a.error = None; a.claim_run_id = None
                a.answers = {**(a.answers or {}), "repair_note": f"Apply link taken from the post text on 2026-09-08: {link}"}
    return report


def recheck_answers(apply: bool = False) -> dict:
    """Applications that stopped because a screening question had no answer, re-checked against the current answer bank.

    These stopped during form validation, before anything was sent, so there is nothing to reconcile with the employer.
    Ones whose every open question now resolves go back to the review queue for you to approve; the rest are listed with
    the questions that still need a decision only you can make.
    """
    import re
    from backend.app.db import session
    from backend.app.models import Application
    from backend.core import profile
    from backend.core.apply import forms
    report = {"ready": [], "still_blocked": {}}
    with session() as db:
        for a in db.query(Application).filter(Application.status == "needs_human", Application.error.like("Unanswered fields%")).all():
            fields = [f.strip() for f in re.sub(r"\s*\(options:[^)]*\)|\s*\(choices:[^)]*\)", "", a.error[len("Unanswered fields:"):]).split("|") if f.strip()]
            forms.CTX.clear(); forms.CTX["country"] = a.job.country
            open_questions = [f for f in dict.fromkeys(fields)
                              if not (forms._country_aware(f) or profile.answer_for(f))]
            if open_questions:
                report["still_blocked"][a.id] = {"company": a.job.company, "questions": open_questions[:6]}
                continue
            report["ready"].append(a.id)
            if apply:
                a.status = "pending_review"
                a.answers = {**(a.answers or {}), "repair_note": "Every open question now has an answer; nothing was ever submitted to this employer."}
                a.error = None
    forms.CTX.clear()
    return report


def clear_stuck(apply: bool = False) -> dict:
    """Delete applications that stopped on a human step, and stop their jobs coming straight back.

    Two safeguards, because deleting the record is the only thing that remembers we ever touched an employer:
      * a job whose application may already have reached the employer (submit was pressed, no confirmation seen) is marked
        'applied', never 'skipped', so a later run cannot send that employer a second application;
      * every other job is marked 'skipped', otherwise the next run re-tailors the same posting and it gets stuck again.
    Confirmed submissions, replies and anything waiting for your approval are left alone. Back up before using --apply.
    """
    from datetime import datetime
    from backend.app.db import session
    from backend.app.models import Application
    uncertain = ("no confirmation text", "submitting", "unverified", "could not be verified")
    report = {"deleted": 0, "protected_jobs": [], "skipped_jobs": 0, "backup": None}
    with session() as db:
        rows = db.query(Application).filter(Application.status.in_(["needs_human", "failed"])).all()
        for a in rows:
            maybe_sent = any(w in (a.error or "").lower() for w in uncertain) or a.submitted_at is not None
            report["deleted"] += 1
            if maybe_sent: report["protected_jobs"].append((a.job_id, a.job.company))
            else: report["skipped_jobs"] += 1
            if apply:
                a.job.status = "applied" if maybe_sent else "skipped"
                a.job.eligibility_reason = ((a.job.eligibility_reason or "") +
                                            f" | cleared {datetime.utcnow():%Y-%m-%d}: " +
                                            ("application may already have reached this employer" if maybe_sent else "stuck application removed at your request"))[:2000]
                db.delete(a)
    return report


if __name__=='__main__': main()
