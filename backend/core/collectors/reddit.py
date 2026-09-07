"""Reddit hiring-post collector (public, no API key).

Reddit blocks the plain .json endpoints from most non-logged-in clients (403),
so we try .json first and fall back to the public Atom feeds (/new/.rss and
/search.rss), which are still served anonymously but are aggressively rate
limited -> polite delays + backoff on 429/403.
"""
from __future__ import annotations
import html
import re
import time
from datetime import datetime

import feedparser
from bs4 import BeautifulSoup

from backend.core.collectors.base import ingest

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

SUBS = ["forhire", "hiring", "remotejobs", "remotework", "jobbit", "WorkOnline", "freelance",
        "digitalnomad", "webdev", "learnprogramming", "datascience", "Upwork", "careerguidance"]

HIRING_RE = re.compile(r"\[\s*hiring\s*\]|\bhiring\b|we(?:'| a)?re looking for|looking to hire|seeking a", re.I)
FORHIRE_RE = re.compile(r"\[\s*for\s*hire\s*\]|\[\s*task\s*\]|\bfor hire\b", re.I)
TECH_RE = re.compile(r"\b(engineer|developer|dev|python|ai|ml|llm|backend|back[- ]end|front[- ]end|full[- ]?stack|react|node|django|fastapi|software|programmer|data scien)\w*", re.I)
LOC_RE = re.compile(r"\b(remote(?:\s*\(?[a-z ,/]{0,25}\)?)?|hybrid|on-?site|worldwide|anywhere|us[- ]only|usa|united states|europe|emea|uk|canada|india|germany|apac)\b", re.I)
COMPANY_RE = re.compile(r"(?:company|employer|company name)\s*[:\-]\s*([A-Z][\w&.\-]{2,30}(?:\s[A-Z][\w&.\-]{1,20}){0,2})", re.I)
AT_RE = re.compile(r"(?:\bat|@)\s+([A-Z][\w.&'\-]{2,30}(?:\s[A-Z][\w.&'\-]{2,30})?)")  # case-sensitive on purpose: proper nouns


def _text(v: str | None) -> str:
    if not v:
        return ""
    return BeautifulSoup(html.unescape(v), "lxml").get_text("\n", strip=True)


def _dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def _keep(title: str, body: str, flair: str = "") -> bool:
    head = f"{title} {flair}"
    if FORHIRE_RE.search(head):
        return False
    if not HIRING_RE.search(head):
        return False
    return bool(TECH_RE.search(f"{title}\n{body}"))


def _company(title: str, body: str, sub: str, author: str) -> str:
    for rx in (COMPANY_RE, AT_RE):
        m = rx.search(body[:2000]) or rx.search(title)
        if m:
            c = m.group(1).strip(" .,-")
            if 2 < len(c) < 45:
                return c
    return f"r/{sub} ({author or 'unknown'})"


def _item(title, body, permalink, sub, author, posted):
    loc = LOC_RE.search(body[:3000]) or LOC_RE.search(title)
    loc = loc.group(0).strip() if loc else ""
    clean = re.sub(r"^\s*\[[^\]]{0,20}\]\s*", "", title).strip()[:250] or title[:250]
    url = permalink if permalink.startswith("http") else "https://www.reddit.com" + permalink
    return dict(company=_company(title, body, sub, author), title=clean, url=url, apply_url=url,
                location=loc or None, remote_scope=("remote" if re.search(r"remote", f"{title} {body[:3000]}", re.I) else None),
                description=body[:12000] or title, salary=None, posted_at=posted,
                tags=[f"r/{sub}"], raw={"subreddit": sub, "author": author, "permalink": url})


def _from_json(c, ctx, sub, items, seen):
    got = 0
    for url in (f"https://www.reddit.com/r/{sub}/new.json?limit=100&raw_json=1",
                f"https://www.reddit.com/r/{sub}/search.json?q=hiring&restrict_sr=1&sort=new&t=month&limit=100&raw_json=1"):
        r = c.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=30, follow_redirects=True)
        if r.status_code in (403, 429, 404):
            ctx.log("warn", f"reddit r/{sub}: json {r.status_code}, falling back to rss", platform="reddit")
            return 0
        try:
            data = r.json()
        except Exception:
            return 0
        for ch in data.get("data", {}).get("children", []):
            d = ch.get("data", {})
            title, body = d.get("title", ""), d.get("selftext", "")
            flair = d.get("link_flair_text") or ""
            perma = d.get("permalink", "")
            if not perma or perma in seen or not _keep(title, body, flair):
                continue
            seen.add(perma)
            items.append(_item(title, body, perma, sub, d.get("author", ""),
                               datetime.utcfromtimestamp(d.get("created_utc") or 0) if d.get("created_utc") else None))
            got += 1
        time.sleep(2)
    return got


def _from_rss(c, ctx, sub, items, seen):
    got = 0
    feeds = [f"https://www.reddit.com/r/{sub}/new/.rss?limit=100",
             f"https://www.reddit.com/r/{sub}/search.rss?q=hiring&restrict_sr=1&sort=new&t=month&limit=100"]
    for idx, url in enumerate(feeds):
        if idx and got:
            break            # /new already gave us posts; skip the (rate-limited) search feed
        feed = None
        for attempt in range(2):
            try:
                r = c.get(url, headers={"User-Agent": UA, "Accept": "application/atom+xml, application/xml"}, timeout=30, follow_redirects=True)
            except Exception as e:  # noqa: BLE001
                ctx.log("warn", f"reddit r/{sub}: {str(e)[:120]}", platform="reddit")
                return got
            if r.status_code == 200:
                feed = feedparser.parse(r.text)
                break
            if r.status_code in (403, 429):
                time.sleep(5 * (attempt + 1))
                continue
            break
        if feed is None:
            ctx.log("warn", f"reddit r/{sub}: rss blocked (403/429) after retries, skipping", platform="reddit")
            time.sleep(3)
            continue
        for e in feed.entries:
            title = html.unescape(e.get("title", ""))
            body = _text(e.get("summary") or (e.get("content", [{}])[0] or {}).get("value"))
            link = e.get("link", "")
            if not link or link in seen or not _keep(title, body):
                continue
            seen.add(link)
            author = (e.get("author") or "").lstrip("/u/")
            items.append(_item(title, body, link, sub, author, _dt(e.get("updated") or e.get("published"))))
            got += 1
        time.sleep(4)
    return got


def reddit(ctx):
    """Harvest employer hiring posts from job-ish subreddits. Excludes [For Hire] self-promo."""
    import httpx

    items: list[dict] = []
    seen: set[str] = set()
    with httpx.Client(follow_redirects=True) as c:
        for sub in SUBS:
            try:
                n = _from_json(c, ctx, sub, items, seen)
                if n == 0:
                    n = _from_rss(c, ctx, sub, items, seen)
                ctx.log("info", f"reddit r/{sub}: {n} hiring posts", platform="reddit")
            except Exception as e:  # noqa: BLE001
                ctx.bump("failed"); ctx.log("warn", f"reddit r/{sub}: {str(e)[:160]}", platform="reddit")
            time.sleep(2)
    return ingest(ctx, "reddit", [i for i in items if i["url"] and i["title"]])


REGISTRY = {"reddit": reddit}
