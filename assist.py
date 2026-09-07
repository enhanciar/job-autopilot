"""Assisted apply: for applications that stopped on a human step (emailed security code, CAPTCHA, an odd question),
open ONE visible browser ON THE SHARED PROFILE (the same one your platform logins live in, so nothing asks you to sign
in twice), fill the whole form automatically, and hand it over with only the human bit left to do.
Do not run it while a platform skill (LinkedIn / Naukri / Cutshort ...) is running — they share that profile.

You never retype your details — the form is complete when you get it. Type the code (or solve the challenge), press the
employer's submit button, and the script records the result and moves to the next one.

    .venv/bin/python assist.py              # every application waiting on a human step
    .venv/bin/python assist.py 105 60       # only these application ids
    .venv/bin/python assist.py workday 3    # the next 3 Workday applications (sign in once per employer)
"""
from __future__ import annotations
import sys, time
sys.path.insert(0, ".")
from playwright.sync_api import sync_playwright
from datetime import datetime
from backend.app.db import session
from backend.app.models import Application, Job
from backend.core import humanize, config
from backend.core.apply import forms
from backend.core.runner import RunContext
from backend.core.apply.worker import ATSApplySkill
from backend.core.pipeline import _job_text
from pathlib import Path

CODE_FILE = Path("data/security_code.txt")

HUMAN_ERRORS = ("security code", "CAPTCHA", "verification code")
DONE_MARKERS = ("thank you for applying", "application received", "we've received your application", "successfully submitted",
                "application submitted", "thanks for applying", "your application has been received")


def targets(argv) -> list[int]:
    if argv and argv[0] == "workday":
        return workday_targets(int(argv[1]) if len(argv) > 1 else None)
    if argv:
        return [int(a) for a in argv]
    with session() as db:
        rows = db.query(Application).filter(Application.status == "needs_human").all()
        return [a.id for a in rows if any(k.lower() in (a.error or "").lower() for k in HUMAN_ERRORS)]


def workday_targets(limit: int | None = None) -> list[int]:
    with session() as db:
        rows = db.query(Application).join(Job).filter(Application.status.in_(["needs_human", "failed", "approved"])).all()
        ids = [a.id for a in rows if "workday" in ((a.job.apply_url or a.job.url or "").lower())]
    return ids[:limit] if limit else ids


def main():
    ids = targets(sys.argv[1:])
    if not ids:
        print("nothing waiting on a human step"); return
    ctx = RunContext("skill", "assisted_apply")
    skill = ATSApplySkill(ctx)
    print(f"{len(ids)} application(s) to assist: {ids}  (run {ctx.run_id})", flush=True)

    with __import__("backend.core.browser", fromlist=["open_context"]).open_context("ats") as bctx:
        page = bctx.new_page()
        humanize.set_pacing("fast")

        for aid in ids:
            if ctx.should_stop(): break
            from backend.core.submissions import claim_assistance
            if not claim_assistance(aid, ctx.run_id):
                print(f"Application {aid} cannot be assisted: check its state and regenerate stale documents in Review.", flush=True)
                continue
            with session() as db:
                a = db.get(Application, aid); j = a.job
                url = j.apply_url or j.url
                company, title, resume, cover = j.company, j.title, a.resume_path, a.cover_note
                job_text = _job_text(j, 3000)
                forms.CTX.clear()
                forms.CTX["country"] = j.country
            print(f"\n──────── {company} — {title}\n{url}", flush=True)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(3)
                for sel in ("button:has-text('Accept Cookies')", "button:has-text('Accept all')", "button:has-text('No, thanks')"):
                    c = page.locator(sel).filter(visible=True).first
                    if c.count(): c.click(); page.wait_for_timeout(1200); break
                page = skill._follow_apply_links(page, aid)
                # Workday-style sites gate the form behind a per-employer account: wait while you sign in, then continue.
                if wait_for_login(page):
                    page = skill._follow_apply_links(page, aid)
                skill._dismiss_cookie_banner(page)
                root = skill._form_root(page)
                try:
                    root.locator("input[type='file'], input[type='email'], input[type='text'], textarea").first.wait_for(state="visible", timeout=20000)
                except Exception:
                    pass
                if "greenhouse" in page.url or page.locator("iframe[src*='greenhouse']").count():
                    forms.greenhouse_specifics(page, cover, print_log, scope=root)
                forms.fill_work_history(page, print_log, scope=root)
                forms.fill_text_inputs(page, job_text, cover, print_log, scope=root)
                forms.fill_selects(page, print_log, scope=root)
                if resume:
                    forms.upload_resume(page, resume, print_log, scope=root)
                skill._fill_custom_radios(page, scope=root)
                forms.fill_button_choices(page, print_log, scope=root)
                forms.fill_checkbox_groups(page, print_log, scope=root)
                forms.fill_custom_dropdowns(page, print_log, scope=root)
                forms.refill_phone(page, print_log, scope=root)
                forms.tick_certifications(page, print_log, scope=root)
                shot_filled = ctx.screenshot(page, f"assist{aid}_filled")
                print(f"FORM FILLED ({shot_filled}).", flush=True)
                print(f"WAITING FOR CODE — tell Claude the code from the employer's email; it lands in {CODE_FILE}", flush=True)
            except Exception as e:  # noqa: BLE001
                print(f"could not fill automatically: {type(e).__name__}: {e}\nfill it by hand in the window.", flush=True)

            submitted = False
            typed = False
            CODE_FILE.write_text("")                   # clear any stale code before waiting
            for _ in range(180):                       # 15 minutes
                if ctx.should_stop(): break
                time.sleep(5)
                try:
                    body = page.inner_text("body", timeout=4000).lower()
                except Exception:
                    continue
                if any(m in body for m in DONE_MARKERS):
                    submitted = True; break
                if not typed:
                    code = CODE_FILE.read_text().strip() if CODE_FILE.exists() else ""
                    if code:
                        typed = enter_code(page, code)
                        CODE_FILE.write_text("")
                        if typed:
                            print("Verification code entered; verifying result…", flush=True)
            shot = ctx.screenshot(page, f"assist{aid}")
            with session() as db:
                a = db.get(Application, aid)
                a.screenshot_after = shot
                if submitted:
                    a.status = "submitted"; a.submitted_at = datetime.utcnow(); a.error = None; a.job.status = "applied"
                else:
                    a.status = "needs_human"
                    a.error = "assisted attempt: no confirmation seen; check the screenshot"
            ctx.bump("submitted" if submitted else "unconfirmed")
            print("SUBMITTED ✓" if submitted else "no confirmation seen — left as needs_human", flush=True)

        ctx.finish()
        print(f"\nDONE {ctx.stats}", flush=True)
        pass  # shared Chrome remains open


def wait_for_login(page, minutes: int = 6) -> bool:
    """If the form is behind a sign-in (every Workday employer runs its own tenant), pause and let the user sign in.
    Returns True once the login screen is gone, so the caller can pick the form up from there."""
    def gated():
        try:
            body = page.inner_text("body", timeout=4000).lower()
        except Exception:
            return False
        return (page.locator("input[type='password']").count() > 0
                or "sign in with google" in body
                or ("create account" in body and "sign in" in body))
    if not gated():
        return False
    print("SIGN IN in the window (Google is usually two clicks). Waiting up to "
          f"{minutes} minutes, then I fill the rest of the form…", flush=True)
    for _ in range(minutes * 12):
        time.sleep(5)
        if not gated():
            print("signed in — continuing", flush=True); time.sleep(3); return True
    raise RuntimeError("Sign-in not completed; finish it manually before filling this form")


def enter_code(page, code: str) -> bool:
    """Type the verification code the user supplied. The human retrieved it from their own inbox, so the employer's
    human-in-the-loop check is satisfied; this only saves the retyping. Handles a single input and split character boxes."""
    try:
        boxes = page.locator("input[autocomplete='one-time-code'], input[name*='code' i], input[id*='code' i], "
                             "input[maxlength='1'], input[type='text'][maxlength='8']").filter(visible=True)
        n = boxes.count()
        if n == 0:
            print("  could not find a code field — type it in the window yourself", flush=True); return False
        if n >= len(code):                              # one box per character
            for i, ch in enumerate(code):
                boxes.nth(i).click(); boxes.nth(i).fill(ch); page.wait_for_timeout(120)
        else:
            boxes.first.click(); boxes.first.fill(code)
        page.wait_for_timeout(1200)
        print("Code entered. Review the form and click the employer's final button yourself.", flush=True)
        return True
    except Exception as e:                              # noqa: BLE001
        print(f"  entering the code failed: {str(e)[:120]}", flush=True); return False


def print_log(level, msg, **kw):
    if level != "info":
        print(f"  [{level}] {msg}", flush=True)


if __name__ == "__main__":
    main()
