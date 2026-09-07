"""Poll Greenhouse / Lever / Ashby public boards for seed companies from config.yaml."""
from __future__ import annotations
from datetime import datetime
from bs4 import BeautifulSoup
from backend.core import config
from backend.core.collectors.base import client, ingest


def _txt(html):
    return BeautifulSoup(html or "", "lxml").get_text("\n", strip=True)[:20000] if html else None


def greenhouse(ctx):
    items = []
    with client() as c:
        for token in config.load().get("seed_companies", {}).get("greenhouse", []):
            r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
            if r.status_code != 200:
                ctx.bump("boards_missing"); continue
            for j in r.json().get("jobs", []):
                loc = (j.get("location") or {}).get("name")
                items.append(dict(company=token, title=j["title"], url=j["absolute_url"], apply_url=j["absolute_url"], location=loc, remote_scope=loc,
                                  posted_at=_iso(j.get("updated_at")), description=_txt(j.get("content")), tags=[d.get("name") for d in j.get("departments", []) if d.get("name")], raw={"gh_id": j.get("id")}))
    return ingest(ctx, "greenhouse", items)


def lever(ctx):
    items = []
    with client() as c:
        for token in config.load().get("seed_companies", {}).get("lever", []):
            r = c.get(f"https://api.lever.co/v0/postings/{token}?mode=json")
            if r.status_code != 200:
                ctx.bump("boards_missing"); continue
            for j in r.json():
                cat = j.get("categories") or {}
                loc = cat.get("location") or cat.get("allLocations", [None])[0]
                items.append(dict(company=token, title=j["text"], url=j["hostedUrl"], apply_url=j.get("applyUrl") or j["hostedUrl"], location=loc, remote_scope=(cat.get("workplaceType") or loc),
                                  employment_type=cat.get("commitment"), posted_at=datetime.utcfromtimestamp(j["createdAt"] / 1000) if j.get("createdAt") else None,
                                  description=_txt(j.get("descriptionBody") or j.get("description")), tags=[cat.get("team")] if cat.get("team") else None, raw={"lever_id": j.get("id")}))
    return ingest(ctx, "lever", items)


def ashby(ctx):
    items = []
    with client() as c:
        for token in config.load().get("seed_companies", {}).get("ashby", []):
            r = c.get(f"https://api.ashbyhq.com/posting-api/job-board/{token}?includeCompensation=true")
            if r.status_code != 200:
                ctx.bump("boards_missing"); continue
            for j in r.json().get("jobs", []):
                loc = j.get("location"); remote = "remote" if j.get("isRemote") else loc
                comp = (j.get("compensation") or {}).get("compensationTierSummary")
                items.append(dict(company=token, title=j["title"], url=j["jobUrl"], apply_url=j.get("applyUrl") or j["jobUrl"], location=loc, remote_scope=remote,
                                  employment_type=j.get("employmentType"), salary=comp, posted_at=_iso(j.get("publishedAt")), description=_txt(j.get("descriptionHtml")) or j.get("descriptionPlain"),
                                  tags=[j.get("department"), j.get("team")], raw={"ashby_id": j.get("id")}))
    return ingest(ctx, "ashby", items)


def _iso(v):
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).replace(tzinfo=None) if v else None
    except Exception:
        return None


REGISTRY = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}
