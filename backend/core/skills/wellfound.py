"""Wellfound: /jobs (personalised) and /role/r/<slug>. Job pages /jobs/<id>-<slug> have 'Apply' -> modal with optional company
questions (textarea name customQuestionAnswers[...]) e.g. 'What interests you about working for this company?' then 'Send application'."""
from __future__ import annotations
import re, time
from backend.core import humanize
from backend.core.skills.base import BaseSkill
from backend.core.skills import platform_common as pc

LISTS = ["https://wellfound.com/jobs", "https://wellfound.com/role/r/software-engineer", "https://wellfound.com/role/r/machine-learning-engineer"]


class WellfoundSkill(BaseSkill):
    platform = "wellfound"

    def execute(self, page, mode: str = "discover", limit: int = 40, **_):
        if mode == "discover": self.discover(page, limit)
        elif mode == "apply": self.apply(page, limit)

    def discover(self, page, limit):
        urls, seen = [], set()
        for lst in LISTS:
            if self.ctx.should_stop(): break
            page.goto(lst, wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
            for _ in range(4): humanize.human_scroll(page, 2000)
            for a in page.locator("a[href*='/jobs/']").all():
                h = (a.get_attribute("href") or "").split("?")[0]
                if re.search(r"/jobs/\d+-", h) and h not in seen:
                    seen.add(h); urls.append(h if h.startswith("http") else "https://wellfound.com" + h)
        items = []
        for url in urls[:limit]:
            if self.ctx.should_stop(): break
            try:
                page.goto(url, wait_until="domcontentloaded"); humanize.pause("action_delay_s")
                h1 = page.locator("h1").first.inner_text(timeout=3000).strip()
                m = re.match(r"(.+?) at (.+)$", h1)
                title, company = (m.group(1), m.group(2)) if m else (h1, "unknown")
                body = page.inner_text("body", timeout=5000)
                lines = body.split("\n")
                loc = next((l for l in lines if re.search(r"remote|everywhere|india|bangalore|mumbai|delhi", l, re.I) and len(l) < 80), "")
                salary = next((l for l in lines if re.search(r"[$₹€£]\s?\d", l) and len(l) < 60), "")
                visa = next((lines[i + 1] for i, l in enumerate(lines) if l.strip().lower() == "visa sponsorship" and i + 1 < len(lines)), "")
                items.append({"company": company[:150], "title": title[:250], "url": url, "apply_url": url, "location": loc, "remote_scope": loc, "salary": salary,
                              "description": body[:10000] + (f"\n\nVisa sponsorship: {visa}" if visa else ""), "raw": {"visa": visa}})
            except Exception as e:  # noqa: BLE001
                self.log("warn", f"wellfound {url}: {e}")
        new = pc.upsert_many("wellfound", items)
        self.ctx.bump("seen", len(items)); self.ctx.bump("new", new)
        self.log("info", f"wellfound: discovery done, {len(items)} seen, {new} new")

    def apply(self, page, limit):
        for aid in pc.pending_apps("wellfound", limit):
            if self.ctx.should_stop(): break
            if not self.take("applies", str(aid)): break
            info = pc.app_info(aid, self.ctx.run_id)
            page.goto(info["url"], wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
            if page.locator("button:has-text('Applied')").count():
                pc.mark(aid, "submitted", None, None, "already applied"); continue
            if not pc.click_first(page, ["button:has-text('Apply')"]):
                pc.mark(aid, "needs_human", "no Apply button", self.ctx.screenshot(page, f"wf{aid}")); continue
            un = pc.handle_questionnaire(self, page, info)
            if un:
                pc.mark(aid, "needs_human", "unanswered: " + " | ".join(un[:6]), self.ctx.screenshot(page, f"wf{aid}_q")); continue
            shot = self.ctx.screenshot(page, f"wf{aid}_before")
            if not pc.click_first(page, ["button:has-text('Send application'):enabled", "button:has-text('Submit application'):enabled"]):
                pc.mark(aid, "needs_human", "Send application button missing or disabled", shot); continue
            time.sleep(3)
            still_open = page.locator("button:has-text('Send application')").count() > 0
            ok = pc.confirmed(page)
            shot2 = self.ctx.screenshot(page, f"wf{aid}_after")
            pc.mark(aid, "submitted" if ok else "needs_human", None if ok else "dialog still open after send", shot2, "wellfound apply")
            self.log("info" if ok else "warn", f"wellfound {'applied' if ok else 'unverified'}: {info['company']} — {info['title']}", screenshot=shot2)
            self.ctx.bump("submitted" if ok else "unverified")
            humanize.pause("between_items_s")


SKILLS = {"wellfound": WellfoundSkill}
