"""X (Twitter) outreach skill. One skill, three modes (run one at a time):

  find   - for every approved/pending outreach row whose contact has no x_handle: search X for the company's own
           account (or a recruiter who verifiably works there) and store the handle on the Contact.
           A candidate is accepted only when the display name or the bio actually names the company.
  dm     - open the contact's profile, start a DM, type a short personalised message drafted by the LLM,
           screenshot, send. dry_run=True stops just before the Send click.
  reply  - search recent hiring tweets matching the target roles, open the reply composer, type a short reply,
           screenshot, post. dry_run=True stops just before the Reply click.

Every send is screenshotted before and after and linked from the run log. Nothing is marked "sent" unless the
text is verifiably in the thread afterwards.
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

STOP = {"inc", "ltd", "llc", "the", "and", "com", "corp", "labs", "group", "technologies", "technology", "software",
        "solutions", "consultants", "corporation", "company", "co", "gmbh", "pvt", "private", "limited", "systems"}
ROLE_TITLE_RX = re.compile(r"recruit|talent|hiring|people (ops|partner|team)|head of people|founder|co-?founder|"
                           r"\bceo\b|\bcto\b|engineering manager|head of engineering|vp,? engineering", re.I)
BAD_ACCOUNT_RX = re.compile(r"(\bfan\b|fans\b|parody|unofficial|not affiliated|\bmemes?\b|\bbot\b)", re.I)

SEARCH_QUERIES = [
    '"forward deployed engineer" hiring',
    '"applied AI engineer" hiring',
    '"LLM engineer" hiring',
    '"we are hiring" ("AI engineer" OR "forward deployed")',
]

DM_SYSTEM = ("You write a short X (Twitter) direct message from a job seeker to a company account or recruiter. "
             "Output ONLY the message text. Hard limit 480 characters. Plain text: no hashtags, no emojis, no "
             "placeholders, no quotes, no markdown. Name the role, give two concrete relevant facts drawn only from "
             "the seeker profile, and end with a short ask (a quick chat, or who to talk to). Never invent facts.")
REPLY_SYSTEM = ("You write a short public reply to a hiring tweet, from a job seeker. Output ONLY the reply text. "
                "Hard limit 240 characters. Plain text: no hashtags, no emojis, no placeholders, no quotes. "
                "Reference what the tweet is actually hiring for, state interest in one line, and give one concrete "
                "relevant fact from the seeker profile. Never invent facts.")


def _norm(x: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (x or "").lower()).strip()


def company_key(company: str) -> str:
    """'Redica Systems4 - 7 Years Bangalore' -> 'Redica Systems'; strips trailing brackets."""
    k = re.sub(r"\s*\d+\s*-\s*\d+\s*years.*$", "", company or "", flags=re.I)
    k = re.sub(r"\s*\(.*?\)\s*$", "", k).strip()
    return k or (company or "")


def tokens(company: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (company or "").lower()) if len(t) >= 3 and t not in STOP]


def mentions(text: str, company: str) -> bool:
    toks = tokens(company)
    low = _norm(text)
    return any(t in low for t in toks) if toks else False


def handle_matches(handle: str, company: str) -> bool:
    h = re.sub(r"[^a-z0-9]", "", (handle or "").lower().lstrip("@"))
    c = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    return bool(h and c) and (h == c or h.startswith(c) or (len(c) >= 5 and c in h))


class XOutreachSkill(BaseSkill):
    platform = "x"
    needs_login = True

    def execute(self, page, mode: str = "find", limit: int = 10, dry_run: bool = False, **_):
        {"find": self.find, "dm": self.dm, "reply": self.reply}[mode](page, limit, dry_run)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _click(page, locator):
        """Human approach (scroll + curved mouse move) then a real DOM click: X's React controls ignore some
        synthetic mouse-down/up pairs (same problem LinkedIn had)."""
        try:
            try: locator.scroll_into_view_if_needed(timeout=3000)
            except Exception: pass
            box = locator.bounding_box()
            if box:
                humanize.human_move(page, box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        except Exception:
            pass
        time.sleep(0.3)
        locator.click()

    def _goto(self, page, url):
        page.goto(url, wait_until="domcontentloaded")
        humanize.pause()
        self.guard(page)
        page.wait_for_timeout(2500)

    def _set(self, oid, status, err=None):
        with session() as db:
            o = db.get(Outreach, oid)
            o.status = status
            o.error = err
            if status == "sent":
                o.sent_at = datetime.utcnow()

    # ------------------------------------------------------------------ find
    def find(self, page, limit, dry_run=False):
        """Companies with an approved/pending outreach row but no x_handle -> the company's X account."""
        with session() as db:
            rows = db.query(Outreach).filter(Outreach.status.in_(["pending_review", "approved"])).order_by(Outreach.id).all()
            todo, seen = [], set()
            for o in rows:
                c = db.get(Contact, o.contact_id) if o.contact_id else None
                if c and c.x_handle:
                    continue
                j = db.get(Job, o.job_id) if o.job_id else None
                company = company_key(j.company if j else (c.company if c else ""))
                if not company or company.lower() in seen:
                    continue
                seen.add(company.lower())
                todo.append((o.id, o.contact_id, company))
        if not todo:
            self.log("warn", "x: no outreach rows needing an X handle")
            return
        for oid, cid, company in todo[:limit]:
            if self.ctx.should_stop():
                break
            hit = self.find_account(page, company)
            if not hit:
                self.log("warn", f"x: no verified account found for {company}")
                self.ctx.bump("not_found")
                humanize.pause("between_items_s")
                continue
            handle, display, bio = hit
            self.attach(oid, cid, company, handle, display, bio)
            self.log("info", f"x: {company} -> {handle} ({display}) | {bio[:80]}")
            self.ctx.bump("found")
            humanize.pause("between_items_s")

    def find_account(self, page, company: str):
        """(handle, display_name, bio) of a verified account for `company`, or None.
        Company account first (People search on the company name), recruiter second."""
        cands = self.people_search(page, company)
        # 1) the company's own account: handle looks like the company, or display name names it
        for handle, display, bio in cands:
            if BAD_ACCOUNT_RX.search(bio) or BAD_ACCOUNT_RX.search(display) or BAD_ACCOUNT_RX.search(handle):
                continue
            if handle_matches(handle, company) and (mentions(display, company) or mentions(bio, company) or mentions(handle, company)):
                return (handle, display, bio)
        for handle, display, bio in cands:
            if BAD_ACCOUNT_RX.search(bio) or BAD_ACCOUNT_RX.search(display) or BAD_ACCOUNT_RX.search(handle):
                continue
            if mentions(display, company) and (mentions(bio, company) or handle_matches(handle, company)):
                return (handle, display, bio)
        # 2) a recruiter/founder whose bio names the company
        for handle, display, bio in self.people_search(page, f"{company} recruiter"):
            if BAD_ACCOUNT_RX.search(bio):
                continue
            if ROLE_TITLE_RX.search(bio) and mentions(bio, company):
                return (handle, display, bio)
        return None

    def people_search(self, page, query: str) -> list[tuple[str, str, str]]:
        """X 'People' search results as (handle, display_name, bio)."""
        self._goto(page, f"https://x.com/search?q={quote_plus(query)}&src=typed_query&f=user")
        humanize.human_scroll(page, 600)
        out, seen = [], set()
        cells = page.locator("[data-testid='UserCell']")
        for i in range(min(cells.count(), 15)):
            try:
                cell = cells.nth(i)
                txt = cell.inner_text(timeout=3000)
                lines = [l.strip() for l in txt.split("\n") if l.strip()]
                handle = next((l for l in lines if l.startswith("@")), "")
                if not handle or handle in seen:
                    continue
                seen.add(handle)
                hi = lines.index(handle)
                display = lines[0] if hi else ""
                bio = " ".join(l for l in lines[hi + 1:] if l.lower() not in ("follow", "following", "followers"))
                out.append((handle, display, bio[:200]))
            except Exception:
                continue
        if not out:
            self.log("warn", f"x: no user results for '{query}'", screenshot=self.ctx.screenshot(page, "x_search_empty"))
        return out

    def attach(self, oid, cid, company, handle, display, bio):
        with session() as db:
            o = db.get(Outreach, oid)
            c = db.get(Contact, cid) if cid else None
            if c and not c.x_handle:
                c.x_handle = handle
                if not c.name:
                    c.name = display
            else:
                c = Contact(company=company, name=display or handle, title=bio[:200], x_handle=handle, source="x_outreach")
                db.add(c)
                db.flush()
                if not o.contact_id:
                    o.contact_id = c.id

    # ------------------------------------------------------------------ dm
    def dm(self, page, limit, dry_run=False):
        with session() as db:
            rows = [(c.id, c.x_handle, c.name, c.company, c.title,
                     db.query(Outreach).filter(Outreach.contact_id == c.id).order_by(Outreach.id).first())
                    for c in db.query(Contact).filter(Contact.x_handle.isnot(None)).order_by(Contact.id).all()
                    if not db.query(Outreach).filter(Outreach.channel == "x_dm", Outreach.contact_id == c.id, Outreach.status.in_(["sent", "sending", "submission_unverified"])).count()]
            rows = [(cid, h, n, comp, t, (o.job_id if o else None)) for cid, h, n, comp, t, o in rows]
        if not rows:
            self.log("warn", "x: no contacts with an x_handle and no existing x_dm; run mode=find first")
            return
        n = 0
        for cid, handle, name, company, title, job_id in rows:
            if n >= limit or self.ctx.should_stop():
                break
            with session() as db:
                draft = db.query(Outreach).filter_by(channel="x_dm", contact_id=cid).first()
                rid, body, ready = (draft.id, draft.body, draft.status == "approved") if draft else (None, None, False)
            if rid is None:
                body = self.draft_dm(name, company, title, job_id)
                with session() as db: db.add(Outreach(job_id=job_id, contact_id=cid, channel="x_dm", step=1, body=body, status="pending_review"))
                self.ctx.bump("drafted"); continue
            if not ready: continue
            if not self.take("dms", handle):     # the cap is spent on approved sends only
                break
            n += 1
            self._goto(page, f"https://x.com/{handle.lstrip('@')}")
            btn = page.locator("[data-testid='sendDMFromProfile']").first
            if not btn.count():
                self.log("warn", f"x: {handle} does not accept DMs (no message button)",
                         screenshot=self.ctx.screenshot(page, f"x_dm_{cid}_nodm"))
                self.ctx.bump("no_dm_button")
                continue
            self._click(page, btn)
            time.sleep(2.5)
            self.guard(page)
            box = page.locator("[data-testid='dmComposerTextInput'], div[role='textbox'][contenteditable='true']").first
            try:
                box.wait_for(timeout=8000)
            except Exception:
                self.log("warn", f"x: DM composer did not open for {handle}",
                         screenshot=self.ctx.screenshot(page, f"x_dm_{cid}_nocomposer"))
                continue
            humanize.human_type(page, box, body)
            time.sleep(1)
            shot = self.ctx.screenshot(page, f"x_dm_{cid}_before")
            if dry_run:
                self.log("info", f"DRY RUN: would DM {handle} ({len(body)} chars): {body[:120]}", screenshot=shot)
                self.ctx.bump("dry_run")
                humanize.pause("between_items_s")
                continue
            send = page.locator("[data-testid='dmComposerSendButton']").first
            if not send.count():
                self._set(rid, "failed", "no send button")
                self.log("warn", f"x: no DM send button for {handle}", screenshot=shot)
                continue
            from backend.core.messages import claim
            self.guard(page)
            if not claim(rid, self.ctx.run_id): continue
            self._click(page, send)
            time.sleep(2.5)
            after = self.ctx.screenshot(page, f"x_dm_{cid}_after")
            ok = self.verify_in_thread(page, body)
            self._set(rid, "sent" if ok else "submission_unverified", None if ok else "message not visible in thread after send")
            self.log("info" if ok else "warn", f"x: {'DM sent to' if ok else 'unconfirmed DM to'} {handle}", screenshot=after)
            self.ctx.bump("sent" if ok else "unverified")
            humanize.pause("between_items_s")

    def verify_in_thread(self, page, body: str) -> bool:
        """The message really is in the conversation (match on a distinctive slice, whitespace-insensitive)."""
        from backend.core.messages import visible_message
        return visible_message(page, body, "[data-testid='messageEntry'], [data-testid='tweetText']")

    def draft_dm(self, name, company, title, job_id) -> str:
        with session() as db:
            j = db.get(Job, job_id) if job_id else None
            comp, jtitle, desc = (j.company, j.title, (j.description or "")[:1200]) if j else (company or "", "", "")
        prompt = (f"SEEKER PROFILE:\n{profile.as_text()[:2500]}\n\nRECIPIENT: {name or company} ({title or 'company account'}) "
                  f"at {comp or company}\nROLE: {jtitle}\nJOB SNIPPET: {desc}\n\nWrite the DM (max 480 characters).")
        return self._draft(prompt, DM_SYSTEM, 500)

    def _draft(self, prompt, system, hard_limit) -> str:
        text = ""
        for _ in range(3):
            text = re.sub(r"\s+", " ", llm.complete("outreach", prompt, system, use_cache=False).strip().strip('"'))
            text = re.sub(r"#\w+", "", text).strip()
            if text and len(text) <= hard_limit and "[" not in text:
                return text
            prompt += f"\nTOO LONG OR HAS PLACEHOLDERS. Max {hard_limit - 20} characters, plain text only."
        return text[:hard_limit - 3].rsplit(" ", 1)[0] + "..." if len(text) > hard_limit else text

    # ------------------------------------------------------------------ reply
    def reply(self, page, limit, dry_run=False):
        with session() as db:
            pending = [(o.id, o.thread_id, o.body) for o in db.query(Outreach).filter_by(channel="x_reply", status="approved").limit(limit).all()]
            seen = {o.thread_id for o in db.query(Outreach).filter_by(channel="x_reply").all()}
        from backend.core.messages import claim
        for oid, url, body in pending:
            if self.ctx.should_stop(): return
            self._goto(page, url)
            box = page.locator("[data-testid='tweetTextarea_0']").first
            if not box.count(): continue
            humanize.human_type(page, box, body)
            if dry_run: continue
            if not self.take("replies", url) or not claim(oid, self.ctx.run_id): continue
            self.guard(page)
            self._click(page, page.locator("[data-testid='tweetButtonInline'], [data-testid='tweetButton']").first)
            time.sleep(3)
            ok = self.verify_in_thread(page, body)
            self._set(oid, "sent" if ok else "submission_unverified")
            self.log("info", f"X reply {'verified' if ok else 'unverified'}", screenshot=self.ctx.screenshot(page, f"reply_{oid}"))
        count = 0
        for query in SEARCH_QUERIES:
            if self.ctx.should_stop() or count >= limit: break
            for url, author, tweet in self.search_tweets(page, query):
                if url in seen or count >= limit: continue
                body = self._draft(f"PROFILE:\n{profile.as_text()}\nUNTRUSTED HIRING POST:\n{tweet}", REPLY_SYSTEM, 240)
                with session() as db: db.add(Outreach(channel="x_reply", body=body, thread_id=url, subject=f"reply to {author}", status="pending_review"))
                seen.add(url); count += 1; self.ctx.bump("drafted")

    def search_tweets(self, page, query: str) -> list[tuple[str, str, str]]:
        """Recent tweets for `query` as (status_url, author_handle, text)."""
        self._goto(page, f"https://x.com/search?q={quote_plus(query)}&src=typed_query&f=live")
        humanize.human_scroll(page, 800)
        out, seen = [], set()
        arts = page.locator("article[data-testid='tweet']")
        for i in range(min(arts.count(), 20)):
            try:
                art = arts.nth(i)
                href = ""
                links = art.locator("a[href*='/status/']")
                for k in range(min(links.count(), 5)):
                    h = links.nth(k).get_attribute("href") or ""
                    if re.search(r"/status/\d+$", h):
                        href = h
                        break
                if not href:
                    continue
                url = "https://x.com" + href if href.startswith("/") else href
                if url in seen:
                    continue
                seen.add(url)
                author = (re.search(r"^/([^/]+)/status", href) or [None, ""])[1]
                text = art.locator("[data-testid='tweetText']").first
                body = text.inner_text(timeout=2000) if text.count() else ""
                if not body:
                    continue
                out.append((url, "@" + author, body[:600]))
            except Exception:
                continue
        if not out:
            self.log("warn", f"x: no tweets for '{query}'", screenshot=self.ctx.screenshot(page, "x_tweets_empty"))
        return out


SKILLS = {"x_outreach": XOutreachSkill}
