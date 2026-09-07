"""Relocate.me collector: relocation/visa-sponsorship jobs abroad.

The board at https://relocate.me/search is JS-rendered, so we drive it with a
headless Chrome (Playwright) and read the `.jobs-list__job` cards. Job links look
like /<country>/<city>/<company>/<slug>-<id>, which gives us the destination
country for `location`.
"""
from __future__ import annotations
import re
import time

from playwright.sync_api import sync_playwright

from backend.core.collectors.base import ingest

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
SEARCH_URL = "https://relocate.me/search"
MAX_PAGES = 8
TECH_RE = re.compile(r"engineer|developer|programmer|data scien|machine learning|\bai\b|\bml\b|llm|devops|sre|architect|tech lead|cto|full[- ]?stack|backend|frontend|qa|analytics", re.I)


def _title_case(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.replace("-", " ").split())


def relocateme(ctx):
    items: list[dict] = []
    seen: set[str] = set()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, channel="chrome")
            page = browser.new_context(user_agent=UA, locale="en-US").new_page()
            url = SEARCH_URL
            for page_no in range(1, MAX_PAGES + 1):
                try:
                    if page_no == 1:
                        page.goto(url, wait_until="networkidle", timeout=90000)
                    page.wait_for_timeout(2500)
                    body = page.inner_text("body")[:400]
                    if re.search(r"just a moment|verify you are human|security verification|captcha", body, re.I):
                        ctx.log("warn", "relocateme: bot challenge shown; needs a real browser session", platform="relocateme")
                        break
                    cards = page.query_selector_all(".jobs-list__job")
                    for card in cards:
                        a = card.query_selector("a[href]") or card
                        href = a.get_attribute("href") or ""
                        if not href.startswith("/") or href.count("/") < 4:
                            continue
                        job_url = "https://relocate.me" + href
                        if job_url in seen:
                            continue
                        parts = [x for x in href.split("/") if x]
                        country, city, company_slug = parts[0], parts[1], parts[2]
                        te = card.query_selector(".job__title")
                        ces = card.query_selector_all(".job__company")   # [country, company]
                        pe = card.query_selector(".job__preview")
                        title = (te.inner_text() if te else _title_case(parts[3])).strip()[:250]
                        if not TECH_RE.search(title):
                            continue
                        seen.add(job_url)
                        company = (ces[-1].inner_text().strip()[:150] if len(ces) > 1 else "") or _title_case(company_slug)
                        remote = country.lower() == "remote" or city.lower() == "remote"
                        loc = "Remote" if remote else f"{_title_case(city)}, {_title_case(country)}"
                        desc = (pe.inner_text().strip() if pe else "")
                        items.append(dict(
                            company=company, title=title, url=job_url, apply_url=job_url,
                            location=loc, remote_scope="remote" if remote else _title_case(country),
                            salary=None,
                            description=(desc + "\n\nSource: Relocate.me; sponsorship must be verified in the employer posting.").strip()[:12000],
                            tags=["relocation", _title_case(country)],
                            raw={"country": country, "city": city, "company_slug": company_slug, "source_url": url},
                        ))
                    nxt = page.query_selector("li.next-page:not(.disabled) a, li.next-page:not(.unavailable) a")
                    if not nxt:
                        break
                    nxt.click()
                    page.wait_for_load_state("networkidle", timeout=60000)
                except Exception as e:  # noqa: BLE001
                    ctx.log("warn", f"relocateme page {page_no}: {str(e)[:160]}", platform="relocateme")
                    break
                time.sleep(2)
            browser.close()
    except Exception as e:  # noqa: BLE001
        ctx.bump("failed"); ctx.log("warn", f"relocateme: browser failed: {str(e)[:200]}", platform="relocateme")
    return ingest(ctx, "relocateme", items)


REGISTRY = {"relocateme": relocateme}
