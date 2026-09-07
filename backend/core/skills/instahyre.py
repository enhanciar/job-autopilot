"""Instahyre: logged-in 'Opportunities' feed (matching=true). Cards: .employer-details, 'View job »' opens a modal with JD and an 'Apply' button.
Jobs have no public URL; we store the company page link and the card index and re-find the card by company+title when applying."""
from __future__ import annotations
import re, time
from backend.core import humanize
from backend.core.skills.base import BaseSkill
from backend.core.skills import platform_common as pc

FEED = "https://www.instahyre.com/candidate/opportunities/?matching=true"


class InstahyreSkill(BaseSkill):
    platform = "instahyre"

    def execute(self, page, mode: str = "discover", limit: int = 30, **_):
        page.goto(FEED, wait_until="domcontentloaded"); humanize.pause(); self._dismiss(page); self.guard(page)
        if mode == "discover": self.discover(page, limit)
        elif mode == "apply": self.apply(page, limit)

    def _dismiss(self, page):
        try: page.wait_for_selector(".employer-details", timeout=15000)
        except Exception: pass
        for sel in [".modal:visible button:has-text('Save')", ".modal:visible button:has-text('Maybe later')", ".modal:visible .close"]:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                try: loc.first.click(timeout=1500)
                except Exception: pass

    def _cards(self, page):
        return page.locator(".employer-details")

    def _open_modal(self, page, card) -> dict | None:
        btn = card.locator("button:has-text('View job')")
        if not btn.count(): return None
        humanize.human_click(page, btn.first); time.sleep(1.5)
        modal = page.locator(".application-modal-wrap").first
        try:
            modal.wait_for(state="visible", timeout=8000)
        except Exception:
            return None
        right = modal.locator(".right-section-modal").first
        text = (right.inner_text(timeout=4000) if right.count() else modal.inner_text(timeout=4000))
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        title = lines[0] if lines else ""
        company = lines[1] if len(lines) > 1 else ""
        loc = next((l for l in lines[:8] if re.search(r"bangalore|bengaluru|mumbai|delhi|gurgaon|hyderabad|pune|chennai|remote|noida", l, re.I)), "")
        link = modal.locator("a[href*='/jobs-at-']")
        url = link.first.get_attribute("href") if link.count() else None
        if url and url.startswith("/"): url = "https://www.instahyre.com" + url
        return {"title": title, "company": company, "location": loc, "description": text[:8000], "url": url or f"https://www.instahyre.com/candidate/opportunities/#{re.sub(r'[^a-z0-9]+','-',(company+'-'+title).lower())}", "modal": modal}

    def _close_modal(self, page):
        for sel in [".application-modal-close", ".application-modal-block .fa-close", ".application-modal-block .close"]:
            loc = page.locator(sel)
            if loc.count():
                try: loc.first.click(timeout=1500); break
                except Exception: pass
        time.sleep(0.8)
        if page.locator(".application-modal-wrap").count() and page.locator(".application-modal-wrap").first.is_visible():
            page.mouse.click(30, 400); time.sleep(0.8)   # backdrop click closes it

    def discover(self, page, limit):
        items, seen = [], 0
        for i in range(min(limit, self._cards(page).count())):
            if self.ctx.should_stop(): break
            card = self._cards(page).nth(i)
            try:
                card.scroll_into_view_if_needed(); humanize.pause("action_delay_s")
                info = self._open_modal(page, card)
                if info and info["title"]:
                    items.append({"company": info["company"], "title": info["title"], "url": info["url"], "apply_url": info["url"], "location": info["location"],
                                  "remote_scope": info["location"], "description": info["description"], "raw": {"card_index": i}})
                    seen += 1
                self._close_modal(page); self.guard(page)
            except Exception as e:  # noqa: BLE001
                self.log("warn", f"instahyre card {i}: {e}"); self._close_modal(page)
        new = pc.upsert_many("instahyre", items)
        self.ctx.bump("seen", seen); self.ctx.bump("new", new)
        self.log("info", f"instahyre: discovery done, {seen} seen, {new} new")

    def apply(self, page, limit):
        for aid in pc.pending_apps("instahyre", limit):
            if self.ctx.should_stop(): break
            if not self.take("applies", str(aid)): break
            info = pc.app_info(aid, self.ctx.run_id)
            target = None
            cards = self._cards(page)
            for i in range(cards.count()):
                t = cards.nth(i).inner_text(timeout=2000).lower()
                if info["title"].lower()[:25] in t or (info["company"].lower()[:12] in t and info["title"].lower()[:12] in t):
                    target = cards.nth(i); break
            if target is None:
                pc.mark(aid, "failed", "card no longer in Instahyre feed"); self.ctx.bump("failed"); continue
            m = self._open_modal(page, target)
            if not m:
                pc.mark(aid, "needs_human", "could not open job modal", self.ctx.screenshot(page, f"ih{aid}")); continue
            if not pc.click_first(page, [".application-modal-wrap button:has-text('Apply')", ".application-modal-wrap .new-btn"]):
                pc.mark(aid, "needs_human", "no Apply button in modal", self.ctx.screenshot(page, f"ih{aid}")); self._close_modal(page); continue
            un = pc.handle_questionnaire(self, page, info)
            if un:
                pc.mark(aid, "needs_human", "unanswered: " + " | ".join(un[:6]), self.ctx.screenshot(page, f"ih{aid}_q")); self._close_modal(page); continue
            pc.click_first(page, [".application-modal-wrap button:has-text('Submit')", ".application-modal-wrap button:has-text('Confirm')"])
            time.sleep(2)
            shot = self.ctx.screenshot(page, f"ih{aid}_after")
            ok = pc.confirmed(page)
            pc.mark(aid, "submitted" if ok else "needs_human", None if ok else "no confirmation detected", shot, "instahyre apply clicked")
            self.log("info" if ok else "warn", f"instahyre {'applied' if ok else 'unverified'}: {info['company']} — {info['title']}", screenshot=shot)
            self.ctx.bump("submitted" if ok else "unverified")
            self._close_modal(page); humanize.pause("between_items_s"); self.guard(page)


SKILLS = {"instahyre": InstahyreSkill}
