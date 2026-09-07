"""Google Sheet tracker: one spreadsheet, tabs Jobs / Applications / Outreach, fully rewritten on each sync (source of truth stays SQLite)."""
from __future__ import annotations
import json
from datetime import datetime
from backend.app.db import session
from backend.app.models import Job, Application, Outreach, Contact
from backend.core import config, google_auth

SHEET_ID_KEY = "GOOGLE_SHEET_ID"


def _svc():
    from googleapiclient.discovery import build
    return build("sheets", "v4", credentials=google_auth.sheets_credentials(), cache_discovery=False)


def _ensure_sheet(svc) -> str:
    sid = config.env(SHEET_ID_KEY)
    if sid: return sid
    body = {"properties": {"title": f"Job Autopilot tracker ({datetime.now():%Y-%m-%d})"},
            "sheets": [{"properties": {"title": t}} for t in ("Applications", "Jobs", "Outreach")]}
    res = svc.spreadsheets().create(body=body).execute()
    sid = res["spreadsheetId"]
    with open(config.ROOT / ".env", "a") as f:
        f.write(f"\n{SHEET_ID_KEY}={sid}\n")
    import os; os.environ[SHEET_ID_KEY] = sid
    return sid


def _rows():
    with session() as db:
        apps = [["id", "status", "company", "title", "platform", "method", "fit_score", "submitted_at", "created_at", "url", "resume", "error", "cover_note"]]
        for a in db.query(Application).join(Job).order_by(Application.created_at.desc()).all():
            apps.append([a.id, a.status, a.job.company, a.job.title, a.platform, a.method, a.job.fit_score, str(a.submitted_at or ""), str(a.created_at)[:19], a.job.url, a.resume_path or "", a.error or "", (a.cover_note or "")[:1000]])
        jobs = [["id", "status", "fit_score", "company", "title", "location", "source", "ats", "eligible", "reason", "posted_at", "url"]]
        for j in db.query(Job).filter(Job.eligible == True).order_by(Job.fit_score.desc().nullslast(), Job.created_at.desc()).limit(3000).all():  # noqa: E712
            jobs.append([j.id, j.status, j.fit_score, j.company, j.title, j.location or "", j.source, j.ats or "", bool(j.eligible), j.eligibility_reason or "", str(j.posted_at or "")[:10], j.url])
        outs = [["id", "status", "channel", "step", "company", "contact", "subject", "sent_at", "replied_at", "body"]]
        for o in db.query(Outreach).order_by(Outreach.created_at.desc()).all():
            c = db.get(Contact, o.contact_id) if o.contact_id else None
            j = db.get(Job, o.job_id) if o.job_id else None
            outs.append([o.id, o.status, o.channel, o.step, (j.company if j else (c.company if c else "")), (c.name if c else ""), o.subject or "", str(o.sent_at or ""), str(o.replied_at or ""), (o.body or "")[:1000]])
    return {"Applications": apps, "Jobs": jobs, "Outreach": outs}


def sync(ctx=None):
    log = ctx.log if ctx else (lambda *a, **k: None)
    if not google_auth.sheets_configured():
        log("warn", "sheets: put the service-account key at data/google_service_account.json (and share the sheet with its email) or an OAuth client at data/google_client_secret.json"); return None
    svc = _svc()
    sid = _ensure_sheet(svc)
    data = _rows()
    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    have = {sh["properties"]["title"] for sh in meta.get("sheets", [])}
    reqs = [{"addSheet": {"properties": {"title": t}}} for t in data if t not in have]
    if reqs:
        svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": reqs}).execute()
    # if the only pre-existing tab is the default 'Sheet1', delete it
    if "Sheet1" in have and "Sheet1" not in data:
        sid_del = next((sh["properties"]["sheetId"] for sh in meta.get("sheets", []) if sh["properties"]["title"] == "Sheet1"), None)
        if sid_del is not None and len(have | set(data)) > 1:
            try: svc.spreadsheets().batchUpdate(spreadsheetId=sid, body={"requests": [{"deleteSheet": {"sheetId": sid_del}}]}).execute()
            except Exception: pass
    svc.spreadsheets().values().batchClear(spreadsheetId=sid, body={"ranges": [f"{t}!A:Z" for t in data]}).execute()
    svc.spreadsheets().values().batchUpdate(spreadsheetId=sid, body={"valueInputOption": "RAW", "data": [
        {"range": f"{t}!A1", "values": [[("" if v is None else v) for v in r] for r in rows]} for t, rows in data.items()]}).execute()
    log("info", f"sheets: synced {len(data['Applications'])-1} applications, {len(data['Jobs'])-1} jobs, {len(data['Outreach'])-1} outreach -> https://docs.google.com/spreadsheets/d/{sid}")
    if ctx: ctx.bump("synced")
    return sid
