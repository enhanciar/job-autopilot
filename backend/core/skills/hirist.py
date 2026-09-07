"""Hirist: /jobfeed (personalised) + /k/<keyword>-jobs pages. Job pages /j/<slug>-<id> have an 'Apply' button (may open a questionnaire modal)."""
from __future__ import annotations
import re, time
from backend.core import humanize
from backend.core.skills.base import BaseSkill
from backend.core.skills import platform_common as pc

FEED = "https://www.hirist.tech/jobfeed"
KEYWORD_PAGES = ["https://www.hirist.tech/k/generative-ai-jobs", "https://www.hirist.tech/k/artificial-intelligence-jobs", "https://www.hirist.tech/k/python-jobs", "https://www.hirist.tech/k/backend-development-jobs"]


class HiristSkill(BaseSkill):
    platform = "hirist"

    def execute(self, page, mode: str = "discover", limit: int = 40, **_):
        if mode == "discover": self.discover(page, limit)
        elif mode == "apply": self.apply(page, limit)

    def _links(self, page) -> list[tuple[str, str]]:
        out, seen = [], set()
        loc = page.locator("a[href^='/j/'], a[href*='hirist.tech/j/']")
        for i in range(loc.count()):
            a = loc.nth(i)
            try:
                href = (a.get_attribute("href") or "").split("?")[0]
                if not href or href in seen: continue
                seen.add(href)
                out.append((href if href.startswith("http") else "https://www.hirist.tech" + href, a.inner_text(timeout=800).strip().split("\n")[0]))
            except Exception:
                continue
        return out

    def discover(self, page, limit):
        links = []
        for url in [FEED] + KEYWORD_PAGES:
            if self.ctx.should_stop() or len(links) >= limit * 2: break
            page.goto(url, wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
            for _ in range(2): humanize.human_scroll(page, 1500)
            links += self._links(page)
        items = []
        seen = set()
        for url, title in links:
            if url in seen: continue
            seen.add(url)
            if len(items) >= limit or self.ctx.should_stop(): break
            try:
                page.goto(url, wait_until="domcontentloaded"); humanize.pause("action_delay_s")
                h = page.locator("h1").first
                t = h.inner_text(timeout=2000).strip() if h.count() else title
                head = page.locator("h1").first.locator("xpath=..").inner_text(timeout=2000) if h.count() else ""
                parts = [p.strip() for p in re.split(r"[•|\n]", head) if p.strip()]
                company = parts[1] if len(parts) > 1 else "unknown"
                loc = next((p for p in parts if re.search(r"bangalore|bengaluru|mumbai|delhi|gurgaon|hyderabad|pune|chennai|remote|noida|kolkata|anywhere", p, re.I)), "")
                exp = next((p for p in parts if re.search(r"\d+\s*-\s*\d+\s*years", p, re.I)), "")
                body = page.inner_text("body", timeout=5000)
                items.append({"company": company[:150], "title": t[:250], "url": url, "apply_url": url, "location": loc, "remote_scope": loc,
                              "description": body[:10000], "raw": {"experience": exp}})
            except Exception as e:  # noqa: BLE001
                self.log("warn", f"hirist {url}: {e}")
        new = pc.upsert_many("hirist", items)
        self.ctx.bump("seen", len(items)); self.ctx.bump("new", new)
        self.log("info", f"hirist: discovery done, {len(items)} seen, {new} new")

    def apply(self, page, limit):
        for aid in pc.pending_apps("hirist", limit):
            if self.ctx.should_stop(): break
            if not self.take("applies", str(aid)): break
            info = pc.app_info(aid, self.ctx.run_id)
            page.goto(info["url"], wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
            if pc.confirmed(page, ("applied on", "already applied", "application sent")):
                pc.mark(aid, "submitted", None, None, "already applied"); continue
            if not pc.click_first(page, ["button:has-text('Apply'):visible"]):
                pc.mark(aid, "needs_human", "no Apply button", self.ctx.screenshot(page, f"hr{aid}")); continue
            un = pc.handle_questionnaire(self, page, info)
            if un:
                pc.mark(aid, "needs_human", "unanswered: " + " | ".join(un[:6]), self.ctx.screenshot(page, f"hr{aid}_q")); continue
            pc.click_first(page, ["button:has-text('Submit'):visible", "button:has-text('Apply Now'):visible", "button:has-text('Continue'):visible"])
            time.sleep(2.5)
            shot = self.ctx.screenshot(page, f"hr{aid}_after")
            ok = pc.confirmed(page, ("application sent", "application submitted", "successfully applied", "thank you for applying"))
            pc.mark(aid, "submitted" if ok else "needs_human", None if ok else "no confirmation detected", shot, "hirist apply clicked")
            self.log("info" if ok else "warn", f"hirist {'applied' if ok else 'unverified'}: {info['company']} — {info['title']}", screenshot=shot)
            self.ctx.bump("submitted" if ok else "unverified")
            humanize.pause("between_items_s")


SKILLS = {"hirist": HiristSkill}
