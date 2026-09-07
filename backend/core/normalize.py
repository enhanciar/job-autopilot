"""Turn raw postings into Job rows: ATS detection, dedupe key, eligibility rules, sponsor flag."""
from __future__ import annotations
import hashlib, re
from datetime import datetime
from urllib.parse import urlparse
from backend.core import config
from backend.app.db import session
from backend.app.models import Job

ATS_DOMAINS = {
    "greenhouse.io": "greenhouse", "boards.greenhouse.io": "greenhouse", "job-boards.greenhouse.io": "greenhouse",
    "lever.co": "lever", "ashbyhq.com": "ashby", "workable.com": "workable", "smartrecruiters.com": "smartrecruiters",
    "rippling.com": "rippling", "recruitee.com": "recruitee", "teamtailor.com": "teamtailor", "personio.de": "personio",
    "bamboohr.com": "bamboohr", "breezy.hr": "breezy", "jazzhr.com": "jazzhr", "workday.com": "workday", "myworkdayjobs.com": "workday",
    "linkedin.com": "linkedin", "wellfound.com": "wellfound", "workatastartup.com": "ycombinator", "naukri.com": "naukri",
}


COUNTRY_HINTS = [  # (regex, country) — first match wins; cities map to their country
    (r"\b(india|bengaluru|bangalore|mumbai|delhi|gurgaon|gurugram|noida|hyderabad|pune|chennai|kolkata|ahmedabad|jaipur|kochi|indore)", "India"),
    (r"\b(united states|usa|u\.s\.|\bus\b|san francisco|new york|nyc|seattle|austin|boston|chicago|los angeles|denver|palo alto|mountain view|sunnyvale|remote \(us\)|, ca\b|, ny\b|, wa\b|, tx\b|, ma\b)", "United States"),
    (r"\b(united kingdom|\buk\b|london|manchester|edinburgh|cambridge|england|\bgb\b)", "United Kingdom"),
    (r"\b(germany|berlin|munich|münchen|hamburg|frankfurt|\bde\b)", "Germany"),
    (r"\b(netherlands|amsterdam|rotterdam|utrecht|\bnl\b)", "Netherlands"),
    (r"\b(canada|toronto|vancouver|montreal|ottawa|\bca\b)", "Canada"),
    (r"\b(singapore|\bsg\b)", "Singapore"), (r"\b(japan|tokyo|osaka|\bjp\b)", "Japan"), (r"\b(australia|sydney|melbourne|\bau\b)", "Australia"),
    (r"\b(ireland|dublin|\bie\b)", "Ireland"), (r"\b(france|paris|\bfr\b)", "France"), (r"\b(switzerland|zurich|zürich|\bch\b)", "Switzerland"),
    (r"\b(uae|dubai|abu dhabi|emirates)", "UAE"), (r"\b(spain|barcelona|madrid|\bes\b)", "Spain"), (r"\b(portugal|lisbon|\bpt\b)", "Portugal"),
    (r"\b(sweden|stockholm|\bse\b)", "Sweden"), (r"\b(poland|warsaw|\bpl\b)", "Poland"), (r"\b(israel|tel aviv)", "Israel"), (r"\b(brazil|são paulo|sao paulo)", "Brazil"),
    (r"\b(europe|emea|\beu\b)", "Europe (remote)"), (r"\b(worldwide|anywhere|global|remote)", "Remote (worldwide)"),
]


def country_of(location: str | None, remote_scope: str | None = None) -> str:
    text = f"{location or ''} {remote_scope or ''}".lower()
    for rx, c in COUNTRY_HINTS:
        if re.search(rx, text): return c
    return "Unknown"


def detect_ats(url: str | None) -> str | None:
    if not url:
        return None
    host = urlparse(url).netloc.lower()
    for dom, ats in ATS_DOMAINS.items():
        if host == dom or host.endswith("." + dom):
            return ats
    return None


def norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def dedupe_key(company: str, title: str, location: str | None) -> str:
    t = norm(title)
    # Seniority is part of the role identity.
    return hashlib.sha1(f"{norm(company)}|{t}|{norm(location)}".encode()).hexdigest()[:40]


def sponsorship_signal(description: str | None) -> bool:
    desc = (description or "").lower()
    if re.search(r"no (visa )?sponsorship|unable to sponsor|cannot sponsor|sponsorship\s*:?\s*(false|no|not available|not offered)", desc): return False
    return bool(re.search(r"visa sponsorship (available|provided|offered)|we (will |can )?sponsor|will sponsor|relocation (package|assistance|support)", desc))


def eligibility(title, location, remote_scope, description, sponsor_flag):
    f = config.load().get("filters", {})
    title = (title or "").lower()
    if not any(k in title for k in f.get("titles_include", [])): return False, "title not in include list"
    for k in f.get("titles_exclude", []):
        if re.search(r"(?<![a-z])" + re.escape(k.strip()) + r"(?![a-z])", title): return False, f"title excluded: {k}"
    loc = f"{location or ''} {remote_scope or ''}".lower()
    desc = (description or "").lower()
    if re.search(r"clearance required|us citizens only|u\.s\. citizens only|must be a us citizen", desc): return False, "citizenship or clearance restriction"
    country = country_of(location, remote_scope)
    if country == "India": return True, "India role"
    restricted = re.search(r"us.only|u\.s\..only|must be (located|based) in|must (be authorized|have the right) to work|unable to sponsor|cannot sponsor|no (visa )?sponsorship", loc + " " + desc)
    if restricted: return False, "location/work authorization restriction requires review"
    worldwide = re.search(r"\b(worldwide|anywhere|globally|global remote)\b", loc)
    india_remote = re.search(r"\b(india|apac|asia)\b", loc) and "remote" in loc
    if worldwide or india_remote: return True, "remote scope includes India"
    if sponsorship_signal(desc) or (sponsor_flag and not re.search(r"sponsorship\s*:?\s*(no|false)", desc)):
        return True, "explicit sponsorship/relocation signal"
    return False, "work location or sponsorship needs verification"


def canonical_url(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
    u = urlsplit(url or "")
    query = [(k,v) for k,v in parse_qsl(u.query) if not k.lower().startswith("utm_") and k.lower() not in ("source", "ref", "referrer", "trackingid")]
    return urlunsplit((u.scheme.lower(), u.netloc.lower(), u.path.rstrip('/'), urlencode(sorted(query)), ''))


def upsert(db, *, source: str, company: str, title: str, url: str, apply_url: str | None = None, location: str | None = None,
           remote_scope: str | None = None, employment_type: str | None = None, salary: str | None = None,
           posted_at: datetime | None = None, description: str | None = None, tags: list | None = None,
           company_domain: str | None = None, raw: dict | None = None) -> tuple[Job, bool]:
    key = hashlib.sha1(canonical_url(url).encode()).hexdigest() if url else dedupe_key(company, title, location)
    existing = db.query(Job).filter((Job.dedupe_key == key) | (Job.url == url)).first()
    if existing:
        longer = bool(description) and len(description) > len(existing.description or "")
        moved = (location and location != existing.location) or (remote_scope and remote_scope != existing.remote_scope)
        if longer: existing.description = description
        if apply_url: existing.apply_url = apply_url
        if location: existing.location = location
        if remote_scope: existing.remote_scope = remote_scope
        existing.country = country_of(existing.location, existing.remote_scope)
        sponsor = sponsorship_signal(existing.description)
        # Re-evaluate whenever the facts the rules depend on changed, not only when the text grew.
        if existing.status in ("new", "filtered", "scored") and (longer or moved or sponsor != bool(existing.sponsor_flag)):
            if longer: existing.fit_score = None; existing.fit_reasons = None
            existing.sponsor_flag = sponsor
            existing.eligible, existing.eligibility_reason = eligibility(existing.title, existing.location, existing.remote_scope, existing.description, sponsor)
            if existing.fit_score is None or not existing.eligible:
                existing.status = "new" if existing.eligible else "filtered"
        existing.updated_at = datetime.utcnow()
        return existing, False
    sponsor = sponsorship_signal(description)
    ok, reason = eligibility(title, location, remote_scope, description, sponsor)
    job = Job(dedupe_key=key, source=source, ats=detect_ats(apply_url or url), company=company.strip()[:200], company_domain=company_domain,
              title=title.strip()[:300], url=url, apply_url=apply_url or url, location=location, remote_scope=remote_scope,
              employment_type=employment_type, salary=salary, posted_at=posted_at, description=description, tags=tags,
              sponsor_flag=sponsor, eligible=ok, eligibility_reason=reason, status="new" if ok else "filtered", raw=raw, country=country_of(location, remote_scope))
    db.add(job)
    return job, True
