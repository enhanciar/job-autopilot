"""Shared helpers for one-click job platforms (Instahyre, Cutshort, Hirist, Wellfound, YC...).
Pattern: discover() scrapes the logged-in feed into the jobs table; apply() submits Applications that the user approved
(or, when review_mode is off, high-score ones), handling any post-click questionnaire with the answer bank; anything unknown -> needs_human."""
from __future__ import annotations
import re, time
from datetime import datetime
from backend.app.db import session
from backend.app.models import Application, Job
from backend.core import config, humanize
from backend.core.normalize import upsert
from backend.core.apply import forms
from backend.core.pipeline import _job_text


def upsert_many(source: str, items: list[dict]) -> int:
    new = 0
    with session() as db:
        for it in items:
            try:
                with db.begin_nested():
                    _, created = upsert(db, source=source, **it); db.flush()
                new += int(created)
            except Exception:
                pass
    return new


def pending_apps(platform: str, limit: int) -> list[int]:
    cfg = config.load()
    with session() as db:
        q = db.query(Application).join(Job).filter(Application.platform == platform)
        if cfg.get("review_mode", True):
            q = q.filter(Application.status == "approved")
        else:
            q = q.filter(Application.status.in_(["approved", "pending_review"]), Job.fit_score >= cfg.get("auto_submit_min_score", 75))
        return [a.id for a in q.order_by(Application.created_at).limit(limit).all() if (a.answers or {}).get("factcheck", {}).get("ok") is True]


def app_info(aid: int, run_id: int | None = None) -> dict:
    from backend.core.submissions import claim
    if not claim(aid, run_id): raise RuntimeError("Application is already claimed or not approved/fact-checked")
    with session() as db:
        a = db.get(Application, aid); j = a.job
        forms.CTX.clear()
        forms.CTX["country"] = j.country
        a.status = "submitting"
        return {"id": aid, "url": j.apply_url or j.url, "company": j.company, "title": j.title, "resume": a.resume_path, "cover": a.cover_note, "job_text": _job_text(j, 3000), "raw": j.raw or {}}


def mark(aid: int, status: str, error: str | None = None, shot: str | None = None, confirmation: str | None = None):
    with session() as db:
        a = db.get(Application, aid)
        a.status = status
        if error: a.error = error[:1000]
        if shot: a.screenshot_after = shot
        if confirmation: a.confirmation_text = confirmation[:500]
        if status == "submitted":
            a.submitted_at = datetime.utcnow(); a.job.status = "applied"


def handle_questionnaire(skill, page, info: dict) -> list[str]:
    """After clicking Apply: fill any modal/form fields; upload resume if asked. Returns unanswered labels."""
    time.sleep(2); humanize.pause()
    if forms.has_captcha(page):
        return ["CAPTCHA"]
    scope = dialog_scope(page)
    root = scope if scope is not None else page
    reloc_unanswered = relocation_choice(page, root)
    # textareas: an interest/motivation box gets the cover note
    tas = root.locator("textarea")
    for i in range(tas.count()):
        ta = tas.nth(i)
        try:
            if not ta.is_visible() or ta.input_value(): continue
            for _ in range(6):
                if ta.is_enabled(): break
                time.sleep(1)
            if not ta.is_enabled(): continue
            try: q = forms._label_for(page, ta)
            except Exception: q = ""
            ans = info["cover"] if (not q or re.search(r"interest|why|about you|tell us|motivat|message|note|cover", q, re.I)) else forms.answer_question(q, info["job_text"], info["cover"], skill.log)
            if ans:
                humanize.human_type(page, ta, ans[:1500])
                if not ta.input_value(): ta.fill(ans[:1500])
        except Exception:
            continue
    unanswered = forms.fill_text_inputs(page, info["job_text"], info["cover"], skill.log, scope=scope)
    unanswered += forms.fill_selects(page, skill.log, scope=scope)
    unanswered += forms.fill_button_choices(page, skill.log, scope=scope)
    unanswered += reloc_unanswered
    if root.locator("input[type='file']").count() and info.get("resume"):
        forms.upload_resume(page, info["resume"], skill.log)
    # Wellfound's own location block is answered by relocation_choice above; generic fillers must not report it
    return [u for u in unanswered if not re.match(r"all \(\d+\)", u.lower()) and not re.search(r"currently in|relocate to", u, re.I)]


def relocation_choice(page, root) -> list[str]:
    """Wellfound-style 'I am currently in... / I can relocate to...' block. The picker is a react-select
    (.select__control -> .select__option). Select EVERY option matching the user's relocation list when the picker is multi-select,
    else the best single match. Countries first; generic words like 'Remote' last so US cities don't win by accident."""
    from backend.core import profile
    prefs = [c for c in profile.get("preferences.relocation_cities", default=[]) if c and "anywhere" not in c.lower()]
    prefs += ["India", "Singapore", "Dubai", "United Arab Emirates", "Japan", "Europe", "United Kingdom", "Germany", "Netherlands", "Ireland", "Canada", "Australia", "Switzerland", "France", "Spain", "Portugal", "United States", "Remote", "Anywhere"]
    def rank(t):
        tl = t.lower()
        for i, pref in enumerate(prefs):
            if pref.lower() in tl: return i
        return None
    try:
        lab = root.get_by_text("I can relocate to", exact=False)
        if not lab.count(): return []
        humanize.human_click(page, lab.first); time.sleep(1.5)
        ctl = root.locator(".select__control, [class*='select__control'], [data-test*='Dropdown'] [class*='control']").first
        if not ctl.count():
            sel = root.locator("select").first
            if sel.count():
                opts = [o.strip() for o in sel.locator("option").all_inner_texts() if o.strip() and o.strip() != "-"]
                ranked = sorted((o for o in opts if rank(o) is not None), key=rank)
                if ranked: sel.select_option(label=ranked[0]); return []
            return ["relocation location picker not found"]
        chosen = 0
        for attempt in range(12):
            humanize.human_click(page, ctl); time.sleep(1.2)
            opts = page.locator(".select__option, [class*='select__option'], [role='option']")
            texts = [t.strip() for t in opts.all_inner_texts()]
            if not texts:
                page.keyboard.press("Escape"); break
            ranked = sorted(((rank(t), i, t) for i, t in enumerate(texts) if rank(t) is not None), key=lambda x: x[0])
            if not ranked:
                if chosen == 0: humanize.human_click(page, opts.nth(0)); chosen += 1
                else: page.keyboard.press("Escape")
                break
            _, idx, txt = ranked[0]
            humanize.human_click(page, opts.nth(idx)); chosen += 1; time.sleep(1.0)
            multi = ctl.locator(".select__multi-value, [class*='multi-value']").count() > 0
            if not multi: break
        return [] if chosen else ["relocation dropdown opened but showed no options"]
    except Exception as e:
        return [f"relocation picker error: {str(e)[:60]}"]


def click_first(page, selectors: list[str]):
    for sel in selectors:
        loc = page.locator(sel)
        if loc.count() and loc.first.is_visible():
            humanize.human_click(page, loc.first); return True
    return False


def confirmed(page, texts=("application sent", "application submitted", "submitted successfully", "successfully applied", "applied successfully", "thank you for applying", "you have applied", "already applied", "your application has been")) -> bool:
    """Only sentence-level confirmations count; single words like 'applied' appear in nav bars."""
    try:
        body = page.inner_text("body", timeout=4000).lower()
    except Exception:
        return False
    return any(t in body for t in texts)


def dialog_scope(page):
    """The open dialog/modal if any, else None (so questionnaire filling never touches page-level filters)."""
    for sel in [".ReactModalPortal .ReactModal__Content:visible", "[role='dialog']:visible", ".application-modal-wrap:visible", ".modal:visible", "[class*='modal-content']:visible", "[class*='Modal']:visible"]:
        loc = page.locator(sel)
        if loc.count():
            return loc.first
    return None
