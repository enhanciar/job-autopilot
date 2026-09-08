"""Persistent, non-headless browser sessions. One dedicated Chrome profile under data/profiles/shared.
If a platform needs login, the skill pauses, logs a 'human' event, and waits for the user to log in in the visible window."""
from __future__ import annotations
import time
from contextlib import contextmanager
from datetime import datetime
from playwright.sync_api import sync_playwright, BrowserContext, Page
from backend.core import config
from backend.app.db import session
from backend.app.models import PlatformSession

LOGIN_MARKERS = {
    "jobright":   {"url": "https://jobright.ai/jobs/recommend", "logged_in_selector": ["[class*='avatar'], a[href*='/profile'], [class*='job-card'], [class*='jobCard']"], "login_url_part": "/login"},
    "x":          {"url": "https://x.com/home", "logged_in_selector": ["[data-testid='AppTabBar_Profile_Link'], [data-testid='SideNav_AccountSwitcher_Button']"], "login_url_part": "/i/flow/login"},
    "linkedin":   {"url": "https://www.linkedin.com/feed/", "logged_in_selector": ["input[placeholder*='Search'], .global-nav__me"], "login_url_part": "/login"},
    "wellfound":  {"url": "https://wellfound.com/jobs", "logged_in_selector": ["a[href='/jobs/applications'], a[href='/jobs/messages'], a[href*='/candidates/']"], "login_url_part": "/login"},
    "ycombinator":{"url": "https://www.workatastartup.com/companies", "logged_in_selector": ["a[href='/application'], a[href='/conversations'], a[href*='/inbox']"], "login_url_part": "account.ycombinator.com"},
    "naukri":     {"url": "https://www.naukri.com/mnjuser/homepage", "logged_in_selector": [".nI-gNb-drawer__icon, .view-profile-wrapper, a[href*='/mnjuser/profile']", "text=Complete profile"], "login_url_part": "/nlogin"},
    "cutshort":   {"url": "https://cutshort.io/profile/all-jobs", "logged_in_selector": ["a[href*='/profile/'], a[href*='/messages']", "text=Dashboard"], "login_url_part": "redirect_url"},
    "instahyre":  {"url": "https://www.instahyre.com/candidate/opportunities/", "logged_in_selector": ["a[href*='/logout/'], a[href='/candidate/profile/'], .employer-details"], "login_url_part": "/login"},
    "hirist":     {"url": "https://www.hirist.tech/jobfeed", "logged_in_selector": ["img[alt*='profile'], [class*='profile'], a[href*='/jobfeed']"], "login_url_part": "/login"},
    "peerlist":   {"url": "https://peerlist.io/jobs", "logged_in_selector": ["a[href*='/jobs/applied-jobs'], a[href='/inbox']"], "login_url_part": "/login"},
    "ats":        {"url": "about:blank", "logged_in_selector": ["body"], "login_url_part": "__never__"},
    "gmail":      {"url": "https://mail.google.com/", "logged_in_selector": ["a[aria-label*='Google Account']"], "login_url_part": "accounts.google"},
}


LOGIN_ALIASES = {"linkedin_people": "linkedin", "x_outreach": "x"}


def login_key(platform):
    return LOGIN_ALIASES.get(platform, platform)


def lock_busy(path):
    """Probe an existing advisory lock without launching a process."""
    import fcntl
    if not path.exists(): return False
    with path.open("a") as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return True
        fcntl.flock(lock, fcntl.LOCK_UN)
    return False


def health():
    return {"profile": "shared", "profile_exists": (config.PROFILES / "shared").exists(),
            "busy": lock_busy(config.DATA / "browser-owner.lock"),
            "separate_from_personal_chrome": True}


def profile_dir(platform: str):
    """All automation entry points share one persistent, dedicated profile."""
    d = config.PROFILES / "shared"
    d.mkdir(parents=True, exist_ok=True)
    return d



@contextmanager
def open_context(platform: str, headless: bool = False, profile: str | None = None, should_stop=lambda: False):
    """Serialize authenticated workflows; attach to one persistent dedicated Chrome.

    Chrome lives beyond the worker connection so a human can finish a blocked form.
    All entry points use the same profile; no session copying between ATS workers.
    """
    import fcntl, subprocess, urllib.request, json, os
    lock_path = config.DATA / "browser-owner.lock"
    with lock_path.open("a") as lock:
        deadline = time.monotonic() + 60
        while True:
            if should_stop(): raise InterruptedError("Browser wait cancelled")
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB); break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Automation browser is busy; finish the current browser task before retrying")
                time.sleep(.5)
        try:
            cdp = config.env("CHROME_CDP_URL") or "http://127.0.0.1:9223"
            from urllib.parse import urlparse
            if urlparse(cdp).hostname not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("Browser debugging endpoint must be local")
            def available():
                try:
                    with urllib.request.urlopen(cdp.rstrip("/") + "/json/version", timeout=1) as response:
                        return bool(json.load(response).get("webSocketDebuggerUrl"))
                except Exception: return False
            if not available():
                if config.env("CHROME_CDP_URL"):
                    raise RuntimeError("Configured Chrome endpoint is unavailable; open the dedicated automation Chrome")
                executable = config.env("CHROME_EXECUTABLE", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
                directory = config.PROFILES / "shared"
                directory.mkdir(parents=True, exist_ok=True)
                proc = subprocess.Popen([executable, "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=9223",
                    f"--user-data-dir={directory}", "--no-first-run", "--start-maximized"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                for _ in range(40):
                    if should_stop(): raise InterruptedError("Browser launch cancelled")
                    if available(): break
                    if proc.poll() is not None: break
                    time.sleep(.25)
                if not available():
                    raise RuntimeError("Could not open automation Chrome. An older automation window may own the shared profile; close that window once, then retry. No profile data was deleted.")
            # Closing the last window leaves Chrome alive with no page target and no browser context; attaching then
            # fails with "Browser context management is not supported". Ask it for a blank tab first.
            def page_targets():
                try:
                    with urllib.request.urlopen(cdp.rstrip("/") + "/json/list", timeout=2) as response:
                        return [t for t in json.load(response) if t.get("type") == "page"]
                except Exception: return []
            if not page_targets():
                for method in ("PUT", "GET"):
                    try:
                        request = urllib.request.Request(cdp.rstrip("/") + "/json/new?about:blank", method=method)
                        urllib.request.urlopen(request, timeout=5).read()
                        break
                    except Exception: continue
                for _ in range(20):
                    if page_targets(): break
                    time.sleep(.25)
                if not page_targets():
                    raise RuntimeError("The automation Chrome has no window open and would not open one. "
                                       "Quit Chrome once (the window using data/profiles/shared), then retry; no profile data is lost.")
            with sync_playwright() as p:
                b = p.chromium.connect_over_cdp(cdp)
                if not b.contexts:
                    b.close()
                    raise RuntimeError("Attached to the automation Chrome but it exposes no browser context; quit that Chrome window once and retry")
                context = b.contexts[0]
                try: yield context
                finally: b.close()  # disconnect; externally launched Chrome and human tabs survive
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def is_logged_in(page: Page, platform: str) -> bool:
    platform = login_key(platform)
    m = LOGIN_MARKERS.get(platform)
    if not m:
        return True
    if m["login_url_part"] in page.url:
        return False
    # Each entry is tried on its own: a CSS list cannot contain a text= engine selector, and mixing them made the whole
    # check throw, so Cutshort and Naukri could never be seen as logged in however many times you signed in.
    selectors = m["logged_in_selector"]
    if isinstance(selectors, str): selectors = [selectors]
    for i, selector in enumerate(selectors):
        try:
            page.wait_for_selector(selector, timeout=6000 if i == 0 else 1500)
            return True
        except Exception:
            continue
    return False


def ensure_login(page: Page, platform: str, log, wait_minutes: int = 15, should_stop=lambda: False, on_ready=lambda: None) -> bool:
    """Navigate to platform; if not logged in, ask the human and poll until they are."""
    platform = login_key(platform)
    m = LOGIN_MARKERS.get(platform)
    if not m:
        return True
    if should_stop(): return False
    page.goto(m["url"], wait_until="domcontentloaded")
    time.sleep(2)
    if is_logged_in(page, platform):
        _set_session(platform, True)
        on_ready()
        return True
    log("human", f"{platform}: please log in in the open Chrome window. Waiting up to {wait_minutes} min.", platform=platform)
    _set_session(platform, False, "waiting for user login")
    deadline = time.time() + wait_minutes * 60
    while time.time() < deadline:
        if should_stop(): return False
        time.sleep(1)
        try:
            if is_logged_in(page, platform):
                _set_session(platform, True, "logged in by user")
                log("info", f"{platform}: login detected, continuing.", platform=platform)
                on_ready()
                return True
        except Exception:
            pass
    _set_session(platform, False, "login timed out")
    return False


def _set_session(platform: str, ok: bool, note: str | None = None):
    with session() as db:
        ps = db.query(PlatformSession).filter_by(platform=platform).one_or_none()
        if not ps:
            ps = PlatformSession(platform=platform)
            db.add(ps)
        ps.logged_in = ok
        ps.last_checked = datetime.utcnow()
        ps.note = note


# Buttons that cost money or change an account. A promotional overlay is closed, never accepted.
NEVER_CLICK = ("unlock", "upgrade", "subscribe", "buy", "checkout", "start trial", "start free trial", "get turbo",
               "claim offer", "redeem", "continue to payment", "pay ", "add card", "enable autofill", "install")
DISMISS_LABELS = ("maybe later", "no thanks", "no, thanks", "not now", "skip for now", "skip", "dismiss", "close",
                  "remind me later", "continue for free", "stay on free")
DISMISS_SELECTORS = ("[aria-label='Close']", "[aria-label='close']", "[aria-label='Dismiss']", "button[class*='close' i]",
                     "[data-testid*='close' i]", "[class*='modal' i] [class*='close' i]", "[role='dialog'] button[class*='close' i]")


def dismiss_overlay(page, log=None) -> bool:
    """Close a promotional or upsell popup so it stops covering the page.

    Only closes: the X, an explicit 'maybe later' style button, then Escape. Anything that would buy, upgrade, subscribe
    or install is never pressed, however prominent the site makes it.
    """
    try:
        overlays = page.locator("[role='dialog'], [class*='modal' i], [class*='popup' i], [class*='overlay' i]").filter(visible=True)
        if not overlays.count():
            return False
    except Exception:
        return False
    overlay = overlays.first
    for selector in DISMISS_SELECTORS:
        try:
            button = overlay.locator(selector).filter(visible=True).first
            if not button.count():
                button = page.locator(selector).filter(visible=True).first
            if button.count():
                label = (button.inner_text(timeout=500) or "") + (button.get_attribute("aria-label") or "")
                if any(w in label.lower() for w in NEVER_CLICK):
                    continue
                button.click(timeout=2000); page.wait_for_timeout(600)
                if log: log("info", "closed a popup")
                return True
        except Exception:
            continue
    for label in DISMISS_LABELS:
        try:
            button = overlay.locator(f"button:has-text('{label}'), a:has-text('{label}')").filter(visible=True).first
            if button.count() and not any(w in (button.inner_text(timeout=500) or "").lower() for w in NEVER_CLICK):
                button.click(timeout=2000); page.wait_for_timeout(600)
                if log: log("info", f"closed a popup via '{label}'")
                return True
        except Exception:
            continue
    try:
        page.keyboard.press("Escape"); page.wait_for_timeout(500)
        return not overlays.count()
    except Exception:
        return False


WARNING_TEXTS = ["unusual activity", "verify you're human", "security check", "we've restricted", "captcha", "are you a robot", "temporarily restricted"]


def detect_warning(page: Page) -> str | None:
    try:
        body = page.inner_text("body", timeout=3000).lower()
    except Exception:
        return None
    for t in WARNING_TEXTS:
        if t in body:
            return t
    return None
