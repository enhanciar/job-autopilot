"""Open one automation Chrome window with a tab per platform login page. Log in to each, then close the window (or press Ctrl+C)."""
import time
SITES = ["https://www.linkedin.com/login", "https://www.instahyre.com/login/", "https://cutshort.io/profile/all-jobs", "https://www.hirist.tech/login",
         "https://www.naukri.com/nlogin/login", "https://wellfound.com/login", "https://account.ycombinator.com/?continue=https%3A%2F%2Fwww.workatastartup.com%2F", "https://peerlist.io/login"]
with __import__("backend.core.browser", fromlist=["open_context"]).open_context("ats") as ctx:
    pages = [ctx.new_page()]
    pages[0].goto(SITES[0])
    for u in SITES[1:]:
        pg = ctx.new_page(); pg.goto(u, wait_until="domcontentloaded"); pages.append(pg)
    pages[0].bring_to_front()
    print("Login tabs open; close these tabs or press Ctrl+C when finished", flush=True)
    try:
        while any(not page.is_closed() for page in pages):
            time.sleep(3)
    except Exception:
        pass
    print("window closed; sessions saved in data/profiles/shared", flush=True)
