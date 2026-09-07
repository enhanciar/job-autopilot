"""Extra public collectors: HiringCafe (public API), Relocate.me, Japan Dev, Reddit job subreddits (public JSON)."""
from __future__ import annotations
import re, html
from datetime import datetime
from backend.core.collectors.base import client, ingest

HEADERS = {"User-Agent": "Mozilla/5.0 (job-autopilot; personal use)"}


def hiringcafe(ctx):
    items = []
    with client() as c:
        for q in ["forward deployed engineer", "AI engineer LLM", "applied AI engineer", "solutions engineer AI"]:
            try:
                r = c.post("https://hiring.cafe/api/search-jobs", json={"size": 40, "page": 0, "searchState": {"searchQuery": q, "workplaceTypes": ["Remote", "Hybrid", "Onsite"], "sortBy": "date"}}, headers=HEADERS)
                r.raise_for_status()
                for it in r.json().get("results", []):
                    ji = it.get("job_information", {}); pd = it.get("v5_processed_job_data", {}) or {}
                    items.append(dict(company=pd.get("company_name") or ji.get("company") or "unknown", title=ji.get("title") or pd.get("core_job_title") or "", url=it.get("apply_url") or ji.get("url") or "",
                                      apply_url=it.get("apply_url"), location=pd.get("formatted_workplace_location") or ", ".join(pd.get("workplace_countries", []) or []), remote_scope=pd.get("workplace_type"),
                                      salary=(f"{pd.get('yearly_min_compensation')}-{pd.get('yearly_max_compensation')} {pd.get('listed_compensation_currency','')}" if pd.get("yearly_min_compensation") else None),
                                      description=re.sub(r"<[^>]+>", " ", html.unescape(ji.get("description") or ""))[:12000], tags=pd.get("technical_tools"), raw={"query": q, "visa": pd.get("visa_sponsorship")}))
            except Exception as e:  # noqa: BLE001
                ctx.bump("failed"); ctx.log("warn", f"hiringcafe '{q}': {e}", platform="hiringcafe")
    ingest(ctx, "hiringcafe", [i for i in items if i["url"] and i["title"]])


def relocateme(ctx):
    items = []
    with client() as c:
        for page in range(1, 4):
            try:
                r = c.get(f"https://relocate.me/api/v1/jobs?page={page}&per_page=50&category=software-engineering", headers=HEADERS)
                if r.status_code != 200: break
                for j in r.json().get("jobs", r.json() if isinstance(r.json(), list) else []):
                    items.append(dict(company=j.get("company", {}).get("name") if isinstance(j.get("company"), dict) else j.get("company_name", "unknown"), title=j.get("title", ""), url=j.get("url") or j.get("link", ""),
                                      location=f"{j.get('city','')}, {j.get('country','')}", remote_scope=j.get("remote"), description=(j.get("description") or "") + "\n\nRelocation package offered (Relocate.me).", raw=j))
            except Exception as e:  # noqa: BLE001
                ctx.bump("failed"); ctx.log("warn", f"relocateme p{page}: {e}", platform="relocateme"); break
    if not items:
        # HTML fallback
        try:
            with client() as c:
                r = c.get("https://relocate.me/search?query=software%20engineer", headers=HEADERS)
                for m in re.finditer(r'href="(/international-jobs/[^"]+)"[^>]*>([^<]{5,120})<', r.text):
                    items.append(dict(company="see posting", title=html.unescape(m.group(2)).strip(), url="https://relocate.me" + m.group(1), location="relocation abroad", description="Relocation package offered (Relocate.me)."))
        except Exception as e:  # noqa: BLE001
            ctx.bump("failed"); ctx.log("warn", f"relocateme html: {e}", platform="relocateme")
    ingest(ctx, "relocateme", [i for i in items if i["url"] and i["title"]])


def japandev(ctx):
    """HTML scrape of japan-dev.com/jobs (job links /jobs/<company>/<slug>)."""
    items = []
    try:
        with client() as c:
            r = c.get("https://japan-dev.com/jobs", headers=HEADERS); r.raise_for_status()
            links = sorted(set(re.findall(r'href="(/jobs/[^"/]+/[^"]+)"', r.text)))[:60]
            for path in links:
                try:
                    p = c.get("https://japan-dev.com" + path, headers=HEADERS)
                    txt = re.sub(r"<script.*?</script>|<style.*?</style>", " ", p.text, flags=re.S)
                    txt = html.unescape(re.sub(r"<[^>]+>", "\n", txt))
                    txt = re.sub(r"\n\s*\n+", "\n", txt)
                    title = (re.search(r"<h1[^>]*>(.*?)</h1>", p.text, re.S) or [None, path.split("/")[-1]])[1]
                    title = html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
                    company = path.split("/")[2].replace("-", " ").title()
                    remote = bool(re.search(r"remote", txt[:3000], re.I))
                    items.append(dict(company=company, title=title[:250], url="https://japan-dev.com" + path, location="Tokyo, Japan" if not remote else "Remote (Japan)",
                                      remote_scope="remote japan" if remote else "onsite japan", description=txt[:10000] + "\n\nSource: Japan Dev; sponsorship must be verified in the employer posting.",
                                      raw={"japanese_required": bool(re.search(r"japanese (required|fluent|business)", txt, re.I))}))
                except Exception as e:  # noqa: BLE001
                    ctx.log("warn", f"japandev {path}: {e}", platform="japandev")
    except Exception as e:  # noqa: BLE001
        ctx.bump("failed"); ctx.log("warn", f"japandev: {e}", platform="japandev")
    ingest(ctx, "japandev", [i for i in items if i["url"] and i["title"]])


def reddit(ctx):
    items = []
    subs = ["forhire", "remotejs", "MachineLearningJobs", "remotepython", "developersIndia"]
    with client() as c:
        for s in subs:
            try:
                r = c.get(f"https://www.reddit.com/r/{s}/search.json?q=%5BHiring%5D+OR+hiring&restrict_sr=1&sort=new&t=week&limit=50", headers=HEADERS)
                for ch in r.json().get("data", {}).get("children", []):
                    d = ch["data"]; t = d.get("title", "")
                    if not re.search(r"hiring", t, re.I) or re.search(r"\[for hire\]", t, re.I): continue
                    items.append(dict(company=(re.search(r"(?:at|@)\s+([A-Z][\w.&-]+)", t) or [None, "see post"])[1], title=re.sub(r"\[hiring\]\s*", "", t, flags=re.I)[:250], url="https://www.reddit.com" + d.get("permalink", ""),
                                      location="remote" if re.search(r"remote", t + d.get("selftext", ""), re.I) else "", remote_scope="remote" if re.search(r"remote", t, re.I) else None,
                                      description=d.get("selftext", "")[:8000], posted_at=datetime.utcfromtimestamp(d.get("created_utc", 0)), raw={"sub": s, "author": d.get("author")}))
            except Exception as e:  # noqa: BLE001
                ctx.log("warn", f"reddit r/{s}: {e}", platform="reddit")
    ingest(ctx, "reddit", items)


REGISTRY = {"hiringcafe": hiringcafe, "relocateme": relocateme, "japandev": japandev, "reddit": reddit}
