"""Cutshort: matched jobs at /profile/all-jobs?matchesfor=<id> (and /jobs search). Each job has a stable /job/<slug> URL with an
'Apply to this job' button that may open a short questionnaire."""
from __future__ import annotations
import re, time
from urllib.parse import quote_plus
from backend.core import humanize
from backend.core.skills.base import BaseSkill
from backend.core.skills import platform_common as pc

MATCHES = "https://cutshort.io/profile/all-jobs"
SEARCH = "https://cutshort.io/jobs?q={q}"
QUERIES = ["AI engineer", "LLM", "forward deployed", "backend python", "full stack AI"]


class CutshortSkill(BaseSkill):
    platform = "cutshort"

    def execute(self, page, mode: str = "discover", limit: int = 40, **_):
        if mode == "discover": self.discover(page, limit)
        elif mode == "apply": self.apply(page, limit)

    def _collect_cards(self, page) -> list[dict]:
        out = []
        links = page.locator("a[href^='/job/'], a[href*='cutshort.io/job/']")
        n = links.count()
        seen = set()
        for i in range(n):
            a = links.nth(i)
            try:
                href = a.get_attribute("href") or ""
                if not href or href in seen: continue
                title = a.inner_text(timeout=1000).strip()
                if not title or len(title) < 4: continue
                seen.add(href)
                url = href if href.startswith("http") else "https://cutshort.io" + href
                m = re.search(r"/job/.+?-([^-/]+)-[A-Za-z0-9]+$", href)
                card = a.locator("xpath=ancestor::*[self::li or self::article or contains(@class,'card') or contains(@class,'job')][1]")
                ctext = card.inner_text(timeout=1500) if card.count() else ""
                company = ""
                mm = re.search(r"\bat\s+([^\n·]+)", ctext)
                if mm: company = mm.group(1).strip()[:100]
                loc = next((l for l in ctext.split("\n") if re.search(r"remote|bangalore|bengaluru|mumbai|delhi|pune|hyderabad|chennai|gurgaon|noida", l, re.I)), "")
                out.append({"company": company or "unknown", "title": title, "url": url, "apply_url": url, "location": loc, "remote_scope": loc, "description": ctext[:3000], "raw": {"list_text": ctext[:500]}})
            except Exception:
                continue
        return out

    def discover(self, page, limit):
        items = []
        page.goto(MATCHES, wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
        for _ in range(3): humanize.human_scroll(page, 1500)
        items += self._collect_cards(page)
        for q in QUERIES:
            if self.ctx.should_stop() or len(items) >= limit: break
            if not self.take("searches", q): break
            page.goto(SEARCH.format(q=quote_plus(q)), wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
            for _ in range(2): humanize.human_scroll(page, 1500)
            items += self._collect_cards(page)
        # enrich with full JD for the first `limit` items
        enriched = []
        for it in items[:limit]:
            if self.ctx.should_stop(): break
            try:
                page.goto(it["url"], wait_until="domcontentloaded"); humanize.pause("action_delay_s")
                body = page.inner_text("main, body", timeout=5000)
                it["description"] = body[:10000]
                h = page.locator("h1").first
                if h.count(): it["title"] = h.inner_text(timeout=1000).strip() or it["title"]
                comp = page.locator("h1 ~ * a[href*='/company/'], a[href*='/company/']").first
                if comp.count(): it["company"] = comp.inner_text(timeout=1000).strip() or it["company"]
                if not it.get("location"):
                    m = re.search(r"(?im)^\s*location\s*[:\-]?\s*(.+)$", body) or re.search(r"(Remote only|Remote|Bengaluru|Bangalore|Mumbai|Delhi|Hyderabad|Pune|Chennai|Gurgaon|Noida)[^\n]{0,40}", body)
                    if m: it["location"] = it["remote_scope"] = m.group(1 if m.re.groups else 0).strip()[:120]
            except Exception:
                pass
            enriched.append(it)
        new = pc.upsert_many("cutshort", enriched)
        self.ctx.bump("seen", len(enriched)); self.ctx.bump("new", new)
        self.log("info", f"cutshort: discovery done, {len(enriched)} seen, {new} new")

    def apply(self, page, limit):
        """Cutshort flow (2026-09): 'Apply to this job' -> redirect to /profile/all-jobs?jobid=... -> 'are you still looking?' modal
        ('I am looking') -> 'Apply now' -> 'Please verify your data before applying' sheet (experience years, current company,
        location, salary numbers, employment status, declaration) -> 'Save and continue' -> optional questions -> confirmation."""
        from backend.core import profile
        prof = profile.load()
        for aid in pc.pending_apps("cutshort", limit):
            if self.ctx.should_stop(): break
            if not self.take("applies", str(aid)): break
            info = pc.app_info(aid, self.ctx.run_id)
            page.goto(info["url"], wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
            if pc.confirmed(page, ("already applied", "you applied", "application sent")):
                pc.mark(aid, "submitted", None, None, "already applied"); continue
            btn = page.locator("button:has-text('Apply to this job'), button:has-text('Apply now')").first
            if not btn.count():
                pc.mark(aid, "needs_human", "no Apply button", self.ctx.screenshot(page, f"cs{aid}")); continue
            btn.evaluate("e => e.click()"); time.sleep(4); self.guard(page)          # sticky button: a script click is the reliable one
            self._click_if(page, "button:has-text('I am looking')", 2.5)
            self._click_if(page, "button:has-text('Apply now')", 4)
            sheet = page.locator("[role='dialog'], [class*='modal' i]").filter(visible=True).first
            if sheet.count() and "verify your data" in sheet.inner_text(timeout=3000).lower():
                self._verify_sheet(page, sheet, prof, info.get('resume'))
                shot = self.ctx.screenshot(page, f"cs{aid}_verify")
                for attempt in range(3):
                    self._click_if(page, "button:has-text('Save and continue')", 4); self.guard(page)
                    still = page.locator("[role='dialog'], [class*='modal' i]").filter(visible=True).first
                    if not (still.count() and "verify your data" in still.inner_text(timeout=2000).lower()): break
                    txt = still.inner_text(timeout=2000)
                    self.log("warn", f"cutshort verify sheet still open (try {attempt + 1}); required markers: {txt.count('Required')}", screenshot=self.ctx.screenshot(page, f"cs{aid}_sheet_stuck{attempt}"))
                    # only flip the declaration when its own 'Required' marker is showing (the box is a toggle; never double-click it)
                    try:
                        tail = txt[txt.find("days"):] if "days" in txt else txt[-400:]
                        if "Required" in tail:
                            lab = still.get_by_text("I hereby declare", exact=False).first.locator("xpath=ancestor-or-self::label[1]")
                            if lab.count(): lab.first.click(force=True); time.sleep(0.8)
                        if "Resume" in txt and txt.find("Required") < txt.find("Work experience"):
                            self.log("warn", "cutshort: resume still required on the sheet; upload one on cutshort.io/profile once"); break
                    except Exception: pass
            un = [u for u in pc.handle_questionnaire(self, page, info) if not re.search(r"e\.g|salary|lacs|days", u, re.I)]
            if un:
                pc.mark(aid, "needs_human", "unanswered: " + " | ".join(un[:6]), self.ctx.screenshot(page, f"cs{aid}_q")); continue
            for sel in ("button:has-text('Submit application')", "button:has-text('Submit')", "button:has-text('Send application')", "button:has-text('Apply now')"):
                if self._click_if(page, sel, 3): break
            shot = self.ctx.screenshot(page, f"cs{aid}_after")
            ok = pc.confirmed(page) or pc.confirmed(page, ("applied successfully", "application sent", "you have applied", "already applied"))
            pc.mark(aid, "submitted" if ok else "needs_human", None if ok else "no confirmation detected", shot, "cutshort apply clicked")
            self.log("info" if ok else "warn", f"cutshort {'applied' if ok else 'unverified'}: {info['company']} — {info['title']}", screenshot=shot)
            self.ctx.bump("submitted" if ok else "unverified")
            humanize.pause("between_items_s")

    def _click_if(self, page, selector, wait_s):
        loc = page.locator(selector).filter(visible=True)
        if not loc.count(): return False
        humanize.human_click(page, loc.first); time.sleep(wait_s); return True

    def _verify_sheet(self, page, sheet, prof, resume=None):
        """Fill Cutshort's 'verify your data' sheet: years of experience, current company, city, salary (LPA), employment
        status and the declaration checkbox. Existing values are left alone."""
        import re as _re
        years = 3
        try:
            first = prof["experience"][0]; y = _re.match(r"(\d{4})", str(first.get("start", "")))
            if y: years = max(1, 2026 - int(y.group(1)))
        except Exception: pass
        try:   # resume is required on the sheet: 'Upload resume' opens a native file chooser (no <input type=file> in the DOM)
            if resume:
                from backend.core import config as _cfg
                up = sheet.get_by_text("Upload resume", exact=False).first
                if up.count():
                    with page.expect_file_chooser(timeout=8000) as fc:
                        up.click(force=True)
                    fc.value.set_files(str(_cfg.ROOT / resume)); time.sleep(4)
        except Exception as e:
            self.log("warn", f"cutshort resume upload: {str(e)[:80]}")
        nums = sheet.locator("input[type='number']")
        vals = [str(years), str(prof.get("preferences", {}).get("current_salary_lpa") or ""), str(prof.get("preferences", {}).get("expected_salary_lpa") or "")]
        for i in range(min(nums.count(), 3)):
            try:
                el = nums.nth(i)
                if vals[i] and el.is_visible() and not el.input_value(): el.fill(vals[i])
            except Exception: continue
        current_company = next((e.get("company") for e in prof.get("experience", []) if e.get("current")), (prof.get("experience") or [{}])[0].get("company", ""))
        city = (prof.get("identity", {}).get("location") or "").split(",")[0].strip()
        for sel, val in (("input[placeholder*='company' i]", current_company), ("input[placeholder*='city' i]", city)):
            if not val: continue
            try:
                el = sheet.locator(sel).first
                if el.count() and el.is_visible() and not el.input_value():
                    el.fill(val); time.sleep(1.2)
                    opt = page.locator("[role='option'], li").filter(has_text=val.split(".")[0]).filter(visible=True).first
                    if opt.count(): opt.click()
            except Exception: continue
        # Cutshort's radios/checkboxes are custom <label tabindex=0> elements with no real input: click the label itself
        def tick(text):
            try:
                el = sheet.get_by_text(text, exact=False).first
                if not el.count(): return False
                lab = el.locator("xpath=ancestor-or-self::label[1]")
                (lab.first if lab.count() else el).click(force=True); time.sleep(0.6); return True
            except Exception:
                return False
        tick("Not resigned yet")
        if "Work remotely" in sheet.inner_text(timeout=2000) and sheet.get_by_text("In any timezone", exact=False).count():
            tick("In any timezone")
        try:   # notice-period days appears once 'Not resigned yet' is chosen
            np = sheet.locator("input[placeholder*='E.g 30' i], input[placeholder*='days' i]").first
            if np.count() and np.is_visible() and not np.input_value(): np.fill("30"); time.sleep(0.5)
        except Exception: pass
        tick("I hereby declare")


SKILLS = {"cutshort": CutshortSkill}
