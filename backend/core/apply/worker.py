"""ATS apply worker (a BaseSkill). Takes Applications in status 'approved' (or 'pending_review' when review_mode is off and
score >= auto_submit_min_score), opens the apply URL in the persistent 'ats' browser profile, fills the form from the profile +
answer bank + tailored resume, screenshots before/after, and submits. Anything it cannot answer -> needs_human with a screenshot."""
from __future__ import annotations
import json, re, time
from datetime import datetime
from backend.app.db import session
from backend.app.models import Application, Job
from backend.core import browser, config, humanize
from backend.core.skills.base import BaseSkill, SkillPaused
from backend.core.apply import forms
from backend.core.pipeline import _job_text

SUBMIT_SELECTORS = ["button[type='submit']:visible", "input[type='submit']:visible", "a.btn__submit", "a:has-text('Submit Application')", "button:has-text('Submit application')",
                    "button:has-text('Submit Application')", "button:has-text('Submit')", "button:has-text('Apply')"]
CONFIRM_TEXTS = ["application received", "we've received your application", "successfully submitted", "application submitted",
                 "thanks for applying", "thank you for applying",
                 # Boards that mark the listing itself rather than showing a thank-you page
                 "applied today", "apply again", "you have already applied", "already applied to this"]
# A board asking whether we applied is the opposite of proof: it means it handed us off and does not know either.
NOT_CONFIRMATION = ["did you apply", "let us know so we can help you track", "yes, i applied", "no, i didn't apply"]


import contextlib
@contextlib.contextmanager
def cm_guard(cm, ctx):
    try:
        yield ctx
    finally:
        try: cm.__exit__(None, None, None)
        except Exception: pass


class _SiteBlocked(Exception):
    pass


class ATSApplySkill(BaseSkill):
    platform = "ats"
    needs_login = False

    def run(self, limit: int = 10, dry_run: bool = False, workers: int = 1, sources: list[str] | None = None, **params):
        """Company ATS forms need no login: run `workers` browsers in parallel with fast pacing."""
        workers = 1  # authenticated ATS and manual assistance share one session owner
        import threading
        from backend.core import browser
        ids = self._pending(limit, sources)
        self.log("info", f"apply worker: {len(ids)} application(s), {workers} parallel browser(s), fast pacing (dry_run={dry_run})")
        if not ids: return
        lock = threading.Lock()
        def take_next(pool):
            with lock:
                return pool.pop(0) if pool else None
        def worker(n, pool=None, profile=None):
            pool = ids if pool is None else pool
            humanize.set_pacing("fast")
            try:
                cm = browser.open_context(profile or f"ats_{n}", profile=profile or f"ats_{n}", should_stop=self.ctx.should_stop); ctx = cm.__enter__()
            except Exception as e:  # noqa: BLE001  (profile already open in another window, e.g. the manual sign-in window)
                self.ctx.bump("failed")
                self.log("warn", f"browser profile '{profile or f'ats_{n}'}' unavailable: {str(e)[:120]}; {len(pool)} application(s) left for the next run"); return
            with cm_guard(cm, ctx) as ctx:
                page = ctx.new_page()
                if (config.load().get("browser") or {}).get("hidden"): browser.hide_window(page)
                while not self.ctx.should_stop():
                    aid = take_next(pool)
                    if aid is None: break
                    if not self.take("applies", str(aid)): break
                    try:
                        self.apply_one(page, aid, dry_run)
                    except _SiteBlocked as e:
                        self._fail(aid, f"site shows '{e}' (CAPTCHA/verification); solve it via assist.py then re-approve", page, "needs_human")
                    except SkillPaused:
                        break
                    except Exception as e:  # noqa: BLE001
                        self._fail(aid, f"{type(e).__name__}: {e}", page)
                    humanize.pause("between_items_s")
        threads = [threading.Thread(target=worker, args=(i,), daemon=True, name=f"ats-worker-{i}") for i in range(min(workers, len(ids)))]
        for t in threads: t.start()
        for t in threads: t.join()

    def _pending(self, limit, sources=None):
        cfg = config.load()
        with session() as db:
            q = db.query(Application).join(Job).filter(Application.method.in_(["ats_form", "external"]))
            if sources: q = q.filter(Job.source.in_(sources))
            if cfg.get("review_mode", True): q = q.filter(Application.status == "approved")
            else: q = q.filter(Application.status.in_(["approved", "pending_review"]), Job.fit_score >= cfg.get("auto_submit_min_score", 75))
            return [a.id for a in q.order_by(Application.created_at.asc()).limit(limit).all() if (a.answers or {}).get("factcheck", {}).get("ok") is True]

    def execute(self, page, limit: int = 10, dry_run: bool = False, **_):
        cfg = config.load()
        review_mode = cfg.get("review_mode", True)
        with session() as db:
            q = db.query(Application).join(Job).filter(Application.method.in_(["ats_form", "external"]))
            if review_mode:
                q = q.filter(Application.status == "approved")
            else:
                q = q.filter(Application.status.in_(["approved", "pending_review"]), Job.fit_score >= cfg.get("auto_submit_min_score", 75))
            ids = [a.id for a in q.order_by(Application.created_at.asc()).limit(limit).all()]
        self.log("info", f"apply worker: {len(ids)} application(s) to submit (review_mode={review_mode}, dry_run={dry_run})")
        for aid in ids:
            if self.ctx.should_stop(): break
            if not humanize.in_business_hours():
                self.log("warn", "outside business hours; stopping apply worker"); break
            if not self.take("applies", str(aid)):
                break
            try:
                self.apply_one(page, aid, dry_run)
            except SkillPaused:
                raise
            except Exception as e:  # noqa: BLE001
                self._fail(aid, f"{type(e).__name__}: {e}", page)
            humanize.pause("between_items_s")

    def guard(self, page):
        """ATS worker override: employers are unrelated sites, so a CAPTCHA or warning on one must not pause the run.
        Raise a private signal that apply_one turns into needs_human for that one application."""
        from backend.core import browser
        if self.ctx.should_stop():
            raise SkillPaused("stopped by user")
        browser.dismiss_overlay(page, self.log)   # boards cover their own forms with upsell popups
        w = browser.detect_warning(page)
        if w:
            raise _SiteBlocked(w)

    def _fail(self, aid: int, err: str, page=None, status: str = "failed"):
        shot = self.ctx.screenshot(page, f"app{aid}_{status}") if page else None
        with session() as db:
            a = db.get(Application, aid); a.status = status; a.error = err[:1000]
            if shot: a.screenshot_after = shot
        self.log("warn" if status == "needs_human" else "error", f"application {aid}: {status}: {err[:200]}", screenshot=shot)
        self.ctx.bump(status)

    def apply_one(self, page, aid: int, dry_run: bool):
        from backend.core.submissions import claim
        if not claim(aid, self.ctx.run_id): return
        with session() as db:
            a = db.get(Application, aid); j = a.job
            url, resume_path, cover, job_text, company, title = j.apply_url or j.url, a.resume_path, a.cover_note, _job_text(j, 3000), j.company, j.title
            job_id = j.id
            forms.CTX["country"] = j.country
            a.status = "submitting"
        self.log("info", f"applying: {company} — {title}  ({url})")
        if "news.ycombinator.com" in url:
            self._fail(aid, "Hacker News post: no application form; use outreach (email in post) instead", None, "needs_human"); return
        if "arbeitnow.com/jobs/" in url and not url.rstrip("/").endswith("/apply"):
            url = url.rstrip("/") + "/apply"          # Arbeitnow's /apply redirects straight to the employer's form
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        humanize.pause()
        self.guard(page)
        try:
            head = page.inner_text("body", timeout=4000).lower()[:1500]
            if any(k in head for k in ("page not found", "no longer accepting applications", "this job is no longer available", "job has been closed", "position has been filled", "404")):
                with session() as db:
                    a = db.get(Application, aid); a.status = "failed"; a.error = "posting expired / not found"; a.job.status = "expired"
                self.log("warn", f"expired posting: {company} — {title}"); self.ctx.bump("expired"); return
        except Exception:
            pass
        self.log("info", f"[{company}] opening the posting")
        page = self._follow_apply_links(page, aid)   # aggregator page -> employer form (up to 3 hops, new tabs handled)
        self.guard(page)
        self._dismiss_cookie_banner(page)
        root = self._form_root(page)                 # embedded Greenhouse/Lever/Ashby iframe, else the page itself
        self.log("info", f"[{company}] found the application form")
        mode = (config.load().get("apply") or {}).get("mode", "agent")
        uploaded = True
        if mode == "agent":
            # Claude drives the whole form. The script's answer bank, country rules and fact-check are still what supply
            # the values; the model decides which control to put them in, which is the part no rule ever got right.
            from backend.core.apply import agent
            self.log("info", f"[{company}] Claude is filling the form")
            outcome = agent.finish_form(page, root, job_text=job_text, cover=cover, log=self.log,
                                        should_stop=self.ctx.should_stop, resume_path=resume_path)
            shot_before = self.ctx.screenshot(page, f"app{aid}_before")
            with session() as db:
                db.get(Application, aid).screenshot_before = shot_before
            if not outcome["done"]:
                from backend.core import questions
                open_now = forms.validate_required(page, root)
                if open_now:
                    questions.record(open_now, company, job_id)
                self.ctx.bump("agent_blocked")
                self._fail(aid, f"Claude stopped: {outcome['blocked']}", page, "needs_human"); return
            self.log("info", f"[{company}] form complete after {outcome['steps']} step(s)")
            self.ctx.bump("agent_finished")
        else:
            unanswered = forms.fill_work_history(page, self.log, scope=root)
            unanswered += forms.fill_text_inputs(page, job_text, cover, self.log, scope=root)
            unanswered += forms.fill_selects(page, self.log, scope=root)
            self.log("info", f"[{company}] attaching the resume")
            uploaded = forms.upload_resume(page, resume_path, self.log, scope=root) if resume_path else False
            self._fill_custom_radios(page, scope=root)
            unanswered += forms.fill_button_choices(page, self.log, scope=root)
            unanswered += forms.fill_checkbox_groups(page, self.log, scope=root)
            unanswered += forms.fill_custom_dropdowns(page, self.log, scope=root)
            self.log("info", f"[{company}] answering the dropdowns and screening questions")
            forms.refill_phone(page, self.log, scope=root)
            forms.fill_text_inputs(page, job_text, cover, self.log, scope=root, simple_only=True)
            forms.tick_certifications(page, self.log, scope=root)
            humanize.human_scroll(page, 600)
            shot_before = self.ctx.screenshot(page, f"app{aid}_before")
            with session() as db:
                db.get(Application, aid).screenshot_before = shot_before
            unanswered += forms.validate_required(page, root)
            if unanswered:
                from backend.core.apply import agent
                self.log("info", f"[{company}] script stopped on {len(unanswered)} field(s); Claude is taking over")
                outcome = agent.finish_form(page, root, job_text=job_text, cover=cover, log=self.log,
                                            should_stop=self.ctx.should_stop, resume_path=resume_path)
                if not outcome["done"]:
                    from backend.core import questions
                    questions.record(unanswered, company, job_id)
                    self.ctx.bump("agent_blocked")
                    self._fail(aid, f"Claude stopped: {outcome['blocked']}. Open fields: " + " | ".join(unanswered[:5]),
                               page, "needs_human")
                    return
                self.ctx.bump("agent_finished")
                uploaded = True
        if not uploaded and root.locator("input[type='file']").count():
            self._fail(aid, "resume upload field present but upload failed", page, "needs_human"); return
        if dry_run:
            self.log("info", f"dry run: would submit {company} — {title}", screenshot=shot_before)
            with session() as db: db.get(Application, aid).status = "approved"
            return
        btn = None
        for sel in SUBMIT_SELECTORS:
            loc = root.locator(sel)
            if loc.count() and loc.first.is_visible():
                btn = loc.first; break
        self.log("info", f"[{company}] form complete; looking for the submit button")
        if not btn:
            # No submit here usually means a multi-step form: walk the remaining steps with a learned recipe.
            if self._multi_step(page, root, aid, job_text, cover, resume_path, company, title, job_id):
                return
            from backend.core.apply import agent
            self.log("info", f"[{company}] no submit button; the agent is taking over")
            outcome = agent.finish_form(page, root, job_text=job_text, cover=cover, log=self.log,
                                        should_stop=self.ctx.should_stop)
            if outcome["done"]:
                for sel in SUBMIT_SELECTORS:
                    loc = root.locator(sel)
                    if loc.count() and loc.first.is_visible():
                        btn = loc.first; break
            if not btn:
                self._fail(aid, f"no submit button found ({outcome.get('blocked') or 'agent found none either'})",
                           page, "needs_human"); return
        self.guard(page)
        humanize.human_click(page, btn)
        time.sleep(4); humanize.pause()
        body = page.inner_text("body", timeout=5000).lower()
        shot_after = self.ctx.screenshot(page, f"app{aid}_after")
        ok = any(t in body for t in CONFIRM_TEXTS) and not any(t in body for t in NOT_CONFIRMATION)
        if any(t in body for t in NOT_CONFIRMATION):
            self._fail(aid, "this board hands the application off to the employer's own site and then asks whether you "
                            "applied; nothing was submitted here. Apply on the employer's site instead.",
                       page, "needs_human")
            self.ctx.bump("handed_off"); return
        if not ok:
            try:
                form_gone = root.locator("input[type='file']").count() == 0 and not btn.is_visible()
            except Exception:
                form_gone = False
            url_ok = any(k in page.url.lower() for k in ("thank", "confirmation", "success", "submitted", "applied"))
            # a form that merely vanished (blank iframe, SPA re-render) is not proof of submission: require a confirming URL
            ok = form_gone and url_ok
        if not ok:
            capped = forms.application_limit(page)
            if capped:
                self._fail(aid, f"the employer caps applications per candidate and refused this one ({capped}). "
                                f"The form was complete; try again after their window resets.", page, "needs_human")
                self.ctx.bump("employer_cap"); return
        errs = [] if ok else forms.form_errors(page)
        if not ok and re.search(r"verification code was sent|enter the .{0,12}code to confirm|security code", body):
            with session() as db:
                a = db.get(Application, aid); a.status = "needs_human"; a.screenshot_after = shot_after
                a.error = "employer emailed a security code to confirm you're human; open the form yourself, enter the code and submit"
            self.log("warn", f"email verification code required: {company} — {title}", screenshot=shot_after); self.ctx.bump("needs_human"); return
        capped = (not ok) and ("limits for applications" in body or "applied to the maximum" in body or "already applied" in body)
        with session() as db:
            a = db.get(Application, aid)
            a.screenshot_after = shot_after
            if capped:
                a.status = "failed"; a.error = "employer application cap / already applied (see screenshot)"
            elif ok:
                a.status = "submitted"; a.submitted_at = datetime.utcnow(); a.confirmation_text = body[:500]; a.job.status = "applied"
            else:
                a.status = "needs_human"; a.error = ("form errors: " + " | ".join(errs)) if errs else "no confirmation text detected after submit; verify in screenshot"
        self.log("info" if ok else "warn", f"{'submitted' if ok else ('capped' if capped else 'submit unverified')}: {company} — {title}", screenshot=shot_after)
        self.ctx.bump("submitted" if ok else ("capped" if capped else "unverified"))

    APPLY_LINK_SELECTORS = ["a[data-testid='job_header-button-apply']", "a[href*='/c/new']", "a:has-text('Postuler')", "button:has-text('Postuler')", "a:has-text('Apply for this job')", "a:has-text(\"I'm interested\")", "button:has-text(\"I'm interested\")", "a:has-text('Apply for this position')", "button:has-text('Apply for this position')", "a:has-text('Apply for this job')", "a:has-text('Apply now')", "a:has-text('Apply Now')", "a:has-text('Apply again')", "a:has-text('Apply on company site')",
                            "a:has-text('Apply to this job')", "a[href*='#app']", "a:has-text('Apply')", "button:has-text('Apply for this job')", "button:has-text('Apply now')", "button:has-text('Apply')"]

    def _has_form(self, page) -> bool:
        """A real, usable application form: a visible name/email field (hidden file inputs alone don't count — Recruitee keeps
        the form collapsed behind an 'Apply for this job' button)."""
        try:
            return page.locator("input[type='email'], input[name*='email' i], input[name*='first' i], input[name*='name' i], input[id*='name' i]").filter(visible=True).count() > 0
        except Exception:
            return False

    def _follow_apply_links(self, page, aid):
        """If the current page is a job description without a form, click through 'Apply' links (they may open new tabs)."""
        ctx = page.context
        for hop in range(3):
            if self._has_form(page):
                return page
            target = None
            for sel in self.APPLY_LINK_SELECTORS:
                loc = page.locator(sel)
                for k in range(min(loc.count(), 3)):
                    if loc.nth(k).is_visible():
                        target = loc.nth(k); break
                if target is not None: break
            if target is None:
                return page
            href = target.get_attribute("href") if target.evaluate("e => e.tagName") == "A" else None
            if href and href.startswith("http") and "javascript:" not in href:
                self.log("info", f"following apply link: {href[:100]}")
                page.goto(href, wait_until="domcontentloaded", timeout=60000)
            else:
                before = set(ctx.pages)
                humanize.human_click(page, target)
                time.sleep(3)
                new = [p for p in ctx.pages if p not in before]
                if new:
                    page = new[-1]; page.bring_to_front()
                    self.log("info", f"apply opened new tab: {page.url[:100]}")
            humanize.pause()
            with session() as db:
                a = db.get(Application, aid)
                if page.url and page.url != a.job.apply_url: a.job.apply_url = page.url
            if forms.has_captcha(page):
                return page
        return page

    def _form_root(self, page):
        """Return a FrameLocator for an embedded ATS iframe (Greenhouse job_app embed, Lever, Ashby), else the page."""
        for sel in ["iframe[src*='greenhouse.io']", "iframe[src*='lever.co']", "iframe[src*='ashbyhq.com']", "iframe[id*='grnhse']", "iframe[src*='job_app']"]:
            if page.locator(sel).count():
                try:
                    fl = page.frame_locator(sel).first
                    fl.locator("body").wait_for(timeout=8000)
                    # only trust the iframe if the form really lives there (Lever pages carry tracking iframes on lever.co)
                    if page.locator("input[type='file'], input[type='email'], form input[name='email']").filter(visible=True).count() and not fl.locator("input[type='file'], input[type='email']").count():
                        continue
                    self.log("info", f"form is inside an iframe ({sel})")
                    return fl
                except Exception:
                    continue
        return page

    def _multi_step(self, page, root, aid, job_text, cover, resume_path, company, title, job_id=None, max_steps: int = 8) -> bool:
        """Walk a multi-page application using a recipe for this ATS family, learning it on first encounter.

        Returns True if the application reached a terminal state (submitted, or parked as needs_human with a real reason).
        Each step is mapped once by the model reading the page; every later application on that family replays the stored
        recipe with no model call. A step that will not advance marks the recipe stale so the next run re-derives it."""
        from backend.core.apply import recipes
        fam = recipes.family_of(page.url)
        recipe = recipes.load(fam) or {"family": fam, "steps": []}
        learned = list(recipe.get("steps") or [])
        self.log("info", f"multi-step form ({fam}); {'replaying' if learned else 'learning'} recipe")
        for n in range(max_steps):
            if self.ctx.should_stop(): return False
            if n < len(learned) and not recipes.matches(page, learned[n], root):
                recipes.mark_stale(fam, "Form controls/questions changed")
                learned = learned[:n]
            step = learned[n] if n < len(learned) else recipes.derive(page, fam, self.log, root=root)
            if not step:
                self._fail(aid, f"{fam}: could not map step {n + 1} of the form", page, "needs_human"); return True
            if n >= len(learned):
                learned.append(step); recipes.save(fam, {"family": fam, "steps": learned})
            body = page.inner_text("body", timeout=5000).lower()
            wall_words = ("create account", "sign in with google", "sign in with email", "already have an account", "use my last application")
            if any(k in body for k in wall_words) or (("sign in" in body) and page.locator("input[type='password']").count()):
                self._fail(aid, f"{fam} requires an account for this employer; run assist.py, sign in once, and it fills the rest",
                           page, "needs_human"); return True
            unanswered = recipes.fill_step(page, step, job_text, cover, resume_path, self.log, root=root)
            unanswered += forms.validate_required(page, root)
            wall = forms.payment_wall(page)
            if wall:
                self._fail(aid, f"stopped at step {n + 1}: this flow leads to a paid subscription page ('{wall}'). "
                                f"Nothing was submitted and no payment was made.", page, "needs_human")
                recipes.mark_stale(fam, "flow leads to a paid subscription page")
                return True
            if unanswered:
                from backend.core import questions
                questions.record(unanswered, company, job_id)   # a multi-step form's questions belong in the inbox too
                self._fail(aid, f"{fam} step {n + 1} unanswered: " + " | ".join(unanswered[:5]), page, "needs_human"); return True
            if step.get("is_final"):
                shot = self.ctx.screenshot(page, f"app{aid}_final")
                self.guard(page)
                if not recipes.advance(page, step, self.log, root=root, should_stop=self.ctx.should_stop):
                    recipes.mark_stale(fam, f"final step would not submit (app {aid})")
                    self._fail(aid, f"{fam}: final submit did not go through", page, "needs_human"); return True
                time.sleep(4)
                body = page.inner_text("body", timeout=5000).lower()
                from backend.core.submissions import confirmation
                ok = confirmation(body, step.get("confirmation"))
                shot = self.ctx.screenshot(page, f"app{aid}_after")
                with session() as db:
                    a = db.get(Application, aid); a.screenshot_after = shot
                    if ok:
                        a.status = "submitted"; a.submitted_at = datetime.utcnow(); a.confirmation_text = body[:500]; a.job.status = "applied"
                    else:
                        a.status = "needs_human"; a.error = f"{fam}: submitted but no confirmation seen; verify in the screenshot"
                self.log("info" if ok else "warn", f"{'submitted' if ok else 'unverified'} via {fam} recipe: {company} — {title}", screenshot=shot)
                self.ctx.bump("submitted" if ok else "unverified")
                return True
            if not recipes.advance(page, step, self.log, root=root, should_stop=self.ctx.should_stop):
                recipes.mark_stale(fam, f"step {n + 1} would not advance (app {aid})")
                self._fail(aid, f"{fam}: step {n + 1} would not advance; recipe marked for relearning", page, "needs_human"); return True
            root = self._form_root(page)
        self._fail(aid, f"{fam}: more than {max_steps} steps; stopped", page, "needs_human"); return True

    def _dismiss_cookie_banner(self, page):
        for sel in ["button:has-text('No, thanks')", "button:has-text('Reject All')", "button:has-text('Reject all')", "button:has-text('Decline')", "button:has-text('OK for me')", "button:has-text('Accept All')", "button:has-text('Accept all')",
                    "button:has-text('Accept')", "#onetrust-reject-all-handler", "#onetrust-accept-btn-handler", "button[aria-label='Close']"]:
            try:
                loc = page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    loc.first.click(timeout=1500); time.sleep(0.8); return
            except Exception:
                continue

    def _fill_custom_radios(self, page, scope=None):
        """Yes/No radio groups: sponsorship, authorization, relocation etc."""
        from backend.core import profile
        groups = (scope or page).locator("fieldset, [role='radiogroup'], .field:has(input[type='radio'])")
        for i in range(groups.count()):
            g = groups.nth(i)
            try:
                label = g.inner_text(timeout=800).split("\n")[0][:200]
                ans = forms.answer_question(label, "", None, self.log)
                if not ans or ans == "__LLM__": continue
                want = "yes" if ans.lower().startswith("yes") else "no" if ans.lower().startswith("no") else ans.lower()
                radios = g.locator("input[type='radio'], label")
                for k in range(radios.count()):
                    r = radios.nth(k)
                    txt = (r.inner_text(timeout=500) if r.evaluate("e=>e.tagName") == "LABEL" else (r.get_attribute("value") or "")).strip().lower()
                    if txt.startswith(want):
                        humanize.human_click(page, r); break
            except Exception:
                continue


SKILLS = {"ats_apply": ATSApplySkill}
