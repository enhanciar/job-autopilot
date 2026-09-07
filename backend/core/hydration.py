"""Fetch missing employer posting text before scoring; never submit a form."""
from backend.app.db import session
from backend.app.models import Job
from backend.core.collectors.base import client
from backend.core.normalize import eligibility, sponsorship_signal, country_of
from bs4 import BeautifulSoup
import json, re
from datetime import datetime

CLOSED_RX = re.compile(r"no longer accepting applications|this (job|position|role) (is no longer|has been) (available|filled|closed)|position has been filled|"
                       r"job (posting )?(has )?expired|this posting is closed|we are no longer hiring for this", re.I)
BLOCKED_RX = re.compile(r"verify you are human|checking your browser|sign in to continue|enable javascript and cookies", re.I)


def extract_posting(html: str) -> dict:
    """Pure extraction: prefer a schema.org JobPosting (what the employer actually declares), then main/article text.
    Returns {"description", "closed", "blocked", "source"}; description is "" when nothing usable was found."""
    soup = BeautifulSoup(html or "", "lxml")
    out = {"description": "", "closed": False, "blocked": False, "source": None}
    for tag in soup.find_all("script", type="application/ld+json"):
        try: data = json.loads(tag.string or "")
        except ValueError: continue
        nodes = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for node in nodes:
            if not isinstance(node, dict) or "jobposting" not in str(node.get("@type", "")).lower(): continue
            text = re.sub(r"[ \t]+", " ", BeautifulSoup(node.get("description") or "", "lxml").get_text(" ", strip=True))
            valid = node.get("validThrough")
            if valid:
                try:
                    if datetime.fromisoformat(str(valid).replace("Z", "+00:00")).replace(tzinfo=None) < datetime.utcnow(): out["closed"] = True
                except ValueError: pass
            if len(text) >= 200:
                out["description"], out["source"] = text[:20000], "jsonld"
                return out
    for element in soup(["script", "style", "nav", "footer", "header", "noscript"]): element.decompose()
    body_text = soup.get_text(" ", strip=True)
    if BLOCKED_RX.search(body_text[:1500]): out["blocked"] = True; return out
    if CLOSED_RX.search(body_text[:4000]): out["closed"] = True
    main = soup.find("main") or soup.find("article") or soup.find(attrs={"class": re.compile(r"job|posting|description|content", re.I)}) or soup
    description = main.get_text("\n", strip=True)[:20000]
    if len(description) >= 200: out["description"], out["source"] = description, "page"
    return out


def hydrate(ctx, limit=100, sources=None, job_ids=None):
    from sqlalchemy import func
    with session() as db:
        rows = [(j.id, j.apply_url or j.url) for j in db.query(Job).filter(Job.status.in_(['new','filtered','scored']),
            Job.source != 'linkedin', func.length(func.coalesce(Job.description,'')) < 200,
            Job.source.in_(sources) if sources else True, Job.id.in_(job_ids) if job_ids is not None else True).limit(limit).all()]
    with client() as http:
        for jid, url in rows:
            if ctx.should_stop(): break
            if not url.startswith(('https://','http://')): continue
            try:
                response = http.get(url); response.raise_for_status()
                if 'html' not in response.headers.get('content-type',''): continue
                posting = extract_posting(response.text)
                if posting["blocked"]: continue
                if posting["closed"]:
                    with session() as db:
                        job = db.get(Job, jid); job.status = 'expired'; job.eligibility_reason = 'posting closed (seen during hydration)'
                    ctx.bump('expired'); continue
                description = posting["description"]
                if len(description) < 200: continue
                with session() as db:
                    job = db.get(Job,jid); job.description=description; job.fit_score=None; job.fit_reasons=None
                    job.sponsor_flag=sponsorship_signal(description)
                    job.eligible,job.eligibility_reason=eligibility(job.title,job.location,job.remote_scope,description,job.sponsor_flag)
                    job.status='new' if job.eligible else 'filtered'
                ctx.bump('hydrated')
            except Exception as e: ctx.log('warn',f'Posting {jid} could not be hydrated: {type(e).__name__}')
