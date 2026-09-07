"""BaseSkill: every platform skill subclasses this. Handles browser context, login wait, caps, warning detection, screenshots."""
from __future__ import annotations
from backend.core import browser, humanize
from backend.core.runner import RunContext


class SkillPaused(Exception):
    pass


class BaseSkill:
    platform: str = "generic"
    needs_login: bool = True

    def __init__(self, ctx: RunContext):
        self.ctx = ctx

    def log(self, level, msg, **kw):
        self.ctx.log(level, msg, platform=self.platform, **kw)

    def guard(self, page):
        """Call between actions: stop flag, warning banners, business hours."""
        if self.ctx.should_stop():
            raise SkillPaused("stopped by user")
        w = browser.detect_warning(page)
        if w:
            shot = self.ctx.screenshot(page, "warning")
            self.log("human", f"{self.platform}: platform showed '{w}'. Paused. Solve it in the window, then resume.", screenshot=shot)
            import time
            deadline = time.monotonic() + 900
            while time.monotonic() < deadline:
                if self.ctx.should_stop(): raise SkillPaused("stopped by user")
                if not browser.detect_warning(page):
                    self.ctx.set_status("running"); return
                time.sleep(1)
            raise SkillPaused("human verification timed out; browser remains open")

    def take(self, action: str, ref: str | None = None) -> bool:
        """Consume one unit of a capped action. Returns False if cap reached."""
        if not humanize.take(self.platform, action, ref):
            self.log("warn", f"{self.platform}: daily cap reached for {action}")
            return False
        return True

    def run(self, **params):
        humanize.set_pacing(None)
        with browser.open_context(self.platform, should_stop=self.ctx.should_stop) as ctx:
            page = ctx.new_page()
            if self.needs_login and not browser.ensure_login(page, self.platform, self.ctx.log, should_stop=self.ctx.should_stop, on_ready=lambda: self.ctx.set_status("running")):
                self.ctx.set_status("paused_for_human")
                self.log("warn", f"{self.platform}: login not completed; browser remains open")
                return
            try:
                self.execute(page, **params)
            except SkillPaused as e:
                self.ctx.set_status("paused_for_human")
                self.log("warn", f"{self.platform}: paused ({e}); browser remains open")
            finally:
                if not self.ctx.is_paused() and not page.is_closed():
                    page.close()
                humanize.set_pacing(None)

    def execute(self, page, **params):  # override
        raise NotImplementedError
