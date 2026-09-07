"""Peerlist: /jobs feed; job links /company/<c>/careers/<slug>/<id>. Job page has 'Apply' (Peerlist profile-based, sometimes a note)."""
from __future__ import annotations
import re, time
from backend.core import humanize
from backend.core.skills.base import BaseSkill
from backend.core.skills import platform_common as pc

LISTS = ["https://peerlist.io/jobs", "https://peerlist.io/jobs/recommended", "https://peerlist.io/jobs?q=AI%20engineer", "https://peerlist.io/jobs?q=LLM"]


class PeerlistSkill(BaseSkill):
    platform = "peerlist"

    def execute(self, page, mode: str = "discover", limit: int = 30, **_):
        if mode == "discover": self.discover(page, limit)
        elif mode == "apply": self.apply(page, limit)

    def discover(self, page, limit):
        urls, seen = [], set()
        for lst in LISTS:
            if self.ctx.should_stop(): break
            page.goto(lst, wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
            for _ in range(4): humanize.human_scroll(page, 2000)
            for a in page.locator("a[href*='/careers/']").all():
                h = (a.get_attribute("href") or "").split("?")[0]
                if "/careers/" in h and h not in seen:
                    seen.add(h); urls.append(h if h.startswith("http") else "https://peerlist.io" + h)
        items = []
        for url in urls[:limit]:
            if self.ctx.should_stop(): break
            try:
                page.goto(url, wait_until="domcontentloaded"); humanize.pause("action_delay_s")
                title = page.locator("h1").first.inner_text(timeout=3000).strip()
                m = re.search(r"/company/([^/]+)/careers/", url)
                company = m.group(1) if m else "unknown"
                body = page.inner_text("body", timeout=5000)
                loc = next((l for l in body.split("\n") if re.search(r"remote|india|bangalore|mumbai|delhi|hyderabad|pune|united states|europe", l, re.I) and len(l) < 80), "")
                items.append({"company": company[:150], "title": title[:250], "url": url, "apply_url": url, "location": loc, "remote_scope": loc, "description": body[:10000]})
            except Exception as e:  # noqa: BLE001
                self.log("warn", f"peerlist {url}: {e}")
        new = pc.upsert_many("peerlist", items)
        self.ctx.bump("seen", len(items)); self.ctx.bump("new", new)
        self.log("info", f"peerlist: discovery done, {len(items)} seen, {new} new")

    def apply(self, page, limit):
        for aid in pc.pending_apps("peerlist", limit):
            if self.ctx.should_stop(): break
            if not self.take("applies", str(aid)): break
            info = pc.app_info(aid, self.ctx.run_id)
            page.goto(info["url"], wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
            if page.locator("button:has-text('Applied')").count():
                pc.mark(aid, "submitted", None, None, "already applied"); continue
            ext = page.locator("a:has-text('Apply')[href^='http']:not([href*='peerlist.io'])")
            if ext.count():
                href = ext.first.get_attribute("href")
                from backend.app.db import session as _s; from backend.app.models import Application as _A
                with _s() as db:
                    a = db.get(_A, aid); a.job.apply_url = href; a.platform = "web"; a.method = "ats_form"; a.status = "approved"
                self.log("info", f"peerlist: external apply -> handed to ATS worker: {href[:80]}"); self.ctx.bump("handed_off"); continue
            if page.get_by_text("You can't interact yet", exact=False).count():
                pc.mark(aid, "needs_human", "Peerlist profile below 40% completion; complete Basic Details first", self.ctx.screenshot(page, f"pl{aid}")); continue
            if not pc.click_first(page, ["button:has-text('Apply')", "a:has-text('Apply')"]):
                pc.mark(aid, "needs_human", "no Apply button", self.ctx.screenshot(page, f"pl{aid}")); continue
            un = pc.handle_questionnaire(self, page, info)
            ta = page.locator("[role='dialog'] textarea, .modal textarea").first
            if ta.count() and not ta.input_value(): humanize.human_type(page, ta, (info["cover"] or "")[:1200])
            if un:
                pc.mark(aid, "needs_human", "unanswered: " + " | ".join(un[:6]), self.ctx.screenshot(page, f"pl{aid}_q")); continue
            pc.click_first(page, ["[role='dialog'] button:has-text('Submit')", "[role='dialog'] button:has-text('Apply')", "button:has-text('Send')"])
            time.sleep(2.5)
            ok = pc.confirmed(page)
            shot = self.ctx.screenshot(page, f"pl{aid}_after")
            pc.mark(aid, "submitted" if ok else "needs_human", None if ok else "not confirmed", shot, "peerlist apply")
            self.ctx.bump("submitted" if ok else "unverified")
            humanize.pause("between_items_s")


SKILLS = {"peerlist": PeerlistSkill}
