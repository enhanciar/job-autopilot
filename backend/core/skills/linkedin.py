"""LinkedIn skill: discover (search), easy_apply (approved Applications with platform=linkedin), connect + dm (approved Outreach rows).
Recorded flow (2026-09-05): search list cards li[data-occludable-job-id]; detail pane button.jobs-apply-button (aria-label 'Easy Apply to ...');
modal .jobs-easy-apply-modal with <progress>, steps: Contact info -> Resume (radio select or Upload resume input[type=file]) -> questions -> Review -> 'Submit application'.
Buttons: aria-label 'Continue to next step', 'Review your application', 'Submit application', 'Dismiss'. Save dialog: 'Discard'."""
from __future__ import annotations
import re, time
from urllib.parse import quote_plus
from backend.app.db import session
from backend.app.models import Application, Outreach, Contact
from backend.core import humanize, config, profile
from backend.core.apply import forms
from backend.core.skills.base import BaseSkill
from backend.core.skills import platform_common as pc

DEFAULT_QUERIES = ["forward deployed engineer", "applied AI engineer", "AI engineer LLM", "agent engineer", "solutions engineer AI"]
MODAL = ".jobs-easy-apply-modal, div[role='dialog']:has(progress)"


class LinkedInSkill(BaseSkill):
    platform = "linkedin"

    def execute(self, page, mode: str = "discover", queries: list[str] | None = None, remote_only: bool = True, max_pages: int = 3, limit: int = 10, **_):
        if mode == "hydrate": self.hydrate(page, limit)
        elif mode == "discover": self.discover(page, queries or DEFAULT_QUERIES, remote_only, max_pages)
        elif mode == "easy_apply": self.easy_apply(page, limit)
        elif mode == "connect":
            from backend.core.skills.linkedin_people import LinkedInPeopleSkill
            LinkedInPeopleSkill(self.ctx).connect(page, limit)
        elif mode == "dm":
            from backend.core.skills.linkedin_people import LinkedInPeopleSkill
            LinkedInPeopleSkill(self.ctx).message(page, limit)
        elif mode == "find_people": self.find_people(page, limit)

    # ---------------------------------------------------------------- discover
    def hydrate(self, page, limit):
        """Search cards carry only title/company, so scoring skips them. Open each job page and store its real description,
        exact location and Easy Apply flag, so the scorer can rank LinkedIn jobs like every other source."""
        from backend.app.db import session
        from backend.app.models import Job
        with session() as db:
            ids = [j.id for j in db.query(Job).filter(Job.source == "linkedin", Job.status.in_(["new", "filtered", "scored"]))
                   .filter((Job.description.is_(None)) | (Job.description == "")).order_by(Job.created_at.desc()).limit(limit).all()]
        humanize.set_pacing(None)  # retain platform pacing during hydration
        self.log("info", f"linkedin: hydrating {len(ids)} job description(s)")
        for jid in ids:
            if self.ctx.should_stop(): break
            with session() as db:
                j = db.get(Job, jid); url = j.url
            page.goto(url, wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
            try:
                more = page.locator("button:has-text('See more'), button:has-text('Show more'), button[aria-label*='see more' i]").filter(visible=True).first
                if more.count(): more.click(timeout=3000); page.wait_for_timeout(600)
            except Exception:
                pass
            # LinkedIn's 2026 markup has hashed class names and no .jobs-description__content: read <main> and cut at
            # "About the job", which is the stable heading that starts the posting body.
            desc = loc = ""
            try:
                main = page.locator("main").first.inner_text(timeout=8000)
                head_lines = [l.strip() for l in main[:400].split("\n") if l.strip()]
                loc = next((l for l in head_lines[:8] if re.search(r"remote|hybrid|on-?site|,\s*\w", l, re.I) and len(l) < 110), "")
                i = main.find("About the job")
                body = main[i + len("About the job"):] if i >= 0 else main
                for tail in ("Show more", "Show less", "See less", "People also viewed", "Similar jobs", "Set alert"):
                    j = body.find(tail)
                    if j > 400: body = body[:j]
                desc = body.strip()
            except Exception:
                pass
            easy = page.locator("button:has-text('Easy Apply')").count() > 0
            with session() as db:
                j = db.get(Job, jid)
                if desc:
                    j.description = desc[:20000]
                    j.fit_score = None; j.fit_reasons = None
                    from backend.core.normalize import eligibility, country_of
                    ok, reason = eligibility(j.title, loc or j.location, loc or j.remote_scope, desc, j.sponsor_flag)
                    j.eligible, j.eligibility_reason = ok, reason
                    j.status = "new" if ok else "filtered"
                    j.country = country_of(loc or j.location, loc or j.remote_scope)
                if loc: j.location = loc[:200]; j.remote_scope = loc[:200]
                raw = dict(j.raw or {}); raw["easy_apply"] = easy; j.raw = raw
            self.ctx.bump("hydrated" if desc else "no_description")

    # LinkedIn scopes every search to one location (the account's country by default). Search each target market explicitly:
    # remote roles worldwide + the relocation countries + India. Overridable via config.yaml linkedin.locations.
    DEFAULT_LOCATIONS = ["Worldwide", "United States", "United Kingdom", "European Union", "Germany", "Netherlands", "Singapore",
                         "United Arab Emirates", "Canada", "Australia", "Japan", "India"]

    def discover(self, page, queries, remote_only, max_pages):
        from backend.core import config as _cfg
        locations = (_cfg.load().get("linkedin") or {}).get("locations") or self.DEFAULT_LOCATIONS
        new_total = 0
        for q in queries:
            for loc in locations:
                if self.ctx.should_stop(): break
                if not self.take("searches", f"{q}@{loc}"): break
                f_wt = "&f_WT=2" if (remote_only and loc != "India") else ""     # remote filter everywhere except India (on-site ok there)
                page.goto(f"https://www.linkedin.com/jobs/search/?keywords={quote_plus(q)}&location={quote_plus(loc)}&f_TPR=r86400{f_wt}&sortBy=DD", wait_until="domcontentloaded")
                humanize.pause(); self.guard(page)
                new_total += self._harvest(page, q, loc, remote_only, max_pages)
                humanize.pause("between_items_s")
        self.ctx.bump("new", new_total); self.log("info", f"linkedin: discovery done, {new_total} new jobs across {len(locations)} locations")

    def _harvest(self, page, q, loc_hint, remote_only, max_pages):
        new_total = 0
        if True:
            for p in range(max_pages):
                humanize.human_scroll(page, 1500)
                cards = page.locator("li[data-occludable-job-id]")
                items = []
                for i in range(cards.count()):
                    c = cards.nth(i)
                    try:
                        jid = c.get_attribute("data-occludable-job-id")
                        txt = [l.strip() for l in c.inner_text(timeout=1500).split("\n") if l.strip()]
                        if not jid or len(txt) < 2: continue
                        title, company = txt[0], txt[1] if not txt[1].endswith("verification") else txt[2]
                        company = re.sub(r"\s*with verification$", "", company)
                        loc = next((l for l in txt if re.search(r"remote|india|\(|,", l, re.I) and l not in (title, company)), "")
                        easy = any("easy apply" in l.lower() for l in txt)
                        loc = loc or loc_hint
                        items.append(dict(company=company, title=title, url=f"https://www.linkedin.com/jobs/view/{jid}/", apply_url=f"https://www.linkedin.com/jobs/view/{jid}/",
                                          location=loc, remote_scope=("remote " + loc) if remote_only else loc, raw={"query": q, "job_id": jid, "easy_apply": easy, "search_location": loc_hint}))
                    except Exception:
                        continue
                new_total += pc.upsert_many("linkedin", items); self.ctx.bump("seen", len(items))
                nxt = page.locator("button[aria-label='View next page']")
                if p + 1 < max_pages and nxt.count() and nxt.first.is_enabled():
                    humanize.human_click(page, nxt.first); humanize.pause(); self.guard(page)
                else: break
        return new_total

    # ---------------------------------------------------------------- easy apply
    def easy_apply(self, page, limit):
        for aid in pc.pending_apps("linkedin", limit):
            if self.ctx.should_stop(): break
            if not self.take("easy_apply", str(aid)): break
            info = pc.app_info(aid, self.ctx.run_id)
            try:
                self._easy_apply_one(page, aid, info)
            except Exception as e:  # noqa: BLE001
                pc.mark(aid, "needs_human", f"{type(e).__name__}: {e}", self.ctx.screenshot(page, f"li{aid}_err")); self.ctx.bump("needs_human")
                self._dismiss(page)
            humanize.pause("between_items_s")

    def _dismiss(self, page):
        try:
            page.locator("button[aria-label='Dismiss']").first.click(timeout=2000); time.sleep(1)
            d = page.locator("button:has-text('Discard')")
            if d.count(): d.first.click(timeout=2000)
        except Exception:
            pass

    def _easy_apply_one(self, page, aid, info):
        page.goto(info["url"], wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
        body = page.inner_text("body", timeout=5000).lower()
        if pc.confirmed(page, ("you applied to this job", "your application was sent")):
            pc.mark(aid, "submitted", None, None, "already applied on LinkedIn"); return
        btn = page.locator("button.jobs-apply-button[aria-label^='Easy Apply']")
        if not btn.count():
            # External apply: the button opens the employer's own form in a new tab. Capture that URL and hand the
            # application to the ATS worker, which knows how to fill Greenhouse / Ashby / Lever / company forms.
            ext = page.locator("button.jobs-apply-button, a.jobs-apply-button").filter(visible=True).first
            target = None
            if ext.count():
                try:
                    with page.context.expect_page(timeout=8000) as pop:
                        ext.click()
                    np = pop.value; np.wait_for_load_state("domcontentloaded", timeout=20000); time.sleep(2)
                    target = np.url; np.close()
                except Exception:
                    if page.url and "linkedin.com" not in page.url: target = page.url
            if target and "linkedin.com" not in target:
                from backend.app.db import session as _s
                from backend.app.models import Application as _A
                with _s() as db:
                    a = db.get(_A, aid); a.method = "ats_form"; a.platform = "web"; a.status = "approved"; a.error = None
                    a.job.apply_url = target.split("?")[0] if "utm_" in target else target
                self.log("info", f"linkedin: external apply -> handed to ATS worker: {target[:90]}"); self.ctx.bump("handed_off"); return
            pc.mark(aid, "needs_human", "external apply but the employer link could not be captured", self.ctx.screenshot(page, f"li{aid}")); return
        humanize.human_click(page, btn.first); time.sleep(2)
        modal = page.locator(MODAL).first
        modal.wait_for(state="visible", timeout=10000)
        for step in range(12):
            self.guard(page)
            heading = (modal.locator("h3").first.inner_text(timeout=1500) if modal.locator("h3").count() else "").lower()
            if "resume" in heading:
                self._pick_resume(page, modal, info)
            else:
                un = forms.fill_text_inputs(page, info["job_text"], info["cover"], self.log, scope=modal)
                un += forms.fill_selects(page, self.log, scope=modal)
                self._fill_radios(page, modal)
                if un:
                    pc.mark(aid, "needs_human", "unanswered: " + " | ".join(un[:6]), self.ctx.screenshot(page, f"li{aid}_q")); self.ctx.bump("needs_human"); self._dismiss(page); return
            if modal.locator("button[aria-label='Submit application']").count():
                shot = self.ctx.screenshot(page, f"li{aid}_review")
                # untick "follow company" to keep footprint small
                fc = modal.locator("label[for='follow-company-checkbox']")
                if fc.count(): fc.first.click()
                humanize.human_click(page, modal.locator("button[aria-label='Submit application']").first); time.sleep(3)
                done = pc.confirmed(page, ("application sent", "your application was sent"))
                shot2 = self.ctx.screenshot(page, f"li{aid}_done")
                pc.mark(aid, "submitted" if done else "needs_human", None if done else "no 'Application sent' confirmation", shot2, "linkedin easy apply")
                self.log("info" if done else "warn", f"linkedin easy apply {'sent' if done else 'unverified'}: {info['company']} — {info['title']}", screenshot=shot2)
                self.ctx.bump("submitted" if done else "unverified")
                try: page.locator("button[aria-label='Dismiss']").first.click(timeout=2000)
                except Exception: pass
                return
            nxt = modal.locator("button[aria-label='Continue to next step'], button[aria-label='Review your application']")
            if not nxt.count():
                pc.mark(aid, "needs_human", "no next/review button in modal", self.ctx.screenshot(page, f"li{aid}_stuck")); self._dismiss(page); return
            humanize.human_click(page, nxt.first); time.sleep(1.5)
            if modal.locator(".artdeco-inline-feedback--error").count():
                errs = modal.locator(".artdeco-inline-feedback--error").all_inner_texts()
                pc.mark(aid, "needs_human", "form errors: " + " | ".join(e.strip() for e in errs[:4]), self.ctx.screenshot(page, f"li{aid}_errs")); self._dismiss(page); return
        pc.mark(aid, "needs_human", "too many steps", self.ctx.screenshot(page, f"li{aid}_steps")); self._dismiss(page)

    def _pick_resume(self, page, modal, info):
        """Upload the tailored PDF if we have one, else keep LinkedIn's selected resume."""
        if info.get("resume"):
            f = modal.locator("input[type='file']")
            if f.count():
                f.first.set_input_files(str(config.ROOT / info["resume"])); time.sleep(3); return
        sel = modal.locator("input[type='radio']")
        if sel.count() and not sel.first.is_checked():
            modal.locator("label[for]").first.click()

    def _fill_radios(self, page, modal):
        groups = modal.locator("fieldset")
        for i in range(groups.count()):
            g = groups.nth(i)
            try:
                if g.locator("input[type='radio']:checked").count(): continue
                q = g.locator("legend").first.inner_text(timeout=800)
                ans = forms.answer_question(q, "", None, self.log)
                if not ans or ans == "__LLM__": continue
                want = ans.split()[0].lower().strip(",.")
                for lab in g.locator("label").all():
                    if lab.inner_text(timeout=500).strip().lower().startswith(want):
                        humanize.human_click(page, lab); break
            except Exception:
                continue

    # ---------------------------------------------------------------- find people (recruiters / hiring managers)
    TITLE_RX = re.compile(r"recruit|talent|hiring|people ops|head of engineering|engineering manager|cto|founder|co-founder|vp engineering", re.I)

    def find_people(self, page, limit):
        """For each pending LinkedIn-connect outreach row without a person: find the employer's company page, open its People
        tab filtered by 'recruiter', and pick a recruiter / talent / founder card (2nd-degree 'Connect' cards preferred)."""
        from backend.app.models import Job, Application, Contact, Outreach
        with session() as db:
            rows = db.query(Outreach).filter(Outreach.channel == "linkedin_connect", Outreach.status.in_(["pending_review", "approved"])).all()
            todo = []
            for o in rows:
                c = db.get(Contact, o.contact_id) if o.contact_id else None
                if c and c.linkedin_url: continue
                j = db.get(Job, o.job_id) if o.job_id else None
                if j: todo.append((o.id, o.contact_id, j.company))
        seen_company = {}
        for oid, cid, company in todo[:limit]:
            if self.ctx.should_stop(): break
            key = re.sub(r"\s*\d+\s*-\s*\d+\s*years.*$", "", company, flags=re.I).strip()   # "Acme3 - 5 Years Bangalore" (Hirist) -> "Acme"
            key = re.sub(r"\s*\(.*?\)\s*$", "", key).strip() or company
            if key in seen_company:
                if seen_company[key]: self._attach_person(oid, cid, *seen_company[key])
                continue
            if not self.take("searches", key): break
            found = self._find_recruiter(page, key)
            seen_company[key] = found
            if not found:
                self.log("warn", f"linkedin: no recruiter/founder found for {key}"); self.ctx.bump("not_found"); continue
            self._attach_person(oid, cid, *found)
            self.log("info", f"linkedin: {key} -> {found[0]} ({found[1][:40]}) {found[2]}"); self.ctx.bump("found")
            humanize.pause("between_items_s")

    @staticmethod
    def _mentions(text: str, company: str) -> bool:
        toks = [t for t in re.findall(r"[a-z0-9]+", company.lower()) if len(t) >= 3 and t not in ("inc", "ltd", "llc", "the", "and", "com", "corp", "labs", "group", "technologies", "technology", "software", "solutions", "consultants", "corporation")]
        low = re.sub(r"[^a-z0-9]+", " ", text.lower())
        return any(t in low for t in toks) if toks else False

    def _company_slug(self, page, company: str) -> str | None:
        page.goto(f"https://www.linkedin.com/search/results/companies/?keywords={quote_plus(company)}", wait_until="domcontentloaded")
        humanize.pause(); self.guard(page)
        comp = re.sub(r"[^a-z0-9]", "", company.lower())
        slugs = []
        for a in page.locator("a[href*='/company/']").all()[:12]:
            m = re.search(r"/company/([^/?]+)", a.get_attribute("href") or "")
            if m and m.group(1) not in slugs: slugs.append(m.group(1))
        if not slugs: return None
        # prefer the slug that looks like the company name ("gitlab-com" for GitLab, not "gitlab-foundation")
        for sl in slugs:
            n = re.sub(r"[^a-z0-9]", "", sl.lower())
            if n == comp or n.startswith(comp) or (len(comp) >= 5 and comp in n): return sl
        n0 = re.sub(r"[^a-z0-9]", "", slugs[0].lower())
        return slugs[0] if (n0[:5] == comp[:5]) else None      # avoid "Interview Resources" -> interview-cracker

    def _find_recruiter(self, page, company: str):
        slug = self._company_slug(page, company)
        if not slug: return None
        strong_slug = re.sub(r"[^a-z0-9]", "", slug.lower()).startswith(re.sub(r"[^a-z0-9]", "", company.lower())[:8])
        page.goto(f"https://www.linkedin.com/company/{slug}/people/?keywords=recruiter", wait_until="domcontentloaded")
        humanize.pause(); self.guard(page); humanize.human_scroll(page, 1200); page.wait_for_timeout(2500)
        best, seen = None, set()
        for a in page.locator("a[href*='/in/']").all()[:60]:
            try:
                url = (a.get_attribute("href") or "").split("?")[0]
                if not url or url in seen: continue
                seen.add(url)
                card = a.evaluate("e => (e.closest('li') || e.parentElement.parentElement).innerText")
                lines = [l.strip() for l in card.split("\n") if l.strip()]
                if len(lines) < 2 or "works here" in lines[0].lower(): continue
                name = lines[0]
                deg = next((l for l in lines if re.search(r"\b(1st|2nd|3rd)\b", l)), "")
                # headline = first line after the degree marker that is not another degree line
                idx = max((i for i, l in enumerate(lines) if re.search(r"\b(1st|2nd|3rd)\b", l)), default=0)
                headline = next((l for l in lines[idx + 1:] if not re.search(r"mutual connection|^connect$|^message$|^follow$", l, re.I)), "")
                if not headline or not self.TITLE_RX.search(headline): continue
                if not self._mentions(headline, company) and not strong_slug: continue   # never a look-alike company
                can_connect = any(l.strip().lower() == "connect" for l in lines)
                cand = (name, headline[:120], url if url.startswith("http") else "https://www.linkedin.com" + url)
                if can_connect: return cand
                best = best or cand
            except Exception:
                continue
        return best

    def _attach_person(self, oid, cid, name, title, url):
        from backend.app.models import Contact, Outreach
        with session() as db:
            o = db.get(Outreach, oid)
            c = db.get(Contact, cid) if cid else None
            if c and not c.linkedin_url and (c.name in ("Hiring team", None) or not c.name):
                c.name, c.title, c.linkedin_url, c.source = name, title, url, "linkedin_search"
            else:
                from backend.app.models import Job
                j = db.get(Job, o.job_id) if o.job_id else None
                c = Contact(company=j.company if j else "", name=name, title=title, linkedin_url=url, source="linkedin_search"); db.add(c); db.flush()
                o.contact_id = c.id
            if o.body and name and "Hi," in o.body: o.body = o.body.replace("Hi,", f"Hi {name.split()[0]},", 1)

    # ---------------------------------------------------------------- connect / dm
    def _outreach(self, channel: str, limit: int):
        with session() as db:
            rows = db.query(Outreach).filter(Outreach.channel == channel, Outreach.status == "approved").order_by(Outreach.created_at).limit(limit).all()
            return [(o.id, db.get(Contact, o.contact_id).linkedin_url if o.contact_id else None, o.body) for o in rows]

    def connect(self, page, limit):
        """Send a connection request with a personalised note (<=200 chars, LinkedIn's free-account limit). Screenshots the
        filled dialog before sending and the result after, so every send is verifiable in the Outreach view."""
        for oid, url, note in self._outreach("linkedin_connect", limit):
            if self.ctx.should_stop() or not url: continue
            if not self.take("connects", url): break
            page.goto(url, wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
            try: page.wait_for_selector("main h1", timeout=15000)
            except Exception: pass
            head = page.locator("main").first.inner_text(timeout=5000)[:600].lower()
            if "pending" in head and page.locator("main button:has-text('Pending')").count():
                self._set_outreach(oid, "skipped", "invitation already pending"); self.ctx.bump("pending"); continue
            if page.locator("main button:has-text('Message')").count() and not page.locator("main button:has-text('Connect')").count() and "1st" in head:
                self._set_outreach(oid, "skipped", "already connected; use dm mode"); self.ctx.bump("connected"); continue
            # Identify the profile owner from the top card's own Follow/Message buttons, then use the name-specific invite control
            # ("Invite <Name> to connect") so sidebar 'People also viewed' buttons can never be hit by mistake.
            follow = page.locator("main button[aria-label^='Follow '], main button[aria-label^='Unfollow ']").first
            pname = re.sub(r"^(Un)?[Ff]ollow ", "", follow.get_attribute("aria-label") or "").strip() if follow.count() else ""
            if not pname:
                pname = re.split(r"\s+[|\-–]\s+", page.title())[0].strip()      # "Ponnappa PM | LinkedIn"
                pname = "" if pname.lower() in ("linkedin", "") else pname
            if pname and follow.count():
                card = follow.locator("xpath=ancestor::*[.//button[@aria-label='More'] or .//button[@aria-label='More actions']][1]")
            else:
                card = page.locator("main").first
            btn = (page.locator(f"main [aria-label='Invite {pname} to connect']").filter(visible=True).first if pname
                   else page.locator("main [aria-label^='Invite ']").filter(visible=True).first)
            if not btn.count():
                more = card.locator("button[aria-label='More'], button[aria-label='More actions']").first
                if more.count():
                    humanize.human_click(page, more); time.sleep(1.3)
                    btn = (page.locator(f"[aria-label='Invite {pname} to connect'], [role='menu'] a[role='menuitem']:has-text('Connect')").filter(visible=True).first
                           if pname else page.locator("[role='menu'] a[role='menuitem']:has-text('Connect')").first)
            if not btn.count():
                shot = self.ctx.screenshot(page, f"li_connect_{oid}_nobutton")
                self._set_outreach(oid, "failed", "no Connect button (Follow-only profile or already pending)"); self.log("warn", f"linkedin: no Connect button on {url}", screenshot=shot); continue
            humanize.human_click(page, btn); time.sleep(1.5)
            dialog = page.locator("div[role='dialog']").first
            if dialog.count() and dialog.inner_text(timeout=3000).lower().count("email") and dialog.locator("input[type='email'], input[name='email']").count():
                page.keyboard.press("Escape"); self._set_outreach(oid, "failed", "LinkedIn asks for the person's email to connect"); self.ctx.bump("needs_email"); continue
            add = page.locator("button[aria-label='Add a note']")
            noted = False
            if add.count() and note:
                humanize.human_click(page, add.first); time.sleep(1)
                box = page.locator("textarea[name='message'], textarea#custom-message").first
                if box.count():
                    humanize.human_type(page, box, note[:200]); noted = True
                elif re.search(r"limit|premium|no more personalized|out of", (dialog.inner_text(timeout=2000) if dialog.count() else "").lower()):
                    self.log("warn", "linkedin: monthly personalised-note limit reached; sending without a note")
            shot_before = self.ctx.screenshot(page, f"li_connect_{oid}_before")
            send = page.locator("button[aria-label='Send now'], button[aria-label='Send invitation'], button[aria-label='Send without a note'], div[role='dialog'] button:has-text('Send')").first
            if send.count():
                humanize.human_click(page, send); time.sleep(2)
                shot_after = self.ctx.screenshot(page, f"li_connect_{oid}_after")
                self._set_outreach(oid, "sent", None if noted else "sent without note (note box unavailable)")
                self.log("info", f"linkedin: invitation sent to {url} (note={'yes' if noted else 'no'})", screenshot=shot_after); self.ctx.bump("sent")
            else:
                self._set_outreach(oid, "failed", "no Send button"); self.log("warn", f"linkedin: no Send button for {url}", screenshot=shot_before)
            humanize.pause("between_items_s")

    def dm(self, page, limit):
        for oid, url, body in self._outreach("linkedin_dm", limit):
            if self.ctx.should_stop() or not url: continue
            if not self.take("dms", url): break
            page.goto(url, wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
            msg = page.locator("main button:has-text('Message')").first
            if not msg.count():
                self._set_outreach(oid, "failed", "no Message button (not connected)"); continue
            humanize.human_click(page, msg); time.sleep(2)
            box = page.locator("div.msg-form__contenteditable[contenteditable='true']").first
            box.wait_for(timeout=8000)
            humanize.human_type(page, box, body)
            humanize.human_click(page, page.locator("button.msg-form__send-button").first); time.sleep(1.5)
            self._set_outreach(oid, "sent"); self.ctx.bump("sent")
            try: page.locator("button[data-control-name='overlay.close_conversation_window']").first.click(timeout=1500)
            except Exception: pass
            humanize.pause("between_items_s")

    def _set_outreach(self, oid, status, err=None):
        from datetime import datetime
        with session() as db:
            o = db.get(Outreach, oid); o.status = status; o.error = err
            if status == "sent": o.sent_at = datetime.utcnow()


SKILLS = {"linkedin": LinkedInSkill}
