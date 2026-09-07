"""Outreach pipeline (no browser): for queued/applied jobs, find a likely hiring contact and draft a personalised email / LinkedIn note.
Contact finding without paid APIs: (1) job description (recruiter name/email, 'apply to <email>' patterns, HN 'Who is hiring' contact lines),
(2) company careers/about pages (mailto:), (3) YC founder name from raw, (4) fallback pattern email firstname@domain (confidence 'pattern').
LinkedIn people lookup is done by the linkedin skill mode 'find_people' (browser) which fills Contact.linkedin_url."""
from __future__ import annotations
import re, json
from urllib.parse import urlparse
import httpx
from backend.app.db import session
from backend.app.models import Job, Application, Contact, Outreach
from backend.core import llm, profile, config

EMAIL_RX = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
SKIP_EMAIL = re.compile(r"noreply|no-reply|privacy|legal|support@|help@|info@|hello@|contact@|sales@|billing@|press@|security@|abuse@|"
                        r"example\.com|sentry|wixpress", re.I)
# Aggregators and job boards are not the employer: emailing them produces polite "we're just a job board" replies
# (Japan Dev, 2026-09-07) and burns the daily cold-email budget. Never treat their domain as a hiring contact.
BOARD_DOMAINS = re.compile(r"japan-dev\.com|hiring\.cafe|hiringcafe|relocate\.me|arbeitnow|weworkremotely|remoteok|remotive|"
                           r"himalayas\.app|wellfound|angel\.co|ycombinator|workatastartup|linkedin\.com|indeed|glassdoor|"
                           r"naukri|instahyre|cutshort|hirist|peerlist|jobright|greenhouse|lever\.co|ashbyhq|workable|"
                           r"smartrecruiters|recruitee|freshteam|bamboohr|jazzhr|teamtailor", re.I)

EMAIL_SYSTEM = """Write a short cold email (90-140 words) from the candidate to a hiring contact about ONE specific role. Use only profile facts.
Structure: 1 line why this company/role specifically (from the JD), 2-3 lines of the most relevant proof (Purplle production LLM work), 1 line ask (15-min chat or forward to hiring manager), sign-off with name + LinkedIn.
No flattery, no buzzword lists, no 'I hope this finds you well'. Subject line <= 60 chars mentioning the role. Return JSON {"subject": str, "body": str, "linkedin_note": str (<=280 chars, for a connection request)}"""


def _domain(job: Job) -> str | None:
    for u in (job.company_domain, job.apply_url, job.url):
        if not u: continue
        host = urlparse(u if "://" in u else "https://" + u).netloc.lower().removeprefix("www.")
        if host and not re.search(r"greenhouse|lever|ashby|workable|linkedin|wellfound|workatastartup|naukri|instahyre|cutshort|hirist|peerlist|remoteok|remotive|himalayas|arbeitnow|weworkremotely|ycombinator|news\.ycombinator", host):
            return host
    return None


def _search_domain(company: str) -> str | None:
    """Find the company website via DuckDuckGo HTML (no API key). Returns a host or None."""
    try:
        r = httpx.get("https://html.duckduckgo.com/html/", params={"q": f"{company} official website"}, timeout=12, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=True)
        for m in re.finditer(r'class="result__url"[^>]*>\s*([^<\s]+)', r.text):
            host = m.group(1).strip().lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").split("/")[0]
            if host and not re.search(r"linkedin|wikipedia|crunchbase|glassdoor|indeed|facebook|twitter|x\.com|youtube|ycombinator|greenhouse|lever|ashby|instagram|bloomberg|zoominfo|pitchbook|g2\.com|reddit", host):
                # sanity: the host must share a real token with the company name, else we email strangers (SWARA -> a hotel)
                toks = [t for t in re.findall(r"[a-z0-9]+", company.lower()) if len(t) >= 3 and t not in ("inc", "ltd", "llc", "the", "and", "com", "corp", "labs", "group", "technologies", "technology", "software", "solutions")]
                if any(t in host.replace("-", "") for t in toks):
                    return host
                continue
    except Exception:
        return None
    return None


def _from_description(job: Job) -> tuple[str | None, str | None]:
    d = job.description or ""
    emails = [e for e in EMAIL_RX.findall(d) if not SKIP_EMAIL.search(e)]
    name = None
    m = re.search(r"(?:contact|reach out to|email|apply to|write to)\s*:?\s*([A-Z][a-z]+(?: [A-Z][a-z]+)?)", d)
    if m: name = m.group(1)
    return (emails[0] if emails else None), name


def _from_site(domain: str) -> str | None:
    for path in ("", "/careers", "/jobs", "/about", "/contact"):
        try:
            r = httpx.get(f"https://{domain}{path}", timeout=10, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code >= 400: continue
            emails = [e for e in EMAIL_RX.findall(r.text) if not SKIP_EMAIL.search(e) and e.lower().endswith(domain)]
            pref = [e for e in emails if re.search(r"careers|jobs|hiring|talent|recruit|people|founders|hello|team", e, re.I)]
            if pref or emails: return (pref or emails)[0]
        except Exception:
            continue
    return None


def find_contact(db, job: Job) -> Contact | None:
    existing = db.query(Contact).filter(Contact.company == job.company, Contact.email_confidence == "found").first()
    if existing: return existing
    email, name = _from_description(job)
    if email and BOARD_DOMAINS.search(email):
        email = None                      # e.g. help@stickermule.com-style support desks and board addresses
    conf, source = ("found", "jd") if email else (None, None)
    domain = _domain(job) or _search_domain(job.company)
    if domain and BOARD_DOMAINS.search(domain):
        domain = None                     # the posting lives on a board; its domain is not the employer's
    if domain:
        job.company_domain = domain
    if not email and domain:
        email = _from_site(domain)
        if email: conf, source = "found", "website"
    if not email and (job.raw or {}).get("founder"):
        name = job.raw["founder"]
    if not email and domain:
        email = f"careers@{domain}"; conf, source = "pattern", "guess"   # kept as a placeholder only; never emailed
    if not email and not domain: return None
    c = Contact(company=job.company, name=name or "Hiring team", title="Hiring contact", email=email, email_confidence=conf, source=source)
    db.add(c); db.flush()
    return c


def draft(ctx, limit: int = 20, provider: str | None = None):
    """Draft email + LinkedIn note for jobs with an application (queued/applied) and no outreach yet."""
    prof = profile.as_text()
    with session() as db:
        done = {o.job_id for o in db.query(Outreach.job_id).filter(Outreach.job_id.isnot(None)).all()}
        jobs = [j.id for j in db.query(Job).join(Application).filter(Job.status.in_(["queued", "applied"]), ~Job.id.in_(done)).order_by(Job.fit_score.desc()).limit(limit).all()]
    ctx.log("info", f"outreach: drafting for {len(jobs)} job(s)")
    for jid in jobs:
        if ctx.should_stop(): break
        with session() as db:
            j = db.get(Job, jid)
            c = find_contact(db, j)
            if not c:
                ctx.bump("no_contact"); continue
            cid, cname, cemail, cconf = c.id, c.name, c.email, c.email_confidence
            jt = f"Company: {j.company}\nTitle: {j.title}\nURL: {j.url}\n\n{(j.description or '')[:4000]}"
            company, title = j.company, j.title
        try:
            out = llm.complete_json("outreach", f"PROFILE:\n{prof}\n\nCONTACT: {cname} ({cemail}, confidence {cconf})\n\nJOB:\n{jt}", EMAIL_SYSTEM, provider=provider)
        except Exception as e:  # noqa: BLE001
            ctx.log("warn", f"outreach draft failed for {company}: {e}"); ctx.bump("failed"); continue
        with session() as db:
            if cconf == "found":   # guessed careers@ addresses bounce; email only real contacts, LinkedIn covers the rest
                db.add(Outreach(job_id=jid, contact_id=cid, channel="email", step=1, subject=out.get("subject"), body=out.get("body"), status="pending_review"))
            db.add(Outreach(job_id=jid, contact_id=cid, channel="linkedin_connect", step=1, body=out.get("linkedin_note"), status="pending_review"))
        ctx.log("info", f"outreach drafted: {company} — {title} -> {cemail} ({cconf})"); ctx.bump("drafted")
    ctx.log("info", f"outreach done: {ctx.stats}")


PIPELINES = {"outreach_draft": draft}
