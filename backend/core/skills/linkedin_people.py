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
from backend.core import humanize, llm, profile
from backend.core.skills.base import BaseSkill, SkillPaused

TITLE_RX = re.compile(r"recruit|talent|hiring|people (ops|partner|team)|head of people|founder|co-founder|\bceo\b|\bcto\b|"
                      r"engineering manager|head of engineering|vp,? engineering", re.I)
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
        {"find": self.find, "connect": self.connect, "message": self.message}[mode](page, limit, dry_run)

    # ------------------------------------------------------------------ find
    def find(self, page, limit, dry_run=False):
        with session() as db:
            rows = db.query(Outreach).filter(Outreach.channel == "linkedin_connect",
                                             Outreach.status.in_(["pending_review", "approved"])).order_by(Outreach.id).all()
            todo = []
            for o in rows:
                c = db.get(Contact, o.contact_id) if o.contact_id else None
                if c and c.linkedin_url: continue
                j = db.get(Job, o.job_id) if o.job_id else None
                if j: todo.append((o.id, o.contact_id, company_key(j.company)))
        cache: dict[str, tuple | None] = {}
        for oid, cid, company in todo[:limit]:
            if self.ctx.should_stop(): break
            if company not in cache:
                if not self.take("searches", company): break
                cache[company] = self.find_recruiter(page, company)
                humanize.pause("between_items_s")
            person = cache[company]
            if not person:
                self.log("warn", f"no verified recruiter found for {company}"); self.ctx.bump("not_found"); continue
            self.attach(oid, cid, *person)
            self.log("info", f"{company} -> {person[0]} ({person[1][:50]}) {person[2]}"); self.ctx.bump("found")

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
        """(name, headline, url) of a recruiter/talent/founder at `company`, or None. Company page first; global search second."""
        slug = self.company_slug(page, company)
        if slug:
            page.goto(f"https://www.linkedin.com/company/{slug}/people/?keywords=recruiter", wait_until="domcontentloaded")
            humanize.pause(); self.guard(page); humanize.human_scroll(page, 1200); page.wait_for_timeout(2000)
            hit = self._pick_card(page, company, on_company_page=True)
            if hit: return hit
        q = quote_plus(f'"{company}" recruiter OR "talent acquisition" OR "technical recruiter"')
        page.goto(f"https://www.linkedin.com/search/results/people/?keywords={q}&origin=GLOBAL_SEARCH_HEADER", wait_until="domcontentloaded")
        humanize.pause(); self.guard(page); humanize.human_scroll(page, 800)
        return self._pick_card(page, company, on_company_page=False)

    def _pick_card(self, page, company, on_company_page):
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
                idx = max((i for i, l in enumerate(lines) if re.search(r"\b(1st|2nd|3rd)\b", l)), default=0)
                headline = next((l for l in lines[idx + 1:] if not re.search(r"mutual connection|^connect$|^message$|^follow$", l, re.I)), "")
                if not headline or not TITLE_RX.search(headline): continue
                if not mentions(headline, company) and not on_company_page: continue   # global search: headline must name the company
                if on_company_page and re.search(r"freelance|ex-|former|independent", headline, re.I) and not mentions(headline, company): continue
                full = url if url.startswith("http") else "https://www.linkedin.com" + url
                cand = (name, headline[:120], full)
                if any(l.lower() == "connect" for l in lines): return cand      # 2nd degree: can connect straight away
                best = best or cand
            except Exception:
                continue
        return best

    def attach(self, oid, cid, name, title, url):
        with session() as db:
            o = db.get(Outreach, oid)
            c = db.get(Contact, cid) if cid else None
            if c and not c.linkedin_url:
                c.name, c.title, c.linkedin_url, c.source = name, title, url, "linkedin_people"
            else:
                j = db.get(Job, o.job_id) if o.job_id else None
                c = Contact(company=j.company if j else "", name=name, title=title, linkedin_url=url, source="linkedin_people")
                db.add(c); db.flush(); o.contact_id = c.id
            from backend.core.messages import set_body
            if set_body(o, self.draft_note(name, title, o.job_id), "note redrafted for the matched person"):
                self.log("info", f"outreach #{oid}: note changed after approval; back to review")

    def draft_note(self, name, title, job_id) -> str:
        with session() as db:
            j = db.get(Job, job_id) if job_id else None
            company, jtitle, desc = (j.company, j.title, (j.description or "")[:1500]) if j else ("", "", "")
        first = name.split()[0].title()
        prompt = (f"SEEKER PROFILE:\n{profile.as_text()[:2500]}\n\nRECRUITER: {name} ({title}) at {company}\nROLE: {jtitle}\n"
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
            if status == "sent": o.sent_at = datetime.utcnow()


SKILLS = {"linkedin_people": LinkedInPeopleSkill}
