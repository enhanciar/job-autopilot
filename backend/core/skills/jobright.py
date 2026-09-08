"""Jobright (jobright.ai): an AI job-matching site that keeps a personalised recommendation feed behind a login.

discover: read the recommendation feed (and the search results for the user's target roles) in the logged-in shared Chrome
profile, and ingest each posting. Jobright itself does not host the application form: every card links out to the employer's
ATS (Greenhouse / Lever / Ashby / Workday / company site), so apply_url is set to that external link and the ATS worker
handles the actual submission — the same hand-off Peerlist uses.
"""
from __future__ import annotations
import re
import time
from backend.core import browser, humanize
from backend.core.skills.base import BaseSkill
from backend.core.skills import platform_common as pc

FEEDS = ["https://jobright.ai/jobs/recommend"]
QUERIES = ["forward deployed engineer", "applied AI engineer", "AI engineer", "LLM engineer", "full stack engineer"]
CARD_SELECTORS = ["[class*='job-card']", "[class*='jobCard']", "div[data-jobid]", "li[class*='job']", "[class*='index_job']"]


class JobrightSkill(BaseSkill):
    platform = "jobright"
    needs_login = True

    def execute(self, page, mode: str = "discover", limit: int = 60, **_):
        if mode == "discover":
            self.discover(page, limit)

    def discover(self, page, limit):
        urls = FEEDS + [f"https://jobright.ai/jobs?searchKeyword={q.replace(' ', '%20')}" for q in QUERIES]
        new_total = 0
        for u in urls:
            if self.ctx.should_stop(): break
            if not self.take("searches", u): break
            page.goto(u, wait_until="domcontentloaded"); humanize.pause()
            page.wait_for_timeout(4000)
            self.guard(page)                       # Jobright shows a paid-plan popup over the results
            browser.dismiss_overlay(page, self.log)
            for _ in range(5):
                humanize.human_scroll(page, 1800); page.wait_for_timeout(1200)
            items = self._cards(page)
            if not items:
                self.log("warn", f"jobright: no cards parsed on {u}", screenshot=self.ctx.screenshot(page, "jobright_empty"))
                continue
            new_total += pc.upsert_many("jobright", items[:limit]); self.ctx.bump("seen", len(items))
            humanize.pause("between_items_s")
        self.ctx.bump("new", new_total)
        self.log("info", f"jobright: discovery done, {new_total} new jobs")

    def _cards(self, page):
        """Each card is the block wrapping an /jobs/info/<id> link. Jobright's class names are hashed, so read the card's
        own text: line order is <posted> / <badge?> / TITLE / COMPANY / <industry> / <location> / <type> / <salary> ..."""
        items, seen = [], set()
        links = page.locator("a[href*='/jobs/info/']")
        for i in range(min(links.count(), 120)):
            a = links.nth(i)
            try:
                href = (a.get_attribute("href") or "").split("?")[0]
                if not href or href in seen: continue
                seen.add(href)
                url = href if href.startswith("http") else "https://jobright.ai" + href
                card = a.locator("xpath=ancestor::*[contains(@class,'job-card')][1]")
                if not card.count(): card = a.locator("xpath=ancestor::*[position()<=6][1]")
                text = card.first.inner_text(timeout=2000)
                lines = [l.strip() for l in text.split("\n") if l.strip() and l.strip() != "/"]
                # drop the leading chrome: relative dates, "Early applicant", "N school alum", match badges
                skip = re.compile(r"^(reposted\s+)?\d+\s+(minute|hour|day|week|month)s?\s+ago$|^early applicant$|^\d+\s+school alum|^why this job|^recommended$|^promoted", re.I)
                body = [l for l in lines if not skip.match(l)]
                if len(body) < 2: continue
                title, company = body[0], body[1]
                loc = next((l for l in body[2:8] if re.search(r"remote|hybrid|onsite|on-?site|,\s*[A-Z]{2}\b|india|united|kingdom|germany|canada|singapore|europe", l, re.I)), "")
                salary = next((l for l in body[2:9] if re.search(r"\$|₹|€|/yr|per year|lpa", l, re.I)), "")
                items.append({"company": company[:150], "title": title[:250], "url": url, "apply_url": url,
                              "location": loc[:150], "remote_scope": loc[:150], "salary": salary[:80],
                              "description": "\n".join(body)[:6000], "raw": {"card": text[:800]}})
            except Exception:
                continue
        return items


SKILLS = {"jobright": JobrightSkill}
