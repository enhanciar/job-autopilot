"""Open pages that need a human step (CAPTCHA / portal login) in the automation Chrome so the session persists. Close the window when done."""
import time, sys
urls = [u.strip() for u in open("data/manual_tabs.txt") if u.strip()]
with __import__("backend.core.browser", fromlist=["open_context"]).open_context("ats") as ctx:
    pages=[]
    for i,u in enumerate(urls):
        pg = ctx.new_page(); pg.goto(u, wait_until="domcontentloaded"); pages.append(pg)
    if not pages: raise SystemExit("No manual URLs to open")
    pages[0].bring_to_front(); print("open; close the window when done", flush=True)
    try:
        while any(not page.is_closed() for page in pages): time.sleep(3)
    except Exception: pass
    print("closed", flush=True)
