"""Naukri: search pages /<slug>-jobs?k=...&experience=3&jobAge=7 list .srp-jobtuple-wrapper cards with a.title -> /job-listings-...-<id>.
Job page: button#apply-button (on-site apply, may open a chatbot questionnaire .chatbot_Drawer / ._chatBotContainer) or 'Apply on company site'."""
from __future__ import annotations
import re, time
from urllib.parse import quote_plus
from backend.core import humanize
from backend.core.skills.base import BaseSkill
from backend.core.skills import platform_common as pc

QUERIES = ["ai engineer llm", "generative ai engineer", "forward deployed engineer", "python backend engineer ai", "langchain langgraph"]


class NaukriSkill(BaseSkill):
    platform = "naukri"

    def execute(self, page, mode: str = "discover", limit: int = 40, **_):
        if mode == "discover": self.discover(page, limit)
        elif mode == "apply": self.apply(page, limit)

    def discover(self, page, limit):
        items, seen = [], set()
        for q in QUERIES:
            if self.ctx.should_stop() or len(items) >= limit: break
            if not self.take("searches", q): break
            slug = re.sub(r"[^a-z0-9]+", "-", q.lower()).strip("-")
            page.goto(f"https://www.naukri.com/{slug}-jobs?k={quote_plus(q)}&experience=3&jobAge=7", wait_until="domcontentloaded")
            humanize.pause(); self.guard(page)
            for _ in range(3): humanize.human_scroll(page, 1500)
            cards = page.locator(".srp-jobtuple-wrapper")
            for i in range(cards.count()):
                c = cards.nth(i)
                try:
                    a = c.locator("a.title").first
                    href = (a.get_attribute("href") or "").split("?")[0]
                    if not href or href in seen: continue
                    seen.add(href)
                    txt = [l.strip() for l in c.inner_text(timeout=1500).split("\n") if l.strip()]
                    title = a.inner_text(timeout=1000).strip()
                    comp = c.locator("a.comp-name, .comp-name").first
                    company = comp.inner_text(timeout=1000).strip() if comp.count() else (txt[1] if len(txt) > 1 else "unknown")
                    loc = c.locator(".locWdth, .loc").first.inner_text(timeout=1000).strip() if c.locator(".locWdth, .loc").count() else ""
                    exp = c.locator(".expwdth, .exp").first.inner_text(timeout=1000).strip() if c.locator(".expwdth, .exp").count() else ""
                    desc = c.locator(".job-desc").first.inner_text(timeout=1000).strip() if c.locator(".job-desc").count() else ""
                    tags = c.locator(".tag-li").all_inner_texts()
                    items.append({"company": company[:150], "title": title[:250], "url": href, "apply_url": href, "location": loc, "remote_scope": loc,
                                  "description": f"{desc}\nExperience: {exp}\nSkills: {', '.join(tags)}", "tags": tags, "raw": {"query": q, "experience": exp}})
                except Exception:
                    continue
            humanize.pause("between_items_s")
        new = pc.upsert_many("naukri", items[:limit])
        self.ctx.bump("seen", len(items)); self.ctx.bump("new", new)
        self.log("info", f"naukri: discovery done, {len(items)} seen, {new} new")

    def apply(self, page, limit):
        for aid in pc.pending_apps("naukri", limit):
            if self.ctx.should_stop(): break
            if not self.take("applies", str(aid)): break
            info = pc.app_info(aid, self.ctx.run_id)
            page.goto(info["url"], wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
            if page.locator("#already-applied").count() or page.get_by_text("Already Applied", exact=False).count():
                pc.mark(aid, "submitted", None, None, "already applied"); continue
            if page.locator("#company-site-button, button:has-text('Apply on company site')").count() and not page.locator("#apply-button").count():
                pc.mark(aid, "needs_human", "external apply on company site — route via ATS worker", self.ctx.screenshot(page, f"nk{aid}")); continue
            self._profile_drawer(page)
            if not pc.click_first(page, ["#apply-button", "button:has-text('Apply')"]):
                pc.mark(aid, "needs_human", "no Apply button", self.ctx.screenshot(page, f"nk{aid}")); continue
            time.sleep(3)
            self._profile_drawer(page)
            if page.locator(".chatbot_Drawer, ._chatBotContainer, [class*='chatbot']").count():
                # Naukri questionnaire chatbot: answer text prompts from the answer bank, stop on unknowns
                for _ in range(8):
                    q_el = page.locator(".botMsg, [class*='botMsg'], .chatbot_MessageContainer li").last
                    q = q_el.inner_text(timeout=2000) if q_el.count() else ""
                    from backend.core import profile
                    ans = profile.answer_for(q)
                    if not ans or ans == "__LLM__":
                        pc.mark(aid, "needs_human", f"chatbot question unanswered: {q[:100]}", self.ctx.screenshot(page, f"nk{aid}_chat")); break
                    inp = page.locator(".chatbot_InputContainer [contenteditable], .chatbot_InputContainer textarea, [class*='chatbot'] [contenteditable='true']").first
                    if not inp.count():
                        opt = page.locator(f"[class*='chatbot'] label:has-text('{ans.split()[0]}'), [class*='chatbot'] .ssrc__radio-btn-container:has-text('{ans.split()[0]}')").first
                        if opt.count(): humanize.human_click(page, opt)
                    else:
                        humanize.human_type(page, inp, ans)
                    pc.click_first(page, [".sendMsg, [class*='send']"]); time.sleep(2)
                    if pc.confirmed(page, ("successfully applied", "applied successfully", "you have successfully")): break
                else:
                    pass
            time.sleep(2)
            ok = pc.confirmed(page, ("successfully applied", "applied successfully", "already applied", "you have successfully"))
            shot = self.ctx.screenshot(page, f"nk{aid}_after")
            if ok:
                pc.mark(aid, "submitted", None, shot, "naukri apply"); self.ctx.bump("submitted")
                self.log("info", f"naukri applied: {info['company']} — {info['title']}", screenshot=shot)
            else:
                pc.mark(aid, "needs_human", "no confirmation detected", shot); self.ctx.bump("unverified")
            humanize.pause("between_items_s")


    def _profile_drawer(self, page, rounds: int = 6):
        """Naukri's 'update your profile' side drawer: answer each question via chips or text, press Save, repeat."""
        from backend.core import profile
        prof = profile.load(); edu = (prof.get("education") or [{}])[0]; prefs = prof.get("preferences") or {}
        degree = str(edu.get("degree") or "")
        level = "B.Tech / B.E." if re.search(r"b\.?\s*(tech|e)\b", degree, re.I) else "B.Sc" if re.search(r"b\.?\s*sc", degree, re.I) else "M.Tech / M.E." if re.search(r"m\.?\s*(tech|e)\b", degree, re.I) else "MCA" if "mca" in degree.lower() else degree.split(",")[0]
        stream = "Computers" if re.search(r"computer|software|information", degree, re.I) else (degree.split(",", 1)[1].strip() if "," in degree else "")
        chips = [(r"undergraduate|graduation|degree|course", level), (r"specializ|branch|stream", stream), (r"university|college|institute", str(edu.get("school") or "").split(" (")[0]),
                 (r"passing year|year of (passing|graduation)|graduat.*year", str(edu.get("end") or "")), (r"notice period", prefs.get("notice_period", "")),
                 (r"current (ctc|salary)", str(prefs.get("current_salary_lpa") or "")), (r"expected (ctc|salary)", str(prefs.get("expected_salary_lpa") or "")),
                 (r"location|city", (prof.get("identity", {}).get("location") or "").split(",")[0].strip()), (r"gender", str(prof.get("identity", {}).get("gender") or "")),
                 (r"experience", str(prefs.get("experience_years") or ""))]
        CHIP_ANSWERS = [(rx, val) for rx, val in chips if val]          # unknown facts are left for the human, never guessed
        for _ in range(rounds):
            drawer = page.locator("[class*='drawer'], [class*='Drawer'], [class*='sidesheet'], [class*='SideSheet']").filter(has_text="Save").first
            if not drawer.count() or not drawer.is_visible(): return
            q = drawer.inner_text(timeout=2000)
            ql = q.lower()
            ans = next((a for rx, a in CHIP_ANSWERS if re.search(rx, ql)), None) or profile.answer_for(q.split("?")[0][-120:])
            if not ans or ans == "__LLM__":
                self.log("warn", f"naukri drawer question unanswered: {q[-160:].strip()}"); return
            chip = drawer.locator(f"text={ans.split('/')[0].strip()}").first
            if chip.count(): humanize.human_click(page, chip)
            else:
                inp = drawer.locator("input:visible, textarea:visible").first
                if inp.count(): humanize.human_type(page, inp, ans); time.sleep(1); page.keyboard.press("Enter")
            time.sleep(0.8)
            save = drawer.locator("button:has-text('Save')").first
            if save.count() and save.is_enabled(): humanize.human_click(page, save); time.sleep(2)
            else:
                close = drawer.locator("[class*='close'], [aria-label='Close']").first
                if close.count(): close.click(); return


SKILLS = {"naukri": NaukriSkill}
