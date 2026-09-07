"""hiring.cafe (now hiringcafe.com) collector.

The site is a Next.js app behind Cloudflare. Its public /api/search-jobs endpoint
now answers 401/405, but the SSR data route
`/_next/data/<buildId>/index.json?searchState={...}` returns the full result set
as JSON and is reachable with plain httpx as long as we send browser-ish headers.
buildId is read from the homepage HTML on each run (it changes on deploys).

If Cloudflare ever challenges us (403 / "Just a moment"), we log it and stop -
we never try to solve a challenge.
"""
from __future__ import annotations
import json
import re
import time
from datetime import datetime

import httpx

from backend.core.collectors.base import ingest

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "en-US,en;q=0.9",
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
}
DOC_HEADERS = {"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
               "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none",
               "Upgrade-Insecure-Requests": "1"}
JSON_HEADERS = {"Accept": "application/json", "Referer": "https://hiringcafe.com/", "x-nextjs-data": "1",
                "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Site": "same-origin"}

QUERIES = ["forward deployed engineer", "applied AI engineer", "AI engineer", "LLM engineer", "full stack engineer"]


def _dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _build_id(c: httpx.Client, ctx) -> str | None:
    r = c.get("https://hiringcafe.com/", headers=DOC_HEADERS)
    if r.status_code != 200 or re.search(r"Just a moment|challenge-platform|security verification", r.text[:3000], re.I):
        ctx.log("warn", f"hiringcafe: Cloudflare challenge on homepage (status {r.status_code}); needs a real browser session", platform="hiringcafe")
        return None
    m = re.search(r'/_next/static/([^/"]+)/_buildManifest', r.text) or re.search(r'"buildId":"([^"]+)"', r.text)
    if not m:
        ctx.log("warn", "hiringcafe: could not read Next.js buildId from homepage", platform="hiringcafe")
        return None
    return m.group(1)


def _salary(pd: dict) -> str | None:
    lo, hi = pd.get("yearly_min_compensation"), pd.get("yearly_max_compensation")
    if not lo and not hi:
        return None
    cur = pd.get("listed_compensation_currency") or ""
    return f"{lo or ''}-{hi or ''} {cur}".strip()


def _description(pd: dict, ji: dict) -> str:
    bits = [pd.get("company_tagline"), pd.get("requirements_summary")]
    ra = pd.get("role_activities")
    if isinstance(ra, list):
        bits.append("\n".join(f"- {x}" for x in ra if isinstance(x, str)))
    elif isinstance(ra, str):
        bits.append(ra)
    for label, key in (("Seniority", "seniority_level"), ("Commitment", "commitment"),
                       ("Visa sponsorship", "visa_sponsorship"), ("Relocation assistance", "relocation_assistance")):
        if pd.get(key) not in (None, "", []):
            bits.append(f"{label}: {pd.get(key)}")
    return "\n\n".join(str(b) for b in bits if b)[:12000] or (ji.get("title") or "")


def _fetch(c: httpx.Client, ctx, build_id: str, query: str) -> list[dict]:
    url = f"https://hiringcafe.com/_next/data/{build_id}/index.json"
    state = {"searchQuery": query, "sortBy": "date"}
    for attempt in range(3):
        try:
            r = c.get(url, params={"searchState": json.dumps(state)}, headers=JSON_HEADERS)
        except Exception as e:  # noqa: BLE001
            ctx.bump("failed"); ctx.log("warn", f"hiringcafe '{query}': {str(e)[:140]}", platform="hiringcafe")
            return []
        if r.status_code == 200:
            try:
                return r.json().get("pageProps", {}).get("ssrHits", []) or []
            except Exception:
                return []
        if r.status_code in (403, 429, 503):
            time.sleep(5 * (attempt + 1))
            continue
        ctx.log("warn", f"hiringcafe '{query}': HTTP {r.status_code}", platform="hiringcafe")
        return []
    ctx.log("warn", f"hiringcafe '{query}': blocked (403/429) after retries", platform="hiringcafe")
    return []


def hiringcafe(ctx):
    items: list[dict] = []
    seen: set[str] = set()
    with httpx.Client(timeout=httpx.Timeout(90, connect=20), follow_redirects=True, headers=BASE_HEADERS) as c:
        build_id = _build_id(c, ctx)
        if not build_id:
            return ingest(ctx, "hiringcafe", [])
        for q in QUERIES:
            hits = _fetch(c, ctx, build_id, q)
            kept = 0
            for h in hits:
                pd = h.get("v5_processed_job_data") or {}
                ji = h.get("job_information") or {}
                url = h.get("apply_url") or ""
                key = h.get("id") or url
                title = ji.get("title") or pd.get("core_job_title") or ji.get("job_title_raw") or ""
                if not url or not title or key in seen:
                    continue
                seen.add(key)
                wt = pd.get("workplace_type")
                items.append(dict(
                    company=str(pd.get("company_name") or h.get("attributed_org") or "unknown")[:200],
                    title=str(title)[:250], url=url, apply_url=url,
                    location=(str(pd["formatted_workplace_location"]) if isinstance(pd.get("formatted_workplace_location"), str) else ", ".join(x for x in (pd.get("workplace_countries") or []) if isinstance(x, str))) or None,
                    remote_scope=("worldwide" if pd.get("is_workplace_worldwide_ok") else (str(wt).lower() if wt else None)),
                    employment_type=pd.get("commitment") if isinstance(pd.get("commitment"), str) else None,
                    salary=_salary(pd), posted_at=_dt(pd.get("estimated_publish_date")),
                    description=_description(pd, ji),
                    tags=[t for t in (pd.get("technical_tools") or []) if isinstance(t, str)][:20],
                    raw={"query": q, "id": h.get("id"), "source": h.get("source"),
                         "visa_sponsorship": pd.get("visa_sponsorship"), "relocation": pd.get("relocation_assistance"),
                         "workplace_type": wt},
                ))
                kept += 1
            ctx.log("info", f"hiringcafe '{q}': {len(hits)} hits, {kept} kept", platform="hiringcafe")
            time.sleep(3)
    # remote / visa-sponsoring roles first
    items.sort(key=lambda i: (0 if (i.get("remote_scope") or "").startswith(("remote", "worldwide")) else 1,
                              0 if i["raw"].get("visa_sponsorship") else 1))
    return ingest(ctx, "hiringcafe", items)


REGISTRY = {"hiringcafe": hiringcafe}
