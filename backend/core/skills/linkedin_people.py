"""LinkedIn people module: find the right recruiter for a job, connect with a personalised note, message once connected.

One skill, three modes (run one at a time):
  find     - for every approved linkedin_connect outreach row without a person: company page -> People tab -> a recruiter
             who verifiably works at that company (2nd-degree 'Connect' cards preferred). Never a look-alike company.
  connect  - open the profile, work out its state, click Connect wherever LinkedIn put it (top card, the '...' / 'More'
             menu), add the note (<=200 chars, the free-account limit), send. dry_run=True stops before Send.
  message  - for invitations that were accepted (profile shows 1st + Message): send the follow-up DM.
Every send is screenshotted before and after and linked from the run log.
"""
from __future__ import annotations
import re
import time
from urllib.parse import quote_plus
from datetime import datetime
from backend.app.db import session
from backend.app.models import Job, Contact, Outreach
from backend.core import config, humanize, llm, profile
from backend.core.skills.base import BaseSkill, SkillPaused

TITLE_RX = re.compile(r"recruit|talent|hiring|people (ops|partner|team)|head of people|founder|co-founder|\bceo\b|\bcto\b|"
                      r"engineering manager|head of engineering|vp,? engineering|"
                      r"software engineer|ai engineer|ml engineer|machine learning|forward deployed|solutions engineer|"
                      r"staff engineer|principal engineer|tech lead|team lead|director of engineering|head of ai|head of product", re.I)

# Who to approach, best first. One person per bucket beats ten recruiters: a hiring manager and a future teammate can
# each act on your message in a way a coordinator cannot.
ROLE_BUCKETS = [
    ("hiring manager", re.compile(r"engineering manager|head of engineering|director of engineering|vp,? engineering|head of ai|tech lead|team lead", re.I)),
    ("recruiter", re.compile(r"technical recruit|recruit|talent acquisition|talent partner|talent$", re.I)),
    ("founder", re.compile(r"founder|co-founder|\bceo\b|\bcto\b", re.I)),
    ("senior engineer", re.compile(r"staff engineer|principal engineer|senior (software|ai|ml) engineer|forward deployed", re.I)),
    ("engineer", re.compile(r"software engineer|ai engineer|ml engineer|machine learning|solutions engineer", re.I)),
    ("people team", re.compile(r"people (ops|partner|team)|head of people|hiring", re.I)),
]


def role_of(headline: str) -> str:
    for name, rx in ROLE_BUCKETS:
        if rx.search(headline or ""): return name
    return "other"


SEARCH_ANGLES = ["recruiter", "talent acquisition", "engineering manager", "forward deployed engineer",
                 "AI engineer", "software engineer", "founder"]
STOP = {"inc", "ltd", "llc", "the", "and", "com", "corp", "labs", "group", "technologies", "technology", "software",
        "solutions", "consultants", "corporation", "company", "co", "gmbh", "pvt", "private", "limited"}
NOTE_SYSTEM = ("You write LinkedIn connection-request notes for a job seeker. Output ONLY the note text. Hard limit 190 characters "
               "total. Format: 'Hi <FirstName>, ' + one specific sentence linking the seeker's real experience to the named "
               "role/company + a short ask like 'Open to a quick chat?'. No hashtags, emojis, placeholders or quotes. Never invent facts.")
DM_SYSTEM = ("You write a short LinkedIn message (max 500 characters) from a job seeker to a recruiter who just accepted their "
             "connection request. Thank them briefly, name the role, give two concrete relevant facts from the profile, and ask "
             "for a short call or whether they can point to the right person. Plain text, no placeholders, no hashtags.")


def _norm(x: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (x or "").lower()).strip()


def company_key(company: str) -> str:
    """'Redica Systems4 - 7 Years Bangalore' (Hirist) -> 'Redica Systems'; strips brackets."""
    k = re.sub(r"\s*\d+\s*-\s*\d+\s*years.*$", "", company or "", flags=re.I)
    k = re.sub(r"\s*\(.*?\)\s*$", "", k).strip()
    return k or company


def mentions(text: str, company: str) -> bool:
    toks = [t for t in re.findall(r"[a-z0-9]+", company.lower()) if len(t) >= 3 and t not in STOP]
    low = _norm(text)
    return any(t in low for t in toks) if toks else False


class LinkedInPeopleSkill(BaseSkill):
    platform = "linkedin"
    needs_login = True

    def execute(self, page, mode: str = "find", limit: int = 10, dry_run: bool = False, **_):
        if mode == "email_found":
            self.email_the_found(); return
        {"find": self.find, "connect": self.connect, "message": self.message}[mode](page, limit, dry_run)

    # ------------------------------------------------------------------ find
    def find(self, page, limit, dry_run=False):
        """Find the people worth approaching at each company, and make one message per person.

        Before this, a company with twelve open roles produced twelve messages all aimed at the same recruiter, of which
        the duplicate guard let exactly one through. Now the unit is the person: each company is searched once, up to
        `people_per_company` people are kept across different roles, and every one of them gets a single note about the
        best-fitting role there. Surplus per-job rows are stopped rather than left to look like pending work.
        """
        want = int((config.load().get("outreach") or {}).get("people_per_company", 10))
        with session() as db:
            rows = db.query(Outreach).filter(Outreach.channel == "linkedin_connect",
                                             Outreach.status.in_(["pending_review", "approved"])).order_by(Outreach.id).all()
            groups: dict[str, dict] = {}
            for o in rows:
                j = db.get(Job, o.job_id) if o.job_id else None
                if not j: continue
                c = db.get(Contact, o.contact_id) if o.contact_id else None
                key = company_key(j.company)
                g = groups.setdefault(key, {"company": j.company, "rows": [], "linked": set(), "best": (None, -1)})
                g["rows"].append(o.id)
                if c and c.linkedin_url: g["linked"].add(c.linkedin_url)
                if (j.fit_score or 0) > g["best"][1]: g["best"] = (j.id, j.fit_score or 0)
            # people already approached anywhere, so nobody is contacted twice
            contacted = {url for (url,) in db.query(Contact.linkedin_url)
                         .join(Outreach, Outreach.contact_id == Contact.id)
                         .filter(Contact.linkedin_url.isnot(None),
                                 Outreach.status.in_(["sent", "sending", "replied", "submission_unverified"])).all()}

        todo = [(key, g) for key, g in groups.items() if len(g["linked"]) < want]
        for key, g in todo[:limit]:
            if self.ctx.should_stop(): break
            company, job_id, spare_rows = g["company"], g["best"][0], list(g["rows"])
            people = self.find_people(page, key, want=want)
            fresh = [p for p in people if p[2] not in g["linked"] and p[2] not in contacted]
            if not fresh:
                self.log("warn", f"no new people found for {company}"); self.ctx.bump("not_found"); continue
            self.log("info", f"{company}: {len(fresh)} person(s) — " + ", ".join(f"{p[0]} ({p[3]})" for p in fresh[:6]))
            for person in fresh:
                name, headline, url, role = person
                contacted.add(url)
                if dry_run: continue
                oid = spare_rows.pop(0) if spare_rows else self._new_row(job_id)
                self._point_at(oid, job_id)      # every note is about the strongest role at that company
                self.attach(oid, None, name, headline, url)
                self.ctx.bump("found")
            for leftover in spare_rows:                       # more roles than people: those rows have no one to go to
                self._set(leftover, "stopped", "one message per person: this company's contacts are covered by other rows")
            humanize.pause("between_items_s")

    def _point_at(self, oid: int, job_id):
        with session() as db:
            o = db.get(Outreach, oid)
            if job_id and o.job_id != job_id: o.job_id = job_id

    def email_the_found(self, ctx=None) -> int:
        """Draft an email to any person we found on LinkedIn whose address we also know.

        LinkedIn gives the person and the role; the address comes from the employer's site or the posting. Without an
        address there is no email row at all, because guessed addresses bounce and damage the domain.
        """
        from backend.core.outreach import EMAIL_SYSTEM
        drafted = 0
        with session() as db:
            people = db.query(Contact).filter(Contact.linkedin_url.isnot(None), Contact.email.isnot(None),
                                              Contact.email_confidence == "found").all()
            todo = [(c.id, c.name, c.title, c.company, c.email) for c in people
                    if not db.query(Outreach).filter_by(contact_id=c.id, channel="email").count()]
        for cid, name, title, company, email in todo:
            with session() as db:
                row = db.query(Outreach).filter_by(contact_id=cid, channel="linkedin_connect").first()
                job = db.get(Job, row.job_id) if row and row.job_id else None
                if not job: continue
                jt = f"Company: {job.company}\nTitle: {job.title}\nURL: {job.url}\n\n{(job.description or '')[:4000]}"
                jid = job.id
            try:
                out = llm.complete_json("outreach", f"PROFILE:\n{profile.as_text()}\n\nCONTACT: {name} ({title}, {email})\n\nJOB:\n{jt}",
                                        EMAIL_SYSTEM, use_cache=False)
            except Exception as e:  # noqa: BLE001
                self.log("warn", f"email draft failed for {name} at {company}: {str(e)[:90]}"); continue
            with session() as db:
                db.add(Outreach(job_id=jid, contact_id=cid, channel="email", step=1,
                                subject=out.get("subject"), body=out.get("body"), status="pending_review"))
            drafted += 1
        self.log("info", f"drafted {drafted} email(s) to people found on LinkedIn")
        return drafted

    def _new_row(self, job_id) -> int:
        with session() as db:
            o = Outreach(job_id=job_id, channel="linkedin_connect", step=1, status="pending_review")
            db.add(o); db.flush(); return o.id

    def company_slug(self, page, company: str) -> str | None:
        page.goto(f"https://www.linkedin.com/search/results/companies/?keywords={quote_plus(company)}", wait_until="domcontentloaded")
        humanize.pause(); self.guard(page)
        comp = re.sub(r"[^a-z0-9]", "", company.lower())
        slugs = []
        for a in page.locator("a[href*='/company/']").all()[:12]:
            m = re.search(r"/company/([^/?]+)", a.get_attribute("href") or "")
            if m and m.group(1) not in slugs: slugs.append(m.group(1))
        for sl in slugs:
            n = re.sub(r"[^a-z0-9]", "", sl.lower())
            if n == comp or n.startswith(comp) or (len(comp) >= 5 and comp in n): return sl
        return None

    def find_recruiter(self, page, company: str):
        """The single best contact at `company`; kept for callers that only need one."""
        people = self.find_people(page, company, want=1)
        return people[0][:3] if people else None

    def find_people(self, page, company: str, want: int = 10) -> list[tuple]:
        """Up to `want` people at `company`, as (name, headline, url, role), spread across roles rather than ten recruiters.

        Searches the company's own People tab first (most reliable for employment), then a few global searches by angle.
        Each search costs one unit of the daily search cap, and stops early once enough distinct roles are covered.
        """
        found: dict[str, tuple] = {}                      # profile url -> (name, headline, url, role)

        def harvest(on_company_page: bool):
            for name, headline, url in self._pick_cards(page, company, on_company_page):
                if url in found: continue
                found[url] = (name, headline, url, role_of(headline))

        slug = self.company_slug(page, company)
        if slug:
            for keywords in ("", "recruiter", "engineering manager", "engineer"):
                if len(found) >= want or self.ctx.should_stop(): break
                if not self.take("searches", f"{company}:{keywords or 'all'}"): break
                page.goto(f"https://www.linkedin.com/company/{slug}/people/?keywords={quote_plus(keywords)}",
                          wait_until="domcontentloaded")
                humanize.pause(); self.guard(page)
                humanize.human_scroll(page, 1400); page.wait_for_timeout(1800)
                harvest(on_company_page=True)
        for angle in SEARCH_ANGLES:
            if len(found) >= want or self.ctx.should_stop(): break
            if not self.take("searches", f"{company}:{angle}"): break
            q = quote_plus(f'"{company}" {angle}')
            page.goto(f"https://www.linkedin.com/search/results/people/?keywords={q}&origin=GLOBAL_SEARCH_HEADER",
                      wait_until="domcontentloaded")
            humanize.pause(); self.guard(page); humanize.human_scroll(page, 900)
            harvest(on_company_page=False)
            humanize.pause("between_items_s")

        # one per role first, so the list is not ten recruiters, then fill up by role priority
        order = [name for name, _ in ROLE_BUCKETS] + ["other"]
        ranked, used_roles = [], set()
        for role in order:
            for person in found.values():
                if person[3] == role and person[2] not in [p[2] for p in ranked]:
                    ranked.append(person); used_roles.add(role); break
        for role in order:
            for person in found.values():
                if len(ranked) >= want: break
                if person[3] == role and person[2] not in [p[2] for p in ranked]:
                    ranked.append(person)
        return ranked[:want]

    def _pick_card(self, page, company, on_company_page):
        hits = self._pick_cards(page, company, on_company_page)
        return hits[0] if hits else None

    def _pick_cards(self, page, company, on_company_page) -> list[tuple]:
        """Every plausible person on the current results page, connectable ones first."""
        connectable, rest, seen = [], [], set()
        for a in page.locator("a[href*='/in/']").all()[:60]:
            try:
                url = (a.get_attribute("href") or "").split("?")[0]
                if not url or url in seen: continue
                seen.add(url)
                card = a.evaluate("e => (e.closest('li') || e.parentElement.parentElement).innerText")
                lines = [l.strip() for l in card.split("\n") if l.strip()]
                if len(lines) < 2 or "works here" in lines[0].lower(): continue
                name = lines[0]
                idx = max((i for i, l in enumerate(lines) if re.search(r"\b(1st|2nd|3rd)\b", l)), default=0)
                headline = next((l for l in lines[idx + 1:] if not re.search(r"mutual connection|^connect$|^message$|^follow$", l, re.I)), "")
                if not headline or not TITLE_RX.search(headline): continue
                if not mentions(headline, company) and not on_company_page: continue   # global search: headline must name the company
                if on_company_page and re.search(r"freelance|ex-|former|independent", headline, re.I) and not mentions(headline, company): continue
                full = url if url.startswith("http") else "https://www.linkedin.com" + url
                cand = (name, headline[:120], full)
                if any(l.lower() == "connect" for l in lines):
                    connectable.append(cand)                                    # 2nd degree: can connect straight away
                else:
                    rest.append(cand)
            except Exception:
                continue
        return connectable + rest

    def attach(self, oid, cid, name, title, url):
        with session() as db:
            o = db.get(Outreach, oid)
            existing = db.query(Contact).filter_by(linkedin_url=url).first()
            c = db.get(Contact, cid) if cid else None
            if existing:
                o.contact_id = existing.id; c = existing
            elif c and not c.linkedin_url:
                c.name, c.title, c.linkedin_url, c.source = name, title, url, "linkedin_people"
            else:
                j = db.get(Job, o.job_id) if o.job_id else None
                c = Contact(company=j.company if j else "", name=name, title=title, linkedin_url=url, source="linkedin_people")
                db.add(c); db.flush(); o.contact_id = c.id
            from backend.core.messages import set_body
            if set_body(o, self.draft_note(name, title, o.job_id), "note redrafted for the matched person"):
                self.log("info", f"outreach #{oid}: note changed after approval; back to review")

    def draft_note(self, name, title, job_id) -> str:
        """A note written for this person: an engineer is asked what the work is really like, a manager about the role."""
        with session() as db:
            j = db.get(Job, job_id) if job_id else None
            company, jtitle, desc = (j.company, j.title, (j.description or "")[:1500]) if j else ("", "", "")
        first = name.split()[0].title()
        role = role_of(title)
        angle = {"recruiter": "ask about the hiring process for the role",
                 "people team": "ask about the hiring process for the role",
                 "hiring manager": "speak to the problem the team is solving and ask for a short chat",
                 "founder": "speak to the business outcome you can own and ask for a short chat",
                 "senior engineer": "ask what the work is actually like day to day",
                 "engineer": "ask what the work is actually like day to day",
                 "other": "ask for a short chat"}[role]
        prompt = (f"SEEKER PROFILE:\n{profile.as_text()[:2500]}\n\nPERSON: {name} ({title}) at {company} — treat them as a "
                  f"{role}, so {angle}.\nROLE THEY ARE HIRING FOR: {jtitle}\n"
                  f"JOB SNIPPET: {desc}\n\nWrite the note (<=190 chars) starting with 'Hi {first},'.")
        note = ""
        for _ in range(3):
            note = re.sub(r"\s+", " ", llm.complete("outreach", prompt, NOTE_SYSTEM, use_cache=False).strip().strip('"'))
            if len(note) <= 200 and note.lower().startswith("hi"): break
            prompt += "\nTOO LONG. Max 190 characters."
        if len(note) > 200: note = note[:197].rsplit(" ", 1)[0] + "..."
        return note

    # ------------------------------------------------------------------ profile state
    def top_card(self, page):
        """The profile owner's header block: the ancestor of the 'Contact info' link that also holds the action buttons."""
        anchor = page.locator("main a:has-text('Contact info'), main #top-card-text-details-contact-info").first
        if anchor.count():
            card = anchor.locator("xpath=ancestor::*[.//button[@aria-label='More' or @aria-label='More actions']][1]")
            if card.count(): return card
        return page.locator("main section").first if page.locator("main section").count() else page.locator("main").first

    def open_profile(self, page, url) -> dict:
        """Load a profile and describe it: {name, degree, state, card} where state is one of
        connected | pending | can_connect | connect_in_menu."""
        page.goto(url, wait_until="domcontentloaded"); humanize.pause(); self.guard(page)
        page.wait_for_timeout(2500)
        name = re.split(r"\s+[|\-–]\s+", page.title())[0].strip()
        if name.lower() in ("", "linkedin"): name = ""
        card = self.top_card(page)
        text = card.inner_text(timeout=8000) if card.count() else ""
        lines = [l.strip() for l in text.split("\n") if l.strip()]
        deg = "1st" if re.search(r"\b1st\b", text) else "2nd" if re.search(r"\b2nd\b", text) else "3rd" if re.search(r"\b3rd", text) else ""
        if any(l == "Pending" for l in lines): state = "pending"
        elif deg == "1st": state = "connected"
        elif any(l == "Connect" for l in lines): state = "can_connect"
        else: state = "connect_in_menu"
        return {"name": name, "degree": deg, "state": state, "card": card}

    # ------------------------------------------------------------------ connect
    def connect(self, page, limit, dry_run=False):
        with session() as db:
            rows = [(o.id, c.linkedin_url, o.body) for o, c in
                    db.query(Outreach, Contact).join(Contact, Contact.id == Outreach.contact_id)
                      .filter(Outreach.channel == "linkedin_connect", Outreach.status == "approved", Contact.linkedin_url.isnot(None))
                      .order_by(Outreach.id).limit(limit * 3).all()]
        done_urls = set(); n = 0
        for oid, url, note in rows:
            if n >= limit or self.ctx.should_stop(): break
            if url in done_urls:
                self._set(oid, "skipped", "same person as an earlier row"); continue
            done_urls.add(url)
            if not self.take("connects", url): break
            n += 1
            info = self.open_profile(page, url); name = info["name"]
            if info["state"] == "pending":
                self._set(oid, "skipped", "invitation already pending"); self.ctx.bump("pending"); continue
            if info["state"] == "connected":
                self._set(oid, "skipped", "already connected; use message mode"); self.ctx.bump("connected"); continue
            card = info["card"]
            btn = card.locator("button:has-text('Connect'), a:has-text('Connect'), [role='button']:has-text('Connect')").filter(visible=True).first
            if not btn.count():
                more = card.locator("button[aria-label='More'], button[aria-label='More actions']").first
                if more.count():
                    self._click(page, more); time.sleep(1.3)
                    btn = page.locator(f"[aria-label='Invite {name} to connect'], [role='menu'] a[role='menuitem']:has-text('Connect'), [role='menu'] [role='menuitem']:has-text('Connect')").filter(visible=True).first
            if not btn.count():
                shot = self.ctx.screenshot(page, f"li_{oid}_noconnect")
                self._set(oid, "failed", "no Connect control on card or in menu (email-gated or follow-only profile)")
                self.log("warn", f"{name or url}: no Connect control", screenshot=shot); self.ctx.bump("no_button"); continue
            # the invite modal: LinkedIn's current markup has no role=dialog, so key off its own buttons
            add = page.locator("button[aria-label='Add a note'], button:has-text('Add a note')").filter(visible=True).first
            send_wo = page.locator("button[aria-label='Send without a note'], button[aria-label='Send now'], button[aria-label='Send invitation']").filter(visible=True).first
            for attempt in range(2):                   # the first click sometimes lands while the page is still settling
                self._click(page, btn); time.sleep(2)
                for _ in range(4):
                    if add.count() or send_wo.count(): break
                    time.sleep(1)
                if add.count() or send_wo.count(): break
                time.sleep(2)
            modal = page.locator("[role='dialog'], [aria-modal='true'], .artdeco-modal").filter(visible=True).first
            mtxt = modal.inner_text(timeout=2000).lower() if modal.count() else ""
            if page.locator("[aria-modal='true'] input[type='email'], [role='dialog'] input[type='email']").filter(visible=True).count():
                page.keyboard.press("Escape"); self._set(oid, "failed", "LinkedIn requires this person's email to connect"); self.ctx.bump("needs_email"); continue
            if not (add.count() or send_wo.count()):
                shot = self.ctx.screenshot(page, f"li_{oid}_nomodal")
                self._set(oid, "failed", "invite modal did not open"); self.log("warn", f"{name}: invite modal did not open", screenshot=shot); continue
            noted = False
            if add.count() and note and not getattr(self, "notes_exhausted", False):
                self._click(page, add); time.sleep(1.5)
                box = page.locator("textarea[name='message'], textarea#custom-message, textarea").filter(visible=True).first
                if box.count():
                    humanize.human_type(page, box, note[:200]); noted = True
                else:
                    mtxt = page.locator("[aria-modal='true'], [role='dialog'], .artdeco-modal").filter(visible=True).first.inner_text(timeout=2000).lower() if page.locator("[aria-modal='true'], [role='dialog'], .artdeco-modal").filter(visible=True).count() else ""
                    if re.search(r"limit|premium|personali[sz]ed|out of", mtxt):
                        self.log("warn", "monthly personalised-note limit reached; sending without a note")
                        self.notes_exhausted = True
                        x = page.locator("[aria-modal='true'] button[aria-label='Dismiss'], [role='dialog'] button[aria-label='Dismiss'], button[aria-label='Dismiss']").filter(visible=True).first
                        if x.count(): self._click(page, x); time.sleep(1.2)
                        if not send_wo.count():                                 # the upsell closed the invite sheet too: reopen it
                            page.keyboard.press("Escape"); time.sleep(0.8)
                            btn = card.locator("button:has-text('Connect'), a:has-text('Connect'), [role='button']:has-text('Connect')").filter(visible=True).first
                            if not btn.count():
                                more = card.locator("button[aria-label='More'], button[aria-label='More actions']").first
                                if more.count():
                                    self._click(page, more); time.sleep(1.3)
                                    btn = page.locator(f"[aria-label='Invite {name} to connect'], [role='menu'] [role='menuitem']:has-text('Connect')").filter(visible=True).first
                            if btn.count(): self._click(page, btn); time.sleep(2.5)
            shot_before = self.ctx.screenshot(page, f"li_{oid}_before")
            if dry_run:
                page.keyboard.press("Escape"); self.log("info", f"DRY RUN: would send invite to {name} (note={'yes' if noted else 'no'})", screenshot=shot_before)
                self.ctx.bump("dry_run"); continue
            send = page.locator("button[aria-label='Send now'], button[aria-label='Send invitation'], button[aria-label='Send without a note'], button[aria-label='Send'], [aria-modal='true'] button:has-text('Send')").filter(visible=True).first
            if not send.count():
                self._set(oid, "failed", "no Send button"); self.log("warn", f"{name}: no Send button", screenshot=shot_before); continue
            from backend.core.messages import claim
            self.guard(page)
            if not claim(oid, self.ctx.run_id): continue
            self._click(page, send); time.sleep(2)
            shot_after = self.ctx.screenshot(page, f"li_{oid}_after")
            ok = "invitation sent" in (page.inner_text("body", timeout=3000).lower()) or page.locator("main button:has-text('Pending')").count() > 0
            self._set(oid, "sent" if ok else "submission_unverified", None if ok else "no 'Invitation sent' confirmation")
            with session() as db:                       # tracking: what exactly went out
                db.get(Outreach, oid).subject = "invite with note" if noted else "invite without note (quota exhausted)"
            self.log("info" if ok else "warn", f"{'invitation sent to' if ok else 'unconfirmed send to'} {name} (note={'yes' if noted else 'no'})", screenshot=shot_after)
            self.ctx.bump("sent" if ok else "unverified")
            humanize.pause("between_items_s")

    # ------------------------------------------------------------------ message (after acceptance)
    def message(self, page, limit, dry_run=False):
        """Sent invitations whose profile is now 1st-degree get one follow-up DM (creates a linkedin_dm row and sends it)."""
        with session() as db:
            rows = [(o.id, o.job_id, o.contact_id, c.linkedin_url, c.name, c.title)
                    for o, c in db.query(Outreach, Contact).join(Contact, Contact.id == Outreach.contact_id)
                      .filter(Outreach.channel == "linkedin_connect", Outreach.status == "sent", Contact.linkedin_url.isnot(None)).order_by(Outreach.sent_at).all()
                    if not db.query(Outreach).filter(Outreach.channel == "linkedin_dm", Outreach.contact_id == o.contact_id, Outreach.status.in_(["sent", "sending", "replied"])).count()]
        n = 0
        for oid, jid, cid, url, name, title in rows:
            if n >= limit or self.ctx.should_stop(): break
            info = self.open_profile(page, url)
            if info["state"] != "connected":
                continue                                    # not accepted yet
            with session() as db:
                draft = db.query(Outreach).filter_by(channel="linkedin_dm", contact_id=cid).first()
                ready = bool(draft and draft.status == "approved")
                rid, body = (draft.id, draft.body) if draft else (None, None)
            if rid is None:
                body = self.draft_dm(name, title, jid)
                with session() as db: db.add(Outreach(job_id=jid, contact_id=cid, channel="linkedin_dm", step=2, body=body, status="pending_review"))
                self.ctx.bump("drafted"); continue
            if not ready: continue
            if not self.take("dms", url): break      # the daily cap is spent on sends, never on drafts
            n += 1
            msg = info["card"].locator("button:has-text('Message'), a:has-text('Message')").filter(visible=True).first
            if not msg.count():
                self.log("warn", f"{name}: connected but no Message button"); continue
            self._click(page, msg); time.sleep(2)
            box = page.locator("div.msg-form__contenteditable[contenteditable='true'], div[role='textbox'][contenteditable='true']").first
            try: box.wait_for(timeout=8000)
            except Exception:
                self.log("warn", f"{name}: message box did not open"); continue
            humanize.human_type(page, box, body)
            shot = self.ctx.screenshot(page, f"li_dm_{cid}_before")
            if dry_run:
                self.log("info", f"DRY RUN: would DM {name}", screenshot=shot); self.ctx.bump("dry_run"); page.keyboard.press("Escape"); continue
            from backend.core.messages import claim, visible_message
            if self.ctx.should_stop() or not claim(rid, self.ctx.run_id): continue
            self.guard(page)
            humanize.human_click(page, page.locator("button.msg-form__send-button, button[type='submit']:has-text('Send')").first); time.sleep(1.5)
            if not visible_message(page, body, ".msg-s-event-listitem__body, .msg-s-message-list__event .msg-s-event-listitem__message-bubble"):
                self._set(rid, "submission_unverified", "Message not visible in conversation; reconcile before retry"); continue
            self._set(rid, "sent"); self.log("info", f"DM sent to {name}", screenshot=self.ctx.screenshot(page, f"li_dm_{cid}_after")); self.ctx.bump("sent")
            try: page.locator("button[data-control-name='overlay.close_conversation_window'], button[aria-label*='Close your conversation']").first.click(timeout=1500)
            except Exception: pass
            humanize.pause("between_items_s")

    def draft_dm(self, name, title, job_id) -> str:
        with session() as db:
            j = db.get(Job, job_id) if job_id else None
            company, jtitle, desc = (j.company, j.title, (j.description or "")[:1500]) if j else ("", "", "")
        first = (name or "").split()[0].title()
        prompt = (f"SEEKER PROFILE:\n{profile.as_text()[:2500]}\n\nRECRUITER: {name} ({title}) at {company}\nROLE: {jtitle}\nJOB SNIPPET: {desc}\n\n"
                  f"Write the message starting with 'Hi {first},'.")
        body = llm.complete("outreach", prompt, DM_SYSTEM, use_cache=False).strip().strip('"')
        return body[:500]

    @staticmethod
    def _click(page, locator):
        """Human-looking approach (scroll + curved mouse move), then a real DOM click: LinkedIn's Connect/Message controls are
        links whose handlers ignore synthetic mouse-down/up pairs."""
        try:
            locator.scroll_into_view_if_needed(); box = locator.bounding_box()
            if box: humanize.human_move(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        except Exception:
            pass
        time.sleep(0.3); locator.click()

    def _set(self, oid, status, err=None):
        with session() as db:
            o = db.get(Outreach, oid); o.status = status; o.error = err
            if status == "sent":
                o.sent_at = datetime.utcnow()
                self._mark_contacted(db, o)

    @staticmethod
    def _mark_contacted(db, outreach):
        """For a board we cannot apply through, reaching a person IS the application: record it as one, with the
        invitation as the evidence. Ordinary applications are left alone; a form submission is what completes those."""
        from backend.app.models import Application
        if not outreach.job_id or outreach.channel != "linkedin_connect": return
        app = db.query(Application).filter_by(job_id=outreach.job_id, method="outreach").first()
        if not app or app.status not in ("approved", "pending_review", "needs_human"): return
        app.status = "submitted"; app.submitted_at = datetime.utcnow(); app.job.status = "applied"
        app.confirmation_text = f"LinkedIn invitation sent to the contact for this role (outreach #{outreach.id}); this board charges to apply through it."


SKILLS = {"linkedin_people": LinkedInPeopleSkill}
