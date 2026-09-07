"""YC Work at a Startup: directory /companies?role=eng&... lists jobs (a[href*='/jobs/<id>']). Job page has 'Apply' -> modal
'Reach out to <founder> at <company>' with a textarea (min 50 chars) and 'Send'. The message IS the application, so we send the cover note."""
from __future__ import annotations
import re, time
from backend.core import humanize
from backend.core.skills.base import BaseSkill
from backend.core.skills import platform_common as pc

LISTS = ["https://www.workatastartup.com/companies?role=eng&jobType=fulltime&sortBy=created_desc&remote=yes&layout=list-compact",
         "https://www.workatastartup.com/companies?role=eng&jobType=fulltime&sortBy=created_desc&layout=list-compact&usVisaNotRequired=any&query=AI"]


class YCSkill(BaseSkill):
    platform = "ycombinator"

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
                if re.search(r"/jobs/\d+$", h) and h not in seen:
                    seen.add(h); urls.append(h if h.startswith("http") else "https://www.workatastartup.com" + h)
        items = []
        for url in urls[:limit]:
            if self.ctx.should_stop(): break
            try:
                page.goto(url, wait_until="domcontentloaded"); humanize.pause("action_delay_s")
                h1 = page.locator("h1").first.inner_text(timeout=3000).strip()
                m = re.match(r"(.+?) at (.+?)(?:\s*\(\w\d\d\))?$", h1)
                title, company = (m.group(1), m.group(2)) if m else (h1, "unknown")
                body = page.inner_text("body", timeout=5000)
                loc = next((l.strip() for l in body.split("\n") if re.search(r"^[A-Za-z .]+, [A-Z]{2}, [A-Z]{2}|^Remote|, India$|\bIN\b", l.strip()) and len(l.strip()) < 120), "")
                salary = next((l for l in body.split("\n") if re.search(r"[$₹€£]\s?\d", l) and len(l) < 60), "")
                sponsor = "will sponsor" in body.lower()
                items.append({"company": company[:150], "title": title[:250], "url": url, "apply_url": url, "location": loc, "remote_scope": loc, "salary": salary,
                              "description": body[:10000] + ("\n\nVisa: Will Sponsor" if sponsor else ""), "raw": {"sponsor": sponsor}})
            except Exception as e:  # noqa: BLE001
                self.log("warn", f"yc {url}: {e}")
        new = pc.upsert_many("ycombinator", items)
        self.ctx.bump("seen", len(items)); self.ctx.bump("new", new)
        self.log("info", f"yc: discovery done, {len(items)} seen, {new} new")

    def apply(self, page, limit):
        for aid in pc.pending_apps("ycombinator", limit):
            if self.ctx.should_stop(): break
            if not self.take("applies", str(aid)): break
            info = pc.app_info(aid, self.ctx.run_id)
            page.goto(info["url"], wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
            if page.locator("text=Applied").count() and not page.locator("a:has-text('Apply'), button:has-text('Apply')").count():
                pc.mark(aid, "submitted", None, None, "already applied"); continue
            if not pc.click_first(page, ["a:has-text('Apply')", "button:has-text('Apply')"]):
                pc.mark(aid, "needs_human", "no Apply button", self.ctx.screenshot(page, f"yc{aid}")); continue
            time.sleep(1.5)
            ta = page.locator("textarea").first
            try: ta.wait_for(timeout=6000)
            except Exception:
                pc.mark(aid, "needs_human", "no message box", self.ctx.screenshot(page, f"yc{aid}")); continue
            msg = (info["cover"] or "").strip()
            if len(msg) < 50:
                from backend.core import profile as _profile
                _p = _profile.load(); _cur = next((e for e in _p.get("experience", []) if e.get("current")), None)
                msg = f"Hi, I'm {_p['identity'].get('first_name') or _p['identity']['name'].split()[0]}, {_p['identity'].get('headline', '')}" + (f" at {_cur['company']}." if _cur else ".") + f" {msg}"
            humanize.human_type(page, ta, msg[:1800])
            shot = self.ctx.screenshot(page, f"yc{aid}_before")
            if not pc.click_first(page, ["button:has-text('Send')"]):
                pc.mark(aid, "needs_human", "no Send button", shot); continue
            time.sleep(2.5)
            body = page.inner_text("body", timeout=4000).lower()
            ok = pc.confirmed(page, ("application sent", "message sent", "you have applied", "successfully applied"))
            shot2 = self.ctx.screenshot(page, f"yc{aid}_after")
            pc.mark(aid, "submitted" if ok else "needs_human", None if ok else "send not confirmed", shot2, "yc founder message sent")
            self.log("info" if ok else "warn", f"yc {'sent' if ok else 'unverified'}: {info['company']} — {info['title']}", screenshot=shot2)
            self.ctx.bump("submitted" if ok else "unverified")
            humanize.pause("between_items_s")


SKILLS = {"ycombinator": YCSkill}
