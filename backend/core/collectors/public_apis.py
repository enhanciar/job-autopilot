"""Collectors for sources with public JSON/RSS. Each: run(ctx) -> int new jobs."""
from __future__ import annotations
from datetime import datetime
import feedparser
from bs4 import BeautifulSoup
from backend.core.collectors.base import client, ingest


def _dt(v) -> datetime | None:
    if not v:
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.utcfromtimestamp(v)
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _text(html: str | None) -> str | None:
    if not html:
        return None
    return BeautifulSoup(html, "lxml").get_text("\n", strip=True)[:20000]


def remoteok(ctx):
    with client() as c:
        data = c.get("https://remoteok.com/api").json()
    items = []
    for j in data:
        if not isinstance(j, dict) or "position" not in j:
            continue
        items.append(dict(company=j.get("company") or "?", title=j["position"], url=j.get("url"), apply_url=j.get("apply_url") or j.get("url"),
                          location=j.get("location") or "Remote", remote_scope=j.get("location") or "worldwide", salary=(f"{j.get('salary_min')}-{j.get('salary_max')}" if j.get("salary_min") else None),
                          posted_at=_dt(j.get("epoch")), description=_text(j.get("description")), tags=j.get("tags"), raw={"id": j.get("id")}))
    return ingest(ctx, "remoteok", items)


def remotive(ctx):
    with client() as c:
        data = c.get("https://remotive.com/api/remote-jobs?category=software-dev&limit=300").json()
    items = [dict(company=j["company_name"], title=j["title"], url=j["url"], location=j.get("candidate_required_location") or "Remote",
                  remote_scope=j.get("candidate_required_location") or "worldwide", employment_type=j.get("job_type"), salary=j.get("salary") or None,
                  posted_at=_dt(j.get("publication_date")), description=_text(j.get("description")), tags=j.get("tags"), raw={"id": j.get("id")})
             for j in data.get("jobs", [])]
    return ingest(ctx, "remotive", items)


def himalayas(ctx):
    items = []
    with client() as c:
        for offset in (0, 100, 200):
            r = c.get(f"https://himalayas.app/jobs/api?limit=100&offset={offset}")
            if r.status_code != 200:
                break
            for j in r.json().get("jobs", []):
                loc = ", ".join(j.get("locationRestrictions") or []) or "Worldwide"
                items.append(dict(company=j.get("companyName") or "?", title=j.get("title"), url=j.get("applicationLink") or j.get("guid"), apply_url=j.get("applicationLink"),
                                  location=loc, remote_scope=loc, employment_type=j.get("employmentType"), salary=(f"{j.get('minSalary')}-{j.get('maxSalary')} {j.get('salaryCurrency','')}" if j.get("minSalary") else None),
                                  posted_at=_dt(j.get("pubDate")), description=_text(j.get("description")), tags=j.get("categories"), raw={"guid": j.get("guid")}))
    return ingest(ctx, "himalayas", items)


def arbeitnow(ctx):
    items = []
    with client() as c:
        for page in (1, 2, 3):
            data = c.get(f"https://www.arbeitnow.com/api/job-board-api?page={page}").json()
            for j in data.get("data", []):
                items.append(dict(company=j["company_name"], title=j["title"], url=j["url"], location=j.get("location"), remote_scope="remote" if j.get("remote") else j.get("location"),
                                  posted_at=_dt(j.get("created_at")), description=_text(j.get("description")), tags=j.get("tags"), raw={"slug": j.get("slug")}))
    return ingest(ctx, "arbeitnow", items)


def weworkremotely(ctx):
    feeds = ["https://weworkremotely.com/categories/remote-programming-jobs.rss", "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
             "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss", "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss"]
    items = []
    for f in feeds:
        d = feedparser.parse(f)
        for e in d.entries:
            title = e.get("title", "")
            company, _, role = title.partition(":")
            items.append(dict(company=company.strip() or "?", title=(role or title).strip(), url=e.get("link"), location=e.get("region") or "Remote", remote_scope=e.get("region") or "worldwide",
                              posted_at=_dt(e.get("published")) if isinstance(e.get("published"), str) and "T" in e.get("published") else None, description=_text(e.get("summary")), raw={"id": e.get("id")}))
    return ingest(ctx, "weworkremotely", items)


def hackernews(ctx):
    """Latest 'Ask HN: Who is hiring?' thread via Algolia; each top-level comment is a posting."""
    with client() as c:
        s = c.get("https://hn.algolia.com/api/v1/search_by_date", params={"query": "Ask HN: Who is hiring?", "tags": "story,author_whoishiring", "hitsPerPage": 1}).json()
        if not s.get("hits"):
            return 0
        story_id = s["hits"][0]["objectID"]
        month = s["hits"][0]["title"]
        items = []
        for page in range(0, 6):
            r = c.get("https://hn.algolia.com/api/v1/search", params={"tags": f"comment,story_{story_id}", "hitsPerPage": 200, "page": page}).json()
            for h in r.get("hits", []):
                if h.get("parent_id") != int(story_id):
                    continue
                text = _text(h.get("comment_text")) or ""
                first = text.split("\n")[0][:200]
                company = first.split("|")[0].strip() or "HN poster"
                title = (first.split("|")[1].strip() if "|" in first else "Engineer (see post)")[:200]
                loc = "remote" if "remote" in text.lower() else first
                items.append(dict(company=company, title=title, url=f"https://news.ycombinator.com/item?id={h['objectID']}", location=loc, remote_scope=loc,
                                  posted_at=_dt(h.get("created_at")), description=text, tags=[month], raw={"author": h.get("author")}))
            if len(r.get("hits", [])) < 200:
                break
    return ingest(ctx, "hackernews", items)


REGISTRY = {"remoteok": remoteok, "remotive": remotive, "himalayas": himalayas, "arbeitnow": arbeitnow, "hackernews": hackernews, "weworkremotely": weworkremotely}
